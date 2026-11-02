"""Device check. Options: rewrap (copy the streams into the device's container), convert (re-encode to
H.264/AAC). Both off by default: the hook then only reports. Text subtitles inside the file are saved
as .srt beside it before a rewrap, because MP4 cannot carry them."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .. import media
from . import run

TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


def _ffmpeg(args: list[str], timeout: int) -> bool:
    p = subprocess.run(["ffmpeg", "-v", "error", "-y", *args], capture_output=True, timeout=timeout, check=False)
    return p.returncode == 0


def _extract_text_subs(src: Path, info_streams: list[dict]) -> list[str]:
    made = []
    for n, s in enumerate(info_streams):
        if s.get("codec") not in TEXT_SUBS:
            continue
        lang = (s.get("language") or "und").lower()
        out = src.with_name(f"{src.stem}.{lang}.srt")
        k = 2
        while out.exists():
            out = src.with_name(f"{src.stem}.{lang}.{k}.srt")
            k += 1
        if _ffmpeg(["-i", str(src), "-map", f"0:s:{n}", "-c:s", "srt", str(out)], 600):
            made.append(str(out))
    return made


def handle(job: dict) -> dict:
    it, opts = job["item"], job.get("options") or {}
    device = (job.get("settings") or {}).get("device") or {}
    src = Path(it["file"])
    info = media.probe(src)
    verdict = media.playable(info, device)
    if verdict["status"] in ("ok", "unknown"):
        return {"status": "done", "message": verdict["text"], "playable": verdict}
    target_ext = device.get("rewrap_to", "mp4")
    out = src.with_suffix(f".{target_ext}")
    if out == src:
        out = src.with_name(src.stem + ".rewrap." + target_ext)
    tmp = out.with_name(out.stem + ".ks-tmp." + target_ext)
    if verdict["status"] == "rewrap" and opts.get("rewrap"):
        args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a", "-c", "copy", "-movflags", "+faststart", str(tmp)]
        what = "Rewrapped"
    elif verdict["status"] == "convert" and opts.get("convert"):
        args = ["-i", str(src), "-map", "0:v:0", "-map", "0:a", "-c:v", "libx264", "-crf", str(opts.get("crf", 20)),
                "-preset", str(opts.get("preset", "medium")), "-c:a", "aac", "-b:a", "192k",
                "-movflags", "+faststart", str(tmp)]
        what = "Converted"
    else:
        return {"status": "skipped", "message": verdict["text"] + " (left as it is)", "playable": verdict}
    subs = _extract_text_subs(src, info.get("subtitles") or [])
    if not _ffmpeg(args, int(opts.get("timeout_minutes", 600)) * 60):
        if tmp.exists():
            tmp.unlink()
        return {"status": "failed", "message": f"{what.lower()[:-1]} failed", "files": subs}
    new = media.probe(tmp)
    a, b = info.get("duration_s") or 0, new.get("duration_s") or 0
    if not b or abs(a - b) > max(2.0, a * 0.005):
        tmp.unlink()
        return {"status": "failed", "message": f"{what} file is {b:.0f}s long, the original {a:.0f}s", "files": subs}
    tmp.rename(out)
    after = media.playable(new | {"container": target_ext}, device)
    after["text"] = f"{what} for {device.get('name') or 'the device'}"
    return {"status": "done", "message": after["text"], "file": str(out), "replaced": [str(src)],
            "files": subs, "playable": after}


if __name__ == "__main__":
    run(handle)
