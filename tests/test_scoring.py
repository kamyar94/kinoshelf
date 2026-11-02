from kinoshelf.config import DEFAULTS
from kinoshelf.models import Candidate, Query
from kinoshelf.scoring import decide, score_candidates

SC, RULE = DEFAULTS["scoring"], DEFAULTS["rule"]


def C(**kw):
    kw.setdefault("kind", "movie")
    return Candidate(**kw)


def test_running_time_separates_two_films_with_one_title():
    q = Query(kind="movie", title="Solaris", duration_min=166.8)
    cands = score_candidates([C(title="Solaris", year=2002, runtime_min=99), C(title="Solaris", year=1972, runtime_min=167)],
                             q, SC)
    assert cands[0].year == 1972 and cands[0].score >= 90
    assert cands[1].score < 60
    assert any("68 minutes shorter" in ck["text"] for ck in cands[1].checks)
    ok, why = decide(cands, q, RULE, "movie")
    assert ok and "share the title" in why


def test_original_title_finds_persian_film():
    q = Query(kind="movie", title="Jodaeiye Nader az Simin", year=2011, duration_min=123.2)
    cands = score_candidates([C(title="A Separation", original_title="Jodaeiye Nader az Simin", year=2011,
                                runtime_min=123)], q, SC)
    assert cands[0].score >= 95
    assert cands[0].checks[0]["text"] == "Original title matches"


def test_transliteration_variants_meet():
    q = Query(kind="movie", title="Jodaeie Nader az Simin", year=2011, duration_min=123)
    c = score_candidates([C(title="A Separation", alt_titles=["Jodaeiye Nader az Simin"], year=2011, runtime_min=123)],
                         q, SC)[0]
    assert c.score >= 90


def test_episode_length_picks_the_us_office():
    q = Query(kind="episode", title="The Office", season=2, episode=1, duration_min=22.0)
    us = C(kind="series", title="The Office", year=2005, runtime_min=22, episode_exists=True)
    uk = C(kind="series", title="The Office", year=2001, runtime_min=30, episode_exists=True)
    cands = score_candidates([uk, us], q, SC)
    assert cands[0].year == 2005
    ok, _ = decide(cands, q, RULE, "episode")
    assert ok


def test_ties_prefer_the_main_title_then_the_older_show():
    q = Query(kind="episode", title="Shameless", season=1, episode=1, duration_min=59)
    remake = C(kind="series", title="Бесстыдники", alt_titles=["Shameless"], year=2017, runtime_min=60,
               episode_exists=True)
    us = C(kind="series", title="Shameless", year=2011, runtime_min=60, episode_exists=True)
    uk = C(kind="series", title="Shameless", year=2004, runtime_min=60, episode_exists=True)
    cands = score_candidates([remake, us, uk], q, SC)
    assert [c.year for c in cands] == [2004, 2011, 2017]
    ok, why = decide(cands, q, RULE, "episode")
    assert not ok and "scores close" in why


def test_missing_episode_caps_the_score():
    q = Query(kind="episode", title="Up", season=9, episode=40, duration_min=22)
    c = score_candidates([C(kind="series", title="Up", runtime_min=22, episode_exists=False)], q, SC)[0]
    assert c.score <= 50


def test_hash_match_lifts_and_close_scores_block():
    q = Query(kind="movie", title="Little Women", duration_min=134.8)
    a = C(title="Little Women", year=2019, runtime_min=135)
    b = C(title="Little Women", year=1994, runtime_min=133)  # both lengths fit
    cands = score_candidates([a, b], q, SC)
    ok, why = decide(cands, q, RULE, "movie")
    assert not ok and "scores close" in why
    b.hash_match = True
    cands = score_candidates([a, b], q, SC, hash_checked=True)
    assert cands[0].year == 1994 or cands[0].score >= 90


def test_no_candidates_and_unknown_runtime():
    q = Query(kind="movie", title="Nothing", duration_min=90)
    assert decide([], q, RULE, "movie") == (False, "Nothing matched by name or by file hash")
    c = score_candidates([C(title="Nothing", year=2000)], q, SC)
    ok, why = decide(c, q, RULE, "movie")
    assert not ok and "Running time unknown" in why


def test_already_in_library_is_never_automatic():
    q = Query(kind="movie", title="Up", year=2009, duration_min=96)
    c = score_candidates([C(title="Up", year=2009, runtime_min=96, in_library="M:\\Movies\\Up (2009)")], q, SC)
    ok, why = decide(c, q, RULE, "movie")
    assert not ok and "Already in your library" in why
