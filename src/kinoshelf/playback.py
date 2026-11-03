"""Make it play on the device: decide, rewrap or re-encode, and prove the result before it replaces anything.

A file the device would not play gets, in this order of preference:

  rewrap     the same picture (and, where the device takes it, the same sound) copied into a container the device
             opens; a sound track the device does not take becomes AAC. Kept only when every decoded picture hashes
             the same as the original's (same count, times in order), every copied sound track decodes the same (or
             its packets are identical and it stays in step), every new AAC track stays within the offset, the
             lengths match, and the streams, their languages and the picture size are unchanged.
  re-encode  (when allowed) H.264 with x264, the CRF chosen from short samples so the file comes out no larger than
             the original, then scored with VMAF against the decoded original, picture by picture: the mean, the
             1% low and the lowest stretch must reach their thresholds, and the new file must be smaller. A file
             that cannot pass keeps its original.

Nothing here deletes or replaces a file: it makes a new file next to the original and reports. The app sets the
original aside on the same drive, journals it, and can put it back (pipeline.Engine.undo_playback).
Sizes are Python integers throughout (files over 2 GB and over 4 GB are ordinary).
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------------------------- device profiles
# Neutral starting points; the user picks one in Settings > Playback or chooses Custom. Names as ffprobe uses them.
PROFILES: dict[str, dict] = {
    "universal": {"label": "Plays almost anywhere", "containers": ["mp4", "m4v"], "video_codecs": ["h264"],
                  "audio_codecs": ["aac", "mp3"], "max_height": 1080, "ten_bit": False},
    "smart-tv": {"label": "Smart TV (recent)", "containers": ["mp4", "mkv", "m4v", "mov", "ts"],
                 "video_codecs": ["h264", "hevc", "vp9"], "audio_codecs": ["aac", "ac3", "eac3", "mp3"],
                 "max_height": 2160, "ten_bit": True},
    "smart-tv-older": {"label": "Smart TV (older)", "containers": ["mp4", "mkv", "avi"],
                       "video_codecs": ["h264", "mpeg4"], "audio_codecs": ["aac", "ac3", "mp3"],
                       "max_height": 1080, "ten_bit": False},
    "browser": {"label": "Web browser", "containers": ["mp4", "webm"], "video_codecs": ["h264", "vp9", "av1"],
                "audio_codecs": ["aac", "mp3", "opus"], "max_height": 2160, "ten_bit": False},
    "apple": {"label": "Apple TV, iPhone, iPad", "containers": ["mp4", "m4v", "mov"],
              "video_codecs": ["h264", "hevc"], "audio_codecs": ["aac", "ac3", "eac3", "alac"],
              "max_height": 2160, "ten_bit": True},
    "android": {"label": "Android TV and streaming sticks", "containers": ["mp4", "mkv", "webm"],
                "video_codecs": ["h264", "hevc", "vp9", "av1"],
                "audio_codecs": ["aac", "ac3", "eac3", "mp3", "opus"], "max_height": 2160, "ten_bit": True},
}
LIST_KEYS = ("containers", "video_codecs", "audio_codecs")

DEFAULTS = {
    "rewrap": True,  # copy into a container the device opens; kept only when it loses nothing
    "reencode": False,  # re-encode a picture the device cannot play (x264 + the VMAF gate)
    "audio_offset_ms": 20,  # how far a sound track that had to become AAC may shift
    "duration_tolerance_s": 0.1,
    "x264_preset": "slow",
    "x264_tune": "",
    "crf_start": 20,
    "crf_min": 16,
    "crf_max": 30,
    "vmaf_mean": 93.0,
    "vmaf_low1": 83.0,  # the mean of the worst 1 % of the scored pictures
    "vmaf_min": 88.0,  # the lowest stretch (see vmaf_window_s; 0 = the single worst picture)
    "vmaf_window_s": 60,
    "vmaf_max_frames": 40000,  # longer files are scored every n-th picture
    "threads": 0,  # 0 = let ffmpeg decide
}
TEXT_SUBS = {"subrip": "srt", "ass": "ass", "ssa": "ass", "mov_text": "srt", "webvtt": "vtt", "text": "srt"}
PICTURE_SUBS = {"hdmv_pgs_subtitle": "sup"}  # picture subtitles ffmpeg can write beside the file
MP4_LIKE = {"mp4", "m4v", "mov"}


def device_profile(dev: dict | None) -> dict:
    """The device's lists: a named preset, or the saved lists for "custom" (and for settings older than presets)."""
    dev = dict(dev or {})
    name = str(dev.get("profile") or "custom")
    base = dict(PROFILES.get(name) or {})
    out = {"profile": name if name in PROFILES else "custom", "name": dev.get("name") or base.get("label") or "the device",
           "rewrap_to": str(dev.get("rewrap_to") or "mp4").lower().lstrip(".")}
    for k in LIST_KEYS:
        v = base.get(k) if base else dev.get(k)
        out[k] = [str(x).lower() for x in (v if v is not None else [])]
    out["max_height"] = int(base.get("max_height") if base else dev.get("max_height") or 2160)
    tb = base.get("ten_bit") if base else dev.get("ten_bit")
    out["ten_bit"] = True if tb is None else bool(tb)
    return out


def options(pb: dict | None) -> dict:
    return {**DEFAULTS, **{k: v for k, v in (pb or {}).items() if k in DEFAULTS}}


