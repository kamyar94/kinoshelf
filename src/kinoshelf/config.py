"""Instance settings: built-in defaults, overlaid by /data/config.yml, which the Settings page writes."""

from __future__ import annotations

import copy
import logging
import os
import threading
from pathlib import Path
from typing import Any

import yaml

from .playback import DEFAULTS as _PLAYBACK_DEFAULTS

log = logging.getLogger(__name__)

VIDEO_EXTS = [
    "mkv", "mp4", "m4v", "avi", "mov", "wmv", "mpg", "mpeg", "ts", "m2ts", "webm", "flv", "divx", "ogm", "vob",
]
SUBTITLE_EXTS = ["srt", "ass", "ssa", "sub", "idx", "vtt", "sup"]

DEFAULTS: dict[str, Any] = {
    "paths": {
        # How a folder looks to you and to your media server ("shown"), and where the same
        # folder is mounted inside the container. Longest match wins.
        "map": [],  # [{"shown": "X:\\", "container": "/media/x", "label": "X"}]
        "inbox": [],  # Inbox folders, in "shown" form
        "set_aside": "_Set aside",  # folder at the root of each drive for junk and replaced files
        "set_aside_parts": {
            "junk": "Junk",
            "replaced": "Replaced",
            "moved": "Moved across drives",
            "empty": "Empty folders",
            "playback": "Playback originals",  # originals a rewrap or re-encode replaced (Settings > Playback)
        },
    },
    "naming": {
        "movie": "X:\\Movies\\{Title} ({Year})\\{Title} ({Year}).{ext}",
        "episode": "X:\\Series\\{Series} ({Year})\\Season {SS}\\{Series} S{SS}E{EE} - {Episode}.{ext}",
    },
    "rule": {
        "min_score": 85,  # best match at least this many percent
        "max_runtime_diff_pct": 5,  # and its running time within this many percent of the file
        "min_margin": 10,  # and at least this many points ahead of the next match
        "dry_run": True,  # the weekly move only writes what it would do
    },
    "schedule": {"enabled": True, "day": "sunday", "time": "03:00"},
    "subtitles": {"languages": ["en"], "mark_default": True},
    "sources": {
        "tmdb": {"enabled": False, "api_key": "", "language": "en-US"},
        "imdb": {"enabled": True, "refresh_days": 30, "include_episodes": False},
        "tvmaze": {"enabled": True},
        "omdb": {"enabled": False, "api_key": ""},
        "opensubtitles": {"enabled": False, "api_key": "", "user_agent": "Kinoshelf v0.1.0"},
        "jellyfin": {"enabled": False, "url": "", "api_key": "", "path_map": []},
    },
    "network": {
        "mode": "direct",  # direct | proxy | route (the optional Xray sidecar, compose profile "route")
        # Secrets: write-only on the page, never logged, never in hook jobs or backups.
        "proxy": {"kind": "http", "address": "", "username": "", "password": ""},  # kind: http | socks5
        "route_url": "http://route:10809",  # the sidecar inside Docker; its own config stays on the host
        # Sources that take the proxy or sidecar. Jellyfin is normally on the home network.
        "use": {"tmdb": True, "imdb": True, "tvmaze": True, "omdb": True, "opensubtitles": True,
                "jellyfin": False},
        "timeout_seconds": 15,
    },
    "scoring": {
        "weights": {"title": 35, "runtime": 30, "year": 15},
        "missing_year": 0.7,  # year check value when the file name has no year
        "hash_floor": 90,  # a file-hash match lifts the score to at least this
        "translit": [
            # Applied to both sides before titles are compared a second time, so Latin
            # spellings of the same Persian or Arabic word meet. [pattern, replacement]
            ["ou", "u"], ["oo", "u"], ["ee", "i"], ["gh", "q"], ["kh", "x"],
            ["aa", "a"], ["ey\\b", "e"], ["iye\\b", "ie"], ["-?ye\\b", "e"], ["w", "v"],
        ],
    },
    # The device you watch on (Settings > Playback): a profile from playback.PROFILES, or "custom" with your own
    # lists (names as ffprobe uses them).
    "device": {
        "profile": "smart-tv",
        "name": "TV",
        "containers": ["mp4", "mkv", "m4v", "mov", "ts"],
        "video_codecs": ["h264", "hevc", "vp9"],
        "audio_codecs": ["aac", "ac3", "eac3", "mp3"],
        "max_height": 2160,
        "ten_bit": True,
        "rewrap_to": "mp4",
    },
    # What the playable stage does for a file the device would not play (docs/PLAYBACK.md).
    "playback": dict(_PLAYBACK_DEFAULTS),
    "hooks": {
        # Each stage runs its hooks in order. kind: command | http | folder.
        "playable": [
            {"name": "Device check", "kind": "command", "enabled": True,
             "command": ["python", "-m", "kinoshelf.default_hooks.playable"],
             "options": {}, "timeout_minutes": 1440},
        ],
        "subtitles": [
            {"name": "Subtitles (subliminal)", "kind": "command", "enabled": False,
             "command": ["python", "-m", "kinoshelf.default_hooks.subtitles"], "options": {}},
        ],
        "nfo": [
            {"name": "NFO writer", "kind": "command", "enabled": True,
             "command": ["python", "-m", "kinoshelf.default_hooks.nfo"], "options": {}},
        ],
        "icon": [
            {"name": "Poster as folder.jpg", "kind": "command", "enabled": True,
             "command": ["python", "-m", "kinoshelf.default_hooks.poster"], "options": {}},
        ],
        "after_move": [],
    },
    "junk": {
        # Words removed from file names before lookup (case-insensitive, whole words)
        "release_words": [
            "YTS", "YIFY", "RARBG", "ETRG", "EVO", "Ganool", "PSA", "Pahe", "MkvCage", "ShAaNiG",
            "Iran-Film", "IranFilm", "FardaDownload", "Par30dl", "TehMedia", "Film2Movie", "Film2Media",
            "AvaMovie", "DigiMoviez", "Zardfilm", "UpTVs", "SkyMovie", "Golchindl", "30nama",
        ],
        # Files set aside as junk (glob patterns, case-insensitive)
        "files": [
            "*.txt", "*.url", "*.nfo.txt", "*.exe", "*.lnk", "*.website", "RARBG*", "WWW.*", "*www.*.*",
            "Torrent downloaded from*", "*.torrent", "*sample*.mkv", "*sample*.mp4", "*sample*.avi",
        ],
        "sample_max_mb": 150,  # a video under this size with "sample" in its name is junk
        "auto_set_aside": True,  # off: junk files and emptied folders stay where they are
    },
    # enabled: off means nothing in the Inbox is picked up, renamed or moved (the pages still work)
    "watcher": {"enabled": True, "poll_seconds": 30, "settle_seconds": 120},
    "server": {"public_url": "", "lan_url": ""},
    # Filing rules (docs/FILING.md): an ordered list of rules edited in Settings > Filing rules. Off: the two
    # naming templates above decide every destination.
    "filing": {"enabled": False, "rules": [], "policies": {}},
    # When the sources give a film different years: imdb (IMDb's, else the first release anywhere, which is how
    # IMDb dates a film), tmdb (TMDB's) or file (the year in the file name when a source gives it).
    "identify": {"year_from": "imdb"},
    # Updates (docs/UPDATES.md): the updater container fetches and checks new images; a host step applies them.
    "update": {
        "enabled": True,  # install new versions by themselves; off: only when you press Update now
        "interval_minutes": 30,  # a check is one small registry request; the image downloads only when it is new
        "channel": "latest",  # latest: every change on main; stable: tagged releases
        "image": "ghcr.io/kamyar94/kinoshelf",
        "verify": "digest",  # digest: every part checked against its digest; cosign: also the signature
        "cosign_identity": "^https://github.com/kamyar94/kinoshelf/",
        "allow_downgrade": False,
        "insecure_registry": False,  # plain http, only for a registry on this computer or network (tests)
    },
}

