"""One HTTP client for every outbound call: short timeouts, optional proxy, clear failures."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from . import __version__
from .models import LookupError_

log = logging.getLogger(__name__)


class Net:
    def __init__(self, proxy: str | None = None, timeout: float = 15.0):
        self.proxy = proxy or None
        self.timeout = timeout
        self._client = httpx.Client(
            proxy=self.proxy,
            timeout=httpx.Timeout(timeout, connect=min(timeout, 8.0)),
            headers={"User-Agent": f"Kinoshelf/{__version__}"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        """GET and parse JSON. 404 returns None; any other failure raises LookupError_."""
        try:
            r = self._client.get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise LookupError_(f"{_host(url)}: {type(e).__name__}") from e
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise LookupError_(f"{_host(url)}: HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as e:
            raise LookupError_(f"{_host(url)}: answer is not JSON") from e

    def post_json(self, url: str, body: Any, headers: dict | None = None, timeout: float | None = None) -> Any:
        try:
            r = self._client.post(url, json=body, headers=headers, timeout=timeout or self.timeout)
        except httpx.HTTPError as e:
            raise LookupError_(f"{_host(url)}: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise LookupError_(f"{_host(url)}: HTTP {r.status_code}")
        if not r.content:
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def download(self, url: str, dest, chunk: int = 1 << 20) -> None:
        """Stream a file to disk (used for posters and the IMDb datasets)."""
        try:
            with self._client.stream("GET", url, timeout=httpx.Timeout(self.timeout, read=120.0)) as r:
                if r.status_code >= 400:
                    raise LookupError_(f"{_host(url)}: HTTP {r.status_code}")
                with open(dest, "wb") as f:
                    for part in r.iter_bytes(chunk):
                        f.write(part)
        except httpx.HTTPError as e:
            raise LookupError_(f"{_host(url)}: {type(e).__name__}") from e


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host
    except Exception:
        return url[:40]
