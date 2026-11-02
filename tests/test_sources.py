"""Lookup sources, tested against a fake Net that serves canned JSON and records calls."""

from __future__ import annotations

import copy
import gzip
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from kinoshelf.models import Candidate, LookupError_, Query
from kinoshelf.sources import imdb as imdb_mod
from kinoshelf.sources.imdb import ImdbSource
from kinoshelf.sources.jellyfin import JellyfinClient
from kinoshelf.sources.omdb import OmdbSource
from kinoshelf.sources.opensubtitles import OpenSubtitlesHash
from kinoshelf.sources.tmdb import TmdbSource
from kinoshelf.sources.tvmaze import TvmazeSource

DATA = Path("unused")


class FakeNet:
    """Duck-typed Net: routes are matched on URL and a subset of the params; every call is recorded."""

    def __init__(self) -> None:
        self.routes: list[tuple[str, str, dict, Any]] = []
        self.calls: list[dict] = []
        self.files: dict[str, bytes] = {}

    def route(self, url: str, response: Any, params: dict | None = None, method: str = "GET") -> "FakeNet":
        """response: JSON data, None (a 404), an Exception to raise, or a callable(params) -> data."""
        self.routes.append((method, url, params or {}, response))
        return self

    def _serve(self, method: str, url: str, params: dict | None, headers: dict | None, body: Any = None) -> Any:
        params = dict(params or {})
        self.calls.append({"method": method, "url": url, "params": params, "headers": dict(headers or {}), "body": body})
        best: tuple[int, Any] | None = None
        for m, u, p, resp in self.routes:
            if m == method and u == url and all(k in params and str(params[k]) == str(v) for k, v in p.items()):
                if best is None or len(p) >= best[0]:
                    best = (len(p), resp)
        if best is None:
            raise AssertionError(f"unexpected {method} {url} {params}")
        resp = best[1]
        if isinstance(resp, Exception):
            raise resp
        if callable(resp):
            resp = resp(params)
        return copy.deepcopy(resp)

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        return self._serve("GET", url, params, headers)

    def post_json(self, url: str, body: Any, headers: dict | None = None, timeout: float | None = None) -> Any:
        return self._serve("POST", url, None, headers, body)

    def download(self, url: str, dest: Any, chunk: int = 1 << 20) -> None:
        self.calls.append({"method": "DOWNLOAD", "url": url, "dest": Path(dest)})
        if url not in self.files:
            raise LookupError_("datasets.imdbws.com: HTTP 503")
        Path(dest).write_bytes(self.files[url])

    def calls_to(self, url: str) -> list[dict]:
        return [c for c in self.calls if c["url"] == url]


def err(status: int = 500) -> LookupError_:
    return LookupError_(f"example.test: HTTP {status}")


# ---------------------------------------------------------------------------- TMDB

TMDB = "https://api.themoviedb.org/3"
V3_KEY = "0123456789abcdef0123456789abcdef"
V4_KEY = "eyJhbGciOiJIUzI1NiJ9.payload.signature"

MOVIE_SEARCH = {"results": [
    {"id": 44115, "title": "A Separation", "original_title": "جدایی نادر از سیمین",
     "release_date": "2011-03-16", "poster_path": "/sep.jpg", "popularity": 20.5},
    {"id": 999, "title": "Separation", "original_title": "Separation", "release_date": "",
     "poster_path": None, "popularity": 3.0},
    {"id": 1000, "title": "", "original_title": "", "popularity": 1.0},
]}
TV_SEARCH = {"results": [
    {"id": 1396, "name": "Breaking Bad", "original_name": "Breaking Bad", "first_air_date": "2008-01-20",
     "poster_path": "/bb.jpg", "popularity": 90.0},
]}
MOVIE_DETAIL = {
    "id": 44115, "title": "A Separation", "original_title": "جدایی نادر از سیمین", "release_date": "2011-03-16",
    "poster_path": "/sep.jpg", "runtime": 123, "imdb_id": "tt1832382",
    "alternative_titles": {"titles": [
        {"iso_3166_1": "DE", "title": "Nader und Simin - Eine Trennung"},
        {"iso_3166_1": "US", "title": "A Separation"},
        {"iso_3166_1": "FR", "title": "Une séparation"},
        {"iso_3166_1": "FR", "title": "Une séparation"},
    ]},
    "credits": {"crew": [
        {"job": "Writer", "name": "Asghar Farhadi"},
        {"job": "Director", "name": "Asghar Farhadi"},
        {"job": "Director", "name": "Asghar Farhadi"},
    ]},
}
TV_DETAIL = {
    "id": 1396, "name": "Breaking Bad", "original_name": "Breaking Bad", "first_air_date": "2008-01-20",
    "poster_path": "/bb.jpg", "episode_run_time": [47, 60, 45],
    "external_ids": {"imdb_id": "tt0903747", "tvdb_id": 81189},
    "alternative_titles": {"results": [{"iso_3166_1": "DE", "title": "Breaking Bad - Reihe"}]},
    "networks": [{"name": "AMC", "origin_country": "US"}], "origin_country": ["US"],
}


def tmdb(net: FakeNet, **cfg: Any) -> TmdbSource:
    return TmdbSource({"enabled": True, "api_key": V3_KEY, "language": "en-US", **cfg}, net, DATA)


def test_tmdb_enabled_needs_flag_and_key() -> None:
    net = FakeNet()
    assert tmdb(net).enabled()
    assert not tmdb(net, api_key="").enabled()
    assert not tmdb(net, enabled=False).enabled()


def test_tmdb_v3_key_goes_in_params_v4_token_in_header() -> None:
    net = FakeNet().route(f"{TMDB}/search/movie", {"results": []})
    tmdb(net).search(Query("movie", "x"))
    call = net.calls[-1]
    assert call["params"]["api_key"] == V3_KEY
    assert "Authorization" not in call["headers"]
    assert call["params"]["language"] == "en-US"

    tmdb(net, api_key=V4_KEY, language="fa-IR").search(Query("movie", "x"))
    call = net.calls[-1]
    assert call["headers"]["Authorization"] == f"Bearer {V4_KEY}"
    assert "api_key" not in call["params"]
    assert call["params"]["language"] == "fa-IR"


def test_tmdb_search_movie_maps_fields() -> None:
    net = FakeNet().route(f"{TMDB}/search/movie", MOVIE_SEARCH)
    out = tmdb(net).search(Query("movie", "A Separation", year=2011))
    assert net.calls[0]["params"]["query"] == "A Separation"
    assert net.calls[0]["params"]["year"] == 2011
    assert [c.ids["tmdb"] for c in out] == ["44115", "999"]  # the nameless result is dropped
    first = out[0]
    assert (first.kind, first.title, first.year) == ("movie", "A Separation", 2011)
    assert first.original_title == "جدایی نادر از سیمین"
    assert first.poster_url == "https://image.tmdb.org/t/p/w185/sep.jpg"
    assert first.sources == ["TMDB"]
    assert out[1].year is None and out[1].poster_url is None


