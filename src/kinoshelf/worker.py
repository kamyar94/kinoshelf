"""Background loop: scan the Inbox, advance items, run the weekly move, keep the IMDb copy fresh."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

from .mover import Mover, last_due
from .pipeline import Engine

log = logging.getLogger(__name__)


class Worker(threading.Thread):
    def __init__(self, engine: Engine, mover: Mover):
        super().__init__(name="kinoshelf-worker", daemon=True)
        self.engine = engine
        self.mover = mover
        self.wake = threading.Event()
        self.stop_flag = threading.Event()
        self.last_error: str | None = None
        self._imdb_busy = False
        db = engine.db
        if db.get_meta("last_weekly_at") is None:
            # First start: do not run a move straight away for a time that passed before install.
            db.set_meta("last_weekly_at", str(time.time()))

    def run(self) -> None:
        while not self.stop_flag.is_set():
            try:
                self.engine.step()
                self.weekly()
                self.imdb_refresh()
                self.last_error = None
            except Exception as e:  # keep the loop alive; the page shows the last error
                log.exception("Worker pass failed")
                self.last_error = f"{type(e).__name__}: {e}"
            self.wake.wait(float(self.engine.settings.get("watcher.poll_seconds", 30)))
            self.wake.clear()

    def stop(self) -> None:
        self.stop_flag.set()
        self.wake.set()

    def weekly(self) -> dict | None:
        sched = self.engine.settings.get("schedule", {})
        if not sched.get("enabled", True):
            return None
        due = last_due(sched).timestamp()
        last = float(self.engine.db.get_meta("last_weekly_at", "0") or 0)
        if last >= due:
            return None
        self.engine.db.set_meta("last_weekly_at", str(time.time()))
        log.info("Weekly move due since %s", datetime.fromtimestamp(due))
        return self.mover.run()

    def imdb_refresh(self) -> None:
        cfg = self.engine.settings.get("sources.imdb", {})
        if not cfg.get("enabled") or self._imdb_busy:
            return
        last_try = float(self.engine.db.get_meta("imdb_attempt_at", "0") or 0)
        if time.time() - last_try < 86400:
            return
        src = next((s for s in self.engine.identifier.sources["titles"] if s.key == "imdb"), None)
        if src is None:
            return
        self.engine.db.set_meta("imdb_attempt_at", str(time.time()))

        def job():
            self._imdb_busy = True
            try:
                msg = src.update()
                log.info("IMDb copy: %s", msg)
            except Exception as e:
                log.warning("IMDb update failed: %s", e)
            finally:
                self._imdb_busy = False

        threading.Thread(target=job, name="imdb-update", daemon=True).start()
