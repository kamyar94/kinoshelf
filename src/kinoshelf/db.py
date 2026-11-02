"""SQLite store (WAL). One connection guarded by a lock; the load is a few writes a minute."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    inbox TEXT NOT NULL,
    src_rel TEXT NOT NULL,
    path TEXT NOT NULL UNIQUE,
    is_dvd INTEGER DEFAULT 0,
    size INTEGER, mtime REAL, tail TEXT,
    seen_at REAL, settled_at REAL,
    state TEXT NOT NULL,
    parsed TEXT, probe TEXT, oshash TEXT,
    candidates TEXT, chosen INTEGER, chosen_cand TEXT,
    confident INTEGER, reason TEXT, notes TEXT,
    dest TEXT, staged TEXT, sidecars TEXT,
    hooks TEXT, playable TEXT,
    group_key TEXT,
    decided_by TEXT, decided_at REAL, replace_existing INTEGER DEFAULT 0,
    retry_at REAL, tries INTEGER DEFAULT 0,
    moved_at REAL, moved_to TEXT, run_id TEXT,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS items_state ON items(state);
CREATE INDEX IF NOT EXISTS items_group ON items(group_key);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY, at REAL NOT NULL, item_id INTEGER, kind TEXT NOT NULL,
    title TEXT, text TEXT, place TEXT
);
CREATE INDEX IF NOT EXISTS events_at ON events(at);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY, started REAL, finished REAL, dry_run INTEGER,
    moved INTEGER DEFAULT 0, problems INTEGER DEFAULT 0, journal TEXT, undo TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, pw_hash TEXT NOT NULL, created REAL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created REAL, expires REAL, csrf TEXT
);
CREATE TABLE IF NOT EXISTS login_fail (key TEXT PRIMARY KEY, count INTEGER, first REAL, locked_until REAL);
"""

JSON_FIELDS = {"parsed", "probe", "candidates", "chosen_cand", "notes", "sidecars", "hooks", "playable", "summary"}


class Db:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    def q(self, sql: str, args: tuple | list = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, args).fetchall()

    def one(self, sql: str, args: tuple | list = ()) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(sql, args).fetchone()

    def run(self, sql: str, args: tuple | list = ()) -> int:
        with self._lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid or cur.rowcount

    # meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        r = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return r["value"] if r else default

    def set_meta(self, key: str, value: str) -> None:
        self.run("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, value))

    # items
    def item(self, item_id: int) -> dict | None:
        r = self.one("SELECT * FROM items WHERE id=?", (item_id,))
        return decode(r) if r else None

    def item_by_path(self, path: str) -> dict | None:
        r = self.one("SELECT * FROM items WHERE path=?", (path,))
        return decode(r) if r else None

    def items(self, states: list[str] | None = None, order: str = "id") -> list[dict]:
        if states:
            marks = ",".join("?" * len(states))
            rows = self.q(f"SELECT * FROM items WHERE state IN ({marks}) ORDER BY {order}", states)
        else:
            rows = self.q(f"SELECT * FROM items ORDER BY {order}")
        return [decode(r) for r in rows]

    def add_item(self, **fields: Any) -> int:
        fields.setdefault("updated_at", time.time())
        cols = ",".join(fields)
        marks = ",".join("?" * len(fields))
        return self.run(f"INSERT INTO items({cols}) VALUES({marks})", [encode(k, v) for k, v in fields.items()])

    def update_item(self, item_id: int, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        sets = ",".join(f"{k}=?" for k in fields)
        self.run(f"UPDATE items SET {sets} WHERE id=?", [encode(k, v) for k, v in fields.items()] + [item_id])

    def event(self, kind: str, title: str, text: str = "", place: str = "", item_id: int | None = None) -> None:
        self.run("INSERT INTO events(at,item_id,kind,title,text,place) VALUES(?,?,?,?,?,?)",
                 (time.time(), item_id, kind, title, text, place))


def encode(key: str, v: Any) -> Any:
    if key in JSON_FIELDS and v is not None and not isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return v


def decode(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in JSON_FIELDS:
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                pass
    return d