def test_tmdb_search_leaves_year_out_when_unknown_and_caps_at_eight() -> None:
    many = {"results": [{"id": i, "title": f"Film {i}", "release_date": "2000-01-01"} for i in range(1, 15)]}
    net = FakeNet().route(f"{TMDB}/search/movie", many)
    out = tmdb(net).search(Query("movie", "Film"))
    assert "year" not in net.calls[0]["params"]
    assert len(out) == 8 and out[0].title == "Film 1"


def test_tmdb_search_episode_uses_tv_endpoint() -> None:
    net = FakeNet().route(f"{TMDB}/search/tv", TV_SEARCH)
    out = tmdb(net).search(Query("episode", "Breaking Bad", year=2008, season=1, episode=1))
    assert net.calls[0]["params"]["first_air_date_year"] == 2008
    c = out[0]
    assert (c.kind, c.title, c.year, c.ids) == ("series", "Breaking Bad", 2008, {"tmdb": "1396"})


def test_tmdb_search_unknown_merges_both_by_popularity() -> None:
    net = FakeNet().route(f"{TMDB}/search/movie", MOVIE_SEARCH).route(f"{TMDB}/search/tv", TV_SEARCH)
    out = tmdb(net).search(Query("unknown", "something"))
    assert [c.title for c in out] == ["Breaking Bad", "A Separation", "Separation"]
    assert [c.kind for c in out] == ["series", "movie", "movie"]


def test_tmdb_search_nothing_found_is_empty_list() -> None:
    net = FakeNet().route(f"{TMDB}/search/movie", {"page": 1, "results": []})
    assert tmdb(net).search(Query("movie", "zzzz")) == []
    assert tmdb(FakeNet().route(f"{TMDB}/search/movie", None)).search(Query("movie", "zzzz")) == []


def test_tmdb_error_raises_lookup_error() -> None:
    net = FakeNet().route(f"{TMDB}/search/movie", err(401))
    with pytest.raises(LookupError_):
        tmdb(net).search(Query("movie", "x"))
    with pytest.raises(LookupError_) as e:
        tmdb(FakeNet(), api_key="").search(Query("movie", "x"))
    assert V3_KEY not in str(e.value)


def test_tmdb_movie_details() -> None:
    net = FakeNet().route(f"{TMDB}/movie/44115", MOVIE_DETAIL)
    t = tmdb(net)
    c = Candidate(kind="movie", title="A Separation", year=2011, ids={"tmdb": "44115"}, sources=["TMDB"])
    out = t.details(c, Query("movie", "A Separation"))
    assert out is c
    assert net.calls[0]["params"]["append_to_response"] == "alternative_titles,external_ids,credits,translations,keywords"
    assert c.runtime_min == 123.0
    assert c.ids["imdb"] == "tt1832382"
    assert c.alt_titles == ["Nader und Simin - Eine Trennung", "Une séparation"]  # no title repeats, no dupes
    assert c.people == "Asghar Farhadi"


def test_tmdb_series_details_with_episode() -> None:
    net = (
        FakeNet()
        .route(f"{TMDB}/tv/1396", TV_DETAIL)
        .route(f"{TMDB}/tv/1396/season/1/episode/1", {"name": "Pilot", "runtime": 58})
    )
    c = Candidate(kind="series", title="Breaking Bad", year=2008, ids={"tmdb": "1396"}, sources=["TMDB"])
    tmdb(net).details(c, Query("episode", "Breaking Bad", season=1, episode=1))
    assert c.runtime_min == 47.0  # median of 45, 47, 60
    assert c.ids["imdb"] == "tt0903747"
    assert c.alt_titles == ["Breaking Bad - Reihe"]
    assert c.people == "AMC (US)"
    assert (c.season, c.episode, c.episode_title) == (1, 1, "Pilot")
    assert c.episode_runtime_min == 58.0 and c.episode_exists is True


def test_tmdb_series_runtime_falls_back_and_missing_episode_is_flagged() -> None:
    detail = dict(TV_DETAIL, episode_run_time=[], last_episode_to_air={"runtime": 49})
    net = FakeNet().route(f"{TMDB}/tv/1396", detail).route(f"{TMDB}/tv/1396/season/1/episode/99", None)
    c = Candidate(kind="series", title="Breaking Bad", ids={"tmdb": "1396"})
    tmdb(net).details(c, Query("episode", "Breaking Bad", season=1, episode=99))
    assert c.runtime_min == 49.0
    assert c.episode_exists is False and c.episode_title is None
    assert (c.season, c.episode) == (1, 99)


def test_tmdb_details_without_season_skips_episode_call() -> None:
    net = FakeNet().route(f"{TMDB}/tv/1396", TV_DETAIL)
    c = Candidate(kind="series", title="Breaking Bad", ids={"tmdb": "1396"})
    tmdb(net).details(c, Query("episode", "Breaking Bad"))
    assert len(net.calls) == 1 and c.episode_exists is None


def test_tmdb_by_id_imdb_movie_and_tv() -> None:
    find = {
        "movie_results": [{"id": 44115, "title": "A Separation", "release_date": "2011-03-16"}],
        "tv_results": [], "tv_episode_results": [],
    }
    net = FakeNet().route(f"{TMDB}/find/tt1832382", find).route(f"{TMDB}/movie/44115", MOVIE_DETAIL)
    out = tmdb(net).by_id(Query("movie", "", imdb_id="tt1832382"))
    assert net.calls[0]["params"]["external_source"] == "imdb_id"
    assert len(out) == 1 and out[0].runtime_min == 123.0 and out[0].ids == {"tmdb": "44115", "imdb": "tt1832382"}

    find_tv = {"movie_results": [], "tv_results": [{"id": 1396, "name": "Breaking Bad"}], "tv_episode_results": []}
    net = FakeNet().route(f"{TMDB}/find/tt0903747", find_tv).route(f"{TMDB}/tv/1396", TV_DETAIL)
    out = tmdb(net).by_id(Query("episode", "", imdb_id="tt0903747"))
    assert out[0].kind == "series" and out[0].ids["imdb"] == "tt0903747"


def test_tmdb_by_id_episode_id_resolves_to_show_with_season_and_episode() -> None:
    find = {"movie_results": [], "tv_results": [], "tv_episode_results": [
        {"id": 62085, "show_id": 1396, "season_number": 1, "episode_number": 1, "name": "Pilot", "runtime": 58},
    ]}
    net = FakeNet().route(f"{TMDB}/find/tt0959621", find).route(f"{TMDB}/tv/1396", TV_DETAIL)
    out = tmdb(net).by_id(Query("episode", "", imdb_id="tt0959621"))
    c = out[0]
    assert c.kind == "series" and c.title == "Breaking Bad"
    assert c.ids == {"tmdb": "1396", "imdb": "tt0903747"}  # the show's id, not the episode's
    assert (c.season, c.episode, c.episode_title, c.episode_exists) == (1, 1, "Pilot", True)
    assert c.episode_runtime_min == 58.0


