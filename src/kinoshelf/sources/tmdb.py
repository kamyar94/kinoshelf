"""TMDB: search, details and id lookups for films and series."""

from __future__ import annotations

import re
import statistics
from typing import Any

from ..models import Candidate, LookupError_, Query
from .base import Source

BASE = "https://api.themoviedb.org/3"
POSTER = "https://image.tmdb.org/t/p/w185"
MAX_RESULTS = 8
MAX_ALT = 50
_IMDB = re.compile(r"^tt\d+$")


def _year(date: Any) -> int | None:
    m = re.match(r"(\d{4})", date) if isinstance(date, str) else None
    return int(m.group(1)) if m else None


def _minutes(value: Any) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _line(name: str | None, country: str | None) -> str | None:
    if name and country:
        return f"{name} ({country})"
    return name or country or None


class TmdbSource(Source):
    key = "tmdb"
    name = "TMDB"

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled") and str(self.cfg.get("api_key") or "").strip())

    # -- requests ---------------------------------------------------------------------

    def _get(self, path: str, **params: Any) -> dict | None:
        """GET one TMDB endpoint. None when TMDB answers 404."""
        api_key = str(self.cfg.get("api_key") or "").strip()
        if not api_key:
            raise LookupError_("TMDB: no API key set")
        headers = {"Accept": "application/json"}
        query = {k: v for k, v in params.items() if v is not None}
        if api_key.startswith("eyJ"):  # v4 read access token
            headers["Authorization"] = f"Bearer {api_key}"
        else:
            query["api_key"] = api_key
        query["language"] = self.cfg.get("language") or "en-US"
        r = self.net.get_json(BASE + path, query, headers)
        if r is not None and not isinstance(r, dict):
            raise LookupError_("TMDB: unexpected answer")
        return r

    # -- mapping ----------------------------------------------------------------------

    def _movie(self, item: dict) -> Candidate | None:
        title = item.get("title") or item.get("original_title")
        if not title or item.get("id") is None:
            return None
        return Candidate(
            kind="movie",
            title=title,
            year=_year(item.get("release_date")),
            original_title=item.get("original_title") or None,
            ids={"tmdb": str(item["id"])},
            sources=[self.name],
            poster_url=_poster(item),
        )

    def _series(self, item: dict) -> Candidate | None:
        title = item.get("name") or item.get("original_name")
        if not title or item.get("id") is None:
            return None
        return Candidate(
            kind="series",
            title=title,
            year=_year(item.get("first_air_date")),
            original_title=item.get("original_name") or None,
            ids={"tmdb": str(item["id"])},
            sources=[self.name],
            poster_url=_poster(item),
        )

    # -- search -----------------------------------------------------------------------

    def search(self, q: Query) -> list[Candidate]:
        if not q.title.strip():
            return []
        found: list[tuple[float, Candidate]] = []
        if q.kind != "episode":
            found += self._search_kind("movie", q)
        if q.kind != "movie":
            found += self._search_kind("tv", q)
        if q.kind not in ("movie", "episode"):
            found.sort(key=lambda t: -t[0])  # popularity order across both lists
        return [c for _, c in found[:MAX_RESULTS]]

    def _search_kind(self, kind: str, q: Query) -> list[tuple[float, Candidate]]:
        if kind == "movie":
            r = self._get("/search/movie", query=q.title, year=q.year)
            build = self._movie
        else:
            r = self._get("/search/tv", query=q.title, first_air_date_year=q.year)
            build = self._series
        out: list[tuple[float, Candidate]] = []
        for item in (r or {}).get("results") or []:
            c = build(item)
            if c:
                pop = item.get("popularity")
                out.append((float(pop) if isinstance(pop, (int, float)) else 0.0, c))
        return out

    # -- details ----------------------------------------------------------------------

    def details(self, c: Candidate, q: Query) -> Candidate:
        if c.ids.get("tmdb"):
            self._fill(c, q)
        return c

    def _fill(self, c: Candidate, q: Query) -> bool:
        """Fetch the detail record into c. False when TMDB does not know the id."""
        if c.kind == "movie":
            return self._fill_movie(c)
        if not self._fill_series(c):
            return False
        if q.season is not None and q.episode is not None:
            self._fill_episode(c, q.season, q.episode)
        return True

    def _basics(self, c: Candidate, d: dict, title_key: str, original_key: str, date_key: str) -> None:
        c.title = c.title or d.get(title_key) or d.get(original_key) or ""
        c.original_title = c.original_title or d.get(original_key) or None
        c.year = c.year or _year(d.get(date_key))
        c.poster_url = c.poster_url or _poster(d)
        if self.name not in c.sources:
            c.sources.append(self.name)

    def _fill_movie(self, c: Candidate) -> bool:
        d = self._get(f"/movie/{c.ids['tmdb']}", append_to_response="alternative_titles,external_ids,credits")
        if d is None:
            return False
        self._basics(c, d, "title", "original_title", "release_date")
        c.runtime_min = _minutes(d.get("runtime")) or c.runtime_min
        imdb = d.get("imdb_id") or (d.get("external_ids") or {}).get("imdb_id")
        if imdb:
            c.ids["imdb"] = imdb
        alts = [t.get("title") for t in (d.get("alternative_titles") or {}).get("titles") or []]
        c.alt_titles = _merge_alts(c, alts)
        crew = (d.get("credits") or {}).get("crew") or []
        directors = list(dict.fromkeys(p["name"] for p in crew if p.get("job") == "Director" and p.get("name")))
        if directors:
            c.people = ", ".join(directors[:3])
        return True

    def _fill_series(self, c: Candidate) -> bool:
        d = self._get(f"/tv/{c.ids['tmdb']}", append_to_response="alternative_titles,external_ids")
        if d is None:
            return False
        self._basics(c, d, "name", "original_name", "first_air_date")
        runs = [r for r in d.get("episode_run_time") or [] if isinstance(r, (int, float)) and r > 0]
        runtime = statistics.median(runs) if runs else _minutes((d.get("last_episode_to_air") or {}).get("runtime"))
        c.runtime_min = float(runtime) if runtime else c.runtime_min
        imdb = (d.get("external_ids") or {}).get("imdb_id")
        if imdb:
            c.ids["imdb"] = imdb
        alts = [t.get("title") for t in (d.get("alternative_titles") or {}).get("results") or []]
        c.alt_titles = _merge_alts(c, alts)
        network = (d.get("networks") or [{}])[0] or {}
        countries = d.get("origin_country") or []
        country = countries[0] if countries else network.get("origin_country")
        c.people = _line(network.get("name"), country) or c.people
        return True

    def _fill_episode(self, c: Candidate, season: int, episode: int) -> None:
        e = self._get(f"/tv/{c.ids['tmdb']}/season/{season}/episode/{episode}")
        c.season, c.episode = season, episode
        if e is None:
            c.episode_exists = False
            return
        c.episode_exists = True
        c.episode_title = e.get("name") or None
        c.episode_runtime_min = _minutes(e.get("runtime"))

    # -- by id ------------------------------------------------------------------------

    def by_id(self, q: Query) -> list[Candidate]:
        out: list[Candidate] = []
        seen: set[tuple[str, str]] = set()

        def add(c: Candidate) -> None:
            key = (c.kind, c.ids["tmdb"])
            if key not in seen:
                seen.add(key)
                out.append(c)

        imdb = (q.imdb_id or "").strip().lower()
        if _IMDB.match(imdb):
            r = self._get(f"/find/{imdb}", external_source="imdb_id") or {}
            for item in r.get("movie_results") or []:
                c = self._movie(item)
                if c and self._fill(c, q):
                    add(c)
            for item in r.get("tv_results") or []:
                c = self._series(item)
                if c and self._fill(c, q):
                    add(c)
            for ep in r.get("tv_episode_results") or []:
                c = self._episode_hit(ep)
                if c:
                    add(c)

        tmdb_id = str(q.tmdb_id or "").strip()
        if tmdb_id.isdigit() and q.tmdb_kind in ("movie", "tv"):
            kind = "movie" if q.tmdb_kind == "movie" else "series"
            if (kind, tmdb_id) not in seen:
                c = Candidate(kind=kind, title="", ids={"tmdb": tmdb_id}, sources=[self.name])
                if self._fill(c, q):
                    add(c)
        return out[:MAX_RESULTS]

    def _episode_hit(self, ep: dict) -> Candidate | None:
        """An IMDb episode id resolves to its show, with the season and episode set."""
        show_id = ep.get("show_id")
        if show_id is None:
            return None
        c = Candidate(kind="series", title="", ids={"tmdb": str(show_id)}, sources=[self.name])
        if not self._fill_series(c):
            return None
        c.season = ep.get("season_number")
        c.episode = ep.get("episode_number")
        c.episode_title = ep.get("name") or None
        c.episode_runtime_min = _minutes(ep.get("runtime"))
        c.episode_exists = True
        return c


def _poster(item: dict) -> str | None:
    path = item.get("poster_path")
    return POSTER + path if path else None


def _merge_alts(c: Candidate, names: list) -> list[str]:
    seen = {c.title, c.original_title}
    out: list[str] = []
    for n in [*c.alt_titles, *names]:
        if isinstance(n, str) and n.strip() and n not in seen:
            seen.add(n)
            out.append(n)
    return out[:MAX_ALT]
