"""Turn a release file name into a title, year, season and episode, and list what was stripped."""

from __future__ import annotations

import fnmatch
import re
from dataclasses import asdict, dataclass, field
from pathlib import PurePath

from guessit import guessit

from .textnorm import norm

# Tokens that only describe the release, matched case-insensitively as whole tokens.
_RELEASE_TOKEN = re.compile(
    r"^(\d{3,4}[pi]|4k|uhd|hdr\d*|dv|sdr|10bit|8bit|x26[45]|h\.?26[45]|hevc|avc|xvid|divx|"
    r"web-?dl|web-?rip|web|hdtv|hdrip|dvdrip|dvd|dvdscr|bdrip|brrip|bluray|blu-ray|remux|"
    r"aac\d?(\.\d)?|ac3|eac3|ddp?\d?(\.\d)?|dts(-hd)?|truehd|atmos|mp3|flac|opus|"
    r"\d\.\d|\d+ch|proper|repack|extended|unrated|limited|internal|dubbed|dual|multi|"
    r"subbed|hardsub|softsub|farsi|persian|english|nf|amzn|hulu|dsnp|hmax|atvp)$",
    re.I,
)
_EPISODE_TOKEN = re.compile(r"^(s\d{1,2}(e\d{1,3})+|s\d{1,2}|e\d{1,3}|\d{1,2}x\d{1,3})$", re.I)
_SPLIT = re.compile(r"[.\s_]+")
_CHANNELS_HEAD = re.compile(r"^(\d|[A-Za-z+]+\d)$")  # "5" or "DDP5", joined back with a following "1"


def _tokens(stem: str) -> list[str]:
    out: list[str] = []
    for tok in _SPLIT.split(stem):
        if out and len(tok) == 1 and tok.isdigit() and _CHANNELS_HEAD.match(out[-1]):
            out[-1] = f"{out[-1]}.{tok}"
        else:
            out.append(tok)
    return out
_SEASON_DIR = re.compile(r"^(season|series|staffel|saison|fasl|fasle)?\s*\d{1,2}$|^s\d{1,2}$", re.I)
_GENERIC_STEM = re.compile(r"^(vts_\d+_\d+|video_ts|e\d{1,3}|ep?\s?\d{1,3}|episode\s?\d{1,3}|\d{1,3})$", re.I)


@dataclass
class Parsed:
    kind: str  # "movie", "episode" or "unknown"
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episodes: list[int] = field(default_factory=list)
    clean: str = ""  # what the review page shows as "Name after stripping"
    removed: list[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)  # screen size, source, codec, release group, languages

    def to_dict(self) -> dict:
        return asdict(self)


def _junk_word_patterns(words: list[str]) -> list[re.Pattern]:
    pats = []
    for w in words:
        w = w.strip()
        if w:
            # whole token, also inside [brackets] or (parentheses)
            pats.append(re.compile(r"[\[\(\{]?(?<![A-Za-z0-9])" + re.escape(w) + r"(?![A-Za-z0-9])[\]\)\}]?", re.I))
    return pats


def strip_junk(stem: str, junk_words: list[str]) -> tuple[str, list[str]]:
    """Remove site tags and listed release words; return the rest and what was removed."""
    removed: list[str] = []
    s = stem
    for m in re.finditer(r"(?:www\.)?[A-Za-z0-9-]+\.(?:com|net|org|ir|me|mx|to|in|cc|tv|info|xyz)\b", s, re.I):
        removed.append(m.group(0))
    s = re.sub(r"(?:www\.)?[A-Za-z0-9-]+\.(?:com|net|org|ir|me|mx|to|in|cc|tv|info|xyz)\b", " ", s, flags=re.I)
    for pat in _junk_word_patterns(junk_words):
        for m in pat.finditer(s):
            removed.append(m.group(0))
        s = pat.sub(" ", s)
    s = re.sub(r"[\[\(\{]\s*[\]\)\}]", " ", s)  # brackets left empty
    s = re.sub(r"[\s._-]+$", "", re.sub(r"^[\s._-]+", "", s))
    return s, removed


def _title_case(t: str) -> str:
    return t.title() if t == t.lower() or t == t.upper() else t