def test_tmdb_by_id_tmdb_id_needs_kind() -> None:
    net = FakeNet().route(f"{TMDB}/tv/1396", TV_DETAIL).route(f"{TMDB}/movie/44115", MOVIE_DETAIL)
    t = tmdb(net)
    tv = t.by_id(Query("episode", "", tmdb_id="1396", tmdb_kind="tv"))
    assert tv[0].kind == "series" and tv[0].title == "Breaking Bad" and tv[0].year == 2008
    movie = t.by_id(Query("movie", "", tmdb_id="44115", tmdb_kind="movie"))
    assert movie[0].kind == "movie" and movie[0].ids["imdb"] == "tt1832382"
    n = len(net.calls)
    assert t.by_id(Query("movie", "", tmdb_id="44115")) == []
    assert t.by_id(Query("movie", "")) == []
    assert t.by_id(Query("movie", "", imdb_id="not-an-id")) == []
    assert len(net.calls) == n


def test_tmdb_by_id_not_found_is_empty() -> None:
    net = FakeNet().route(f"{TMDB}/movie/5", None).route(f"{TMDB}/find/tt0000009", {
        "movie_results": [], "tv_results": [], "tv_episode_results": []})
    t = tmdb(net)
    assert t.by_id(Query("movie", "", tmdb_id="5", tmdb_kind="movie")) == []
    assert t.by_id(Query("movie", "", imdb_id="tt0000009")) == []


# ------------------------------------------------------------------------- TVmaze

TVM = "https://api.tvmaze.com"
SHOW = {
    "id": 169, "name": "Breaking Bad", "premiered": "2008-01-20", "runtime": 60, "averageRuntime": 59,
    "externals": {"tvrage": 18164, "thetvdb": 81189, "imdb": "tt0903747"},
    "image": {"medium": "https://static.tvmaze.com/bb.jpg", "original": "https://static.tvmaze.com/bb_o.jpg"},
    "network": {"id": 20, "name": "AMC", "country": {"name": "United States", "code": "US"}}, "webChannel": None,
}
WEB_SHOW = {
    "id": 5, "name": "Web Thing", "premiered": None, "runtime": 30, "averageRuntime": None,
    "externals": {"imdb": None}, "image": None, "network": None,
    "webChannel": {"id": 1, "name": "Netflix", "country": None},
}


def tvmaze(net: FakeNet, **cfg: Any) -> TvmazeSource:
    return TvmazeSource({"enabled": True, **cfg}, net, DATA)


def test_tvmaze_enabled_follows_flag_and_needs_no_key() -> None:
    assert tvmaze(FakeNet()).enabled()
    assert not tvmaze(FakeNet(), enabled=False).enabled()


def test_tvmaze_skips_movies_without_calling() -> None:
    net = FakeNet()
    assert tvmaze(net).search(Query("movie", "Breaking Bad")) == []
    assert net.calls == []


def test_tvmaze_search_maps_fields() -> None:
    net = FakeNet().route(f"{TVM}/search/shows", [{"score": 1.2, "show": SHOW}, {"score": 0.4, "show": WEB_SHOW}])
    out = tvmaze(net).search(Query("episode", "Breaking Bad"))
    assert net.calls[0]["params"] == {"q": "Breaking Bad"}
    a, b = out
    assert (a.kind, a.title, a.year, a.runtime_min) == ("series", "Breaking Bad", 2008, 59.0)
    assert a.ids == {"tvmaze": "169", "imdb": "tt0903747"}
    assert a.poster_url == "https://static.tvmaze.com/bb.jpg"
    assert a.people == "AMC (US)" and a.sources == ["TVmaze"]
    assert (b.year, b.runtime_min, b.poster_url, b.people) == (None, 30.0, None, "Netflix")
    assert b.ids == {"tvmaze": "5"}
    assert tvmaze(net).search(Query("unknown", "Breaking Bad"))  # unknown still searches


def test_tvmaze_search_nothing_found_and_error() -> None:
    assert tvmaze(FakeNet().route(f"{TVM}/search/shows", [])).search(Query("episode", "zzz")) == []
    with pytest.raises(LookupError_):
        tvmaze(FakeNet().route(f"{TVM}/search/shows", err(429))).search(Query("episode", "x"))


def test_tvmaze_details_akas_and_episode() -> None:
    net = (
        FakeNet()
        .route(f"{TVM}/shows/169/akas", [{"name": "Breaking Bad", "country": None}, {"name": "Total Reset", "country": {}}])
        .route(f"{TVM}/shows/169/episodebynumber", {"name": "Pilot", "season": 1, "number": 1, "runtime": 58})
    )
    c = Candidate(kind="series", title="Breaking Bad", ids={"tvmaze": "169"})
    tvmaze(net).details(c, Query("episode", "Breaking Bad", season=1, episode=1))
    assert c.alt_titles == ["Total Reset"]
    assert net.calls[1]["params"] == {"season": 1, "number": 1}
    assert (c.season, c.episode, c.episode_title, c.episode_runtime_min, c.episode_exists) == (1, 1, "Pilot", 58.0, True)


def test_tvmaze_details_missing_episode_is_flagged() -> None:
    net = FakeNet().route(f"{TVM}/shows/169/akas", []).route(f"{TVM}/shows/169/episodebynumber", None)
    c = Candidate(kind="series", title="Breaking Bad", ids={"tvmaze": "169"})
    tvmaze(net).details(c, Query("episode", "Breaking Bad", season=9, episode=9))
    assert c.episode_exists is False and c.episode_title is None


def test_tvmaze_by_id() -> None:
    net = (
        FakeNet()
        .route(f"{TVM}/shows/169", SHOW)
        .route(f"{TVM}/shows/169/akas", [])
        .route(f"{TVM}/lookup/shows", SHOW, {"imdb": "tt0903747"})
        .route(f"{TVM}/lookup/shows", None, {"imdb": "tt0000009"})
    )
    t = tvmaze(net)
    assert t.by_id(Query("episode", "", tvmaze_id="169"))[0].title == "Breaking Bad"
    assert t.by_id(Query("episode", "", imdb_id="tt0903747"))[0].ids["tvmaze"] == "169"
    assert t.by_id(Query("episode", "", imdb_id="tt0000009")) == []
    both = t.by_id(Query("episode", "", tvmaze_id="169", imdb_id="tt0903747"))
    assert len(both) == 1  # same show by both ids is returned once
    n = len(net.calls)
    assert t.by_id(Query("episode", "")) == []
    assert len(net.calls) == n


# --------------------------------------------------------------------------- OMDb

OMDB = "https://www.omdbapi.com/"
OMDB_KEY = "k3y-secret"


def omdb(net: FakeNet, **cfg: Any) -> OmdbSource:
    return OmdbSource({"enabled": True, "api_key": OMDB_KEY, **cfg}, net, DATA)


def test_omdb_enabled_needs_key() -> None:
    assert omdb(FakeNet()).enabled()
    assert not omdb(FakeNet(), api_key="").enabled()