# ---------------------------------------------------------------------------------------------- small helpers
def human_size(n: int | None) -> str:
    n = int(n or 0)
    for unit, div in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= div:
            v = n / div
            return f"{v:.2f} {unit}" if unit == "GB" else f"{v:.0f} {unit}"
    return f"{n} B"


def human_time(s: float) -> str:
    s = int(round(s))
    if s >= 3600:
        return f"{s // 3600} h {s % 3600 // 60:02d} min"
    if s >= 60:
        return f"{s // 60} min {s % 60:02d} s"
    return f"{s} s"


def file_size(p: Path) -> int:
    return int(os.stat(p).st_size)


def not_larger(new_size: int, old_size: int) -> bool:
    """The never-larger cap, on whole byte counts (no 32-bit arithmetic anywhere)."""
    return int(new_size) <= int(old_size)


def _run(args: list[str], timeout: float | None = None, stdout=subprocess.PIPE, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run(args, stdout=stdout, stderr=subprocess.PIPE, timeout=timeout, check=False, cwd=cwd)


def _ff(*args: str) -> list[str]:
    return ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", *args]


# ---------------------------------------------------------------------------------------------- what is in a file
def probe(path: Path, timeout: int = 120) -> dict:
    """Streams and format as the decisions need them. {} when ffprobe cannot read the file."""
    p = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)], timeout)
    try:
        data = json.loads(p.stdout or b"{}")
    except ValueError:
        return {}
    fmt = data.get("format") or {}
    if not data.get("streams"):
        return {}

    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None

    def rate(x):
        try:
            a, b = str(x).split("/")
            return float(a) / float(b) if float(b) else None
        except (ValueError, ZeroDivisionError):
            return None

    video, audio, subs = [], [], []
    for s in data["streams"]:
        t, tags, disp = s.get("codec_type"), s.get("tags") or {}, s.get("disposition") or {}
        lang = (tags.get("language") or "und").lower()
        if t == "video" and not disp.get("attached_pic"):
            pix = str(s.get("pix_fmt") or "")
            bits = int(s.get("bits_per_raw_sample") or 0) if str(s.get("bits_per_raw_sample") or "").isdigit() else 0
            if not bits:
                bits = 10 if ("10" in pix or "12" in pix) else 8
            video.append({"index": s["index"], "codec": s.get("codec_name"), "width": s.get("width"),
                          "height": s.get("height"), "pix_fmt": pix, "bits": bits,
                          "fps": rate(s.get("avg_frame_rate")) or rate(s.get("r_frame_rate")),
                          "duration": num(s.get("duration")), "frames": int(s["nb_frames"]) if str(s.get("nb_frames", "")).isdigit() else None})
        elif t == "audio":
            audio.append({"index": s["index"], "codec": s.get("codec_name"), "channels": s.get("channels") or 2,
                          "language": lang, "duration": num(s.get("duration")),
                          "bit_rate": int(s["bit_rate"]) if str(s.get("bit_rate", "")).isdigit() else None})
        elif t == "subtitle":
            subs.append({"index": s["index"], "codec": s.get("codec_name"), "language": lang,
                         "forced": bool(disp.get("forced"))})
    size = int(fmt["size"]) if str(fmt.get("size", "")).isdigit() else file_size(path)
    return {"container": path.suffix.lower().lstrip("."), "format": fmt.get("format_name") or "",
            "duration": num(fmt.get("duration")), "size": size, "video": video, "audio": audio, "subs": subs}


# ---------------------------------------------------------------------------------------------- the decision
def plan(info: dict, device: dict) -> dict:
    """What would make this file play: action none | rewrap | reencode | unknown, with the reasons in words."""
    if not info or not info.get("video"):
        return {"action": "unknown", "why": "Could not read the file's picture", "audio": []}
    v = info["video"][0]
    dev = device
    why: list[str] = []
    vc_ok = v["codec"] in dev["video_codecs"]
    h_ok = not v.get("height") or int(v["height"]) <= int(dev["max_height"])
    bit_ok = v.get("bits", 8) <= 8 or dev.get("ten_bit", True)
    if not vc_ok:
        why.append(f"the picture is {v['codec']}, the device plays {', '.join(dev['video_codecs']) or 'none'}")
    if not h_ok:
        why.append(f"the picture is {v['height']} lines high, the device shows up to {dev['max_height']}")
    if not bit_ok:
        why.append(f"the picture is {v.get('bits')}-bit, the device plays 8-bit")
    audio = ["copy" if a["codec"] in dev["audio_codecs"] else "aac" for a in info.get("audio") or []]
    bad_a = sorted({a["codec"] for a, x in zip(info.get("audio") or [], audio) if x == "aac"})
    if bad_a:
        why.append(f"the sound is {', '.join(bad_a)}, the device plays {', '.join(dev['audio_codecs']) or 'none'}")
    c_ok = info.get("container") in dev["containers"]
    if not c_ok:
        why.append(f"the device does not open {info.get('container') or 'this'} files")
    if vc_ok and h_ok and bit_ok and c_ok and not bad_a:
        return {"action": "none", "why": "", "audio": audio}
    target = info["container"] if c_ok else dev["rewrap_to"]
    if vc_ok and h_ok and bit_ok:
        return {"action": "rewrap", "why": "; ".join(why), "audio": audio, "target": target}
    return {"action": "reencode", "why": "; ".join(why), "audio": audio, "target": dev["rewrap_to"],
            "scale_to": None if h_ok else int(dev["max_height"]), "eight_bit": True}


