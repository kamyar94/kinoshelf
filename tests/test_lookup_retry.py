"""Unreachable lookups: a file waits and is tried again instead of going to review as "Nothing matched";
and Review > Look up again."""

import re
from pathlib import Path

import pytest
from conftest import make_video, needs_ffmpeg

from kinoshelf.identify import Identifier
from kinoshelf.models import Candidate, LookupError_, Query
from kinoshelf.pipeline import PARTIAL_LOOKUP_TRIES, Engine
from kinoshelf.sources.base import Source


class FakeSource(Source):
    """mode: down (times out), empty (answers nothing) or found (answers with self.cands)."""

    def __init__(self, key, name, kinds=("movie", "series"), mode="empty", cands=None):
        super().__init__({"enabled": True}, None, Path("."))
        self.key, self.name, self.kinds, self.mode, self.cands = key, name, kinds, mode, cands or []
        self.asked = 0

    def search(self, q):
        self.asked += 1
        if self.mode == "down":
            raise LookupError_(f"{self.name}: ConnectTimeout")
        if q.kind == "movie" and "movie" not in self.kinds:
            return []  # like TVmaze for a film: nothing asked, nothing said
        return [Candidate.from_dict(dict(c, sources=[self.name])) for c in self.cands] if self.mode == "found" else []


class Off:
    def enabled(self):
        return False


def identifier(settings, *sources):
    ident = Identifier(settings, None, Path("."), sources={"titles": list(sources), "hash": Off(), "jellyfin": Off()})
    ident.fixed = True  # Engine keeps it across reloads
    return ident


FILM = Query(kind="movie", title="Robot Overlords", year=2014, duration_min=90)
EPISODE = Query(kind="episode", title="The Office", season=2, episode=1, duration_min=22)


def test_a_film_with_its_only_film_source_down_is_retried(inst):
    tmdb, tvmaze = FakeSource("tmdb", "TMDB", mode="down"), FakeSource("tvmaze", "TVmaze", kinds=("series",))
    with pytest.raises(LookupError_) as e:
        identifier(inst["settings"], tmdb, tvmaze).identify(FILM)
    assert not e.value.partial and "TMDB" in str(e.value)  # TVmaze's empty answer did not count for a film


def test_nothing_matched_while_another_source_was_down_is_partial(inst):
    tmdb, omdb = FakeSource("tmdb", "TMDB", mode="down"), FakeSource("omdb", "OMDb", mode="empty")
    with pytest.raises(LookupError_) as e:
        identifier(inst["settings"], tmdb, omdb).identify(FILM)
    assert e.value.partial


def test_asked_and_nothing_matched_is_an_answer(inst):
    tmdb, tvmaze = FakeSource("tmdb", "TMDB", mode="empty"), FakeSource("tvmaze", "TVmaze", kinds=("series",), mode="down")
    cands, notes = identifier(inst["settings"], tmdb, tvmaze).identify(FILM)
    assert cands == [] and any("TVmaze" in n for n in notes)  # TVmaze cannot know a film; TMDB said no


def test_an_episode_counts_the_series_source(inst):
    show = [{"kind": "series", "title": "The Office", "year": 2005, "runtime_min": 22}]
    tmdb, tvmaze = FakeSource("tmdb", "TMDB", mode="down"), FakeSource("tvmaze", "TVmaze", kinds=("series",))
    with pytest.raises(LookupError_) as e:
        identifier(inst["settings"], tmdb, tvmaze).identify(EPISODE)
    assert e.value.partial
    tvmaze.mode, tvmaze.cands = "found", show
    cands, notes = identifier(inst["settings"], tmdb, tvmaze).identify(EPISODE)
    assert cands[0].title == "The Office" and any("TMDB could not be reached" in n for n in notes)


# ---------------------------------------------------------------------- the Inbox
ROBOT = [{"kind": "movie", "title": "Robot Overlords", "year": 2014, "runtime_min": 0.1, "ids": {"tmdb": "1"}}]