def test_omdb_search_maps_and_filters() -> None:
    body = {"Response": "True", "totalResults": "4", "Search": [
        {"Title": "A Separation", "Year": "2011", "imdbID": "tt1832382", "Type": "movie", "Poster": "https://img/sep.jpg"},
        {"Title": "Some Show", "Year": "2005–2013", "imdbID": "tt0000111", "Type": "series", "Poster": "N/A"},
        {"Title": "Some Show S1E1", "Year": "2005", "imdbID": "tt0000112", "Type": "episode", "Poster": "N/A"},
        {"Title": "Some Game", "Year": "2005", "imdbID": "tt0000113", "Type": "game", "Poster": "N/A"},
    ]}
    net = FakeNet().route(OMDB, body)
    out = omdb(net).search(Query("unknown", "Separation", year=2011))
    params = net.calls[0]["params"]
    assert params["apikey"] == OMDB_KEY and params["s"] == "Separation" and params["y"] == 2011
    assert "type" not in params
    assert [c.ids["imdb"] for c in out] == ["tt1832382", "tt0000111"]
    assert (out[0].kind, out[0].year, out[0].poster_url, out[0].sources) == ("movie", 2011, "https://img/sep.jpg", ["OMDb"])
    assert (out[1].kind, out[1].year, out[1].poster_url) == ("series", 2005, None)


def test_omdb_search_type_follows_query_kind() -> None:
    net = FakeNet().route(OMDB, {"Response": "False", "Error": "Movie not found!"})
    o = omdb(net)
    o.search(Query("movie", "x"))
    o.search(Query("episode", "x"))
    assert [c["params"].get("type") for c in net.calls] == ["movie", "series"]
    assert "y" not in net.calls[0]["params"]


def test_omdb_not_found_is_empty() -> None:
    for message in ("Movie not found!", "Too many results.", "Series or episode not found!"):
        net = FakeNet().route(OMDB, {"Response": "False", "Error": message})
        assert omdb(net).search(Query("movie", "zzz")) == []


@pytest.mark.parametrize("message", ["Invalid API key!", "Request limit reached!", "No API key provided."])
def test_omdb_key_and_limit_errors_raise(message: str) -> None:
    net = FakeNet().route(OMDB, {"Response": "False", "Error": message})
    with pytest.raises(LookupError_) as e:
        omdb(net).search(Query("movie", "x"))
    assert OMDB_KEY not in str(e.value)


def test_omdb_http_error_raises_and_hides_key() -> None:
    net = FakeNet().route(OMDB, err(401))
    with pytest.raises(LookupError_) as e:
        omdb(net).search(Query("movie", "x"))
    assert OMDB_KEY not in str(e.value)


def test_omdb_details_movie() -> None:
    net = FakeNet().route(OMDB, {
        "Response": "True", "Title": "A Separation", "Year": "2011", "imdbID": "tt1832382", "Type": "movie",
        "Runtime": "123 min", "Director": "Asghar Farhadi", "Country": "Iran, France",
    })
    c = Candidate(kind="movie", title="A Separation", year=2011, ids={"imdb": "tt1832382"})
    omdb(net).details(c, Query("movie", "A Separation"))
    assert net.calls[0]["params"]["i"] == "tt1832382"
    assert c.runtime_min == 123.0 and c.people == "Asghar Farhadi"


def test_omdb_details_na_values_are_ignored() -> None:
    net = FakeNet().route(OMDB, {"Response": "True", "Runtime": "N/A", "Director": "N/A", "Type": "movie"})
    c = Candidate(kind="movie", title="X", ids={"imdb": "tt0000001"})
    omdb(net).details(c, Query("movie", "X"))
    assert c.runtime_min is None and c.people is None


def test_omdb_details_series_episode() -> None:
    net = (
        FakeNet()
        .route(OMDB, {"Response": "True", "Type": "series", "Runtime": "47 min", "Country": "United States"}, {"i": "tt0903747"})
        .route(OMDB, {"Response": "True", "Title": "Pilot", "Runtime": "58 min", "Type": "episode"},
               {"i": "tt0903747", "Season": 1, "Episode": 1})
        .route(OMDB, {"Response": "False", "Error": "Series or episode not found!"},
               {"i": "tt0903747", "Season": 1, "Episode": 99})
    )
    o = omdb(net)
    c = Candidate(kind="series", title="Breaking Bad", ids={"imdb": "tt0903747"})
    o.details(c, Query("episode", "Breaking Bad", season=1, episode=1))
    assert c.runtime_min == 47.0 and c.people == "United States"
    assert (c.episode_title, c.episode_runtime_min, c.episode_exists) == ("Pilot", 58.0, True)

    c2 = Candidate(kind="series", title="Breaking Bad", ids={"imdb": "tt0903747"})
    o.details(c2, Query("episode", "Breaking Bad", season=1, episode=99))
    assert c2.episode_exists is False


def test_omdb_by_id_movie_series_and_episode() -> None:
    net = (
        FakeNet()
        .route(OMDB, {"Response": "True", "Title": "A Separation", "Year": "2011", "imdbID": "tt1832382",
                      "Type": "movie", "Poster": "N/A", "Runtime": "123 min", "Director": "Asghar Farhadi"},
               {"i": "tt1832382"})
        .route(OMDB, {"Response": "True", "Title": "Breaking Bad", "Year": "2008–2013", "imdbID": "tt0903747",
                      "Type": "series", "Poster": "N/A", "Runtime": "49 min", "Country": "United States"},
               {"i": "tt0903747"})
        .route(OMDB, {"Response": "True", "Title": "Pilot", "Year": "2008", "imdbID": "tt0959621",
                      "Type": "episode", "Runtime": "58 min", "Season": "1", "Episode": "1", "seriesID": "tt0903747"},
               {"i": "tt0959621"})
        .route(OMDB, {"Response": "False", "Error": "Incorrect IMDb ID."}, {"i": "tt0000009"})
    )
    o = omdb(net)
    m = o.by_id(Query("movie", "", imdb_id="tt1832382"))[0]
    assert (m.kind, m.year, m.runtime_min, m.people) == ("movie", 2011, 123.0, "Asghar Farhadi")
    s = o.by_id(Query("episode", "", imdb_id="tt0903747"))[0]
    assert (s.kind, s.year, s.runtime_min) == ("series", 2008, 49.0)
    e = o.by_id(Query("episode", "", imdb_id="tt0959621"))[0]
    assert e.ids == {"imdb": "tt0903747"} and e.title == "Breaking Bad"
    assert (e.season, e.episode, e.episode_title, e.episode_runtime_min, e.episode_exists) == (1, 1, "Pilot", 58.0, True)
    assert o.by_id(Query("movie", "", imdb_id="tt0000009")) == []
    assert o.by_id(Query("movie", "")) == []


# ------------------------------------------------------------------ OpenSubtitles

OS_URL = "https://api.opensubtitles.com/api/v1/subtitles"
OS_KEY = "os-key-123"
HASH = "8e245d9679d31e12"