def verdict(info: dict, device: dict) -> dict:
    """The short answer the pages show before anything is done (media.playable's shape)."""
    p = plan(info, device)
    name = device.get("name") or "the device"
    if p["action"] == "unknown":
        return {"status": "unknown", "text": "Could not read the file"}
    if p["action"] == "none":
        return {"status": "ok", "text": f"Plays on {name} as it is"}
    if p["action"] == "rewrap":
        return {"status": "rewrap", "text": f"Plays on {name} after a rewrap ({p['why']})"}
    return {"status": "convert", "text": f"Needs re-encoding for {name} ({p['why']})"}


# ---------------------------------------------------------------------------------------------- results
@dataclass
class Result:
    action: str  # none | rewrapped | reencoded | kept | unknown
    headline: str
    status: str = ""  # the device verdict afterwards: ok | rewrap | convert | unknown
    why: str = ""
    checks: list[dict] = field(default_factory=list)  # {"ok": bool, "text", "detail"}
    facts: list[list[str]] = field(default_factory=list)  # [label, value]
    new_file: str = ""
    sidecars: list[str] = field(default_factory=list)
    size_before: int = 0
    size_after: int = 0
    seconds: float = 0.0
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


def _check(ok: bool, text: str, detail: str = "") -> dict:
    return {"ok": bool(ok), "text": text, "detail": detail}


def _describe(info: dict) -> str:
    v = (info.get("video") or [{}])[0]
    a = info.get("audio") or []
    snd = ", ".join(f"{x['codec']} {x['channels']} ch" for x in a[:3]) or "no sound"
    return (f"{(info.get('container') or '?').upper()} · {v.get('codec')} {v.get('width')}x{v.get('height')}"
            f"{' ' + str(v.get('bits')) + '-bit' if (v.get('bits') or 8) > 8 else ''} · {snd} · {human_size(info.get('size'))}")


# ---------------------------------------------------------------------------------------------- proofs
def frame_hashes(path: Path, spec: str, decode: bool = True, timeout: float | None = None) -> list[tuple[float, str]]:
    """(time in seconds, md5) per decoded picture or sound frame of one stream (spec like "0:3" or "0:a:1"); with
    decode=False per packet. Pictures pass through as decoded: no frame is dropped or doubled."""
    args = _ff("-i", str(path), "-map", spec)
    args += ["-fps_mode", "passthrough", "-f", "framemd5", "-"] if decode else ["-c", "copy", "-f", "framemd5", "-"]
    p = _run(args, timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or b"").decode("utf-8", "replace").strip()[-300:] or "ffmpeg could not read it")
    out: list[tuple[float, str]] = []
    tb = 1.0
    for line in (p.stdout or b"").decode("ascii", "replace").splitlines():
        if line.startswith("#tb"):  # "#tb 0: 1/12800": the stream's time base
            try:
                num, den = line.split(":", 1)[1].strip().split("/")
                tb = int(num) / int(den)
            except (ValueError, ZeroDivisionError):
                pass
            continue
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 6:
            try:
                out.append((int(parts[2]) * tb, parts[5]))
            except ValueError:
                continue
    return out


def _pcm(path: Path, spec: str, start: float, seconds: float, rate: int = 8000):
    import numpy as np

    # A fast seek to 5 s before, then a cut at the exact time on the file's own timestamps (-copyts): a seek alone
    # lands on a sound frame boundary, which differs between containers by up to a frame.
    pre = max(0.0, start - 5.0)
    p = _run(_ff("-copyts", "-ss", f"{pre:.3f}", "-i", str(path), "-map", spec, "-ss", f"{max(0.0, start):.3f}",
                 "-t", f"{seconds:.3f}", "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"), 600)
    return np.frombuffer(p.stdout or b"", dtype="<i2").astype("float64")


def audio_offset_ms(src: Path, src_spec: str, out: Path, out_spec: str, duration: float,
                    points=(0.15, 0.5, 0.85), seconds: float = 8.0, max_lag_ms: float = 500.0) -> float | None:
    """The largest shift (ms) between the same moments of two sound tracks, by cross-correlation at a few points;
    None when every point is silent (nothing to compare)."""
    import numpy as np

    rate = 8000
    worst: float | None = None
    for f in points:
        t = max(0.0, duration * f - seconds / 2)
        a, b = _pcm(src, src_spec, t, seconds, rate), _pcm(out, out_spec, t, seconds, rate)
        n = min(len(a), len(b))
        if n < rate or float(np.std(a[:n])) < 1.0 or float(np.std(b[:n])) < 1.0:
            continue
        a, b = a[:n] - a[:n].mean(), b[:n] - b[:n].mean()
        size = 1 << (2 * n - 1).bit_length()
        c = np.fft.irfft(np.fft.rfft(b, size) * np.conj(np.fft.rfft(a, size)), size)
        lag_max = int(rate * max_lag_ms / 1000)
        c = np.concatenate([c[-lag_max:], c[: lag_max + 1]])
        lag = int(np.argmax(c)) - lag_max
        ms = abs(lag) * 1000.0 / rate
        worst = ms if worst is None else max(worst, ms)
    return worst


