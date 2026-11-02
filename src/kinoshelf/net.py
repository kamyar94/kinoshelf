"""One HTTP client for every outbound call: short timeouts, a route per source, clear failures.

Settings > Network picks how lookups reach the internet: direct, through an HTTP or SOCKS5 proxy, or
through the optional route sidecar, and which sources take that route. The proxy's address, username
and password are secrets: they live only in the saved settings and in the client below. They are never
logged, never put in an error message and never passed to http or folder hooks.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import quote, unquote, urlsplit

import httpx

from . import __version__
from .models import LookupError_

log = logging.getLogger(__name__)

# httpx logs every request URL at INFO (TMDB and OMDb keys travel in the query string) and httpcore
# logs the proxy's host at DEBUG. Neither belongs in the app's log.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

# The sources, in page order, and the internet hosts each one uses (posters follow their source).
SOURCES = ["tmdb", "imdb", "tvmaze", "omdb", "opensubtitles", "jellyfin"]
SOURCE_NAMES = {"tmdb": "TMDB", "imdb": "IMDb datasets", "tvmaze": "TVmaze", "omdb": "OMDb",
                "opensubtitles": "OpenSubtitles", "jellyfin": "Jellyfin"}
DEFAULT_USE = {"tmdb": True, "imdb": True, "tvmaze": True, "omdb": True, "opensubtitles": True,
               "jellyfin": False}  # Jellyfin is normally on the home network
HOSTS = {
    "api.themoviedb.org": "tmdb", "image.tmdb.org": "tmdb",
    "datasets.imdbws.com": "imdb",
    "api.tvmaze.com": "tvmaze", "static.tvmaze.com": "tvmaze",
    "www.omdbapi.com": "omdb", "omdbapi.com": "omdb", "m.media-amazon.com": "omdb", "ia.media-imdb.com": "omdb",
    "api.opensubtitles.com": "opensubtitles",
}
KINDS = {"http": "http", "socks5": "socks5h"}  # socks5h: the proxy looks names up, not this computer
_ADDRESS = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?):(\d{1,5})$")


def check_address(addr: str) -> str:
    """host:port, or raise ValueError with a message that never repeats what was typed."""
    addr = addr.strip()
    m = _ADDRESS.match(addr)
    if not m or not 1 <= int(m.group(1)) <= 65535:
        raise ValueError("The address is a name or IP address and a port, like proxy.example.invalid:8080")
    return addr


def parse_proxy_url(url: str) -> dict:
    """Split a pasted proxy URL (http://user:pass@host:port, socks5://host:port) into the saved parts."""
    try:
        u = urlsplit(url.strip())
        port = u.port
        host = u.hostname
    except ValueError:
        raise ValueError("A proxy looks like http://host:port or socks5://host:port") from None
    scheme = (u.scheme or "").lower()
    if scheme in ("http", "https"):
        kind = "http"
    elif scheme in ("socks5", "socks5h"):
        kind = "socks5"
    else:
        raise ValueError("A proxy starts with http:// or socks5://")
    if not host:
        raise ValueError("A proxy looks like http://host:port or socks5://host:port")
    port = port or (1080 if kind == "socks5" else 80)
    host_part = f"[{host}]" if ":" in host else host
    return {"kind": kind, "address": check_address(f"{host_part}:{port}"),
            "username": unquote(u.username or ""), "password": unquote(u.password or "")}


def proxy_url(net_cfg: Mapping) -> str | None:
    """The proxy routed lookups use, built from the saved settings, or None when they go direct."""
    mode = net_cfg.get("mode", "direct")
    if mode == "route":
        return str(net_cfg.get("route_url") or "").strip() or None
    if mode != "proxy":
        return None
    p = net_cfg.get("proxy") or {}
    if not isinstance(p, Mapping):
        return None
    addr = str(p.get("address") or "").strip()
    if not addr:
        return None
    user, pw = str(p.get("username") or ""), str(p.get("password") or "")
    auth = ""
    if user or pw:
        auth = quote(user, safe="") + (":" + quote(pw, safe="") if pw else "") + "@"
    return f"{KINDS.get(p.get('kind'), 'http')}://{auth}{addr}"


def uses_from(cfg_use: Any) -> set[str]:
    """Source keys that take the route, from the saved toggles (missing ones keep their default)."""
    use = dict(DEFAULT_USE)
    if isinstance(cfg_use, Mapping):
        use.update({k: bool(v) for k, v in cfg_use.items() if k in DEFAULT_USE})
    elif isinstance(cfg_use, Iterable) and not isinstance(cfg_use, str):
        use = {k: k in set(cfg_use) for k in DEFAULT_USE}
    return {k for k, v in use.items() if v}


def host_of(url: str) -> str:
    try:
        return (httpx.URL(url).host or "").lower()
    except Exception:
        return ""


_LOCAL_SUFFIXES = (".localhost", ".local", ".lan", ".home", ".home.arpa", ".internal")


def is_local_host(host: str) -> bool:
    """A private, loopback or link-local address, localhost, a one-word name (a Docker service) or a
    home-network suffix. Such places are always reached directly: a proxy cannot reach your LAN."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host:
        return False
    try:
        return not ipaddress.ip_address(host.split("%")[0]).is_global
    except ValueError:
        pass
    return host == "localhost" or "." not in host or host.endswith(_LOCAL_SUFFIXES)


def proxy_from_env(url: str, environ: Mapping[str, str] | None = None) -> str | None:
    """For command hooks: the proxy to use for this URL, from KS_PROXY and KS_PROXY_USE."""
    env = os.environ if environ is None else environ
    proxy = env.get("KS_PROXY") or None
    if not proxy:
        return None
    use = {x.strip() for x in env.get("KS_PROXY_USE", "").split(",") if x.strip()}
    host = host_of(url)
    return proxy if HOSTS.get(host) in use and not is_local_host(host) else None


class Net:
    """Two clients: one direct, one through the proxy. Each request picks one by its host's source."""

    def __init__(self, proxy: str | None = None, timeout: float = 15.0, use: Any = None,
                 hosts: Mapping[str, str] | None = None, route: str = "proxy"):
        self.timeout = timeout
        self.use = uses_from(use)
        self.hosts = {**HOSTS, **{k.lower(): v for k, v in (hosts or {}).items()}}
        self.route = route  # "proxy" or "route": how the page names the proxied path
        self._proxy = proxy or None
        self._direct = self._client(None)
        self._proxied = self._client(self._proxy) if self._proxy else None

    @classmethod
    def from_settings(cls, settings) -> Net:
        cfg = settings.get("network", {}) or {}
        hosts = {}
        jf = host_of(str(settings.get("sources.jellyfin.url", "") or ""))
        if jf:
            hosts[jf] = "jellyfin"
        return cls(proxy_url(cfg), float(cfg.get("timeout_seconds", 15)), use=cfg.get("use"), hosts=hosts,
                   route="route" if cfg.get("mode") == "route" else "proxy")

    def _client(self, proxy: str | None) -> httpx.Client:
        # trust_env off: Settings > Network is the one place that decides; HTTP(S)_PROXY are ignored.
        return httpx.Client(
            proxy=proxy,
            trust_env=False,
            timeout=httpx.Timeout(self.timeout, connect=min(self.timeout, 8.0)),
            headers={"User-Agent": f"Kinoshelf/{__version__}"},
            follow_redirects=True,
        )

    def __repr__(self) -> str:  # never the proxy itself
        return f"<Net {'routed' if self._proxied else 'direct'} use={sorted(self.use)}>"

    @property
    def routed(self) -> bool:
        return self._proxied is not None

    def source_of(self, url: str) -> str | None:
        return self.hosts.get(host_of(url))

    def via_proxy(self, url: str) -> bool:
        """True when this URL's source takes the proxy. Local places never do (see is_local_host)."""
        if self._proxied is None or self.source_of(url) not in self.use:
            return False
        return not is_local_host(host_of(url))

    def client_for(self, url: str) -> httpx.Client:
        return self._proxied if self.via_proxy(url) and self._proxied is not None else self._direct

    def hook_env(self) -> dict[str, str]:
        """Environment for command hooks inside the container (never for http or folder hooks)."""
        if not self._proxy:
            return {}
        return {"KS_PROXY": self._proxy, "KS_PROXY_USE": ",".join(sorted(self.use))}

    def close(self) -> None:
        self._direct.close()
        if self._proxied is not None:
            self._proxied.close()

    def get_json(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        """GET and parse JSON. 404 returns None; any other failure raises LookupError_."""
        try:
            r = self.client_for(url).get(url, params=params, headers=headers)
        except httpx.HTTPError as e:
            raise LookupError_(f"{_host(url)}: {type(e).__name__}") from None
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
            r = self.client_for(url).post(url, json=body, headers=headers, timeout=timeout or self.timeout)
        except httpx.HTTPError as e:
            raise LookupError_(f"{_host(url)}: {type(e).__name__}") from None
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
            with self.client_for(url).stream("GET", url, timeout=httpx.Timeout(self.timeout, read=120.0)) as r:
                if r.status_code >= 400:
                    raise LookupError_(f"{_host(url)}: HTTP {r.status_code}")
                with open(dest, "wb") as f:
                    for part in r.iter_bytes(chunk):
                        f.write(part)
        except httpx.HTTPError as e:
            raise LookupError_(f"{_host(url)}: {type(e).__name__}") from None


def _host(url: str) -> str:
    try:
        return httpx.URL(url).host
    except Exception:
        return url[:40]
