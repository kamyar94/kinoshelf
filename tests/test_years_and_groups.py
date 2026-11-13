"""Which year wins when sources disagree, quality from the picture, and directors' folders."""

from kinoshelf.identify import FIRST_RELEASE, merge, pick_year
from kinoshelf.models import Candidate, Query
from kinoshelf.scoring import score_candidates
from kinoshelf.sources.tmdb import _first_release_year


def C(source, year, **kw):
    return Candidate(kind="movie", title="Robot Overlords", year=year, sources=[source], ids={"imdb": "tt2145829"}, **kw)


def test_imdb_year_wins_by_default_and_the_choice_can_change():
    group = [C("TMDB", 2015), C("IMDb", 2014)]
    assert merge(group).year == 2014 and merge(group).years == {"TMDB": 2015, "IMDb": 2014}
    assert merge(group, "tmdb").year == 2015
    assert merge(group, "file", file_year=2015).year == 2015  # the file name's year, when a source gives it
    assert merge(group, "file", file_year=2013).year == 2014  # no source gives 2013: the IMDb rule


def test_without_imdb_the_first_release_stands_in_for_it():
    tmdb = C("TMDB", 2015, facts={"first_release_year": 2014})  # premiered 2014, released 2015
    assert merge([tmdb]).year == 2014 and merge([tmdb]).years == {"TMDB": 2015, FIRST_RELEASE: 2014}
    assert merge([tmdb], "tmdb").year == 2015
    same = C("TMDB", 2014, facts={"first_release_year": 2014})
    assert merge([same]).year == 2014 and merge([same]).years == {}  # no disagreement, nothing to say


def test_the_review_page_says_when_sources_disagree():
    c = merge([C("TMDB", 2015, runtime_min=90), C("IMDb", 2014)])
    q = Query(kind="movie", title="Robot Overlords", year=2014, duration_min=90)
    checks = [ck["text"] for ck in score_candidates([c], q, {})[0].checks]
    assert "Year matches" in checks
    assert "Sources disagree on the year (TMDB 2015, IMDb 2014); using 2014" in checks


def test_first_release_year_is_the_earliest_anywhere():
    d = {"release_dates": {"results": [
        {"iso_3166_1": "GB", "release_dates": [{"release_date": "2015-03-13T00:00:00.000Z", "type": 3}]},
        {"iso_3166_1": "CA", "release_dates": [{"release_date": "2014-09-07T00:00:00.000Z", "type": 1}]}]}}
    assert _first_release_year(d) == 2014 and _first_release_year({}) is None
    assert pick_year({"OMDb": 2014, "TMDB": 2015}) == 2014  # OMDb carries IMDb's data


# ---------------------------------------------------------------------- filing rules
def _engine(lib, rules):
    from test_filing import engine

    return engine(lib, rules)


def _film(**kw):
    from test_filing import film

    return film("Psycho", 1960, "Inbox/Psycho.1960.mkv", **kw)


RULE = {"name": "Films", "when": {"type": "film"}, "root": "X:\\Movies\\Films", "folder": "{Title} ({Year}){ [|Quality|]}",
        "file": "{Folder}"}


def test_quality_comes_from_the_picture_by_default():
    it, cand = _film(width=1920, height=800)
    it["src_rel"] = "Inbox/Psycho.1960.720p.mkv"  # the name says 720p, the picture is 1080p
    f = _engine([], {"rules": [RULE], "policies": {}}).resolve(it, cand)
    assert f.folder.endswith("Psycho (1960) [1080p]")
    f = _engine([], {"rules": [RULE], "policies": {"quality_from": "name"}}).resolve(it, cand)
    assert f.folder.endswith("Psycho (1960) [720p]")


def test_directors_folders_are_off_unless_a_rule_says_so():
    lib = ["Movies/Films/Alfred Hitchcock/Vertigo (1958)/Vertigo (1958).mkv"]
    RULE = {"name": "Films", "when": {"type": "film"}, "root": "X:\\Movies\\Films", "folder": "{Title} ({Year})",
            "file": "{Folder}"}
    it, cand = _film()
    cand["facts"] = dict(cand.get("facts") or {}, directors=["Alfred Hitchcock"])
    off = _engine(lib, {"rules": [RULE], "policies": {}}).resolve(it, cand)
    assert off.folder == "X:\\Movies\\Films\\Psycho (1960)" and off.group is None
    on = dict(RULE, directors={"existing": True})
    f = _engine(lib, {"rules": [on], "policies": {}}).resolve(it, cand)
    assert f.folder == "X:\\Movies\\Films\\Alfred Hitchcock\\Psycho (1960)" and f.group["new"] is False
    cand["facts"]["directors"] = ["Someone New"]
    assert _engine(lib, {"rules": [on], "policies": {}}).resolve(it, cand).group is None  # no folder, none made
    make = dict(RULE, directors={"existing": True, "create": True})
    f = _engine(lib, {"rules": [make], "policies": {}}).resolve(it, cand)
    assert f.folder == "X:\\Movies\\Films\\Someone New\\Psycho (1960)" and f.group["new"] is True
