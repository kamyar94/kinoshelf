"""The Inbox from arrival to _Ready, with generated videos and a table-driven identifier."""

from pathlib import Path

import pytest
from conftest import FakeIdentifier, make_video, needs_ffmpeg

from kinoshelf.pipeline import Engine

pytestmark = needs_ffmpeg

TABLE = {
    "interstellar": [{"kind": "movie", "title": "Interstellar", "year": 2014, "runtime_min": 0.1,
                      "ids": {"imdb": "tt0816692", "tmdb": "157336"}}],
    "solaris": [{"kind": "movie", "title": "Solaris", "year": 1972, "runtime_min": 0.1},
                {"kind": "movie", "title": "Solaris", "year": 2002, "runtime_min": 0.1}],
    "the office": [{"kind": "series", "title": "The Office", "year": 2005, "runtime_min": 0.1,
                    "ids": {"tmdb": "2316"}}],
}


@pytest.fixture
def eng(inst):
    s = inst["settings"]
    s.values["naming"] = {
        "movie": "X:\\Movies\\{Title} ({Year})\\{Title} ({Year}).{ext}",
        "episode": "X:\\Series\\{Series} ({Year})\\Season {SS}\\{Series} S{SS}E{EE} - {Episode}.{ext}",
    }
    s.values["rule"]["max_runtime_diff_pct"] = 5
    s.save()
    e = Engine(s, inst["db"], inst["data"], identifier=FakeIdentifier(s, TABLE))
    return e, inst


def settle(e):
    e.scan()
    return e.step()


