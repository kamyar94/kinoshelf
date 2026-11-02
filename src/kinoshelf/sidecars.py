"""Names for the files that come with a video when the filing rules place it: subtitles as
<stem>.<language>[.forced|.sdh].<ext> (a second one of a language .alt, .alt2; the language from the file name or
from the text itself), picture subtitles in "Other Files" with an .ignore marker, and pictures as Jellyfin names them
(poster, backdrop, disc, banner, logo)."""

from __future__ import annotations

import re
from pathlib import Path, PurePath

TEXT_SUBS = {"srt", "ass", "ssa", "vtt", "smi"}
PICTURE_SUBS = {"idx", "sup"}  # ".sub" is a picture subtitle when an ".idx" of the same name is beside it
IMAGE_EXTS = {"jpg", "jpeg", "png"}
ARABIC = re.compile(r"[\u0600-\u06ff\u0750-\u077f\ufb50-\ufdff\ufe70-\ufeff]")
# ---------------------------------------------------------------------------------------------- sidecars
LANG_WORDS = {
    "english": "en", "eng": "en", "en": "en", "persian": "fa", "farsi": "fa", "fa": "fa", "per": "fa", "fas": "fa",
    "arabic": "ar", "ara": "ar", "ar": "ar", "french": "fr", "fre": "fr", "fra": "fr", "fr": "fr", "german": "de",
    "ger": "de", "deu": "de", "de": "de", "spanish": "es", "spa": "es", "es": "es", "turkish": "tr", "tur": "tr",
    "tr": "tr", "italian": "it", "ita": "it", "it": "it", "russian": "ru", "rus": "ru", "ru": "ru",
}
FLAG_WORDS = {"forced": "forced", "sdh": "sdh", "cc": "sdh", "hi": "sdh"}
STOP = {
    "en": "the you and to of is that it in what this not be have for we on with are your was don't i'm can".split(),
    "fr": "le la les et est vous je pas de que un une ce qui il en des du pour".split(),
    "es": "el la que de y no es un una los en por se lo qué para con las".split(),
    "de": "der die und ist ich nicht das du sie es zu ein mit den was wir".split(),
    "it": "il che non di è la un per una sono mi ti ho lo cosa ma".split(),
    "tr": "bir ve bu ne ben sen değil mi da de için çok var".split(),
}


def _decode(b: bytes) -> str:
    if b[:3] == b"\xef\xbb\xbf":
        return b[3:].decode("utf-8", "replace")
    if b[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return b.decode("utf-16", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        pass
    t1256 = b.decode("cp1256", "replace")
    if len(ARABIC.findall(t1256)) > 50:
        return t1256
    return b.decode("cp1252", "replace")


def subtitle_language(data: bytes) -> str:
    """Language of a subtitle from its text: script first, then common words. "" when unclear."""
    lines = []
    for line in _decode(data).splitlines():
        s = line.strip()
        if not s or s.isdigit() or "-->" in s:
            continue
        s = re.sub(r"^\{\d+\}\{\d+\}", "", s)
        lines.append(re.sub(r"<[^>]+>|\{[^}]*\}", " ", s))
    t = "\n".join(lines)
    if len(t) < 40:
        return ""
    n = max(1, len(re.findall(r"\w", t)))
    if len(ARABIC.findall(t)) / n > 0.3:
        return "fa" if re.search(r"[پچژگ]", t) or "ی" in t or "ک" in t else "ar"
    from collections import Counter
    words = Counter(w.lower() for w in re.findall(r"[A-Za-zÀ-ÿ']+", t))
    tot = sum(words.values()) or 1
    scores = {k: sum(words[w] for w in v) / tot for k, v in STOP.items()}
    best = max(scores, key=scores.get)
    second = sorted(scores.values())[-2]
    if scores[best] < 0.08 or scores[best] < second * 1.5:
        return ""
    return best


def subtitle_tokens(name: str, old_stem: str) -> tuple[str, list[str]]:
    """Language and flags named in a subtitle file name."""
    rest = name[len(old_stem):] if name.startswith(old_stem) else name
    toks = [t.lower() for t in re.split(r"[\s._\-\[\]()]+", PurePath(rest).stem) if t]
    lang = next((LANG_WORDS[t] for t in reversed(toks) if t in LANG_WORDS), "")
    flags = sorted({FLAG_WORDS[t] for t in toks if t in FLAG_WORDS})
    return lang, flags


def sidecar_name(src: Path, old_stem: str, new_stem: str, taken: set[str]) -> tuple[str, bool] | None:
    """New name for a subtitle beside the video (relative to the video's folder), or None to leave it.
    Returns (relative name, needs_ignore_marker). Text subtitles: <stem>.<lang>[.forced|.sdh].<ext>, a second one
    of the same language .alt, .alt2 ...; picture subtitles go to "Other Files" (the library shows text only)."""
    ext = src.suffix.lower().lstrip(".")
    lang, flags = subtitle_tokens(src.name, old_stem)
    picture = ext in PICTURE_SUBS or (ext == "sub" and src.with_suffix(".idx").exists())
    if not picture and ext not in TEXT_SUBS and ext != "sub":
        return None
    if not lang and not picture:
        try:
            with open(src, "rb") as fh:
                lang = subtitle_language(fh.read(16384))
        except OSError:
            lang = ""
    parts = [new_stem] + ([lang] if lang else [])
    flag = "." + ".".join(flags) if flags else ""
    prefix = "Other Files\\" if picture else ""
    base = ".".join(parts)
    cand = f"{prefix}{base}{flag}.{ext}"
    n = 1
    while cand.lower() in taken:
        alt = "alt" if n == 1 else f"alt{n}"
        cand = f"{prefix}{base}.{alt}{flag}.{ext}"
        n += 1
    taken.add(cand.lower())
    return cand, picture


def artwork_name(src: Path, size: tuple[int, int] | None, have: set[str]) -> str | None:
    """Jellyfin names for pictures that came with a film: poster, backdrop(N), disc, banner, logo. None to leave it."""
    n = src.name.lower()
    ext = src.suffix.lower().lstrip(".")
    if ext not in IMAGE_EXTS or re.match(r"(snap|screen|s\d+|.*snapshot|capture)", n):
        return None
    out_ext = "jpg" if ext in ("jpg", "jpeg") else ext
    if re.search(r"(disc|disk|\bcd\b|dvd)", n) and not re.search(r"cover|front", n):
        return None if "disc" in have else f"disc.{out_ext}"
    if "banner" in n:
        return None if "banner" in have else f"banner.{out_ext}"
    if "logo" in n and ext == "png":
        return None if "logo" in have else "logo.png"
    if not size:
        return None
    w, h = size
    if w <= 0 or h <= 0:
        return None
    if h / w >= 1.25 and h >= 400 and not re.search(r"back|inlay|frontback", n):
        return None if "poster" in have else f"poster.{out_ext}"
    if w / h >= 1.3 and w >= 800 and w / h < 3.5:
        k = 1
        while (f"backdrop{k}" if k > 1 else "backdrop") in have:
            k += 1
        return (f"backdrop{k}" if k > 1 else "backdrop") + f".{out_ext}"
    if w / h >= 3.5:
        return None if "banner" in have else f"banner.{out_ext}"
    return None


