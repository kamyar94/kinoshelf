"""Make it play on the device: profiles, the decision, the rewrap proof, the re-encode gate, never larger, 64-bit
sizes, and the set-aside, journal and undo around them. Videos are tiny generated test patterns."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from conftest import FakeIdentifier, needs_ffmpeg
from fastapi.testclient import TestClient
from test_web import signed_in

from kinoshelf import playback as P
from kinoshelf.config import DEFAULTS, Settings, deep_merge, migrate
from kinoshelf.pipeline import Engine

TV = {"profile": "custom", "name": "TV", "containers": ["mp4"], "video_codecs": ["mpeg4", "h264"],
      "audio_codecs": ["ac3", "aac"], "max_height": 1080, "ten_bit": False, "rewrap_to": "mp4"}


def clip(path: Path, seconds=6, size="320x240", vcodec="mpeg4", acodec="ac3", extra=()) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=25:duration={seconds}",
                    "-f", "lavfi", "-i", f"anoisesrc=d={seconds}:c=pink:seed=7:a=0.3", "-c:v", vcodec, *extra,
                    "-c:a", acodec, "-ac", "2", str(path)], check=True, capture_output=True)
    return path


def info(**over):
    base = {"container": "mkv", "size": 1, "duration": 10.0,
            "video": [{"index": 0, "codec": "h264", "width": 1920, "height": 1080, "bits": 8, "fps": 25}],
            "audio": [{"index": 1, "codec": "aac", "channels": 2, "language": "eng"}], "subs": []}
    base.update(over)
    return base


# ---------------------------------------------------------------------------------------------- decisions
def test_profiles_presets_and_custom():
    tv = P.device_profile({"profile": "smart-tv-older", "name": "Bedroom"})
    assert tv["video_codecs"] == ["h264", "mpeg4"] and tv["max_height"] == 1080 and not tv["ten_bit"]
    assert tv["name"] == "Bedroom"
    old = P.device_profile({"name": "TV", "containers": ["MP4"], "video_codecs": ["h264"], "audio_codecs": ["aac"],
                            "max_height": 720})  # settings from before presets: the saved lists
    assert old["profile"] == "custom" and old["containers"] == ["mp4"] and old["max_height"] == 720 and old["ten_bit"]
    assert all(set(p) >= {"label", "containers", "video_codecs", "audio_codecs", "max_height", "ten_bit"}
               for p in P.PROFILES.values())


def test_the_plan_names_what_is_wrong():
    dev = P.device_profile({"profile": "universal"})
    assert P.plan(info(container="mp4"), dev)["action"] == "none"
    p = P.plan(info(), dev)
    assert p["action"] == "rewrap" and p["target"] == "mp4" and "mkv" in p["why"]
    p = P.plan(info(container="mp4", audio=[{"index": 1, "codec": "dts", "channels": 6, "language": "eng"}]), dev)
    assert p["action"] == "rewrap" and p["audio"] == ["aac"] and p["target"] == "mp4"  # sound only, same container
    p = P.plan(info(video=[{"index": 0, "codec": "hevc", "width": 3840, "height": 2160, "bits": 10, "fps": 24}]), dev)
    assert p["action"] == "reencode" and p["scale_to"] == 1080
    assert "hevc" in p["why"] and "2160 lines" in p["why"] and "10-bit" in p["why"]
    assert P.plan({}, dev)["action"] == "unknown"
    assert P.verdict(info(), dev)["status"] == "rewrap"


def test_older_settings_move_over_without_changing_anything():
    raw = {"device": {"name": "TV", "containers": ["mp4"], "video_codecs": ["h264"], "audio_codecs": ["aac"],
                      "max_height": 1080, "rewrap_to": "mp4"},
           "hooks": {"playable": [{"name": "Device check", "kind": "command", "enabled": True,
                                   "command": ["python", "-m", "kinoshelf.default_hooks.playable"],
                                   "options": {"rewrap": False, "convert": False}}]}}
    v = deep_merge(DEFAULTS, raw)
    assert migrate(v, json.loads(json.dumps(raw)))
    assert v["device"]["profile"] == "custom" and v["device"]["containers"] == ["mp4"]
    assert v["playback"]["rewrap"] is False and v["playback"]["reencode"] is False  # as the hook options said
    assert v["hooks"]["playable"][0]["options"] == {}
    from kinoshelf.confcheck import problems
    import yaml

    assert problems(yaml.safe_dump(v)) == []
    assert "The device profile phone-9000 is not in this version" in problems("device: {profile: phone-9000}\n")


# ---------------------------------------------------------------------------------------------- sizes over 2 GB
def test_sizes_are_whole_numbers_past_2_and_4_gb(monkeypatch, tmp_path):
    big = 5 * 1024 ** 3 + 123  # over 2 GiB and over 4 GiB: no 32-bit wrap anywhere
    assert P.human_size(big) == "5.00 GB" and P.human_size(2 ** 31) == "2.00 GB"
    assert P.not_larger(2 ** 31, 2 ** 31 + 1) and not P.not_larger(2 ** 32 + 1, 2 ** 32)
    assert P.not_larger(big - 1, big) and not P.not_larger(big + 1, big)
    # the CRF choice under a cap of several GB
    sizes = {20: 6_400_000_000, 21: 5_900_000_000, 22: 5_200_000_000, 23: 4_700_000_000}
    crf, notes = P.choose_crf(lambda c: sizes.get(c), big, {"crf_start": 20, "crf_min": 16, "crf_max": 30})
    assert crf == 22 and "GB" in notes[0]  # the first that fits under 97 % of 5 GB
    # ffprobe's size string is read as a Python int
    fake = {"format": {"size": str(big), "duration": "7200.5", "format_name": "matroska"},
            "streams": [{"index": 0, "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
                         "pix_fmt": "yuv420p", "avg_frame_rate": "24000/1001", "nb_frames": "172800"}]}

    class R:
        stdout = json.dumps(fake).encode()
        stderr = b""
        returncode = 0

    monkeypatch.setattr(P, "_run", lambda *a, **k: R())
    got = P.probe(tmp_path / "film.mkv")
    assert got["size"] == big and isinstance(got["size"], int) and got["video"][0]["frames"] == 172800
    rec = P.Result("reencoded", "x", size_before=big, size_after=big - 2 ** 31).to_dict()
    assert json.loads(json.dumps(rec))["size_before"] == big


def test_vmaf_stats_mean_low_and_lowest_stretch():
    scores = [95.0] * 900 + [70.0] * 10 + [95.0] * 90  # one bad moment of 10 pictures
    s = P.vmaf_stats(scores, fps=25, step=1, window_s=2)
    assert s["mean"] == pytest.approx(94.75) and s["low1"] == 70.0 and s["min"] == pytest.approx(90.0)
    assert P.vmaf_stats(scores, 25, 1, 0)["min"] == 70.0  # 0 = the worst single picture
    checks = P.gate(s, P.options({}))
    assert [c["ok"] for c in checks] == [True, False, True]  # 1% low 70 < 83


# ---------------------------------------------------------------------------------------------- with ffmpeg
@needs_ffmpeg
def test_rewrap_is_proved_picture_by_picture(tmp_path):
    src = clip(tmp_path / "Solaris (1972).avi")
    before = src.read_bytes()
    r = P.make_playable(src, TV, {})
    assert r.action == "rewrapped" and r.status == "ok", r.why
    texts = {c["text"]: c for c in r.checks}
    assert all(c["ok"] for c in r.checks)
    assert re.match(r"150 of 150 frame hashes equal", texts["Pictures decode the same"]["detail"])
    assert texts["Picture times match the original"]["ok"] and "offset 0 ms" in texts["Sound decodes the same"]["detail"]
    assert texts["Sound decodes the same"]["ok"] and texts["Lengths match"]["ok"]
    assert Path(r.new_file).name == "Solaris (1972).mp4" and P.probe(Path(r.new_file))["container"] == "mp4"
    assert src.read_bytes() == before  # the original is never touched here
    assert not list(tmp_path.glob("*.ks-*"))


@needs_ffmpeg
def test_rewrap_with_sound_the_device_does_not_take(tmp_path):
    src = clip(tmp_path / "a.avi", acodec="pcm_s16le")
    r = P.make_playable(src, dict(TV, audio_codecs=["aac"]), {"audio_offset_ms": 20})
    assert r.action == "rewrapped", r.why
    aac = next(c for c in r.checks if c["text"] == "New AAC sound in step")
    assert aac["ok"] and "pcm_s16le to AAC" in aac["detail"]
    assert P.probe(Path(r.new_file))["audio"][0]["codec"] == "aac"


@needs_ffmpeg
def test_a_rewrap_that_differs_is_thrown_away(tmp_path, monkeypatch):
    src = clip(tmp_path / "b.avi")
    real = P.frame_hashes

    def changed(path, spec, decode=True, timeout=None):
        out = real(path, spec, decode, timeout)
        if Path(path).suffix == ".mp4" and out:
            out[3] = (out[3][0], "0" * 32)  # one picture decodes differently
        return out

    monkeypatch.setattr(P, "frame_hashes", changed)
    r = P.make_playable(src, TV, {})
    assert r.action == "kept" and r.status == "rewrap"
    bad = next(c for c in r.checks if not c["ok"])
    assert bad["text"] == "Pictures decode the same" and bad["detail"].startswith("149 of 150")
    assert not list(tmp_path.glob("*.mp4")) and src.exists()


@needs_ffmpeg
def test_reencode_passes_the_gate_and_comes_out_smaller(tmp_path):
    src = clip(tmp_path / "c.avi", seconds=6, size="480x270", extra=("-q:v", "2"))
    r = P.make_playable(src, dict(TV, video_codecs=["h264"]),
                        {"reencode": True, "x264_preset": "veryfast", "vmaf_window_s": 2})
    assert r.action == "reencoded", r.why
    names = [c["text"] for c in r.checks]
    assert names[:3] == ["VMAF mean", "VMAF 1% low", "Lowest 2-second stretch"] and all(c["ok"] for c in r.checks)
    assert r.size_after < r.size_before and P.probe(Path(r.new_file))["video"][0]["codec"] == "h264"


@needs_ffmpeg
def test_reencode_keeps_the_original_when_the_gate_or_the_size_says_no(tmp_path):
    src = clip(tmp_path / "d.avi", seconds=4, extra=("-q:v", "2"))
    r = P.make_playable(src, dict(TV, video_codecs=["h264"]),
                        {"reencode": True, "x264_preset": "veryfast", "vmaf_mean": 100.1, "vmaf_window_s": 1})
    assert r.action == "kept" and "VMAF mean" in r.why and not list(tmp_path.glob("*.mp4"))
    tiny = clip(tmp_path / "e.avi", seconds=4, size="160x90", extra=("-q:v", "31"))  # already very small
    r = P.make_playable(tiny, dict(TV, video_codecs=["h264"]),
                        {"reencode": True, "x264_preset": "veryfast", "crf_start": 16, "crf_min": 16, "crf_max": 17})
    assert r.action == "kept" and "smaller" in r.headline and not list(tmp_path.glob("e*.mp4"))
    off = P.make_playable(src, dict(TV, video_codecs=["h264"]), {"reencode": False})
    assert off.action == "none" and off.status == "convert" and "re-encodes are off" in off.headline


# ---------------------------------------------------------------------------------------------- around it
@needs_ffmpeg
def test_set_aside_journal_week_page_and_undo(inst, monkeypatch):
    from kinoshelf.web import App, create_app

    s: Settings = inst["settings"]
    s.values["device"] = dict(TV)
    s.values["hooks"] = {"playable": [{"name": "Device check", "kind": "command", "enabled": True,
                                       "command": ["python", "-m", "kinoshelf.default_hooks.playable"],
                                       "options": {}}]}
    s.save()
    staged = clip(inst["inbox"] / "_Working" / "X" / "Movies" / "Solaris (1972)" / "Solaris (1972).avi")
    eng = Engine(s, inst["db"], inst["data"], identifier=FakeIdentifier(s, {}))
    iid = inst["db"].add_item(inbox="X:\\Inbox", src_rel="Solaris.1972.avi", path=str(staged), staged=str(staged),
                              state="working", dest="X:\\Movies\\Solaris (1972)\\Solaris (1972).avi",
                              chosen_cand={"kind": "movie", "title": "Solaris", "year": 1972})
    assert eng.run_hooks(iid) == "ready"
    it = inst["db"].item(iid)
    rec = it["playable"]["playback"]
    assert rec["action"] == "rewrapped" and it["path"].endswith("Solaris (1972).mp4") and it["state"] == "ready"
    assert it["dest"].endswith("Solaris (1972).mp4")
    aside = Path(rec["original_aside"])
    assert aside.exists() and "Playback originals" in aside.parts and aside.name == "Solaris (1972).avi"
    assert rec["original_aside_shown"].startswith("X:\\_Set aside\\Playback originals\\")
    lines = [json.loads(x) for x in (inst["data"] / "playback" / "journal.jsonl").read_text(encoding="utf-8").splitlines()]
    assert lines[-1]["event"] == "rewrapped" and lines[-1]["original_aside"] == str(aside)

    monkeypatch.delenv("KS_CF_ACCESS_TEAM", raising=False)
    core = App(ddir=inst["data"], start_worker=False, identifier=FakeIdentifier(s, {}))
    c = TestClient(create_app(core), base_url="http://inbox.test")
    csrf = signed_in(c, core)
    week = c.get("/week").text
    assert "Rewrapped into MP4" in week and "Put the original back" in week and "frame hashes equal" in week
    r = c.post(f"/api/items/{iid}/playback/undo", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200, r.text
    it = inst["db"].item(iid)
    assert it["path"].endswith("Solaris (1972).avi") and Path(it["path"]).exists() and not aside.exists()
    assert it["playable"]["playback"]["undone"] and Path(it["playable"]["playback"]["copy_aside"]).exists()
    assert "Original put back" in c.get("/week").text
    assert c.post(f"/api/items/{iid}/playback/undo", headers={"X-CSRF-Token": csrf}).status_code == 400
    assert json.loads((inst["data"] / "playback" / "journal.jsonl").read_text(encoding="utf-8").splitlines()[-1])[
        "event"] == "undone"


def test_settings_page_saves_a_profile_and_the_gate(inst, monkeypatch):
    from kinoshelf.web import App, create_app

    monkeypatch.delenv("KS_CF_ACCESS_TEAM", raising=False)
    core = App(ddir=inst["data"], start_worker=False, identifier=FakeIdentifier(inst["settings"], {}))
    c = TestClient(create_app(core), base_url="http://inbox.test")
    csrf = signed_in(c, core)
    page = c.get("/settings").text
    assert "Make it play on my device" in page and "Android TV and streaming sticks" in page
    h = {"X-CSRF-Token": csrf}
    assert c.post("/api/settings/device", json={"profile": "apple", "name": "Living room", "containers": "avi"},
                  headers=h).status_code == 200
    dev = core.settings.get("device")
    assert dev["profile"] == "apple" and dev["containers"] == ["mp4", "m4v", "mov"]  # the preset's, not the typed
    assert c.post("/api/settings/device", json={"profile": "custom", "containers": "mp4, MKV", "video_codecs": "h264",
                                                "audio_codecs": "aac", "max_height": 720, "ten_bit": False},
                  headers=h).status_code == 200
    assert core.settings.get("device")["containers"] == ["mp4", "mkv"] and core.settings.get("device")["ten_bit"] is False
    r = c.post("/api/settings/playback", json={"rewrap": True, "reencode": True, "vmaf_mean": 95, "crf_start": 12,
                                               "crf_min": 16, "crf_max": 30, "x264_preset": "slow"}, headers=h)
    assert r.status_code == 400 and "start CRF" in r.json()["error"]
    r = c.post("/api/settings/playback", json={"rewrap": True, "reencode": True, "vmaf_mean": 95, "crf_start": 20,
                                               "crf_min": 16, "crf_max": 30, "x264_preset": "slow"}, headers=h)
    assert r.status_code == 200 and core.settings.get("playback")["vmaf_mean"] == 95

