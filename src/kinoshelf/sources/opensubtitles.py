"""OpenSubtitles file-hash lookup. A hash search downloads nothing and uses no download quota."""

from __future__ import annotations

import re
from typing import Any

from ..models import HashHit, LookupError_
from ..net import Net

URL = "https://api.opensubtitles.com/api/v1/subtitles"
_HASH = re.compile(r"^[0-9a-f]{16}$")


def _imdb(value: Any) -> str | None:
    """OpenSubtitles sends IMDb ids as integers: 1832382 becomes "tt1832382"."""
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip().lower()
    s = s[2:] if s.startswith("tt") else s
    if not s.isdigit() or int(s) == 0:
        return None
    return "tt" + str(int(s)).zfill(7)


def _tmdb(value: Any) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    s = str(value).strip()
    return str(int(s)) if s.isdigit() and int(s) > 0 else None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class OpenSubtitlesHash:
    """Looks up a file by its OpenSubtitles hash. Not a title source: it only confirms an identity."""

    def __init__(self, cfg: dict, net: Net):
        self.cfg = cfg or {}
        self.net = net

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled") and str(self.cfg.get("api_key") or "").strip())

    def lookup(self, oshash: str) -> list[HashHit]:
        """Hits whose subtitle was matched by the hash itself (not by name), without duplicates."""
        h = (oshash or "").strip().lower()
        if not _HASH.match(h):
            return []
        api_key = str(self.cfg.get("api_key") or "").strip()
        if not api_key:
            raise LookupError_("OpenSubtitles: no API key set")
        headers = {
            "Api-Key": api_key,
            "User-Agent": self.cfg.get("user_agent") or "Kinoshelf",
            "Accept": "application/json",
        }
        r = self.net.get_json(URL, {"moviehash": h}, headers)
        if r is None:
            return []
        if not isinstance(r, dict):
            raise LookupError_("OpenSubtitles: unexpected answer")
        hits: list[HashHit] = []
        seen: set[tuple] = set()
        for item in r.get("data") or []:
            attrs = item.get("attributes") if isinstance(item, dict) else None
            if not isinstance(attrs, dict) or attrs.get("moviehash_match") is not True:
                continue
            hit = self._hit(attrs.get("feature_details"))
            if hit is None:
                continue
            key = (hit.kind, hit.imdb_id or hit.title.lower(), hit.parent_imdb_id, hit.season, hit.episode)
            if key not in seen:
                seen.add(key)
                hits.append(hit)
        return hits

    @staticmethod
    def _hit(fd: Any) -> HashHit | None:
        if not isinstance(fd, dict):
            return None
        kind = {"movie": "movie", "episode": "episode"}.get(str(fd.get("feature_type") or "").lower())
        if kind is None:
            return None
        hit = HashHit(
            kind=kind,
            title=str(fd.get("title") or fd.get("movie_name") or fd.get("parent_title") or "").strip(),
            year=_int(fd.get("year")),
            imdb_id=_imdb(fd.get("imdb_id")),
            tmdb_id=_tmdb(fd.get("tmdb_id")),
        )
        if kind == "episode":
            hit.parent_imdb_id = _imdb(fd.get("parent_imdb_id"))
            hit.parent_tmdb_id = _tmdb(fd.get("parent_tmdb_id"))
            hit.parent_title = str(fd.get("parent_title") or "").strip() or None
            hit.season = _int(fd.get("season_number"))
            hit.episode = _int(fd.get("episode_number"))
        if not (hit.title or hit.imdb_id or hit.parent_imdb_id):
            return None
        return hit