def _lengths_check(a: dict, b: dict, tol: float) -> dict:
    pairs = [("file", a.get("duration"), b.get("duration"))]
    pairs += [(f"picture {i + 1}", x.get("duration"), y.get("duration")) for i, (x, y) in enumerate(zip(a["video"], b["video"]))]
    pairs += [(f"sound {i + 1}", x.get("duration"), y.get("duration")) for i, (x, y) in enumerate(zip(a["audio"], b["audio"]))]
    known = [(k, x, y) for k, x, y in pairs if x and y]
    bad = [f"{k} {x:.2f} s vs {y:.2f} s" for k, x, y in known if abs(x - y) > tol]
    da, db = a.get("duration") or 0, b.get("duration") or 0
    return _check(not bad and bool(known), "Lengths match", "; ".join(bad) if bad else f"{da:,.1f} s and {db:,.1f} s"
                  + ("" if len(known) == 1 else f" ({len(known)} lengths compared, within {tol} s)"))


def _streams_check(a: dict, b: dict) -> dict:
    va, vb = a["video"], b["video"]
    la, lb = [x["language"] for x in a["audio"]], [x["language"] for x in b["audio"]]
    same_v = len(va) == len(vb) and all((x["width"], x["height"]) == (y["width"], y["height"]) for x, y in zip(va, vb))
    ok = same_v and len(a["audio"]) == len(b["audio"]) and la == lb
    v = va[0] if va else {}
    detail = (f"{len(va)} picture {v.get('width')}x{v.get('height')}, {len(la)} sound"
              + (f" ({', '.join(la)})" if la else ""))
    if not ok:
        detail = (f"before {len(va)} picture / {len(la)} sound ({', '.join(la)}), after {len(vb)} / {len(lb)} "
                  f"({', '.join(lb)})")
    return _check(ok, "Same streams, languages and picture size", detail)


def prove_rewrap(src: Path, out: Path, a: dict, b: dict, audio_plan: list[str], opts: dict) -> list[dict]:
    """Every check of a rewrap, in the order the page lists them."""
    checks: list[dict] = []
    total_pics = 0
    for i, (x, y) in enumerate(zip(a["video"], b["video"])):
        ha, hb = frame_hashes(src, f"0:{x['index']}"), frame_hashes(out, f"0:{y['index']}")
        same = len(ha) == len(hb) and all(p[1] == q[1] for p, q in zip(ha, hb))
        equal = sum(1 for p, q in zip(ha, hb) if p[1] == q[1])
        total_pics += len(ha)
        checks.append(_check(same and len(ha) > 0, "Pictures decode the same" + (f" (picture {i + 1})" if i else ""),
                             f"{equal:,} of {len(ha):,} frame hashes equal" + ("" if len(ha) == len(hb) else
                                                                                f"; {len(hb):,} pictures after")))
        pts = [p[0] for p in hb]
        ordered = all(q > p for p, q in zip(pts, pts[1:]))
        off = [abs((q[0] - hb[0][0]) - (p[0] - ha[0][0])) for p, q in zip(ha, hb)] if ha and hb else [1.0]
        worst = max(off) if off else 0.0
        same_t = ordered and worst <= 0.0015
        checks.append(_check(same_t, "Picture times match the original",
                             "every picture shown at its original time, none twice or out of order" if same_t else
                             ("picture times out of order in the new file" if not ordered else
                              f"{sum(1 for x in off if x > 0.0015):,} pictures shown up to {worst * 1000:.0f} ms off")))
    limit = float(opts["audio_offset_ms"])
    dur = a.get("duration") or 0
    for k, (x, y) in enumerate(zip(a["audio"], b["audio"])):
        label = f"track {k + 1} ({x['language']})"
        if audio_plan[k] == "copy":
            ha, hb = frame_hashes(src, f"0:{x['index']}"), frame_hashes(out, f"0:{y['index']}")
            if len(ha) == len(hb) and all(p[1] == q[1] for p, q in zip(ha, hb)) and ha:
                off = audio_offset_ms(src, f"0:{x['index']}", out, f"0:{y['index']}", dur)
                ok = off is None or off <= limit
                checks.append(_check(ok, "Sound decodes the same", f"{label}: {len(ha):,} frame hashes equal"
                                     + (f", offset {off:.0f} ms (at most {limit:.0f})" if off is not None else "")))
                continue
            pa = frame_hashes(src, f"0:{x['index']}", decode=False)
            pb = frame_hashes(out, f"0:{y['index']}", decode=False)
            same_packets = len(pa) == len(pb) and all(p[1] == q[1] for p, q in zip(pa, pb))
            off = audio_offset_ms(src, f"0:{x['index']}", out, f"0:{y['index']}", dur)
            ok = same_packets and (off is None or off <= limit)
            checks.append(_check(ok, "Sound packets identical and in step",
                                 f"{label}: {len(pa):,} packets {'equal' if same_packets else 'differ'}"
                                 + (f", offset {off:.0f} ms (at most {limit:.0f})" if off is not None else ", silent")))
        else:
            off = audio_offset_ms(src, f"0:{x['index']}", out, f"0:{y['index']}", dur)
            ok = off is None or off <= limit
            checks.append(_check(ok, "New AAC sound in step",
                                 f"{label}: {x['codec']} to AAC, offset {off:.0f} ms (at most {limit:.0f})"
                                 if off is not None else f"{label}: {x['codec']} to AAC, silent where compared"))
    checks.append(_lengths_check(a, b, float(opts["duration_tolerance_s"])))
    checks.append(_streams_check(a, b))
    return checks


# ---------------------------------------------------------------------------------------------- shared ffmpeg parts
def _audio_args(info: dict, audio_plan: list[str]) -> list[str]:
    args = []
    for k, (a, how) in enumerate(zip(info.get("audio") or [], audio_plan)):
        if how == "copy":
            args += [f"-c:a:{k}", "copy"]
        else:
            ch = int(a.get("channels") or 2)
            rate = {1: 96, 2: 192, 6: 448, 8: 640}.get(ch, 64 * ch)
            args += [f"-c:a:{k}", "aac", f"-b:a:{k}", f"{rate}k"]
    return args