_lock = threading.Lock()


def data_dir() -> Path:
    return Path(os.environ.get("KS_DATA_DIR", "/data"))


def deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


class Settings:
    """Settings live in memory; save() writes them back to config.yml (mode 600)."""

    def __init__(self, path: Path):
        self.path = path
        self.values: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        changed = False
        with _lock:
            if self.path.exists():
                raw = yaml.safe_load(self.path.read_text(encoding="utf-8")) or {}
                self.values = deep_merge(DEFAULTS, raw)
                changed = migrate(self.values, raw)
            else:
                self.values = copy.deepcopy(DEFAULTS)
        if changed:
            self.save()

    def save(self) -> None:
        with _lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                "# Kinoshelf settings. The Settings page writes this file.\n"
                + yaml.safe_dump(self.values, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, self.path)

    def __getitem__(self, key: str) -> Any:
        return self.values[key]

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.values
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def set_section(self, key: str, value: Any) -> None:
        self.values[key] = value
        self.save()


def migrate(values: dict, raw: dict) -> bool:
    """Bring settings written by an older version up to date. True when something changed."""
    changed = _migrate_network(values, raw)
    changed = _migrate_update_interval(values, raw) or changed
    return _migrate_playback(values, raw) or changed


def _migrate_update_interval(values: dict, raw: dict) -> bool:
    """Before 0.4.3 the update check ran every update.interval_hours (default 6). It becomes interval_minutes:
    the old default turns into the new one (30), any other value keeps its length."""
    upd = values.get("update")
    if not isinstance(upd, dict) or "interval_hours" not in upd:
        return False
    hours = upd.pop("interval_hours")
    if "interval_minutes" not in (((raw or {}).get("update")) or {}):
        try:
            h = float(hours)
        except (TypeError, ValueError):
            h = 6.0
        upd["interval_minutes"] = 30 if h == 6 else max(10, min(10080, round(h * 60)))
    return True