def sub(match: bool | None, **details: Any) -> dict:
    return {"id": "1", "type": "subtitle", "attributes": {
        "language": "en", "moviehash_match": match, "feature_details": details}}


def opensubs(net: FakeNet, **cfg: Any) -> OpenSubtitlesHash:
    return OpenSubtitlesHash({"enabled": True, "api_key": OS_KEY, "user_agent": "Kinoshelf v0.1.0", **cfg}, net)


def test_opensubtitles_enabled_needs_key() -> None:
    assert opensubs(FakeNet()).enabled()
    assert not opensubs(FakeNet(), api_key="").enabled()
    assert not opensubs(FakeNet(), enabled=False).enabled()


def test_opensubtitles_request_shape() -> None:
    net = FakeNet().route(OS_URL, {"data": []})
    assert opensubs(net).lookup(HASH.upper()) == []
    call = net.calls[0]
    assert call["params"] == {"moviehash": HASH}
    assert call["headers"]["Api-Key"] == OS_KEY
    assert call["headers"]["User-Agent"] == "Kinoshelf v0.1.0"
    assert call["headers"]["Accept"] == "application/json"


def test_opensubtitles_keeps_only_hash_matches_and_formats_imdb_ids() -> None:
    body = {"total_count": 4, "data": [
        sub(True, feature_type="Movie", title="A Separation", movie_name="A Separation", year=2011,
            imdb_id=1832382, tmdb_id=44115),
        sub(False, feature_type="Movie", title="Some Other Film", year=2001, imdb_id=42),
        sub(None, feature_type="Movie", title="Name-only match", year=2001, imdb_id=43),
        sub(True, feature_type="Movie", title="Tiny Id", year=1950, imdb_id=42, tmdb_id=0),
    ]}
    hits = opensubs(FakeNet().route(OS_URL, body)).lookup(HASH)
    assert [h.title for h in hits] == ["A Separation", "Tiny Id"]
    a, b = hits
    assert (a.kind, a.year, a.imdb_id, a.tmdb_id) == ("movie", 2011, "tt1832382", "44115")
    assert b.imdb_id == "tt0000042" and b.tmdb_id is None


def test_opensubtitles_episode_fields_and_dedupe() -> None:
    ep = dict(feature_type="Episode", title="Pilot", movie_name="Breaking Bad - S01E01 - Pilot", year=2008,
              imdb_id=959621, tmdb_id=62085, parent_imdb_id=903747, parent_tmdb_id=1396,
              parent_title="Breaking Bad", season_number=1, episode_number=1)
    body = {"data": [sub(True, **ep), sub(True, **ep), sub(True, feature_type="Tvshow", title="Breaking Bad", imdb_id=903747)]}
    hits = opensubs(FakeNet().route(OS_URL, body)).lookup(HASH)
    assert len(hits) == 1
    h = hits[0]
    assert (h.kind, h.title, h.year) == ("episode", "Pilot", 2008)
    assert (h.imdb_id, h.tmdb_id) == ("tt0959621", "62085")
    assert (h.parent_imdb_id, h.parent_tmdb_id, h.parent_title) == ("tt0903747", "1396", "Breaking Bad")
    assert (h.season, h.episode) == (1, 1)


def test_opensubtitles_no_match_empty_and_bad_hash_makes_no_call() -> None:
    net = FakeNet().route(OS_URL, {"total_count": 0, "data": []})
    o = opensubs(net)
    assert o.lookup(HASH) == []
    n = len(net.calls)
    assert o.lookup("") == [] and o.lookup("xyz") == []
    assert len(net.calls) == n


def test_opensubtitles_rate_limit_and_errors_raise() -> None:
    for status in (429, 401, 500):
        with pytest.raises(LookupError_) as e:
            opensubs(FakeNet().route(OS_URL, err(status))).lookup(HASH)
        assert OS_KEY not in str(e.value)


# ------------------------------------------------------------------------ Jellyfin

JF = "http://jelly:8096"
ITEMS = {"TotalRecordCount": 4, "StartIndex": 0, "Items": [
    {"Name": "A Separation", "Type": "Movie", "ProductionYear": 2011, "Path": "/media/Movies/A Separation (2011)",
     "ProviderIds": {"Imdb": "tt1832382", "Tmdb": "44115"}},
    {"Name": "Breaking Bad", "Type": "Series", "ProductionYear": 2008, "Path": "/media/Series/Breaking Bad (2008)",
     "ProviderIds": {"imdb": "tt0903747", "TvMaze": "169", "Tvdb": "81189"}},
    {"Name": "Tmdb Clash", "Type": "Series", "ProductionYear": 2001, "Path": "/media/Series/Clash",
     "ProviderIds": {"Tmdb": "777", "Tvmaze": "45"}},
    {"Name": "No Path", "Type": "Movie", "ProviderIds": {"Imdb": "tt0000001"}},
]}


def jelly(net: FakeNet, clock: Any = None, **cfg: Any) -> JellyfinClient:
    full = {"enabled": True, "url": JF + "/", "api_key": "jf-key", "path_map": [], **cfg}
    return JellyfinClient(full, net, clock=clock) if clock else JellyfinClient(full, net)


def test_jellyfin_enabled_needs_url_and_key() -> None:
    assert jelly(FakeNet()).enabled()
    assert not jelly(FakeNet(), url="").enabled()
    assert not jelly(FakeNet(), api_key="").enabled()
    assert not jelly(FakeNet(), enabled=False).enabled()


def test_jellyfin_test_sends_token_header() -> None:
    net = FakeNet().route(f"{JF}/System/Info", {"ServerName": "Home", "Version": "10.10.3", "Id": "x"})
    assert jelly(net).test() == {"name": "Home", "version": "10.10.3"}
    call = net.calls[0]
    assert call["headers"]["Authorization"] == 'MediaBrowser Token="jf-key"'
    assert call["url"] == f"{JF}/System/Info"


def test_jellyfin_errors_raise() -> None:
    with pytest.raises(LookupError_):
        jelly(FakeNet().route(f"{JF}/System/Info", err(401))).test()
    with pytest.raises(LookupError_):
        jelly(FakeNet().route(f"{JF}/System/Info", None)).test()
    with pytest.raises(LookupError_):
        jelly(FakeNet().route(f"{JF}/Items", err(500))).library_index()


def test_jellyfin_library_index_maps_ids_case_insensitively() -> None:
    net = FakeNet().route(f"{JF}/Items", ITEMS)
    idx = jelly(net).library_index()
    p = net.calls[0]["params"]
    assert (p["Recursive"], p["IncludeItemTypes"], p["EnableImages"]) == ("true", "Movie,Series", "false")
    assert p["Fields"] == "ProviderIds,Path,ProductionYear"
    assert idx["imdb:tt1832382"] == {
        "name": "A Separation", "year": 2011, "path": "/media/Movies/A Separation (2011)", "type": "Movie"}
    assert idx["tmdb:44115"]["name"] == "A Separation"
    assert idx["imdb:tt0903747"]["type"] == "Series" and idx["tvmaze:169"]["name"] == "Breaking Bad"
    assert "tvdb:81189" not in idx and "imdb:tt0000001" not in idx  # unused providers, items without a path


