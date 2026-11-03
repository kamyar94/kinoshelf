"""The weekly move from each Inbox's _Ready folder into the library, with a journal and an undo file."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

from .fsops import Exists, move, prune_empty_dirs, set_aside, set_aside_path, unique
from .hooks import payload, run_hook
from .models import LookupError_
from .paths import PathError
from .pipeline import READY, Engine, _title
from .scoring import runtime_diff_pct
from .models import Candidate, Query

log = logging.getLogger(__name__)
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def last_due(schedule: dict, now: datetime | None = None) -> datetime:
    """The most recent scheduled time at or before now (local time)."""
    now = now or datetime.now()
    day = DAYS.index(str(schedule.get("day", "sunday")).lower())
    hh, mm = (int(x) for x in str(schedule.get("time", "03:00")).split(":"))
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0) - timedelta(days=(now.weekday() - day) % 7)
    if cand > now:
        cand -= timedelta(days=7)
    return cand


def next_due(schedule: dict, now: datetime | None = None) -> datetime:
    return last_due(schedule, now) + timedelta(days=7)


def still_meets_rule(it: dict, rule: dict) -> tuple[bool, str]:
    """User decisions always move; automatic ones are checked against the rule in use today."""
    if it.get("decided_by") in ("user", "user-group"):
        return True, ""
    cands = it.get("candidates") or []
    if not cands:
        return False, "No match on record"
    best = Candidate.from_dict(cands[0])
    if best.score < float(rule.get("min_score", 85)):
        return False, f"Best match only {best.score}% under today's rule"
    dur = (it.get("probe") or {}).get("duration_s")
    diff = runtime_diff_pct(dur / 60 if dur else None, best)
    if dur and (diff is None or diff > float(rule.get("max_runtime_diff_pct", 5))):
        return False, "Running time outside today's rule"
    if best.in_library:
        return False, "Already in your library"
    return True, ""


class Mover:
    def __init__(self, engine: Engine):
        self.e = engine
        self.journal_dir = engine.data_dir / "journal"

    def run(self, dry_run: bool | None = None) -> dict:
        e = self.e
        dry = bool(e.settings.get("rule.dry_run", True)) if dry_run is None else dry_run
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S") + ("-dry" if dry else "")
        self.journal_dir.mkdir(parents=True, exist_ok=True)
        jpath, upath = self.journal_dir / f"{run_id}.jsonl", self.journal_dir / f"{run_id}.undo.json"
        e.db.run("INSERT INTO runs(id,started,dry_run,journal,undo) VALUES(?,?,?,?,?)",
                 (run_id, time.time(), int(dry), str(jpath), str(upath)))
        undo: list[dict] = []
        moved = problems = 0
        refresh: list[str] = []
        with e.lock, open(jpath, "a", encoding="utf-8") as journal:
            def log_op(op: dict) -> None:
                journal.write(json.dumps({"at": time.time(), **op}, ensure_ascii=False) + "\n")
                journal.flush()

            pm = e.pm()
            for it in e.db.items(["ready"]):
                ok, why = still_meets_rule(it, e.settings.get("rule", {}))
                if not ok:
                    problems += 1
                    log_op({"op": "skip", "item": it["id"], "why": why})
                    if not dry:
                        e.db.update_item(it["id"], state="review", reason=why)
                        e.db.event("review", _title(it), why, item_id=it["id"])
                    continue
                try:
                    root = pm.to_container(it["inbox"])
                    entry, _ = pm.relative_to_root(it["dest"])
                except PathError as err:
                    problems += 1
                    log_op({"op": "skip", "item": it["id"], "why": str(err)})
                    continue
                stage_root = root / READY / entry.label
                files = [Path(it["staged"])] + [Path(s) for s in it.get("sidecars") or []]
                plan = []
                for f in files:
                    try:
                        rel = f.relative_to(stage_root)
                    except ValueError:
                        continue
                    plan.append((f, Path(entry.container) / rel))
                main_src, main_dst = plan[0] if plan else (None, None)
                if main_dst is not None and main_dst.exists() and not it.get("replace_existing"):
                    problems += 1
                    why = f"A file already exists at {e._shown(main_dst)}"
                    log_op({"op": "skip", "item": it["id"], "why": why})
                    if not dry:
                        e.db.update_item(it["id"], state="review", reason=why)
                    continue
                if dry:
                    for src, dst in plan:
                        log_op({"op": "would_move", "item": it["id"], "from": str(src), "to": str(dst)})
                    e.db.event("would_move", _title(it), "Would move (dry run)", it["dest"], item_id=it["id"])
                    moved += 1
                    continue
                item_undo: list[dict] = []
                try:
                    if main_dst is not None and main_dst.exists():
                        aside = set_aside(main_dst, "replaced", pm, e.settings)
                        op = {"op": "set_aside", "item": it["id"], "from": str(main_dst), "to": str(aside)}
                        log_op(op)
                        item_undo.append(op)
                    for src, dst in plan:
                        if dst.exists():  # a shared extra (tvshow.nfo, poster) already in the library
                            aside = set_aside(src, "replaced", pm, e.settings)
                            log_op({"op": "set_aside", "item": it["id"], "from": str(src), "to": str(aside)})
                            continue
                        res = move(src, dst, aside_for_source=lambda s: set_aside_path(s, "moved", pm, e.settings))
                        op = {"op": "move", "item": it["id"], **res}
                        log_op(op)
                        item_undo.append(op)
                        prune_empty_dirs(src.parent, root / READY)
                except (OSError, Exists, PathError) as err:
                    problems += 1
                    log_op({"op": "error", "item": it["id"], "why": str(err)})
                    e.db.update_item(it["id"], reason=f"Move stopped: {err}")
                    undo.extend(item_undo)
                    continue
                undo.extend(item_undo)
                moved += 1
                e.db.update_item(it["id"], state="moved", moved_at=time.time(), moved_to=str(main_dst),
                                 run_id=run_id, path=str(main_dst))
                e.db.event("moved", _title(it), _what(it), it["dest"], item_id=it["id"])
                refresh.append(e._shown(main_dst.parent))
                self._after_move(it, main_dst)
        upath.write_text(json.dumps(list(reversed(undo)), ensure_ascii=False, indent=1), encoding="utf-8")
        if refresh and not dry:
            jf = e.identifier.sources.get("jellyfin")
            if jf is not None and jf.enabled():
                try:
                    jf.refresh_paths(sorted(set(refresh)))
                except LookupError_ as err:
                    log.warning("Jellyfin refresh failed: %s", err)
        summary = {"moved": moved, "problems": problems, "dry_run": dry}
        e.db.run("UPDATE runs SET finished=?, moved=?, problems=?, summary=? WHERE id=?",
                 (time.time(), moved, problems, json.dumps(summary), run_id))
        e.db.set_meta("last_run", run_id)
        return {"run_id": run_id, **summary, "journal": str(jpath), "undo": str(upath)}

    def _after_move(self, it: dict, main_dst: Path) -> None:
        for hook in self.e.settings.get("hooks.after_move", []) or []:
            if not hook.get("enabled"):
                continue
            it2 = dict(it, staged=str(main_dst))
            body = payload("after_move", it2, self.e.pm(), self.e.settings, hook.get("options", {}))
            res = run_hook(hook, body, self.e.pm(), self.e.net, self.e.data_dir)
            if res["status"] == "failed":
                log.warning("after_move hook %s: %s", hook.get("name"), res.get("message"))


def undo(engine: Engine, run_id: str) -> dict:
    """Put every file of a run back where it was. Copies made across drives go to the set-aside
    folder ("Undone"), and the originals come back from it. Nothing is deleted."""
    path = engine.data_dir / "journal" / f"{run_id}.undo.json"
    ops = json.loads(path.read_text(encoding="utf-8"))
    done, failed = 0, []
    with engine.lock:
        pm = engine.pm()
        for op in ops:
            try:
                if op["op"] == "set_aside":
                    os.replace(op["to"], op["from"]) if not Path(op["from"]).exists() else failed.append(op["from"])
                elif op["op"] == "move" and op.get("mode") == "rename":
                    Path(op["from"]).parent.mkdir(parents=True, exist_ok=True)
                    move(Path(op["to"]), Path(op["from"]))
                elif op["op"] == "move" and op.get("mode") == "copy":
                    set_aside(Path(op["to"]), "undone", pm, engine.settings)
                    Path(op["from"]).parent.mkdir(parents=True, exist_ok=True)
                    os.rename(op["source_set_aside"], op["from"])
                done += 1
            except (OSError, Exists, PathError) as err:
                failed.append(f"{op.get('to')}: {err}")
        for it in engine.db.items(["moved"]):
            if it["run_id"] == run_id:
                engine.db.update_item(it["id"], state="ready", path=it["staged"], moved_to=None, run_id=None)
        engine.db.run("UPDATE runs SET summary=json_set(coalesce(summary,'{}'),'$.undone',1) WHERE id=?", (run_id,))
        engine.db.event("undo", f"Run {run_id} undone", f"{done} steps put back, {len(failed)} failed")
    return {"done": done, "failed": failed}


def _what(it: dict, with_play: bool = True) -> str:
    """One line for the week page: how the file will play, and whether subtitles came with it (with_play=False
    leaves the playback step out, for a row that shows it in its own details)."""
    play = it.get("playable") or {}
    text = play.get("text") or ""
    if not with_play:
        bits = ""
    elif text.startswith(("Rewrapped", "Converted", "Re-encoded", "Kept the original", "Original put back")):
        bits = text
    else:
        bits = {"ok": "Plays as it is", "rewrap": "Needs a rewrap to play on the device",
                "convert": "Needs converting to play on the device", "dvd": "DVD folder"}.get(play.get("status") or "", "")
    subs = [Path(s).suffixes for s in it.get("sidecars") or [] if Path(s).suffix.lower() in (".srt", ".ass", ".sub")]
    if subs:
        bits += (" · " if bits else "") + "subtitles"
    return bits or ("Moved" if with_play else "")
