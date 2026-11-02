"""Shared fixtures: a throwaway instance with a mapped Inbox and library, and tiny generated videos."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from kinoshelf.config import Settings
from kinoshelf.db import Db
from kinoshelf.models import Candidate

HAVE_FFMPEG = bool(shutil.which("ffmpeg")) and bool(shutil.which("ffprobe"))
needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg not installed")


def make_video(path: Path, seconds: float = 3.0, codec: str = "libx264", audio: str = "aac") -> Path:
    """A small real video file (test pattern and tone)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=size=160x90:rate=10:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", codec, "-c:a", audio,
            "-shortest", str(path)]
    if subprocess.run(args, capture_output=True).returncode != 0:
        args[args.index(codec)] = "mpeg4"
        subprocess.run(args, capture_output=True, check=True)
    return path


def fake_file(path: Path, size: int = 300_000) -> Path:
    """A non-video file of a given size (for tests that never probe it)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(range(256)) * (size // 256) + b"x" * (size % 256))
    return path


class FakeSource:
    def __init__(self, key="tmdb", name="TMDB"):
        self.key, self.name, self.online = key, name, True

    def enabled(self):
        return True


class Disabled:
    def enabled(self):
        return False


class FakeIdentifier:
    """Answers from a table: title (lower case) -> list of candidate dicts. Scores them for real."""

    fixed = True

    def __init__(self, settings, table: dict[str, list[dict]]):
        self.settings = settings
        self.table = table
        self.calls = []
        self.sources = {"titles": [FakeSource()], "hash": Disabled(), "jellyfin": Disabled()}

    def title_sources(self, only=None):
        return self.sources["titles"]

    def identify(self, q, only=None, details_top=6):
        from kinoshelf.scoring import score_candidates

        self.calls.append(q)
        key = (q.title or "").lower()
        if q.tmdb_id:
            key = next((k for k, v in self.table.items() if any(c.get("ids", {}).get("tmdb") == q.tmdb_id for c in v)), key)
        cands = [Candidate.from_dict(dict(c, sources=["TMDB"])) for c in self.table.get(key, [])]
        for c in cands:
            if c.kind == "series" and q.season and q.episode:
                c.season, c.episode, c.episode_exists = q.season, q.episode, True
                c.episode_title = c.episode_title or f"Episode {q.episode}"
        return score_candidates(cands, q, self.settings.get("scoring", {})), []

    def status(self):
        return [{"name": "TMDB", "note": "online", "ok": True}]


@pytest.fixture
def inst(tmp_path):
    """An instance: data folder, Inbox X:\\Inbox and library X:\\Movies, X:\\Series on one fake drive."""
    drive = tmp_path / "drive"
    (drive / "Inbox").mkdir(parents=True)
    data = tmp_path / "data"
    s = Settings(data / "config.yml")
    s.values["paths"]["map"] = [{"shown": "X:\\", "container": str(drive), "label": "X"}]
    s.values["paths"]["inbox"] = ["X:\\Inbox"]
    s.values["watcher"]["settle_seconds"] = 0
    s.values["hooks"] = {"playable": [], "subtitles": [], "icon": [], "after_move": [],
                         "nfo": [{"name": "NFO writer", "kind": "command", "enabled": True,
                                  "command": ["python", "-m", "kinoshelf.default_hooks.nfo"]}]}
    s.values["rule"]["dry_run"] = False
    s.save()
    db = Db(data / "kinoshelf.sqlite")
    return {"drive": drive, "inbox": drive / "Inbox", "data": data, "settings": s, "db": db}