def test_jellyfin_library_index_pages() -> None:
    def serve(params: dict) -> dict:
        start, limit = int(params["StartIndex"]), int(params["Limit"])
        return {"TotalRecordCount": 5, "Items": [
            {"Name": f"M{i}", "Type": "Movie", "Path": f"/m/{i}", "ProviderIds": {"Imdb": f"tt{i:07d}"}}
            for i in range(start, min(start + limit, 5))]}

    net = FakeNet().route(f"{JF}/Items", serve)
    client = jelly(net)
    client.PAGE = 2
    idx = client.library_index()
    assert len(idx) == 5 and len(net.calls) == 3


def test_jellyfin_index_is_cached_for_six_hours() -> None:
    now = [1000.0]
    net = FakeNet().route(f"{JF}/Items", ITEMS)
    client = jelly(net, clock=lambda: now[0])
    client.library_index()
    now[0] += 5 * 3600
    client.library_index()
    assert len(net.calls) == 1
    now[0] += 2 * 3600
    client.library_index()
    assert len(net.calls) == 2
    client.library_index(force=True)
    assert len(net.calls) == 3


def test_jellyfin_find_by_any_id() -> None:
    client = jelly(FakeNet().route(f"{JF}/Items", ITEMS))
    assert client.find(Candidate(kind="movie", title="x", ids={"imdb": "tt1832382"})) == "/media/Movies/A Separation (2011)"
    assert client.find(Candidate(kind="movie", title="x", ids={"imdb": "tt9", "tmdb": "44115"})) == "/media/Movies/A Separation (2011)"
    assert client.find(Candidate(kind="series", title="x", ids={"tvmaze": "169"})) == "/media/Series/Breaking Bad (2008)"
    assert client.find(Candidate(kind="series", title="x", ids={"imdb": "TT0903747"})) == "/media/Series/Breaking Bad (2008)"
    assert client.find(Candidate(kind="movie", title="x", ids={"imdb": "tt7777777"})) is None
    assert client.find(Candidate(kind="movie", title="x")) is None
    # TMDB numbers are shared by films and shows: a film 777 is not the series 777
    assert client.find(Candidate(kind="movie", title="x", ids={"tmdb": "777"})) is None
    assert client.find(Candidate(kind="series", title="x", ids={"tmdb": "777"})) == "/media/Series/Clash"


def test_jellyfin_refresh_paths_applies_path_map() -> None:
    net = FakeNet().route(f"{JF}/Library/Media/Updated", None, method="POST")
    path_map = [
        {"from": "X:\\", "to": "/media/x"},
        {"from": "X:\\Movies", "to": "/data/films"},
        {"from": "/mnt/m", "to": "M:\\"},
    ]
    client = jelly(net, path_map=path_map)
    client.refresh_paths([
        "X:\\movies\\A (2011)\\A (2011).mkv",   # longest prefix wins, case-insensitive
        "X:\\Movies2\\B.mkv",                    # not under X:\Movies, falls to X:\
        "X:\\Series\\S\\s.mkv",
        "/mnt/m/Movies/C.mkv",                   # separators follow the "to" side
        "D:\\elsewhere\\z.mkv",                  # no rule: unchanged
        "X:\\Series\\S\\s.mkv",                  # duplicate
    ])
    call = net.calls[0]
    assert call["url"] == f"{JF}/Library/Media/Updated" and call["method"] == "POST"
    assert call["headers"]["Authorization"] == 'MediaBrowser Token="jf-key"'
    assert call["body"] == {"Updates": [{"Path": p, "UpdateType": "Created"} for p in [
        "/data/films/A (2011)/A (2011).mkv",
        "/media/x/Movies2/B.mkv",
        "/media/x/Series/S/s.mkv",
        "M:\\Movies\\C.mkv",
        "D:\\elsewhere\\z.mkv",
    ]]}


def test_jellyfin_refresh_paths_empty_makes_no_call_and_errors_raise() -> None:
    net = FakeNet()
    jelly(net).refresh_paths([])
    assert net.calls == []
    with pytest.raises(LookupError_):
        jelly(FakeNet().route(f"{JF}/Library/Media/Updated", err(401), method="POST")).refresh_paths(["/a.mkv"])


# --------------------------------------------------------------------------- IMDb

H_BASICS = ["tconst", "titleType", "primaryTitle", "originalTitle", "isAdult", "startYear", "endYear",
            "runtimeMinutes", "genres"]
H_AKAS = ["titleId", "ordering", "title", "region", "language", "types", "attributes", "isOriginalTitle"]
H_EPS = ["tconst", "parentTconst", "seasonNumber", "episodeNumber"]


def basics_row(tconst: str, ttype: str, primary: str, original: str | None = None, year: int | None = None,
               runtime: int | None = None) -> list[str]:
    return [tconst, ttype, primary, original or primary, "0", str(year) if year else "\\N", "\\N",
            str(runtime) if runtime else "\\N", "Drama"]


BASICS_ROWS = [
    basics_row("tt0000001", "short", "Carmencita", year=1894, runtime=1),
    basics_row("tt0069293", "movie", "Solaris", year=1972, runtime=167),
    basics_row("tt0307987", "movie", "Solaris", year=2002, runtime=99),
    basics_row("tt0903747", "tvSeries", "Breaking Bad", year=2008, runtime=45),
    basics_row("tt0959621", "tvEpisode", "Pilot", year=2008, runtime=58),
    basics_row("tt1832382", "movie", "A Separation", "Jodaeiye Nader az Simin", 2011, 123),
    basics_row("tt5555555", "videoGame", "Solaris Game", year=2010),
    basics_row("tt9999991", "tvMiniSeries", "Solaris", year=2020, runtime=50),
]
AKAS_ROWS = [
    ["tt0000001", "1", "Carmencita", "\\N", "\\N", "original", "\\N", "1"],
    ["tt1832382", "1", "A Separation", "US", "en", "imdbDisplay", "\\N", "0"],
    ["tt1832382", "2", "Jodaeiye Nader az Simin", "IR", "fa", "original", "\\N", "1"],
    ["tt1832382", "3", "Nader and Simin, A Separation", "XWW", "en", "\\N", "\\N", "0"],
    ["tt1832382", "4", "جدایی نادر از سیمین", "IR", "fa", "\\N", "\\N", "0"],
    ["tt1832382", "5", "A Separation", "GB", "en", "\\N", "\\N", "0"],
    ["tt0069293", "1", "Solyaris", "SUHH", "ru", "original", "\\N", "0"],
    ["tt9999991", "1", "Solaris: The Series", "US", "en", "\\N", "\\N", "0"],
]
EPS_ROWS = [
    ["tt0959621", "tt0903747", "1", "1"],
    ["tt0959622", "tt0903747", "1", "2"],
    ["tt0000777", "tt0000778", "1", "1"],   # parent is not a kept title
]
# episode titles come from the basics file
BASICS_EP_ROWS = BASICS_ROWS + [basics_row("tt0959622", "tvEpisode", "Cat's in the Bag...", year=2008, runtime=48)]


