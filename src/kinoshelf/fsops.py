"""File moves that never delete: renames on one drive, verified copies across drives."""

from __future__ import annotations

import errno
import hashlib
import os
import shutil
from pathlib import Path

from .paths import PathMap


class Exists(FileExistsError):
    pass


def unique(path: Path) -> Path:
    """path, or "name (2).ext" and so on when it is taken."""
    if not path.exists():
        return path
    stem, suf = (path.stem, path.suffix) if path.is_file() or path.suffix else (path.name, "")
    n = 2
    while True:
        cand = path.with_name(f"{stem} ({n}){suf}")
        if not cand.exists():
            return cand
        n += 1


def same_device(a: Path, b: Path) -> bool:
    """True when a rename from a to b's folder works (both on one drive / mount)."""
    try:
        bb = b
        while not bb.exists():
            bb = bb.parent
        return os.stat(a).st_dev == os.stat(bb).st_dev
    except OSError:
        return False


def _sha256(path: Path, chunk: int = 4 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def copy_verified(src: Path, dst: Path, chunk: int = 4 << 20) -> str:
    """Copy src to dst, then read dst back and compare SHA-256. Returns the hash.

    A failed copy (our own half-written file) is removed; the source is never touched.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    part = dst.with_name(dst.name + ".ks-part")
    h = hashlib.sha256()
    try:
        with open(src, "rb") as fi, open(part, "wb") as fo:
            while True:
                b = fi.read(chunk)
                if not b:
                    break
                h.update(b)
                fo.write(b)
            fo.flush()
            os.fsync(fo.fileno())
        shutil.copystat(src, part)
        if os.path.getsize(part) != os.path.getsize(src) or _sha256(part) != h.hexdigest():
            raise OSError(f"copy of {src.name} did not verify")
        os.replace(part, dst)
    finally:
        if part.exists():
            part.unlink()
    return h.hexdigest()


def move(src: Path, dst: Path, aside_for_source=None) -> dict:
    """Move a file or folder. Same drive: rename. Other drive: verified copy, then the source goes to
    aside_for_source(src) (a set-aside path on its own drive). Raises Exists when dst is taken."""
    if dst.exists():
        raise Exists(str(dst))
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.rename(src, dst)
        return {"mode": "rename", "from": str(src), "to": str(dst)}
    except OSError as e:
        if e.errno != errno.EXDEV:
            raise
    if aside_for_source is None:
        raise OSError(f"{src} and {dst} are on different drives and no set-aside folder was given")
    if src.is_dir():
        for f in sorted(p for p in src.rglob("*") if p.is_file()):
            copy_verified(f, dst / f.relative_to(src))
    else:
        copy_verified(src, dst)
    aside = unique(aside_for_source(src))
    aside.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, aside)
    return {"mode": "copy", "from": str(src), "to": str(dst), "source_set_aside": str(aside)}


def set_aside_path(path: Path, category: str, pm: PathMap, settings) -> Path:
    """Where a file goes when it is set aside: <drive root>/<set-aside folder>/<category>/<path from root>."""
    entry = pm.entry_for_container(path)
    root = Path(entry.container)
    parts = settings.get("paths.set_aside_parts", {})
    folder = settings.get("paths.set_aside", "_Set aside")
    rel = path.relative_to(root) if str(path).startswith(str(root)) else Path(path.name)
    return root / Path(folder.replace("\\", "/")) / parts.get(category, category) / rel


def set_aside(path: Path, category: str, pm: PathMap, settings) -> Path:
    """Move a file or folder into the set-aside folder on its own drive. Returns where it went."""
    dst = unique(set_aside_path(path, category, pm, settings))
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(path, dst)
    return dst


def prune_empty_dirs(start: Path, stop: Path) -> None:
    """Remove empty folders from start up to (not including) stop. Only used inside our own
    staging folders (_Working, _Ready, _Needs review), never in the library."""
    cur = start
    while cur != stop and stop in cur.parents:
        try:
            cur.rmdir()
        except OSError:
            return
        cur = cur.parent
