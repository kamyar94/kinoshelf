"""TVmaze: series search, alternative titles and episode facts. No key needed."""

from __future__ import annotations

import re
from typing import Any

from ..models import Candidate, Query
from .base import Source

BASE = "https://api.tvmaze.com"
MAX_RESULTS = 8
MAX_ALT = 50
_IMDB = re.compile(r"^tt\d+$")


def _minutes(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


class TvmazeSource(Source):
    key = "tvmaze"
    name = "TVmaze"
    kinds = ("series",)  # it knows no films, so it says nothing about one

    def _get(self, path: str, **params: Any) -> Any:
        return self.net.get_json(BASE + path, params or None, {"Accept": "application/json"})

    def _show(self, show: Any) -> Candidate | None:
        if not isinstance(show, dict) or show.get("id") is None or not show.get("name"):
            return None
        ids = {"tvmaze": str(show["id"])}
        imdb = (show.get("externals") or {}).get("imdb")
        if imdb:
            ids["imdb"] = imdb
        premiered = show.get("premiered") or ""
        channel = show.get("network") or show.get("webChannel") or {}
        country = (channel.get("country") or {}).get("code")
        name = channel.get("name")
        return Candidate(
            kind="series",
            title=show["name"],
            year=int(premiered[:4]) if premiered[:4].isdigit() else None,
            runtime_min=_minutes(show.get("averageRuntime")) or _minutes(show.get("runtime")),
            ids=ids,
            sources=[self.name],
            poster_url=(show.get("image") or {}).get("medium") or None,
            people=f"{name} ({country})" if name and country else (name or country or None),
        )

    def search(self, q: Query) -> list[Candidate]:
        if q.kind == "movie" or not q.title.strip():
            return []
        r = self._get("/search/shows", q=q.title)
        out = []
        for hit in r if isinstance(r, list) else []:
            c = self._show(hit.get("show") if isinstance(hit, dict) else None)
            if c:
                out.append(c)
        return out[:MAX_RESULTS]

    def details(self, c: Candidate, q: Query) -> Candidate:
        show_id = c.ids.get("tvmaze")
        if not show_id or c.kind != "series":
            return c
        akas = self._get(f"/shows/{show_id}/akas")
        if isinstance(akas, list):
            seen = {c.title, c.original_title}
            alts = list(c.alt_titles)
            for a in akas:
                n = a.get("name") if isinstance(a, dict) else None
                if n and n.strip() and n not in seen:
                    seen.add(n)
                    alts.append(n)
            c.alt_titles = alts[:MAX_ALT]
        if q.season is not None and q.episode is not None:
            e = self._get(f"/shows/{show_id}/episodebynumber", season=q.season, number=q.episode)
            c.season, c.episode = q.season, q.episode
            if not isinstance(e, dict):
                c.episode_exists = False
            else:
                c.episode_exists = True
                c.episode_title = e.get("name") or None
                c.episode_runtime_min = _minutes(e.get("runtime"))
        return c

    def by_id(self, q: Query) -> list[Candidate]:
        out: list[Candidate] = []
        tvmaze_id = str(q.tvmaze_id or "").strip()
        if tvmaze_id.isdigit():
            c = self._show(self._get(f"/shows/{tvmaze_id}"))
            if c:
                out.append(self.details(c, q))
        imdb = (q.imdb_id or "").strip().lower()
        if _IMDB.match(imdb):
            c = self._show(self._get("/lookup/shows", imdb=imdb))  # 404 when TVmaze has no such show
            if c and all(o.ids["tvmaze"] != c.ids["tvmaze"] for o in out):
                out.append(self.details(c, q))
        return out