def _maps(info: dict, keep_subs: bool, first_video: bool = False) -> list[str]:
    args = []
    for s in info["video"][:1] if first_video else info["video"]:
        args += ["-map", f"0:{s['index']}"]
    for s in info.get("audio") or []:
        args += ["-map", f"0:{s['index']}"]
    if keep_subs:
        for s in info.get("subs") or []:
            args += ["-map", f"0:{s['index']}"]
    return args


def _subs_plan(info: dict, target: str) -> tuple[list[dict], str]:
    """Subtitles a container change cannot carry become files beside the video; returns them and why it cannot
    go on ("" when it can)."""
    if target not in MP4_LIKE:
        return [], ""
    out, lost = [], []
    for s in info.get("subs") or []:
        if s["codec"] in TEXT_SUBS:
            out.append(dict(s, ext=TEXT_SUBS[s["codec"]]))
        elif s["codec"] in PICTURE_SUBS:
            out.append(dict(s, ext=PICTURE_SUBS[s["codec"]]))
        else:
            lost.append(s["codec"])
    if lost:
        return out, f"an MP4 cannot carry its picture subtitles ({', '.join(sorted(set(lost)))}) and they cannot be saved beside it"
    return out, ""


def _extract_subs(src: Path, subs: list[dict], stem_path: Path, timeout: float) -> list[str]:
    made = []
    for s in subs:
        name = f"{stem_path.stem}.{s['language']}{'.forced' if s.get('forced') else ''}"
        dest = stem_path.with_name(f"{name}.{s['ext']}")
        k = 2
        while dest.exists():
            dest = stem_path.with_name(f"{name}.{k}.{s['ext']}")
            k += 1
        codec = "copy" if s["ext"] in ("sup", "ass") else ("webvtt" if s["ext"] == "vtt" else "srt")
        p = _run(_ff("-y", "-i", str(src), "-map", f"0:{s['index']}", "-c:s", codec, str(dest)), timeout)
        if p.returncode == 0 and dest.exists():
            made.append(str(dest))
    return made


def _new_name(src: Path, target: str) -> Path:
    out = src.with_suffix("." + target)
    return out if out != src else src.with_name(src.stem + ".playable." + target)


# ---------------------------------------------------------------------------------------------- rewrap
def rewrap(src: Path, info: dict, p: dict, opts: dict, timeout: float = 6 * 3600) -> Result:
    start = time.time()
    target = p["target"]
    subs, lost = _subs_plan(info, target)
    if lost:
        return Result("kept", "Kept the original: a rewrap would lose something", why=lost,
                      facts=[["File", _describe(info)]], size_before=info["size"])
    final = _new_name(src, target)
    tmp = src.with_name(src.stem + ".ks-rewrap." + target)
    args = _ff("-y", "-i", str(src), *_maps(info, keep_subs=target not in MP4_LIKE), "-c:v", "copy",
               *_audio_args(info, p["audio"]), "-map_metadata", "0", "-map_chapters", "0")
    if target not in MP4_LIKE:
        args += ["-c:s", "copy"]
    if target in MP4_LIKE:
        args += ["-movflags", "+faststart"]
        if any(v["codec"] == "mpeg4" for v in info["video"]):
            args += ["-bsf:v", "mpeg4_unpack_bframes"]
    args.append(str(tmp))
    r = _run(args, timeout)
    if r.returncode != 0 or not tmp.exists():
        tmp.unlink(missing_ok=True)
        why = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg failed"]
        return Result("kept", "Kept the original: the rewrap did not work", why=why[0][:300],
                      facts=[["File", _describe(info)]], size_before=info["size"])
    new = probe(tmp)
    try:
        checks = prove_rewrap(src, tmp, info, new, p["audio"], opts) if new else [
            _check(False, "The new file can be read", "ffprobe could not read it")]
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        checks = [_check(False, "The checks could run", str(e)[:300])]
    took = time.time() - start
    copied = [a["codec"] for a, h in zip(info.get("audio") or [], p["audio"]) if h == "copy"]
    after = dict(new or info, size=file_size(tmp))
    facts = [["Before", _describe(info)], ["After", _describe(after)], ["Took", human_time(took)]]
    if not all(c["ok"] for c in checks):
        tmp.unlink(missing_ok=True)
        failed = [c["text"] for c in checks if not c["ok"]]
        return Result("kept", "Kept the original: the rewrap did not pass its checks",
                      why=f"{p['why']}. A rewrap was made and thrown away: {'; '.join(failed).lower()}.",
                      checks=checks, facts=facts, size_before=info["size"], seconds=took)
    final = _unique(final) if final.exists() else final  # never overwrite a file that is already there
    side = _extract_subs(src, subs, final, 1800)
    os.replace(tmp, final)
    what = ("Rewrapped into " + target.upper() + ": nothing re-encoded") if len(copied) == len(p["audio"]) else (
        "Rewrapped into " + target.upper() + ": picture copied, sound to AAC where needed")
    return Result("rewrapped", what, why=p["why"].capitalize() + ".", checks=checks, facts=facts,
                  new_file=str(final), sidecars=side, size_before=info["size"], size_after=file_size(final),
                  seconds=took)