def test_confident_film_reaches_ready_with_sidecar_and_nfo(eng):
    e, inst = eng
    folder = inst["inbox"] / "Interstellar.2014.1080p.BluRay.x264-YIFY"
    make_video(folder / "Interstellar.2014.1080p.BluRay.x264-YIFY.mkv", 6)
    (folder / "Interstellar.2014.1080p.BluRay.x264-YIFY.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
    (folder / "Subs").mkdir()
    (folder / "Subs" / "Persian.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nSalam\n")
    (folder / "RARBG.txt").write_text("advert")
    settle(e)
    it = e.db.items()[0]
    assert it["state"] == "ready", it.get("reason")
    ready = inst["inbox"] / "_Ready" / "X" / "Movies" / "Interstellar (2014)"
    assert (ready / "Interstellar (2014).mkv").exists()
    assert (ready / "Interstellar (2014).en.srt").exists()
    assert (ready / "Interstellar (2014).fa.srt").exists()
    assert (ready / "Interstellar (2014).nfo").read_text(encoding="utf-8").count("tt0816692") == 1
    # the emptied download folder and the advert were set aside, not deleted
    aside = inst["drive"] / "_Set aside"
    assert any(p.name == "RARBG.txt" for p in aside.rglob("*"))
    assert not folder.exists()
    assert (aside / "Empty folders" / "Inbox" / folder.name).exists()


def test_unsure_film_goes_to_review_then_approve(eng):
    e, inst = eng
    make_video(inst["inbox"] / "Solaris.DVDRip.XviD-[Iran-Film].mkv", 6)
    settle(e)
    it = e.db.items()[0]
    assert it["state"] == "review"
    assert "scores close" in it["reason"]
    assert (inst["inbox"] / "_Needs review" / "Solaris.DVDRip.XviD-[Iran-Film].mkv").exists()
    assert len(list((inst["data"] / "frames" / str(it["id"])).glob("*.jpg"))) == 4
    err = e.approve(it["id"], it["candidates"][1] if it["candidates"][1]["year"] == 1972 else it["candidates"][0])
    assert err is None
    e.step()
    it = e.db.item(it["id"])
    assert it["state"] == "ready" and it["dest"] == "X:\\Movies\\Solaris (1972)\\Solaris (1972).mkv"
    assert not (inst["inbox"] / "_Needs review").exists() or not any((inst["inbox"] / "_Needs review").iterdir())


def test_episode_group_approval(eng):
    e, inst = eng
    TABLE["the office"][0]["runtime_min"] = 50  # wrong length: every episode lands in review
    try:
        for n in (1, 2, 3):
            make_video(inst["inbox"] / "The.Office.S02.HDTV" / f"the.office.s02e0{n}.hdtv.mkv", 3)
        settle(e)
        items = e.db.items()
        assert {it["state"] for it in items} == {"review"}
        first = items[0]
        assert e.approve(first["id"], first["candidates"][0], group=True) is None
        states = {it["id"]: it["state"] for it in e.db.items()}
        assert set(states.values()) == {"working"}
        e.step()
        dests = sorted(it["dest"] for it in e.db.items())
        assert dests[0].endswith("Season 02\\The Office S02E01 - Episode 1.mkv")
        series = inst["inbox"] / "_Ready" / "X" / "Series" / "The Office (2005)"
        assert (series / "tvshow.nfo").exists()
        assert not list(series.glob("tvshow (*"))  # one shared file, extra copies set aside
    finally:
        TABLE["the office"][0]["runtime_min"] = 0.1


def test_loose_files_in_the_inbox_root_are_not_siblings(eng):
    e, inst = eng
    TABLE["the office"][0]["runtime_min"] = 50
    try:
        make_video(inst["inbox"] / "the.office.s02e01.mkv", 3)
        make_video(inst["inbox"] / "the.office.s02e02.mkv", 3)
        make_video(inst["inbox"] / "Pack" / "the.office.s02e03.mkv", 3)
        make_video(inst["inbox"] / "Pack" / "Solaris.mkv", 3)
        settle(e)
        by_name = {Path(it["src_rel"]).name: it for it in e.db.items()}
        assert e.siblings(by_name["the.office.s02e01.mkv"]) == []
        assert e.siblings(by_name["the.office.s02e03.mkv"]) == []  # Solaris is not the same show
    finally:
        TABLE["the office"][0]["runtime_min"] = 0.1


def test_junk_from_review_is_set_aside(eng):
    e, inst = eng
    make_video(inst["inbox"] / "Unknown.Thing.mkv", 3)
    settle(e)
    it = e.db.items()[0]
    assert it["state"] == "review" and "Nothing matched" in it["reason"]
    assert e.junk(it["id"]) is None
    assert e.db.item(it["id"])["state"] == "junk"
    assert list((inst["drive"] / "_Set aside" / "Junk").rglob("Unknown.Thing.mkv"))


def test_watching_off_touches_nothing(eng):
    e, inst = eng
    e.settings.values["watcher"]["enabled"] = False
    f = make_video(inst["inbox"] / "Interstellar.2014.mkv", 3)
    (inst["inbox"] / "RARBG.txt").write_text("advert")
    for _ in range(3):
        e.step()
    assert f.exists() and (inst["inbox"] / "RARBG.txt").exists()
    assert e.db.items() == []


def test_junk_can_stay_in_place(eng):
    e, inst = eng
    e.settings.values["junk"]["auto_set_aside"] = False
    folder = inst["inbox"] / "Interstellar.2014.1080p"
    make_video(folder / "Interstellar.2014.1080p.mkv", 6)
    (folder / "RARBG.txt").write_text("advert")
    settle(e)
    assert e.db.items()[0]["state"] == "ready"
    assert (folder / "RARBG.txt").exists()  # left for you; the emptied folder stays too
    assert not (inst["drive"] / "_Set aside").exists()


def test_file_still_copying_waits(eng):
    e, inst = eng
    e.settings.values["watcher"]["settle_seconds"] = 3600
    make_video(inst["inbox"] / "Interstellar.2014.mkv", 3)
    e.scan()
    e.step()
    assert e.db.items()[0]["state"] == "settling"


def test_lookups_down_means_retry_not_unknown(eng):
    e, inst = eng
    from kinoshelf.models import LookupError_

    def boom(q, only=None, details_top=6):
        raise LookupError_("TMDB could not be reached")

    e.identifier.identify = boom
    make_video(inst["inbox"] / "Interstellar.2014.mkv", 3)
    settle(e)
    it = e.db.items()[0]
    assert it["state"] == "queued" and it["retry_at"] and it["tries"] == 1
    assert Path(it["path"]).exists()
