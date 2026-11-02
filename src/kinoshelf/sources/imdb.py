"""Offline IMDb lookup from the free non-commercial datasets (https://datasets.imdbws.com/).

The files live in data_dir/imdb. update() downloads what is missing or old, then builds one
SQLite file from them. Files placed in that folder by hand are used as they are.
"""

from __future__ import annotations

import csv
import gzip
import logging
import os
import re
import sqlite3
import threading
import time
import zlib
from collections.abc import Container, Iterable, Iterator
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz

from ..models import Candidate, LookupError_, Query
from ..net import Net
from ..textnorm import norm
from .base import Source

log = logging.getLogger(__name__)

BASE_URL = "https://datasets.imdbws.com/"
BASICS = "title.basics.tsv.gz"
AKAS = "title.akas.tsv.gz"
EPISODES = "title.episode.tsv.gz"

# titleType -> our kind
KINDS = {
    "movie": "movie",
    "tvMovie": "movie",
    "tvSpecial": "movie",
    "tvSeries": "series",
    "tvMiniSeries": "series",
}

POOL = 300  # names pulled from the index per search
MAX_RESULTS = 8
MAX_ALT = 12
MIN_RATIO = 55  # token_sort_ratio below this is not worth returning
BATCH = 20000
_TT = re.compile(r"^tt\d+$")
_STOP = {"the", "and", "for", "with", "der", "die", "das", "les", "des", "una", "uno", "del", "von"}

_SCHEMA = """
CREATE TABLE titles(
    tconst TEXT PRIMARY KEY, kind TEXT NOT NULL, primary_title TEXT, original_title TEXT,
    year INTEGER, runtime REAL
) WITHOUT ROWID;
CREATE TABLE names(
    id INTEGER PRIMARY KEY, tconst TEXT NOT NULL, name TEXT NOT NULL, norm TEXT NOT NULL,
    UNIQUE(tconst, norm)
);
CREATE TABLE episodes(
    tconst TEXT PRIMARY KEY, parent TEXT NOT NULL, season INTEGER, episode INTEGER,
    title TEXT, runtime REAL
) WITHOUT ROWID;
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
"""


