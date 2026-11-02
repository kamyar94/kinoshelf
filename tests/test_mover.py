"""Weekly move: dry run, real run, conflicts, cross-drive copies, undo, and nothing ever deleted."""

import errno
import json
import os
from datetime import datetime
from pathlib import Path

import pytest
from conftest import FakeIdentifier, make_video, needs_ffmpeg

from kinoshelf import fsops
from kinoshelf.mover import Mover, last_due, undo
from kinoshelf.pipeline import Engine

pytestmark = needs_ffmpeg
TABLE = {"interstellar": [{"kind": "movie", "title": "Interstellar", "year": 2014, "runtime_min": 0.1}],
         "up": [{"kind": "movie", "title": "Up", "year": 2009, "runtime_min": 0.1}]}


@pytest.fixture
def ready(inst):
    s = inst["settings"]
    s.values["naming"]["movie"] = "X:\\Movies\\{Title} ({Year})\\{Title} ({Year}).{ext}"
    s.save()
    e = Engine(s, inst["db"], inst["data"], identifier=FakeIdentifier(s, TABLE))
    make_video(inst["inbox"] / "Interstellar.2014.mkv", 3)
    make_video(inst["inbox"] / "Up.2009.mkv", 3)
    e.scan()
    e.step()
    assert {it["state"] for it in e.db.items()} == {"ready"}
    return e, inst


def all_files(root: Path) -> set[str]:
    return {p.name for p in root.rglob("*") if p.is_file()}


def test_dry_run_moves_nothing(ready):
    e, inst = ready
    before = all_files(inst["drive"])
    res = Mover(e).run(dry_run=True)
    assert res["moved"] == 2 and res["dry_run"]
    assert all_files(inst["drive"]) == before
    lines = [json.loads(x) for x in Path(res["journal"]).read_text(encoding="utf-8").splitlines()]
    assert {x["op"] for x in lines} == {"would_move"}


def test_real_run_moves_and_undo_puts_back(ready):
    e, inst = ready
    names_before = all_files(inst["drive"])
    res = Mover(e).run(dry_run=False)
    assert res["moved"] == 2 and res["problems"] == 0
    lib = inst["drive"] / "Movies"
    assert (lib / "Interstellar (2014)" / "Interstellar (2014).mkv").exists()
    assert (lib / "Up (2009)" / "Up (2009).nfo").exists()
    assert {it["state"] for it in e.db.items()} == {"moved"}
    assert all_files(inst["drive"]) == names_before  # same files, new places
    out = undo(e, res["run_id"])
    assert out["failed"] == []
    assert not (lib / "Up (2009)" / "Up (2009).mkv").exists()
    assert (inst["inbox"] / "_Ready" / "X" / "Movies" / "Up (2009)" / "Up (2009).mkv").exists()
    assert {it["state"] for it in e.db.items()} == {"ready"}
    assert json.loads(e.db.one("SELECT summary FROM runs WHERE id=?", (res["run_id"],))["summary"])["undone"] == 1


def test_existing_file_is_not_overwritten(ready):
    e, inst = ready
    taken = inst["drive"] / "Movies" / "Up (2009)" / "Up (2009).mkv"
    taken.parent.mkdir(parents=True)
    taken.write_bytes(b"older copy")
    res = Mover(e).run(dry_run=False)
    assert res["moved"] == 1 and res["problems"] == 1
    assert taken.read_bytes() == b"older copy"
    up = next(it for it in e.db.items() if "Up" in it["dest"])
    assert up["state"] == "review" and "already exists" in up["reason"]


def test_cross_drive_copy_is_verified_and_source_set_aside(ready, monkeypatch):
    e, inst = ready
    real_rename = os.rename

    def fake_rename(src, dst):
        if "_Ready" in str(src) and "Movies" in str(dst) and "_Ready" not in str(dst):
            raise OSError(errno.EXDEV, "cross-device")
        return real_rename(src, dst)

    monkeypatch.setattr(fsops.os, "rename", fake_rename)
    res = Mover(e).run(dry_run=False)
    assert res["moved"] == 2
    lib_file = inst["drive"] / "Movies" / "Up (2009)" / "Up (2009).mkv"
    assert lib_file.exists()
    moved_aside = list((inst["drive"] / "_Set aside" / "Moved across drives").rglob("Up (2009).mkv"))
    assert moved_aside and moved_aside[0].read_bytes() == lib_file.read_bytes()
    ops = [json.loads(x) for x in Path(res["journal"]).read_text(encoding="utf-8").splitlines()]
    assert any(op.get("mode") == "copy" for op in ops)


def test_rule_change_holds_back_automatic_items(ready):
    e, inst = ready
    e.settings.values["rule"]["min_score"] = 101
    res = Mover(e).run(dry_run=False)
    assert res["moved"] == 0 and res["problems"] == 2
    assert {it["state"] for it in e.db.items()} == {"review"}


def test_last_due_finds_the_previous_slot():
    sched = {"day": "sunday", "time": "03:00"}
    assert last_due(sched, datetime(2026, 11, 4, 12, 0)) == datetime(2026, 11, 1, 3, 0)  # Wednesday
    assert last_due(sched, datetime(2026, 11, 1, 2, 59)) == datetime(2026, 10, 25, 3, 0)
    assert last_due(sched, datetime(2026, 11, 1, 3, 0)) == datetime(2026, 11, 1, 3, 0)