def _unique(p: Path) -> Path:
    n = 2
    cand = p
    while cand.exists():
        cand = p.with_name(f"{p.stem} ({n}){p.suffix}")
        n += 1
    return cand


# ---------------------------------------------------------------------------------------------- re-encode
def _vfilter(info: dict, p: dict) -> str:
    parts = []
    if p.get("scale_to"):
        parts.append(f"scale=-2:{int(p['scale_to'])}:flags=lanczos")
    parts += ["pad=ceil(iw/2)*2:ceil(ih/2)*2", "format=yuv420p"]
    return ",".join(parts)


def _x264(opts: dict, crf: int) -> list[str]:
    a = ["-c:v:0", "libx264", "-preset", str(opts["x264_preset"]), "-crf", str(crf), "-profile:v", "high",
         "-pix_fmt", "yuv420p"]
    if opts.get("x264_tune"):
        a += ["-tune", str(opts["x264_tune"])]
    if int(opts.get("threads") or 0):
        a += ["-threads", str(int(opts["threads"]))]
    return a


def predicted_size(info: dict, p: dict, opts: dict, crf: int, work: Path, timeout: float = 3600) -> int | None:
    """Bytes the whole file would take at this CRF: three 20-second samples of the picture, plus the sound as it
    will be (copied tracks at their own rate, new AAC at its set rate), plus 1 % for the container."""
    dur = float(info.get("duration") or 0)
    if dur <= 0:
        return None
    v = info["video"][0]
    secs = min(20.0, dur / 3)
    total_bytes, total_secs = 0, 0.0
    for i, f in enumerate((0.15, 0.5, 0.85)):
        out = work / f"sample-{crf}-{i}.mkv"
        r = _run(_ff("-y", "-ss", f"{max(0.0, dur * f - secs / 2):.3f}", "-i", str(info["_path"]),
                     "-map", f"0:{v['index']}", "-t", f"{secs:.3f}", "-vf", _vfilter(info, p), *_x264(opts, crf),
                     "-an", str(out)), timeout)
        if r.returncode != 0 or not out.exists():
            return None
        total_bytes += file_size(out)
        total_secs += secs
        out.unlink(missing_ok=True)
    video = total_bytes / total_secs * dur
    audio = 0.0
    for a, how in zip(info.get("audio") or [], p["audio"]):
        if how == "copy":
            audio += (a.get("bit_rate") or 192_000) / 8 * dur
        else:
            ch = int(a.get("channels") or 2)
            audio += {1: 96, 2: 192, 6: 448, 8: 640}.get(ch, 64 * ch) * 1000 / 8 * dur
    return int((video + audio) * 1.01)


def choose_crf(predict: Callable[[int], int | None], cap: int, opts: dict) -> tuple[int | None, list[str]]:
    """The lowest CRF (best picture) from crf_start down to crf_min whose predicted size fits under the cap;
    upwards to crf_max when crf_start does not fit. None when even crf_max does not fit."""
    notes = []
    start, lo, hi = int(opts["crf_start"]), int(opts["crf_min"]), int(opts["crf_max"])
    budget = int(cap * 0.97)
    size = predict(start)
    notes.append(f"CRF {start}: about {human_size(size)}" if size else f"CRF {start}: no estimate")
    if size is None:
        return start, notes
    if size <= budget:
        best = start
        for crf in range(start - 1, lo - 1, -1):
            s = predict(crf)
            notes.append(f"CRF {crf}: about {human_size(s)}" if s else f"CRF {crf}: no estimate")
            if s is None or s > budget:
                break
            best = crf
        return best, notes
    for crf in range(start + 1, hi + 1):
        s = predict(crf)
        notes.append(f"CRF {crf}: about {human_size(s)}" if s else f"CRF {crf}: no estimate")
        if s is not None and s <= budget:
            return crf, notes
    return None, notes


def vmaf_scores(new: Path, ref: Path, info: dict, p: dict, opts: dict, work: Path,
                timeout: float = 24 * 3600) -> tuple[list[float], int]:
    """VMAF of every scored picture of the new file against the decoded original (paired by picture number), and
    the scoring step (1 = every picture)."""
    frames = (info["video"][0].get("frames") or int((info.get("duration") or 0) * (info["video"][0].get("fps") or 25)))
    step = max(1, math.ceil(frames / int(opts["vmaf_max_frames"]))) if frames else 1
    log = work / "vmaf.json"
    threads = int(opts.get("threads") or 0) or max(1, (os.cpu_count() or 2) - 1)
    graph = (f"[0:v:0]setpts=N/FRAME_RATE/TB[d];[1:v:0]{_vfilter(info, p)},setpts=N/FRAME_RATE/TB[r];"
             f"[d][r]libvmaf=log_fmt=json:log_path={log.name}:n_subsample={step}:n_threads={threads}")
    # The log is written in the work folder (ffmpeg runs there), so no drive letter has to be escaped in the graph.
    r = _run(_ff("-i", str(new.resolve()), "-i", str(ref.resolve()), "-lavfi", graph, "-f", "null", "-"), timeout,
             cwd=str(work))
    if r.returncode != 0 or not log.exists():
        raise RuntimeError((r.stderr or b"").decode("utf-8", "replace").strip()[-300:] or "VMAF did not run")
    data = json.loads(log.read_text(encoding="utf-8"))
    scores = [float(f["metrics"]["vmaf"]) for f in data.get("frames") or [] if "vmaf" in (f.get("metrics") or {})]
    return scores, step


