"""Make it play on the device (Settings > Playback, docs/PLAYBACK.md).

Checks the file against the device profile and, when the settings allow it, rewraps it (kept only when it loses
nothing, proved picture by picture) or re-encodes it (x264, a VMAF gate, never larger than the original). The
original is never touched here: the answer names the new file and the original, and the app sets the original
aside on the same drive, writes it to the playback journal and can put it back.

Options (all optional; they override Settings > Playback for this hook): any key of playback.DEFAULTS, for example
{rewrap: true, reencode: false}. The old option convert still means reencode.
"""

from __future__ import annotations

from pathlib import Path

from .. import playback
from . import run


def settings_for(job: dict) -> tuple[dict, dict]:
    s = job.get("settings") or {}
    opts = dict(job.get("options") or {})
    if "convert" in opts and "reencode" not in opts:
        opts["reencode"] = opts.pop("convert")
    pb = {**(s.get("playback") or {}), **{k: v for k, v in opts.items() if k in playback.DEFAULTS}}
    return s.get("device") or {}, pb


def handle(job: dict) -> dict:
    it = job["item"]
    device, pb = settings_for(job)
    src = Path(it["file"])
    try:
        res = playback.make_playable(src, device, pb)
    except FileNotFoundError as e:
        return {"status": "failed", "message": f"ffmpeg or ffprobe is missing: {e}"}
    rec = res.to_dict()
    verdict = {"status": res.status or "unknown", "text": res.headline, "playback": rec}
    if res.action in ("rewrapped", "reencoded") and res.new_file:
        return {"status": "done", "message": res.headline, "file": res.new_file, "replaced": [str(src)],
                "files": list(res.sidecars), "playable": verdict}
    return {"status": "done", "message": res.headline, "playable": verdict}


if __name__ == "__main__":
    run(handle)