def _migrate_playback(values: dict, raw: dict) -> bool:
    """Before 0.4.0 the device had only its own lists (now profile "custom"), and the device-check hook carried
    rewrap / convert options; they become playback.rewrap / playback.reencode, so nothing changes by itself."""
    changed = False
    raw = raw or {}
    if isinstance(raw.get("device"), dict) and "profile" not in raw["device"]:
        values["device"]["profile"] = "custom"
        values["device"].setdefault("ten_bit", True)
        changed = True
    hooks = values.get("hooks") or {}
    for h in hooks.get("playable") or []:
        opts = h.get("options") if isinstance(h, dict) else None
        if not isinstance(opts, dict) or not ({"rewrap", "convert"} & set(opts)):
            continue
        if "playback" not in raw:
            values["playback"]["rewrap"] = bool(opts.get("rewrap"))
            values["playback"]["reencode"] = bool(opts.get("convert"))
        opts.pop("rewrap", None)
        opts.pop("convert", None)
        h.setdefault("timeout_minutes", 1440)
        changed = True
    return changed


def _migrate_network(values: dict, raw: dict) -> bool:
    """0.1.0 kept one proxy URL in network.proxy for every lookup and Jellyfin. It becomes mode "proxy"
    with the URL split into kind, address, username and password; Jellyfin now goes direct."""
    net = values.get("network")
    if not isinstance(net, dict) or not isinstance(net.get("proxy"), str):
        return False
    from .net import parse_proxy_url

    old = net["proxy"].strip()
    net["proxy"] = copy.deepcopy(DEFAULTS["network"]["proxy"])
    parts = None
    if old:
        try:
            parts = parse_proxy_url(old)
        except ValueError:
            log.warning("The old proxy setting could not be read; set it again in Settings > Network")
    if parts:
        net["proxy"].update(parts)
    if "mode" not in ((raw or {}).get("network") or {}):
        net["mode"] = "proxy" if parts else "direct"
    return True
