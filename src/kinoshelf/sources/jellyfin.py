"""Jellyfin client: connection test, a cached id index of the library, and path refreshes."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable
from typing import Any

from ..models import Candidate, LookupError_
from ..net import Net

CACHE_SECONDS = 6 * 3600
_DRIVE = re.compile(r"^[A-Za-z]:")


class JellyfinClient:
    PAGE = 500  # items per /Items request

    def __init__(self, cfg: dict, net: Net, clock: Callable[[], float] = time.monotonic):
        self.cfg = cfg or {}
        self.net = net
        self._clock = clock
        self._index: dict[str, dict] | None = None
        self._built = 0.0
        self._lock = threading.Lock()

    def enabled(self) -> bool:
        c = self.cfg
        return bool(c.get("enabled") and str(c.get("url") or "").strip() and str(c.get("api_key") or "").strip())

    # -- requests ---------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        key = str(self.cfg.get("api_key") or "").strip()
        return {"Authorization": f'MediaBrowser Token="{key}"', "Accept": "application/json"}

    def _url(self, path: str) -> str:
        base = str(self.cfg.get("url") or "").strip().rstrip("/")
        if not base:
            raise LookupError_("Jellyfin: no server address set")
        return base + path

    def _get(self, path: str, params: dict | None = None) -> Any:
        r = self.net.get_json(self._url(path), params, self._headers())
        if r is None:
            raise LookupError_(f"Jellyfin: {path} not found, check the server address")
        return r

    # -- public -----------------------------------------------------------------------

    def test(self) -> dict:
        """Server name and version, to prove the address and key work."""
        r = self._get("/System/Info")
        if not isinstance(r, dict):
            raise LookupError_("Jellyfin: unexpected answer")
        return {"name": r.get("ServerName"), "version": r.get("Version")}

    def library_index(self, force: bool = False) -> dict[str, dict]:
        """Map "imdb:tt...", "tmdb:123" and "tvmaze:45" to {"name", "year", "path", "type"}. Cached 6 hours."""
        with self._lock:
            fresh = self._index is not None and self._clock() - self._built < CACHE_SECONDS
            if fresh and not force:
                return self._index  # type: ignore[return-value]
            index: dict[str, dict] = {}
            start = 0
            while True:
                r = self._get("/Items", {
                    "Recursive": "true",
                    "IncludeItemTypes": "Movie,Series",
                    "Fields": "ProviderIds,Path,ProductionYear",
                    "EnableImages": "false",
                    "StartIndex": start,
                    "Limit": self.PAGE,
                })
                if not isinstance(r, dict):
                    raise LookupError_("Jellyfin: unexpected answer")
                items = r.get("Items")
                if not items:
                    break
                for item in items:
                    self._add(index, item)
                start += len(items)
                total = r.get("TotalRecordCount")
                if len(items) < self.PAGE or (isinstance(total, int) and start >= total):
                    break
            self._index, self._built = index, self._clock()
            return index

    @staticmethod
    def _add(index: dict[str, dict], item: Any) -> None:
        if not isinstance(item, dict) or not item.get("Path"):
            return
        entry = {
            "name": item.get("Name"),
            "year": item.get("ProductionYear"),
            "path": item["Path"],
            "type": item.get("Type"),
        }
        providers = item.get("ProviderIds")
        for raw_key, value in providers.items() if isinstance(providers, dict) else ():
            key = str(raw_key).lower()
            if key in ("imdb", "tmdb", "tvmaze") and value:
                index.setdefault(f"{key}:{str(value).strip().lower()}", entry)

    def find(self, c: Candidate) -> str | None:
        """The library path of a title Jellyfin already has, matched by any of its ids."""
        index = self.library_index()
        for key in ("imdb", "tmdb", "tvmaze"):
            value = c.ids.get(key)
            if not value:
                continue
            entry = index.get(f"{key}:{str(value).strip().lower()}")
            if not entry:
                continue
            # TMDB numbers are shared by films and shows, so the type has to agree
            if key == "tmdb" and entry.get("type") != ("Series" if c.kind == "series" else "Movie"):
                continue
            return entry["path"]
        return None

    def refresh_paths(self, paths: list[str]) -> None:
        """Tell Jellyfin about new files so it scans just those, not the whole library."""
        mapped = list(dict.fromkeys(self.map_path(p) for p in paths if p))
        if not mapped:
            return
        body = {"Updates": [{"Path": p, "UpdateType": "Created"} for p in mapped]}
        self.net.post_json(self._url("/Library/Media/Updated"), body, self._headers())

    def map_path(self, path: str) -> str:
        """Rewrite a path from our side to Jellyfin's side with the longest matching path_map prefix."""
        rules = [
            (str(r["from"]), str(r["to"]))
            for r in self.cfg.get("path_map") or []
            if isinstance(r, dict) and r.get("from") and r.get("to") is not None
        ]
        slashed = path.replace("\\", "/")
        for src, dst in sorted(rules, key=lambda r: -len(r[0])):
            prefix = src.replace("\\", "/").rstrip("/")
            if slashed[: len(prefix)].lower() != prefix.lower():
                continue
            rest = slashed[len(prefix):]
            if rest and not rest.startswith("/"):
                continue  # "X:/Movies2" is not under "X:/Movies"
            backslash = "\\" in dst or bool(_DRIVE.match(dst))
            base = dst.rstrip("/\\")
            if backslash:
                return base + (rest.replace("/", "\\") or ("\\" if base.endswith(":") else ""))
            return base + rest
        return path
