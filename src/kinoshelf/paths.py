"""Translate between paths as the user and media server see them ("shown") and container paths."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

_WIN = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def is_windows_style(p: str) -> bool:
    return bool(_WIN.match(p))


def pure(shown: str):
    return PureWindowsPath(shown) if is_windows_style(shown) else PurePosixPath(shown)


@dataclass
class MapEntry:
    shown: str
    container: str
    label: str

    @property
    def windows(self) -> bool:
        return is_windows_style(self.shown)


class PathError(ValueError):
    pass


class PathMap:
    def __init__(self, entries: list[dict]):
        self.entries: list[MapEntry] = []
        for e in entries or []:
            shown = str(e.get("shown", "")).strip()
            cont = str(e.get("container", "")).strip()
            if not shown or not cont:
                continue
            label = str(e.get("label") or "").strip() or _default_label(shown)
            self.entries.append(MapEntry(shown, cont, label))
        # longest prefix first
        self.entries.sort(key=lambda m: len(_key(m.shown, m.windows)), reverse=True)

    def entry_for_shown(self, shown: str) -> MapEntry:
        win = is_windows_style(shown)
        k = _key(shown, win)
        for m in self.entries:
            mk = _key(m.shown, m.windows)
            if m.windows == win and (k == mk or k.startswith(mk.rstrip("/") + "/")):
                return m
        raise PathError(f"{shown} is not inside any mapped folder")

    def entry_for_container(self, container: str | Path) -> MapEntry:
        c = _posix(container)
        best = None
        for m in self.entries:
            mc = _posix(m.container).rstrip("/")
            if c == mc or c.startswith(mc + "/"):
                if best is None or len(mc) > len(_posix(best.container)):
                    best = m
        if best is None:
            raise PathError(f"{container} is not inside any mapped folder")
        return best

    def to_container(self, shown: str) -> Path:
        m = self.entry_for_shown(shown)
        rel = _rel_parts(shown, m)
        return Path(m.container, *rel)

    def to_shown(self, container: str | Path) -> str:
        m = self.entry_for_container(container)
        rel = PurePosixPath(_posix(container)).relative_to(PurePosixPath(_posix(m.container))).parts
        return str(pure(m.shown).joinpath(*rel)) if rel else m.shown

    def relative_to_root(self, shown: str) -> tuple[MapEntry, tuple[str, ...]]:
        m = self.entry_for_shown(shown)
        return m, _rel_parts(shown, m)


def _posix(p: str | Path) -> str:
    return str(p).replace("\\", "/")


def _default_label(shown: str) -> str:
    if is_windows_style(shown) and shown[1:2] == ":":
        return shown[0].upper()
    name = pure(shown).name
    return name or "root"


def _key(p: str, windows: bool) -> str:
    s = p.replace("\\", "/")
    return s.lower() if windows else s


def _rel_parts(shown: str, m: MapEntry) -> tuple[str, ...]:
    sp = pure(shown)
    root = pure(m.shown)
    if m.windows:
        a = [x.lower() for x in sp.parts]
        b = [x.lower() for x in root.parts]
        if a[: len(b)] != b:
            raise PathError(f"{shown} is not inside {m.shown}")
        return tuple(sp.parts[len(b):])
    return tuple(sp.relative_to(root).parts)