def vmaf_stats(scores: list[float], fps: float, step: int, window_s: float) -> dict:
    """mean; 1% low (mean of the worst 1 %, at least one picture); minimum (the lowest mean over any stretch of
    window_s seconds, or the worst single picture when window_s is 0)."""
    if not scores:
        return {"mean": 0.0, "low1": 0.0, "min": 0.0, "frames": 0}
    n = len(scores)
    mean = sum(scores) / n
    worst = sorted(scores)[: max(1, n // 100)]
    low1 = sum(worst) / len(worst)
    if window_s and fps:
        w = max(1, int(round(window_s * fps / step)))
        if w >= n:
            mn = mean
        else:
            s = sum(scores[:w])
            mn = s
            for i in range(w, n):
                s += scores[i] - scores[i - w]
                mn = min(mn, s)
            mn /= w
    else:
        mn = min(scores)
    return {"mean": round(mean, 2), "low1": round(low1, 2), "min": round(mn, 2), "frames": n}


def gate(stats: dict, opts: dict) -> list[dict]:
    w = float(opts["vmaf_window_s"])
    stretch = f"Lowest {int(w)}-second stretch" if w else "Worst single picture"
    return [_check(stats["mean"] >= float(opts["vmaf_mean"]), "VMAF mean", f"{stats['mean']:.1f} (at least {float(opts['vmaf_mean']):g})"),
            _check(stats["low1"] >= float(opts["vmaf_low1"]), "VMAF 1% low", f"{stats['low1']:.1f} (at least {float(opts['vmaf_low1']):g})"),
            _check(stats["min"] >= float(opts["vmaf_min"]), stretch, f"{stats['min']:.1f} (at least {float(opts['vmaf_min']):g})")]


def reencode(src: Path, info: dict, p: dict, opts: dict, timeout: float = 48 * 3600) -> Result:
    start = time.time()
    target = p["target"]
    subs, lost = _subs_plan(info, target)
    base_facts = [["Before", _describe(info)]]
    if lost:
        return Result("kept", "Kept the original: a re-encode would lose something", why=lost, facts=base_facts,
                      size_before=info["size"])
    cap = int(info["size"])
    info = dict(info, _path=str(src))
    with tempfile.TemporaryDirectory(prefix="ks-play-") as tdir:
        work = Path(tdir)
        cache: dict[int, int | None] = {}

        def predict(crf: int) -> int | None:
            if crf not in cache:
                cache[crf] = predicted_size(info, p, opts, crf, work)
            return cache[crf]

        crf, notes = choose_crf(predict, cap, opts)
        if crf is None:
            return Result("kept", "Kept the original: no copy would be smaller", why=(
                f"{p['why'].capitalize()}. Even at CRF {opts['crf_max']} the copy would not be smaller than the "
                f"original ({human_size(cap)})."), facts=base_facts + [["Tried", "; ".join(notes)]],
                size_before=cap, seconds=time.time() - start)
        tmp = src.with_name(src.stem + ".ks-encode." + target)
        tries: list[str] = []
        result_checks: list[dict] = []
        stats: dict = {}
        passed = False
        new: dict = {}
        tried: set[int] = set()
        while crf is not None and crf not in tried and len(tried) < 3:
            tried.add(crf)
            args = _ff("-y", "-i", str(src), *_maps(info, keep_subs=target not in MP4_LIKE, first_video=True),
                       "-filter:v:0", _vfilter(info, p), *_x264(opts, crf), *_audio_args(info, p["audio"]),
                       "-map_metadata", "0", "-map_chapters", "0")
            if target not in MP4_LIKE:
                args += ["-c:s", "copy"]
            else:
                args += ["-movflags", "+faststart"]
            r = _run(args + [str(tmp)], timeout)
            if r.returncode != 0 or not tmp.exists():
                tmp.unlink(missing_ok=True)
                why = (r.stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:] or ["ffmpeg failed"]
                return Result("kept", "Kept the original: the re-encode did not work", why=why[0][:300],
                              facts=base_facts, size_before=cap, seconds=time.time() - start)
            size = file_size(tmp)
            if size >= cap:  # never larger, and a copy of the same size gains nothing
                tries.append(f"CRF {crf}: {human_size(size)}, not smaller")
                crf = crf + 1 if crf < int(opts["crf_max"]) else None
                continue
            new = probe(tmp)
            try:
                scores, step = vmaf_scores(tmp, src, info, p, opts, work)
            except (RuntimeError, OSError, ValueError) as e:
                tmp.unlink(missing_ok=True)
                return Result("kept", "Kept the original: the quality could not be measured", why=str(e)[:300],
                              facts=base_facts, size_before=cap, seconds=time.time() - start)
            fps = info["video"][0].get("fps") or 25
            stats = vmaf_stats(scores, fps, step, float(opts["vmaf_window_s"]))
            result_checks = gate(stats, opts)
            tries.append(f"CRF {crf}: {human_size(size)}, VMAF mean {stats['mean']:.1f}")
            if all(c["ok"] for c in result_checks):
                passed = True
                break
            # More bits may pass, if the size still allows it.
            nxt = crf - 2
            if nxt >= int(opts["crf_min"]) and size < int(cap * 0.85) and nxt not in tried:
                crf = nxt
                continue
            break
        took = time.time() - start
        if not passed:
            tmp.unlink(missing_ok=True)
            fail = [c for c in result_checks if not c["ok"]]
            reason = ("no copy at or under the original's size reached the quality gate"
                      if result_checks else "every copy came out at least as large as the original")
            return Result("kept", "Kept the original: " + ("no smaller copy was good enough" if result_checks else
                                                          "no copy would be smaller"),
                          why=f"{p['why'].capitalize()}. Tried {'; '.join(tries)}: {reason}"
                          + (f" ({', '.join(c['text'] + ' ' + c['detail'] for c in fail)})." if fail else "."),
                          checks=result_checks + ([_check(True, "Smaller than the original",
                                                          "every try was checked")] if result_checks else []),
                          facts=base_facts + [["Tried", "; ".join(tries)], ["Took", human_time(took)]],
                          size_before=cap, seconds=took)
        size = file_size(tmp)
        checks = list(result_checks)
        checks.append(_check(not_larger(size, cap) and size < cap, "Smaller than the original",
                             f"{human_size(size)} of {human_size(cap)}"))
        n_new = (new.get("video") or [{}])[0].get("frames")
        n_old = info["video"][0].get("frames")
        if n_new and n_old:
            checks.append(_check(n_new == n_old, "Same number of pictures", f"{n_new:,} and {n_old:,}"))
        checks.append(_lengths_check(info, new, max(float(opts["duration_tolerance_s"]), 0.5 / (info["video"][0].get("fps") or 25))))
        limit = float(opts["audio_offset_ms"])
        for k, (x, y) in enumerate(zip(info.get("audio") or [], new.get("audio") or [])):
            off = audio_offset_ms(src, f"0:{x['index']}", tmp, f"0:{y['index']}", info.get("duration") or 0)
            checks.append(_check(off is None or off <= limit, "Sound in step",
                                 f"track {k + 1} ({x['language']}): " + (f"offset {off:.0f} ms (at most {limit:.0f})"
                                                                         if off is not None else "silent where compared")))
        if not all(c["ok"] for c in checks):
            tmp.unlink(missing_ok=True)
            return Result("kept", "Kept the original: the re-encode did not pass its checks",
                          why=f"{p['why'].capitalize()}. " + "; ".join(c["text"] + " " + c["detail"] for c in checks
                                                                      if not c["ok"]),
                          checks=checks, facts=base_facts + [["Tried", "; ".join(tries)]], size_before=cap,
                          seconds=took)
        final = _new_name(src, target)
        side = _extract_subs(src, subs, final, 1800)
        final = _unique(final) if final.exists() else final
        os.replace(tmp, final)
        v = (new.get("video") or [{}])[0]
        how = f"x264 {opts['x264_preset']}, CRF {crf}" + (f", scaled to {v.get('height')} lines" if p.get("scale_to") else "")
        return Result("reencoded", f"Re-encoded to H.264 {v.get('width')}x{v.get('height')}: passed the quality gate",
                      why=f"{p['why'].capitalize()}. The picture was re-encoded ({how}); "
                          + ("the sound was copied." if all(h == "copy" for h in p["audio"]) else
                             "sound the device does not take became AAC."),
                      checks=checks, facts=base_facts + [["After", _describe(dict(new, size=size))],
                                                         ["Tried", "; ".join(tries)], ["Took", human_time(took)],
                                                         ["Scored", f"{stats['frames']:,} pictures"]],
                      new_file=str(final), sidecars=side, size_before=cap, size_after=size, seconds=took)


# ---------------------------------------------------------------------------------------------- one file
def make_playable(src: Path, device: dict, pb: dict | None = None) -> Result:
    """Decide and, when the settings allow it, rewrap or re-encode. Never touches the original."""
    opts = options(pb)
    dev = device_profile(device)
    info = probe(src)
    p = plan(info, dev)
    res = _act(src, info, p, dev, opts)
    if res.action in ("rewrapped", "reencoded") or p["action"] == "none":
        res.status = "ok"
    else:
        res.status = {"rewrap": "rewrap", "reencode": "convert"}.get(p["action"], "unknown")
    return res


def _act(src: Path, info: dict, p: dict, dev: dict, opts: dict) -> Result:
    name = dev.get("name") or "the device"
    if p["action"] == "unknown":
        return Result("unknown", "Could not read the file", why="ffprobe found no picture in it")
    if p["action"] == "none":
        v = info["video"][0]
        return Result("none", f"Plays on {name} as it is", facts=[["File", _describe(info)]], checks=[
            _check(True, "Container", f"{info['container']} is on the list"),
            _check(True, "Picture", f"{v['codec']}, {v.get('height')} lines, {v.get('bits', 8)}-bit"),
            _check(True, "Sound", ", ".join(a["codec"] for a in info["audio"]) or "none")],
            size_before=info["size"])
    if p["action"] == "rewrap":
        if not opts["rewrap"]:
            return Result("none", f"Plays on {name} after a rewrap; rewraps are off", why=p["why"].capitalize() + ".",
                          facts=[["File", _describe(info)]], size_before=info["size"])
        res = rewrap(src, info, p, opts)
        if res.action == "kept" and opts["reencode"] and any(not c["ok"] for c in res.checks):
            p2 = dict(p, action="reencode", target=dev["rewrap_to"], scale_to=None, eight_bit=True)
            res2 = reencode(src, info, p2, opts)
            res2.why = res.why + " " + res2.why
            return res2
        return res
    if not opts["reencode"]:
        return Result("none", f"Needs re-encoding for {name}; re-encodes are off", why=p["why"].capitalize() + ".",
                      facts=[["File", _describe(info)]], size_before=info["size"])
    return reencode(src, info, p, opts)
