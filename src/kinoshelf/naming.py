"""Naming templates: {Title} ({Year}) and friends, made safe for Windows and Linux file systems."""

from __future__ import annotations

import re

from .paths import is_windows_style, pure

TOKENS = {
    "Title": "film title",
    "OriginalTitle": "original title",
    "Year": "year",
    "Series": "series title",
    "SS": "season, two digits",
    "S": "season",
    "EE": "episode, two digits (01-E02 for double episodes)",
    "E": "episode",
    "Episode": "episode title",
    "Resolution": "picture height, e.g. 1080p",
    "ImdbId": "IMDb id",
    "TmdbId": "TMDB id",
    "ext": "file extension",
}
_TOKEN = re.compile(r"\{([A-Za-z]+)\}")
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


class TemplateError(ValueError):
    pass


def safe_value(v: object) -> str:
    """A token value with characters that file systems refuse replaced."""
    s = "" if v is None else str(v)
    s = s.replace(": ", " - ").replace(":", "-").replace('"', "'")
    s = re.sub(r"[\\/|]", "-", s)
    s = re.sub(r"[<>?*\x00-\x1f]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:180].rstrip(" .")


def _tidy_component(c: str) -> str:
    c = re.sub(r"\s*\(\s*\)", "", c)  # "( )" left by an empty token
    c = re.sub(r"\s*\[\s*\]", "", c)
    c = re.sub(r"\s+-\s*(?=\.[A-Za-z0-9]+$)", "", c)  # "Name - .mkv"
    c = re.sub(r"\s+-\s*$", "", c)
    c = re.sub(r"\s+(?=\.[A-Za-z0-9]+$)", "", c)
    c = re.sub(r"\s{2,}", " ", c).strip()
    c = c.rstrip(" .") if not re.search(r"\.[A-Za-z0-9]+$", c) else c
    if c.split(".")[0].lower() in _RESERVED:
        c = "_" + c
    return c


def render(template: str, fields: dict) -> str:
    """Fill a template and tidy each folder and file name. Unknown tokens raise TemplateError."""
    for name in _TOKEN.findall(template):
        if name not in TOKENS:
            raise TemplateError(f"Unknown token {{{name}}}")
    out = _TOKEN.sub(lambda m: safe_value(fields.get(m.group(1))), template)
    win = is_windows_style(template)
    sep = "\\" if win else "/"
    parts = re.split(r"[\\/]", out)
    head = parts[0]  # drive ("X:") or "" for an absolute POSIX path, kept as written
    tidy = [head] + [_tidy_component(p) for p in parts[1:]]
    if any(p == "" for p in tidy[1:]):
        raise TemplateError("A folder name came out empty")
    path = sep.join(tidy)
    if win and len(path) > 250:
        raise TemplateError("The path is longer than Windows allows")
    return str(pure(path))


def fields_for(kind: str, cand: dict, parsed: dict, ext: str, probe: dict | None = None) -> dict:
    """Template fields for a chosen candidate (a Candidate dict) and the parsed file name."""
    probe = probe or {}
    height = probe.get("height")
    f = {
        "Title": cand.get("title"),
        "OriginalTitle": cand.get("original_title") or cand.get("title"),
        "Year": cand.get("year"),
        "Series": cand.get("title"),
        "Resolution": f"{height}p" if height else "",
        "ImdbId": (cand.get("ids") or {}).get("imdb", ""),
        "TmdbId": (cand.get("ids") or {}).get("tmdb", ""),
        "ext": ext.lower().lstrip("."),
    }
    if kind == "episode":
        season = parsed.get("season") or cand.get("season") or 1
        eps = parsed.get("episodes") or ([cand.get("episode")] if cand.get("episode") else [])
        f.update({
            "S": season,
            "SS": f"{season:02d}",
            "E": "-".join(str(e) for e in eps),
            "EE": "-E".join(f"{e:02d}" for e in eps),
            "Episode": cand.get("episode_title") or "",
        })
    return f