def write_gz(path: Path, header: list[str], rows: list[list[str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
        for r in [header, *rows]:
            f.write("\t".join(r) + "\n")


def make_imdb(tmp_path: Path, episodes: bool = False, basics: list[list[str]] | None = None, **cfg: Any) -> ImdbSource:
    d = tmp_path / "imdb"
    write_gz(d / "title.basics.tsv.gz", H_BASICS, basics or (BASICS_EP_ROWS if episodes else BASICS_ROWS))
    write_gz(d / "title.akas.tsv.gz", H_AKAS, AKAS_ROWS)
    if episodes:
        write_gz(d / "title.episode.tsv.gz", H_EPS, EPS_ROWS)
    config = {"enabled": True, "refresh_days": 30, "include_episodes": episodes, **cfg}
    return ImdbSource(config, FakeNet(), tmp_path)


@pytest.fixture
def imdb(tmp_path: Path) -> ImdbSource:
    src = make_imdb(tmp_path)
    src.update()
    return src


def test_imdb_is_offline_and_missing_index_gives_nothing(tmp_path: Path) -> None:
    src = ImdbSource({"enabled": True}, FakeNet(), tmp_path)
    assert src.online is False and src.key == "imdb" and src.name == "IMDb"
    assert src.search(Query("movie", "Solaris")) == []
    assert src.by_id(Query("movie", "", imdb_id="tt0069293")) == []
    assert src.status() == {
        "built_at": None, "titles": 0, "episodes": False,
        "files": {"title.basics.tsv.gz": False, "title.akas.tsv.gz": False, "title.episode.tsv.gz": False}}


def test_imdb_update_builds_from_fresh_files_without_downloading(tmp_path: Path) -> None:
    src = make_imdb(tmp_path)
    msg = src.update()
    assert "5 titles" in msg
    assert not [c for c in src.net.calls if c["method"] == "DOWNLOAD"]  # type: ignore[attr-defined]
    st = src.status()
    assert st["titles"] == 5 and st["built_at"] and st["episodes"] is False
    assert st["files"] == {"title.basics.tsv.gz": True, "title.akas.tsv.gz": True, "title.episode.tsv.gz": False}
    assert not list((tmp_path / "imdb").glob("*.build*"))


def test_imdb_second_update_is_a_no_op_until_a_file_changes(tmp_path: Path) -> None:
    src = make_imdb(tmp_path)
    src.update()
    built = src.status()["built_at"]
    assert "up to date" in src.update()
    assert src.status()["built_at"] == built
    # a file the user dropped in by hand later is picked up
    basics = tmp_path / "imdb" / "title.basics.tsv.gz"
    future = time.time() + 5
    os.utime(basics, (future, future))
    assert "built" in src.update()
    assert "built" in src.update(force=True)


def test_imdb_update_downloads_missing_files_via_part_file(tmp_path: Path) -> None:
    src = make_imdb(tmp_path)
    net: FakeNet = src.net  # type: ignore[assignment]
    akas = tmp_path / "imdb" / "title.akas.tsv.gz"
    net.files["https://datasets.imdbws.com/title.akas.tsv.gz"] = akas.read_bytes()
    akas.unlink()
    msg = src.update()
    downloads = [c for c in net.calls if c["method"] == "DOWNLOAD"]
    assert [c["url"] for c in downloads] == ["https://datasets.imdbws.com/title.akas.tsv.gz"]
    assert downloads[0]["dest"].name == "title.akas.tsv.gz.part"
    assert akas.exists() and not downloads[0]["dest"].exists()
    assert "downloaded title.akas.tsv.gz" in msg
    assert src.search(Query("movie", "A Separation"))


def test_imdb_update_refreshes_old_files_and_keeps_old_copy_when_offline(tmp_path: Path) -> None:
    src = make_imdb(tmp_path, refresh_days=7)
    d = tmp_path / "imdb"
    old = time.time() - 40 * 86400
    for f in d.iterdir():
        os.utime(f, (old, old))
    msg = src.update()  # the fake net refuses every download
    assert "could not refresh" in msg and "kept the older copy" in msg
    assert src.status()["titles"] == 5
    assert len([c for c in src.net.calls if c["method"] == "DOWNLOAD"]) == 2  # type: ignore[attr-defined]


def test_imdb_update_missing_file_that_cannot_be_downloaded_raises(tmp_path: Path) -> None:
    src = ImdbSource({"enabled": True, "refresh_days": 30}, FakeNet(), tmp_path)
    with pytest.raises(LookupError_) as e:
        src.update()
    assert "by hand" in str(e.value)
    assert not (tmp_path / "imdb" / "imdb.sqlite").exists()


def test_imdb_update_rejects_files_that_are_not_datasets(tmp_path: Path) -> None:
    src = make_imdb(tmp_path)
    (tmp_path / "imdb" / "title.akas.tsv.gz").write_bytes(b"<html>blocked</html>")
    with pytest.raises(LookupError_):
        src.update()
    assert not (tmp_path / "imdb" / "imdb.sqlite").exists()
    write_gz(tmp_path / "imdb" / "title.akas.tsv.gz", ["a", "b"], [["1", "2"]])
    with pytest.raises(LookupError_):
        src.update()


def test_imdb_failed_rebuild_keeps_the_old_index(tmp_path: Path) -> None:
    src = make_imdb(tmp_path)
    src.update()
    (tmp_path / "imdb" / "title.akas.tsv.gz").write_bytes(b"garbage")
    with pytest.raises(LookupError_):
        src.update(force=True)
    assert src.search(Query("movie", "A Separation"))


def test_imdb_index_content(imdb: ImdbSource) -> None:
    with sqlite3.connect(imdb.db_path) as conn:
        kinds = dict(conn.execute("SELECT tconst, kind FROM titles").fetchall())
        names = [r[0] for r in conn.execute("SELECT name FROM names WHERE tconst = 'tt1832382' ORDER BY id")]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert kinds == {"tt0069293": "movie", "tt0307987": "movie", "tt0903747": "series",
                     "tt1832382": "movie", "tt9999991": "series"}
    assert names == ["A Separation", "Jodaeiye Nader az Simin", "Nader and Simin, A Separation", "جدایی نادر از سیمین"]
    assert "names_fts" in tables


@pytest.mark.parametrize("title", ["Jodaeiye Nader az Simin", "A Separation", "jodaeiye nader az simin!"])
def test_imdb_search_finds_separation_by_original_or_english_title(imdb: ImdbSource, title: str) -> None:
    out = imdb.search(Query("movie", title))
    c = out[0]
    assert c.ids == {"imdb": "tt1832382"} and c.sources == ["IMDb"]
    assert (c.kind, c.title, c.year, c.runtime_min) == ("movie", "A Separation", 2011, 123.0)
    assert c.original_title == "Jodaeiye Nader az Simin"
    assert "Nader and Simin, A Separation" in c.alt_titles and "جدایی نادر از سیمین" in c.alt_titles
    assert "A Separation" not in c.alt_titles and "Jodaeiye Nader az Simin" not in c.alt_titles


def test_imdb_search_tolerates_a_misspelt_word(imdb: ImdbSource) -> None:
    out = imdb.search(Query("movie", "Jodaeie Nader az Simin", year=2011))
    assert out and out[0].ids["imdb"] == "tt1832382"


def test_imdb_search_year_picks_between_two_solaris_films(imdb: ImdbSource) -> None:
    both = imdb.search(Query("movie", "Solaris"))
    assert {c.ids["imdb"] for c in both} == {"tt0069293", "tt0307987"}
    assert imdb.search(Query("movie", "Solaris", year=1972))[0].ids["imdb"] == "tt0069293"
    assert imdb.search(Query("movie", "Solaris", year=2002))[0].ids["imdb"] == "tt0307987"
    assert imdb.search(Query("movie", "Solaris", year=2003))[0].ids["imdb"] == "tt0307987"  # one year off still counts
    old = imdb.search(Query("movie", "Solaris", year=1972))[0]
    assert old.alt_titles == ["Solyaris"] and old.runtime_min == 167.0


def test_imdb_search_filters_by_kind(imdb: ImdbSource) -> None:
    assert {c.ids["imdb"] for c in imdb.search(Query("movie", "Solaris"))} == {"tt0069293", "tt0307987"}
    series = imdb.search(Query("episode", "Solaris"))
    assert [(c.kind, c.ids["imdb"]) for c in series] == [("series", "tt9999991")]
    assert {c.ids["imdb"] for c in imdb.search(Query("unknown", "Solaris"))} == {"tt0069293", "tt0307987", "tt9999991"}
    assert imdb.search(Query("movie", "Breaking Bad")) == []
    bb = imdb.search(Query("episode", "Breaking Bad"))
    assert bb[0].kind == "series" and bb[0].runtime_min == 45.0 and bb[0].year == 2008


def test_imdb_search_ignores_other_title_types_and_unrelated_names(imdb: ImdbSource) -> None:
    assert imdb.search(Query("unknown", "Carmencita")) == []   # a short
    assert all(c.ids["imdb"] != "tt5555555" for c in imdb.search(Query("unknown", "Solaris Game")))
    assert imdb.search(Query("movie", "Zzyzx Qwerty Plugh")) == []
    assert imdb.search(Query("movie", "")) == []
    assert imdb.search(Query("movie", "!!!")) == []


def test_imdb_search_caps_results_at_eight(tmp_path: Path) -> None:
    rows = [basics_row(f"tt01000{i:02d}", "movie", "Echo", year=1990 + i, runtime=90) for i in range(12)]
    src = make_imdb(tmp_path, basics=rows)
    src.update()
    out = src.search(Query("movie", "Echo", year=1995))
    assert len(out) == 8 and out[0].year == 1995


def test_imdb_short_titles_use_exact_lookup(tmp_path: Path) -> None:
    src = make_imdb(tmp_path, basics=[basics_row("tt1049413", "movie", "Up", year=2009, runtime=96),
                                      basics_row("tt0000020", "movie", "Upside Down", year=2012)])
    src.update()
    out = src.search(Query("movie", "Up", year=2009))
    assert out[0].ids["imdb"] == "tt1049413"


def test_imdb_search_without_fts_falls_back_to_like(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(imdb_mod, "_fts_supported", lambda conn: False)
    src = make_imdb(tmp_path)
    src.update()
    with sqlite3.connect(src.db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert "names_fts" not in tables
    assert src.search(Query("movie", "Jodaeie Nader az Simin"))[0].ids["imdb"] == "tt1832382"
    assert src.search(Query("movie", "A Separation"))[0].ids["imdb"] == "tt1832382"
    assert {c.ids["imdb"] for c in src.search(Query("movie", "Solaris"))} == {"tt0069293", "tt0307987"}


def test_imdb_by_id(imdb: ImdbSource) -> None:
    out = imdb.by_id(Query("movie", "", imdb_id="tt1832382"))
    assert len(out) == 1 and out[0].title == "A Separation" and out[0].year == 2011
    assert "Nader and Simin, A Separation" in out[0].alt_titles
    assert imdb.by_id(Query("movie", "", imdb_id="TT1832382"))[0].ids["imdb"] == "tt1832382"
    assert imdb.by_id(Query("movie", "", imdb_id="tt0000009")) == []
    assert imdb.by_id(Query("movie", "", imdb_id="nonsense")) == []
    assert imdb.by_id(Query("movie", "")) == []


def test_imdb_details_without_episode_data_changes_nothing(imdb: ImdbSource) -> None:
    c = imdb.search(Query("episode", "Breaking Bad"))[0]
    imdb.details(c, Query("episode", "Breaking Bad", season=1, episode=1))
    assert c.episode_exists is None and c.episode_title is None
    assert imdb.status()["episodes"] is False


def test_imdb_episode_details(tmp_path: Path) -> None:
    src = make_imdb(tmp_path, episodes=True)
    msg = src.update()
    assert "with episodes" in msg and src.status()["episodes"] is True
    show = src.search(Query("episode", "Breaking Bad"))[0]

    src.details(show, Query("episode", "Breaking Bad", season=1, episode=1))
    assert (show.season, show.episode, show.episode_title, show.episode_runtime_min, show.episode_exists) == (
        1, 1, "Pilot", 58.0, True)

    other = src.search(Query("episode", "Breaking Bad"))[0]
    src.details(other, Query("episode", "Breaking Bad", season=1, episode=2))
    assert other.episode_title == "Cat's in the Bag..."

    missing = src.search(Query("episode", "Breaking Bad"))[0]
    src.details(missing, Query("episode", "Breaking Bad", season=1, episode=99))
    assert missing.episode_exists is False

    # a series IMDb lists no episodes for is not marked as lacking one
    solaris = src.search(Query("episode", "Solaris"))[0]
    src.details(solaris, Query("episode", "Solaris", season=1, episode=1))
    assert solaris.episode_exists is None

    # the film search is unaffected and an episode id resolves to its show
    assert src.search(Query("movie", "Solaris"))
    ep = src.by_id(Query("episode", "", imdb_id="tt0959621"))[0]
    assert ep.ids == {"imdb": "tt0903747"} and (ep.season, ep.episode, ep.episode_title) == (1, 1, "Pilot")


def test_imdb_turning_episodes_on_triggers_a_rebuild(tmp_path: Path) -> None:
    src = make_imdb(tmp_path, episodes=True, include_episodes=False)
    src.update()
    assert src.status()["episodes"] is False
    src.cfg["include_episodes"] = True
    assert "built" in src.update()
    assert src.status()["episodes"] is True
