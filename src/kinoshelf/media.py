"""What the file itself says: ffprobe facts, the OpenSubtitles hash, still frames."""

from __future__ import annotations

import json
import shutil
import struct
import subprocess
import zlib
from pathlib import Path

_CHUNK = 64 * 1024


def oshash(path: Path) -> str | None:
    """OpenSubtitles "moviehash": file size plus the 64-bit sums of the first and last 64 KB.

    Reads 128 KB, never the whole file. None for files smaller than 128 KB.
    """
    size = path.stat().st_size
    if size < 2 * _CHUNK:
        return None
    h = size
    with open(path, "rb") as f:
        for offset in (0, size - _CHUNK):
            f.seek(offset)
            buf = f.read(_CHUNK)
            for (v,) in struct.iter_unpack("<Q", buf):
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


def tail_sum(path: Path) -> str:
    """Checksum of the last 64 KB; changes while a copy is still writing a pre-sized file."""
    size = path.stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - _CHUNK))
        buf = f.read(_CHUNK)
    return f"{size}:{zlib.crc32(buf):08x}"


def have_ffmpeg() -> bool:
    return bool(shutil.which("ffprobe")) and bool(shutil.which("ffmpeg"))


def probe(path: Path, timeout: int = 60) -> dict:
    """The facts the review page and the device check need. Empty dict when ffprobe fails."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=timeout, check=False,
        )
        data = json.loads(out.stdout or b"{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not (s.get("disposition") or {}).get("attached_pic")), None)
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    try:
        duration = float(fmt.get("duration") or (video or {}).get("duration") or 0) or None
    except ValueError:
        duration = None
    info = {
        "duration_s": duration,
        "size": int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else path.stat().st_size,
        "container": path.suffix.lower().lstrip("."),
        "format_name": fmt.get("format_name"),
        "video_codec": (video or {}).get("codec_name"),
        "video_tag": (video or {}).get("codec_tag_string"),
        "width": (video or {}).get("width"),
        "height": (video or {}).get("height"),
        "hdr": (video or {}).get("color_transfer") in ("smpte2084", "arib-std-b67"),
        "audio": [
            {"codec": a.get("codec_name"), "channels": a.get("channels"),
             "language": (a.get("tags") or {}).get("language")}
            for a in audios
        ],
        "subtitles": [
            {"codec": s.get("codec_name"), "language": (s.get("tags") or {}).get("language")}
            for s in subs
        ],
    }
    return info


def describe(info: dict) -> dict:
    """Short human facts for the review page: Length, Picture, Sound."""
    out = {}
    d = info.get("duration_s")
    if d:
        d = int(round(d))
        out["Length"] = f"{d // 3600}:{d % 3600 // 60:02d}:{d % 60:02d}"
    size = info.get("size")
    if size:
        out["Size"] = (f"{size / 1024 ** 3:.2f} GB" if size >= 1024 ** 3 else
                       f"{size / 1024 ** 2:.0f} MB" if size >= 1024 ** 2 else f"{max(1, size // 1024)} KB")
    if info.get("video_codec"):
        name = {"h264": "H.264", "hevc": "HEVC", "mpeg4": "Xvid/DivX", "mpeg2video": "MPEG-2",
                "av1": "AV1", "vp9": "VP9", "wmv3": "WMV"}.get(info["video_codec"], info["video_codec"])
        out["Picture"] = f"{name} {info.get('width')}x{info.get('height')}" + (" HDR" if info.get("hdr") else "")
    if info.get("audio"):
        a = info["audio"][0]
        ch = {1: "mono", 2: "stereo", 6: "5.1", 8: "7.1"}.get(a.get("channels") or 0, f"{a.get('channels')} ch")
        langs = ", ".join(sorted({x["language"] for x in info["audio"] if x.get("language")}))
        out["Sound"] = f"{(a.get('codec') or '').upper()} {ch}" + (f" · {langs}" if langs else "")
    return out


def frames(path: Path, out_dir: Path, duration_s: float | None, points=(0.10, 0.35, 0.60, 0.85),
           width: int = 480, timeout: int = 60) -> list[Path]:
    """Four still frames as JPEG (fast seek, one decode each). Existing frames are kept."""
    out_dir.mkdir(parents=True, exist_ok=True)
    made = []
    if not duration_s:
        return made
    for i, p in enumerate(points):
        dest = out_dir / f"{i}.jpg"
        if not dest.exists():
            try:
                subprocess.run(
                    ["ffmpeg", "-v", "error", "-y", "-ss", f"{duration_s * p:.2f}", "-i", str(path),
                     "-frames:v", "1", "-vf", f"scale={width}:-2", "-q:v", "5", str(dest)],
                    capture_output=True, timeout=timeout, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
        if dest.exists():
            made.append(dest)
    return made


def playable(info: dict, device: dict) -> dict:
    """Will the device play this without conversion? status: ok | rewrap | convert | unknown."""
    if not info.get("video_codec"):
        return {"status": "unknown", "text": "Could not read the file"}
    vc = info["video_codec"]
    acs = [a.get("codec") for a in info.get("audio") or []]
    v_ok = vc in device.get("video_codecs", [])
    a_ok = not acs or any(a in device.get("audio_codecs", []) for a in acs)
    h_ok = not info.get("height") or info["height"] <= int(device.get("max_height", 99999))
    c_ok = info.get("container") in device.get("containers", [])
    name = device.get("name") or "the device"
    if v_ok and a_ok and h_ok and c_ok:
        return {"status": "ok", "text": f"Plays on {name} as it is"}
    if v_ok and a_ok and h_ok:
        return {"status": "rewrap", "text": f"Plays on {name} after a rewrap"}
    why = []
    if not v_ok:
        why.append(f"picture {vc}")
    if not a_ok:
        why.append("sound " + "/".join(a for a in acs if a))
    if not h_ok:
        why.append(f"{info['height']}p")
    return {"status": "convert", "text": f"Needs converting for {name} ({', '.join(why)})"}