@pytest.fixture
def setup(inst):
    s = inst["settings"]
    s.values["naming"]["movie"] = "X:\\Movies\\{Title} ({Year})\\{Title} ({Year}).{ext}"
    s.save()
    tmdb = FakeSource("tmdb", "TMDB", mode="down", cands=ROBOT)
    tvmaze = FakeSource("tvmaze", "TVmaze", kinds=("series",))
    e = Engine(s, inst["db"], inst["data"], identifier=identifier(s, tmdb, tvmaze))
    return e, inst, tmdb


def arrive(e, inst, name="Robot.Overlords.2014.1080p.YIFY.mkv"):
    f = make_video(inst["inbox"] / name, 3)
    e.scan()
    e.step()
    return f


@needs_ffmpeg
def test_source_down_keeps_the_file_in_the_inbox_and_retries(setup):
    e, inst, tmdb = setup
    f = arrive(e, inst)
    it = e.db.items()[0]
    assert it["state"] == "queued" and it["tries"] == 1 and it["retry_at"]
    assert it["reason"] == "Lookups could not be reached; trying again later"
    assert "ConnectTimeout" in it["notes"][0]
    assert f.exists() and not (inst["inbox"] / "_Needs review").exists()  # not moved
    tmdb.mode = "found"  # the route is steady again
    e.db.update_item(it["id"], retry_at=0)
    e.step()
    it = e.db.item(it["id"])
    assert it["state"] == "ready" and it["dest"].endswith("Robot Overlords (2014).mkv")


@needs_ffmpeg
def test_partial_failure_waits_then_goes_to_review_with_the_reason(setup):
    e, inst, tmdb = setup
    e.identifier.sources["titles"].append(FakeSource("omdb", "OMDb", mode="empty"))
    arrive(e, inst)
    it = e.db.items()[0]
    assert it["state"] == "queued" and it["tries"] == 1
    e.db.update_item(it["id"], tries=PARTIAL_LOOKUP_TRIES, retry_at=0)
    e.step()
    it = e.db.item(it["id"])
    assert it["state"] == "review"
    assert it["reason"].startswith("Nothing matched") and f"in {PARTIAL_LOOKUP_TRIES} tries" in it["reason"]


@needs_ffmpeg
def test_look_up_again_from_review(setup):
    e, inst, tmdb = setup
    tmdb.mode = "empty"  # asked, and nothing matched: a real answer
    arrive(e, inst)
    it = e.db.items()[0]
    assert it["state"] == "review" and it["reason"].startswith("Nothing matched")
    in_review = Path(it["path"])
    assert "_Needs review" in str(in_review) and in_review.exists()
    tmdb.mode = "found"
    assert e.lookup_again(it["id"]) is None
    it = e.db.item(it["id"])
    assert it["state"] == "queued" and it["candidates"] == [] and it["reason"] == "Looking it up again"
    e.step()
    it = e.db.item(it["id"])
    assert it["state"] == "ready" and not in_review.exists()  # moved on from _Needs review
    assert e.lookup_again(it["id"]) == "This item is no longer waiting for review"


@needs_ffmpeg
def test_look_up_again_route(setup, monkeypatch):
    from fastapi.testclient import TestClient

    from kinoshelf.web import App, create_app

    e, inst, tmdb = setup
    tmdb.mode = "empty"
    arrive(e, inst)
    item_id = e.db.items()[0]["id"]
    monkeypatch.delenv("KS_CF_ACCESS_TEAM", raising=False)
    monkeypatch.delenv("KS_CF_ACCESS_AUD", raising=False)
    core = App(ddir=inst["data"], start_worker=False, identifier=e.identifier)
    c = TestClient(create_app(core), base_url="http://inbox.test")
    c.post("/setup", data={"code": core.auth.setup_code, "username": "owner", "password": "correct horse 1"})
    c.post("/login", data={"username": "owner", "password": "correct horse 1"})
    page = c.get(f"/review?item={item_id}").text
    assert 'data-act="lookup"' in page
    csrf = re.search(r'name="csrf" content="([^"]+)"', page).group(1)
    r = c.post(f"/api/items/{item_id}/lookup", headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and "next" in r.json()
    assert core.db.item(item_id)["state"] == "queued"
    assert c.post(f"/api/items/{item_id}/lookup", headers={"X-CSRF-Token": csrf}).status_code == 400