def _fts_supported(conn: sqlite3.Connection) -> bool:
    """True when this SQLite build has FTS5 with the trigram tokenizer."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._probe USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE temp._probe")
        return True
    except sqlite3.Error:
        return False


def _int(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value else None
    except ValueError:
        return None


def _rows(path: Path, wanted: tuple[str, ...], only: tuple[str, Container[str]] | None = None) -> Iterator[tuple]:
    """Stream the wanted columns of a gzip TSV dataset, \\N turned into None.

    `only` is (column, allowed values): other rows are skipped before anything else is done with them.
    """
    try:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as f:
            reader = csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE)
            header = next(reader, None) or []
            col = {name: i for i, name in enumerate(header)}
            needed = list(wanted) + ([only[0]] if only else [])
            if any(n not in col for n in needed):
                raise LookupError_(f"IMDb: {path.name} does not look like an IMDb dataset file")
            idx = [col[w] for w in wanted]
            last = max(idx + ([col[only[0]]] if only else []))
            fi, allowed = (col[only[0]], only[1]) if only else (0, None)
            for row in reader:
                if len(row) <= last or (allowed is not None and row[fi] not in allowed):
                    continue
                yield tuple(None if row[i] == "\\N" else row[i] for i in idx)
    except (OSError, EOFError, zlib.error, UnicodeDecodeError, csv.Error) as e:
        raise LookupError_(f"IMDb: could not read {path.name}") from e


def _name_rows(tconst: str, names: Iterable[str | None]) -> Iterator[tuple[str, str, str]]:
    for n in names:
        nn = norm(n) if n else ""
        if nn:
            yield (tconst, n, nn)  # type: ignore[misc]


def _tokens(nq: str) -> list[str]:
    """Words worth searching for: three letters or more, common ones dropped, longest first."""
    words = list(dict.fromkeys(w for w in nq.split() if len(w) >= 3))
    rare = [w for w in words if w not in _STOP]
    return sorted(rare or words, key=len, reverse=True)[:4]


def _tnum(tconst: str) -> int:
    return int(tconst[2:]) if tconst[2:].isdigit() else 0


def _kinds(kind: str) -> tuple[str, ...]:
    return {"movie": ("movie",), "episode": ("series",)}.get(kind, ("movie", "series"))


def _chunks(items: list[str], size: int = 400) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


class ImdbSource(Source):
    key = "imdb"
    name = "IMDb"
    online = False

    def __init__(self, cfg: dict, net: Net, data_dir: Path):
        super().__init__(cfg, net, data_dir)
        self.dir = Path(data_dir) / "imdb"
        self.db_path = self.dir / "imdb.sqlite"
        self._lock = threading.Lock()

    # -- building the index -----------------------------------------------------------

    def update(self, force: bool = False) -> str:
        """Fetch missing or old dataset files, then rebuild the index when anything changed."""
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with_eps = bool(self.cfg.get("include_episodes"))
            wanted = [BASICS, AKAS] + ([EPISODES] if with_eps else [])
            try:
                days = float(self.cfg.get("refresh_days", 30))
            except (TypeError, ValueError):
                days = 30.0
            downloaded: list[str] = []
            kept_old: list[str] = []
            for fname in wanted:
                path = self.dir / fname
                if path.exists() and not force and (time.time() - path.stat().st_mtime) / 86400 <= days:
                    continue
                try:
                    self._download(fname)
                    downloaded.append(fname)
                except LookupError_ as e:
                    if not path.exists():
                        raise LookupError_(
                            f"IMDb: {fname} is missing and could not be downloaded ({e}). "
                            f"Put the file into {self.dir} by hand."
                        ) from e
                    kept_old.append(fname)
            if not (force or downloaded or self._needs_build(with_eps, wanted)):
                return f"IMDb index is up to date ({self.status()['titles']:,} titles)"
            count = self._build(with_eps)
            msg = f"IMDb index built: {count:,} titles" + (" with episodes" if with_eps else "")
            if downloaded:
                msg += f"; downloaded {', '.join(downloaded)}"
            if kept_old:
                msg += f"; could not refresh {', '.join(kept_old)}, kept the older copy"
            return msg

    def _download(self, fname: str) -> None:
        dest = self.dir / fname
        part = dest.with_name(fname + ".part")
        try:
            self.net.download(BASE_URL + fname, part)
            os.replace(part, dest)
        except OSError as e:
            raise LookupError_(f"IMDb: could not save {fname}") from e
        finally:
            part.unlink(missing_ok=True)

    def _needs_build(self, with_eps: bool, wanted: list[str]) -> bool:
        if not self.db_path.exists():
            return True
        newest = max((self.dir / f).stat().st_mtime for f in wanted)
        if self.db_path.stat().st_mtime < newest:
            return True
        conn = self._open()
        if conn is None:
            return True
        with closing(conn):
            return _meta(conn).get("episodes") != ("1" if with_eps else "0")

    def _build(self, with_eps: bool) -> int:
        tmp = self.db_path.with_name(self.db_path.name + ".build")
        for stale in (tmp, tmp.with_name(tmp.name + "-journal")):
            stale.unlink(missing_ok=True)
        conn = sqlite3.connect(tmp)
        try:
            count = self._fill_db(conn, with_eps)
            conn.commit()
            conn.close()
            try:
                os.replace(tmp, self.db_path)
            except OSError as e:
                raise LookupError_("IMDb: could not replace the index file, it may be in use") from e
            return count
        except sqlite3.Error as e:
            raise LookupError_("IMDb: could not build the index") from e
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            tmp.unlink(missing_ok=True)

    def _fill_db(self, conn: sqlite3.Connection, with_eps: bool) -> int:
        conn.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-65536;")
        conn.executescript(_SCHEMA)
        add_title = "INSERT OR IGNORE INTO titles VALUES (?,?,?,?,?,?)"
        add_name = "INSERT OR IGNORE INTO names(tconst, name, norm) VALUES (?,?,?)"

        # Pass 1: titles worth keeping, and their primary and original names
        kept: set[str] = set()
        titles: list[tuple] = []
        names: list[tuple] = []
        cols = ("tconst", "titleType", "primaryTitle", "originalTitle", "startYear", "runtimeMinutes")
        for tconst, ttype, primary, original, year, runtime in _rows(self.dir / BASICS, cols, ("titleType", KINDS)):
            kept.add(tconst)
            titles.append((tconst, KINDS[ttype], primary, original, _int(year), _float(runtime)))
            names.extend(_name_rows(tconst, (primary, original)))
            if len(titles) >= BATCH:
                conn.executemany(add_title, titles)
                conn.executemany(add_name, names)
                titles.clear()
                names.clear()
        conn.executemany(add_title, titles)
        conn.executemany(add_name, names)
        names.clear()

        # Pass 2: other names of those titles, one batch at a time
        for tconst, title in _rows(self.dir / AKAS, ("titleId", "title"), ("titleId", kept)):
            names.extend(_name_rows(tconst, (title,)))
            if len(names) >= BATCH:
                conn.executemany(add_name, names)
                names.clear()
        conn.executemany(add_name, names)
        names.clear()

        if with_eps:
            self._fill_episodes(conn, kept)
        kept.clear()

        conn.execute("CREATE INDEX idx_names_norm ON names(norm)")
        conn.execute("CREATE INDEX idx_episodes_parent ON episodes(parent, season, episode)")
        fts = _fts_supported(conn)
        if fts:
            conn.execute(
                "CREATE VIRTUAL TABLE names_fts USING fts5("
                "norm, content='names', content_rowid='id', tokenize='trigram')"
            )
            conn.execute("INSERT INTO names_fts(names_fts) VALUES('rebuild')")
        count = conn.execute("SELECT count(*) FROM titles").fetchone()[0]
        conn.executemany("INSERT INTO meta VALUES (?,?)", [
            ("built_at", datetime.now(timezone.utc).isoformat(timespec="seconds")),
            ("titles", str(count)),
            ("fts", "1" if fts else "0"),
            ("episodes", "1" if with_eps else "0"),
        ])
        return count

    def _fill_episodes(self, conn: sqlite3.Connection, kept: set[str]) -> None:
        batch: list[tuple] = []
        cols = ("tconst", "parentTconst", "seasonNumber", "episodeNumber")
        sql = "INSERT OR IGNORE INTO episodes(tconst, parent, season, episode) VALUES (?,?,?,?)"
        for tconst, parent, season, episode in _rows(self.dir / EPISODES, cols, ("parentTconst", kept)):
            batch.append((tconst, parent, _int(season), _int(episode)))
            if len(batch) >= BATCH:
                conn.executemany(sql, batch)
                batch.clear()
        conn.executemany(sql, batch)
        batch.clear()

        # Episode titles are in the basics file, as titleType tvEpisode
        sql = "UPDATE episodes SET title = ?, runtime = ? WHERE tconst = ?"
        cols = ("tconst", "primaryTitle", "runtimeMinutes")
        for tconst, title, runtime in _rows(self.dir / BASICS, cols, ("titleType", {"tvEpisode"})):
            batch.append((title, _float(runtime), tconst))
            if len(batch) >= BATCH:
                conn.executemany(sql, batch)
                batch.clear()
        conn.executemany(sql, batch)

    # -- state ------------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection | None:
        """A fresh connection to the index, or None when it is missing or unreadable."""
        if not self.db_path.exists():
            return None
        try:
            conn = sqlite3.connect(self.db_path)
            conn.execute("SELECT 1 FROM meta LIMIT 1")
            return conn
        except sqlite3.Error:
            log.warning("IMDb index at %s is unreadable; run the update again", self.db_path)
            return None

    def status(self) -> dict:
        """When the index was built, how many titles it holds and which dataset files are present."""
        out: dict = {
            "built_at": None,
            "titles": 0,
            "episodes": False,
            "files": {f: (self.dir / f).exists() for f in (BASICS, AKAS, EPISODES)},
        }
        conn = self._open()
        if conn is not None:
            with closing(conn):
                meta = _meta(conn)
                out.update(built_at=meta.get("built_at"), titles=int(meta.get("titles") or 0),
                           episodes=meta.get("episodes") == "1")
        return out

    # -- lookups ----------------------------------------------------------------------

    def search(self, q: Query) -> list[Candidate]:
        nq = norm(q.title)
        conn = self._open()
        if conn is None or not nq:
            return []
        with closing(conn):
            kinds = _kinds(q.kind)
            tconsts = self._pool(conn, nq, kinds)
            if not tconsts:
                return []
            rows: dict[str, tuple] = {}
            names: dict[str, list[tuple[str, str]]] = {}
            for chunk in _chunks(tconsts):
                ph = ",".join("?" * len(chunk))
                for r in conn.execute(
                    f"SELECT tconst, kind, primary_title, original_title, year, runtime FROM titles WHERE tconst IN ({ph})",
                    chunk,
                ):
                    rows[r[0]] = r
                for t, name, nn in conn.execute(
                    f"SELECT tconst, name, norm FROM names WHERE tconst IN ({ph}) ORDER BY id", chunk
                ):
                    names.setdefault(t, []).append((name, nn))
            scored: list[tuple[float, str]] = []
            for t, row in rows.items():
                best = max((fuzz.token_sort_ratio(nq, nn) for _, nn in names.get(t, [])), default=0)
                if best >= MIN_RATIO:
                    scored.append((best + _year_bonus(q.year, row[4]), t))
            scored.sort(key=lambda s: (-s[0], _tnum(s[1])))
            return [self._candidate(rows[t], names.get(t, []), nq) for _, t in scored[:MAX_RESULTS]]

    def _pool(self, conn: sqlite3.Connection, nq: str, kinds: tuple[str, ...]) -> list[str]:
        """Title ids whose names equal the query or share a word with it (at most ~POOL of each)."""
        kind_ph = ",".join("?" * len(kinds))
        found: dict[str, None] = {}
        for (t,) in conn.execute(
            f"SELECT n.tconst FROM names n JOIN titles t ON t.tconst = n.tconst "
            f"WHERE n.norm = ? AND t.kind IN ({kind_ph}) LIMIT {POOL}",
            (nq, *kinds),
        ):
            found[t] = None
        tokens = _tokens(nq)
        if tokens:
            if _meta(conn).get("fts") == "1":
                match = " OR ".join(f'"{w}"' for w in tokens)
                sql = (
                    f"SELECT n.tconst FROM names_fts JOIN names n ON n.id = names_fts.rowid "
                    f"JOIN titles t ON t.tconst = n.tconst "
                    f"WHERE names_fts MATCH ? AND t.kind IN ({kind_ph}) ORDER BY names_fts.rank LIMIT {POOL}"
                )
                params: tuple = (match, *kinds)
            else:
                like = " OR ".join("n.norm LIKE ?" for _ in tokens)
                sql = (
                    f"SELECT n.tconst FROM names n JOIN titles t ON t.tconst = n.tconst "
                    f"WHERE ({like}) AND t.kind IN ({kind_ph}) LIMIT {POOL}"
                )
                params = (*[f"%{w}%" for w in tokens], *kinds)
            for (t,) in conn.execute(sql, params):
                found[t] = None
        return list(found)

    def _candidate(self, row: tuple, names: list[tuple[str, str]], nq: str = "") -> Candidate:
        tconst, kind, primary, original, year, runtime = row
        others = [(name, nn) for name, nn in names if name not in (primary, original)]
        if nq:  # the names closest to the query first, so the one that matched survives the cut
            others.sort(key=lambda a: -fuzz.token_sort_ratio(nq, a[1]))
        return Candidate(
            kind=kind,
            title=primary or original or "",
            year=year,
            original_title=original,
            alt_titles=[name for name, _ in others[:MAX_ALT]],
            runtime_min=runtime,
            ids={"imdb": tconst},
            sources=[self.name],
        )

    def _load(self, conn: sqlite3.Connection, tconst: str) -> Candidate | None:
        row = conn.execute(
            "SELECT tconst, kind, primary_title, original_title, year, runtime FROM titles WHERE tconst = ?",
            (tconst,),
        ).fetchone()
        if row is None:
            return None
        names = conn.execute("SELECT name, norm FROM names WHERE tconst = ? ORDER BY id", (tconst,)).fetchall()
        return self._candidate(row, names)

    def by_id(self, q: Query) -> list[Candidate]:
        imdb = (q.imdb_id or "").strip().lower()
        conn = self._open() if _TT.match(imdb) else None
        if conn is None:
            return []
        with closing(conn):
            c = self._load(conn, imdb)
            if c:
                return [c]
            if _meta(conn).get("episodes") != "1":
                return []
            ep = conn.execute(
                "SELECT parent, season, episode, title, runtime FROM episodes WHERE tconst = ?", (imdb,)
            ).fetchone()
            show = self._load(conn, ep[0]) if ep else None
            if not ep or not show:
                return []
            show.season, show.episode, show.episode_title, show.episode_runtime_min = ep[1], ep[2], ep[3], ep[4]
            show.episode_exists = True
            return [show]

    def details(self, c: Candidate, q: Query) -> Candidate:
        imdb = c.ids.get("imdb")
        if not imdb or c.kind != "series" or q.season is None or q.episode is None:
            return c
        conn = self._open()
        if conn is None:
            return c
        with closing(conn):
            if _meta(conn).get("episodes") != "1":
                return c
            row = conn.execute(
                "SELECT title, runtime FROM episodes WHERE parent = ? AND season = ? AND episode = ?",
                (imdb, q.season, q.episode),
            ).fetchone()
            if row:
                c.season, c.episode = q.season, q.episode
                c.episode_title, c.episode_runtime_min, c.episode_exists = row[0], row[1], True
            elif conn.execute("SELECT 1 FROM episodes WHERE parent = ? LIMIT 1", (imdb,)).fetchone():
                # IMDb lists episodes for this show but not this one
                c.season, c.episode, c.episode_exists = q.season, q.episode, False
        return c


def _meta(conn: sqlite3.Connection) -> dict[str, str]:
    return dict(conn.execute("SELECT key, value FROM meta").fetchall())


def _year_bonus(want: int | None, have: int | None) -> float:
    if want is None or have is None:
        return 0.0
    gap = abs(want - have)
    return 5.0 if gap == 0 else 2.0 if gap == 1 else 0.0
