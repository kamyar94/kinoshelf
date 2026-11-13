"""TMDB: search, details and id lookups for films and series."""

from __future__ import annotations

import datetime
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
        d = self._get(f"/movie/{c.ids['tmdb']}",
                      append_to_response="alternative_titles,external_ids,credits,translations,keywords,release_dates")
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
        coll = d.get("belongs_to_collection") or {}
        c.facts = _facts(d, "movie") | ({"collection": self._collection(coll)} if coll.get("id") else {})
        if directors:
            c.facts["directors"] = directors
        first = _first_release_year(d)
        if first:
            c.facts["first_release_year"] = first  # the earliest release anywhere, festivals too (IMDb's year)
        return True

    def _collection(self, coll: dict) -> dict:
        """The TMDB collection a film belongs to, with how many of its films are already released."""
        cid = str(coll.get("id"))
        cache = getattr(self, "_collections", None)
        if cache is None:
            cache = self._collections = {}
        if cid not in cache:
            info = {"id": cid, "name": coll.get("name") or ""}
            try:
                d = self._get(f"/collection/{cid}") or {}
            except LookupError_:
                d = {}
            today = datetime.date.today().isoformat()
            parts = d.get("parts") or []
            if parts:
                info["released"] = sum(1 for p in parts if (p.get("release_date") or "9999") <= today)
                info["parts"] = len(parts)
            info["backdrop"] = d.get("backdrop_path") or coll.get("backdrop_path")
            cache[cid] = info
        return dict(cache[cid])

    def _fill_series(self, c: Candidate) -> bool:
        d = self._get(f"/tv/{c.ids['tmdb']}", append_to_response="alternative_titles,external_ids,translations,keywords")
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
        c.facts = _facts(d, "tv")
        return True

    def _fill_episode(self, c: Candidate, season: int, episode: int) -> None:
        e = self._get(f"/tv/{c.ids['tmdb']}/season/{season}/episode/{episode}", append_to_response="external_ids")
        c.season, c.episode = season, episode
        if e is None:
            c.episode_exists = False
            return
        c.episode_exists = True
        c.episode_title = e.get("name") or None
        c.episode_runtime_min = _minutes(e.get("runtime"))
        ext = e.get("external_ids") or {}
        ep_ids = {k: str(v) for k, v in (("tmdb", e.get("id")), ("imdb", ext.get("imdb_id")),
                                          ("tvdb", ext.get("tvdb_id"))) if v}
        c.facts = dict(c.facts or {}, episode={"overview": e.get("overview") or "", "air_date": e.get("air_date") or "",
                                               "rating": e.get("vote_average"), "ids": ep_ids})

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


def _first_release_year(d: dict) -> int | None:
    """The year of a film's earliest release in any country (premiere, festival, cinema, ...). TMDB's own date is
    usually the main release; IMDb dates a film by its first showing, which this matches."""
    years = []
    for country in (d.get("release_dates") or {}).get("results") or []:
        for r in country.get("release_dates") or []:
            y = _year(r.get("release_date"))
            if y:
                years.append(y)
    return min(years) if years else None


def _facts(d: dict, kind: str) -> dict:
    """What filing rules read from a TMDB detail record (films: kind "movie", series: "tv"): genres, keywords,
    countries, original language, the title in every language TMDB has, and alternative titles by country."""
    translations: dict[str, str] = {}
    translations_all: dict[str, list[str]] = {}  # every translation per language, in TMDB's order, untitled ones too
    for t in (d.get("translations") or {}).get("translations") or []:
        data = t.get("data") or {}
        title = data.get("title") or data.get("name") or ""
        lang = t.get("iso_639_1") or ""
        if lang:
            translations_all.setdefault(lang, []).append(title)
        if lang and title and lang not in translations:
            translations[lang] = title
    alt_titles: dict[str, list[str]] = {}
    alts = (d.get("alternative_titles") or {}).get("titles") or (d.get("alternative_titles") or {}).get("results") or []
    for a in alts:
        if a.get("iso_3166_1") and a.get("title"):
            alt_titles.setdefault(a["iso_3166_1"], []).append(a["title"])
    kw = d.get("keywords") or {}
    countries = [c.get("iso_3166_1") for c in d.get("production_countries") or [] if c.get("iso_3166_1")]
    out = {
        "source": "TMDB",
        "genres": [g.get("name") for g in d.get("genres") or [] if g.get("name")],
        "countries": countries or list(d.get("origin_country") or []),
        "country_names": [c.get("name") for c in d.get("production_countries") or [] if c.get("name")],
        "language": d.get("original_language") or "",
        "translations": translations,
        "translations_all": {k: v for k, v in translations_all.items() if len(v) > 1 or not v[0]},
        "alt_titles": alt_titles,
        "release_date": d.get("release_date") or d.get("first_air_date") or "",
        "overview": d.get("overview") or "",
        "keywords": [k.get("name") for k in (kw.get("keywords") or kw.get("results") or []) if k.get("name")],
    }
    if kind == "movie" and d.get("runtime"):
        out["runtime"] = d["runtime"]
    ext = d.get("external_ids") or {}
    if ext.get("tvdb_id"):
        out["tvdb"] = str(ext["tvdb_id"])
    if d.get("backdrop_path"):
        out["backdrop"] = d["backdrop_path"]
    return out


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