def parse_name(rel_path: str, junk_words: list[str] | None = None) -> Parsed:
    """Parse one video file. rel_path may include folders; the folder helps when the file name is bare."""
    p = PurePath(rel_path.replace("\\", "/"))
    stem, ext = p.stem, p.suffix.lstrip(".").lower()
    folders = [x for x in p.parent.parts if x not in (".", "")]

    # A DVD (VIDEO_TS folder) is named after the folder that holds it and always goes to review.
    up = [x.lower() for x in folders]
    if "video_ts" in up:
        i = up.index("video_ts")
        name = folders[i - 1] if i > 0 else "DVD"
        nstem, removed = strip_junk(name, junk_words or [])
        info = dict(guessit(nstem))
        title = re.sub(r"\s+", " ", nstem.replace("_", " ").replace(".", " ")).strip()
        year = info.get("year") if isinstance(info.get("year"), int) else None
        return Parsed(kind="unknown", title=title, year=year, clean=f"{title} ({year})" if year else title,
                      removed=removed + [stem, ext.upper()], facts={"dvd": "VIDEO_TS"})

    stem2, removed = strip_junk(stem, junk_words or [])
    info = dict(guessit(f"{stem2}.{ext}" if ext else stem2))

    # Bare names (E01.mkv) take their title from the folders above, skipping "Season 2" folders.
    if folders and (not info.get("title") or _GENERIC_STEM.match(stem2.strip())):
        for folder in reversed(folders[-3:]):
            pstem, _ = strip_junk(folder, junk_words or [])
            pinfo = dict(guessit(pstem))
            if _SEASON_DIR.match(pstem.strip()):
                if info.get("season") is None and pinfo.get("season") is not None:
                    info["season"] = pinfo["season"]
                continue
            if pinfo.get("title"):
                info["title"] = pinfo["title"]
                for k in ("year", "season"):
                    if info.get(k) is None and pinfo.get(k) is not None:
                        info[k] = pinfo[k]
                break
        if info.get("episode") is not None:
            info["type"] = "episode"

    title = str(info.get("title") or "").strip()
    part = re.search(r"(?<![A-Za-z])part[ ._-]+([A-Za-z]+|\d+)(?![A-Za-z])", stem2, re.I)
    if info.get("part") and part and title:
        title = f"{title} Part {part.group(1)}"  # "Dead Reckoning Part One" keeps its part
    year = info.get("year") if isinstance(info.get("year"), int) else None
    season = info.get("season") if isinstance(info.get("season"), int) else None
    ep = info.get("episode")
    episodes = ep if isinstance(ep, list) else ([ep] if isinstance(ep, int) else [])
    if info.get("type") == "episode" and episodes:
        kind = "episode"
        if season is None:
            season = 1
    elif info.get("type") == "episode" and info.get("date"):
        kind = "unknown"  # date-named episodes are left for the review page
    elif title:
        kind = "movie" if info.get("type") == "movie" else "unknown"
    else:
        kind = "unknown"
    if kind == "unknown" and title and not episodes and info.get("type") == "movie":
        kind = "movie"

    # Everything in the original stem that is not part of the title, year or episode marker.
    title_words = norm(title).split()
    year_s = str(year) if year else None
    for tok in _tokens(stem2):
        t = tok.strip("[](){}-")
        if not t:
            continue
        n = norm(t)
        if n and all(w in title_words for w in n.split()):
            continue
        if year_s and t == year_s:
            continue
        if _EPISODE_TOKEN.match(t) or (episodes and n.isdigit() and int(n) in episodes):
            continue
        if _RELEASE_TOKEN.match(t):
            removed.append(t)
            continue
        if "-" in t and _RELEASE_TOKEN.match(t.split("-")[0]):
            removed.extend([t.split("-")[0], t.split("-", 1)[1]])
            continue
        removed.append(tok.strip())
    if ext:
        removed.append(ext.upper())

    title = _title_case(title)
    if kind == "episode":
        e = "-".join(f"E{x:02d}" for x in episodes)
        clean = f"{title} S{season:02d}{e}"
    else:
        clean = f"{title} ({year})" if year else title
    facts = {k: str(info[k]) for k in ("screen_size", "source", "video_codec", "audio_codec", "release_group",
                                        "language", "subtitle_language", "edition") if info.get(k)}
    seen: set[str] = set()
    removed = [r for r in removed if r and not (r.lower() in seen or seen.add(r.lower()))]
    return Parsed(kind=kind, title=title, year=year, season=season, episode=episodes[0] if episodes else None,
                  episodes=episodes, clean=clean or stem, removed=removed, facts=facts)


def is_junk_file(name: str, size: int, junk_cfg: dict, video_exts: list[str]) -> bool:
    """True for adverts, shortcuts, torrents and samples, by the configured patterns."""
    low = name.lower()
    for pat in junk_cfg.get("files", []):
        if fnmatch.fnmatch(low, pat.lower()):
            ext = low.rsplit(".", 1)[-1] if "." in low else ""
            if "sample" in pat.lower() and ext in video_exts:
                return size < int(junk_cfg.get("sample_max_mb", 150)) * 1024 * 1024
            return True
    return False
