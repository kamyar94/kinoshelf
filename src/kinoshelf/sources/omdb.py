"""OMDb: IMDb-keyed search and details through a free API key."""

from __future__ import annotations

import re
from typing import Any

from ..models import Candidate, LookupError_, Query
from .base import Source

BASE = "https://www.omdbapi.com/"
MAX_RESULTS = 8
_IMDB = re.compile(r"^tt\d+$")
_NOT_FOUND = ("not found", "too many results", "incorrect imdb id")
_KINDS = {"movie": "movie", "series": "series"}


def _na(value: Any) -> str | None:
    """OMDb writes "N/A" where it has nothing."""
    if value is None:
        return None
    s = str(value).strip()
    return None if s in ("", "N/A") else s


def _year(value: Any) -> int | None:
    m = re.match(r"\s*(\d{4})", str(value or ""))  # "2011" or "2005–2013"
    return int(m.group(1)) if m else None


def _minutes(value: Any) -> float | None:
    m = re.search(r"(\d+)\s*min", str(value or ""))
    return float(m.group(1)) if m else None


def _int(value: Any) -> int | None:
    s = _na(value)
    return int(s) if s and s.isdigit() else None


class OmdbSource(Source):
    key = "omdb"
    name = "OMDb"

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled") and str(self.cfg.get("api_key") or "").strip())

    def _get(self, **params: Any) -> dict | None:
        """One OMDb call. None when OMDb says it found nothing; bad key and limits raise."""
        api_key = str(self.cfg.get("api_key") or "").strip()
        if not api_key:
            raise LookupError_("OMDb: no API key set")
        query = {"apikey": api_key, **{k: v for k, v in params.items() if v is not None}}
        r = self.net.get_json(BASE, query)
        if r is None:
            return None
        if not isinstance(r, dict):
            raise LookupError_("OMDb: unexpected answer")
        if str(r.get("Response", "True")).lower() != "false":
            return r
        error = str(r.get("Error") or "").strip()
        if any(s in error.lower() for s in _NOT_FOUND):
            return None
        raise LookupError_(f"OMDb: {error or 'request failed'}".replace(api_key, "***"))

    def _item(self, item: dict) -> Candidate | None:
        kind = _KINDS.get(str(item.get("Type") or "").lower())
        imdb, title = _na(item.get("imdbID")), _na(item.get("Title"))
        if not kind or not imdb or not title:
            return None
        poster = _na(item.get("Poster"))
        return Candidate(
            kind=kind,
            title=title,
            year=_year(item.get("Year")),
            ids={"imdb": imdb},
            sources=[self.name],
            poster_url=poster if poster and poster.startswith("http") else None,
        )

    def search(self, q: Query) -> list[Candidate]:
        if not q.title.strip():
            return []
        kind = {"movie": "movie", "episode": "series"}.get(q.kind)
        r = self._get(s=q.title, type=kind, y=q.year)
        out = []
        for item in (r or {}).get("Search") or []:
            c = self._item(item) if isinstance(item, dict) else None
            if c:
                out.append(c)
        return out[:MAX_RESULTS]

    def _apply(self, c: Candidate, d: dict) -> None:
        c.runtime_min = _minutes(d.get("Runtime")) or c.runtime_min
        c.people = (_na(d.get("Director")) if c.kind == "movie" else _na(d.get("Country"))) or c.people

    def _episode(self, c: Candidate, imdb: str, season: int, episode: int) -> None:
        e = self._get(i=imdb, Season=season, Episode=episode)
        c.season, c.episode = season, episode
        if e is None:
            c.episode_exists = False
            return
        c.episode_exists = True
        c.episode_title = _na(e.get("Title"))
        c.episode_runtime_min = _minutes(e.get("Runtime"))

    def details(self, c: Candidate, q: Query) -> Candidate:
        imdb = c.ids.get("imdb")
        if not imdb:
            return c
        d = self._get(i=imdb)
        if d is None:
            return c
        self._apply(c, d)
        if c.kind == "series" and q.season is not None and q.episode is not None:
            self._episode(c, imdb, q.season, q.episode)
        return c

    def by_id(self, q: Query) -> list[Candidate]:
        imdb = (q.imdb_id or "").strip().lower()
        if not _IMDB.match(imdb):
            return []
        d = self._get(i=imdb)
        if d is None:
            return []
        if str(d.get("Type") or "").lower() == "episode":
            return self._from_episode(d)
        c = self._item(d)
        if not c:
            return []
        self._apply(c, d)
        if c.kind == "series" and q.season is not None and q.episode is not None:
            self._episode(c, imdb, q.season, q.episode)
        return [c]

    def _from_episode(self, ep: dict) -> list[Candidate]:
        """An episode id answers with its series, the episode facts set."""
        series_id = _na(ep.get("seriesID"))
        series = self._get(i=series_id) if series_id else None
        c = self._item(series) if series else None
        if not c or not series:
            return []
        self._apply(c, series)
        c.season, c.episode = _int(ep.get("Season")), _int(ep.get("Episode"))
        c.episode_title = _na(ep.get("Title"))
        c.episode_runtime_min = _minutes(ep.get("Runtime"))
        c.episode_exists = True
        return [c]
