"""Test connections: one request per source, without an API key, along the route Settings > Network
picks. Each answer is reached or blocked, with the time it took and a short reason.

Reasons are fixed phrases plus numbers (an HTTP status, seconds). Nothing from an error's own text is
shown or logged, so the proxy's address, username or password can never end up on the page.
"""

from __future__ import annotations

import ipaddress
import logging
import re
import socket
import ssl
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass

import httpx

from .net import SOURCE_NAMES, SOURCES, Net, host_of

log = logging.getLogger(__name__)

# Keyless requests: an answer from the real host over checked HTTPS (even 401) proves the route works.
CHECKS = {
    "tmdb": ("GET", "https://api.themoviedb.org/3/configuration"),
    "imdb": ("HEAD", "https://datasets.imdbws.com/title.basics.tsv.gz"),
    "tvmaze": ("GET", "https://api.tvmaze.com/shows/1"),
    "omdb": ("GET", "https://www.omdbapi.com/"),
    "opensubtitles": ("GET", "https://api.opensubtitles.com/api/v1/infos/formats"),
}

Resolver = Callable[..., list]


@dataclass
class Result:
    key: str
    name: str
    route: str  # "direct", "proxy" or "route"
    ok: bool | None  # None: not tested (no address set)
    ms: int | None
    reason: str

    @property
    def outcome(self) -> str:
        return "Not set up" if self.ok is None else ("Reached" if self.ok else "Blocked")

    @property
    def route_text(self) -> str:
        return {"proxy": "through the proxy", "route": "through the sidecar"}.get(self.route, "direct")

    @property
    def time_text(self) -> str:
        if self.ms is None:
            return ""
        return f"{self.ms} ms" if self.ms < 1000 else f"{self.ms / 1000:.1f} s"


def _chain(exc: BaseException) -> list[BaseException]:
    out, seen = [], set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen and len(out) < 12:
        seen.add(id(cur))
        out.append(cur)
        cur = cur.__cause__ or cur.__context__
    return out


def reason_for(exc: BaseException, via_proxy: bool, timeout: float) -> str:
    """A short fixed phrase for a failed request. The error's text is read, never repeated."""
    chain = _chain(exc)
    text = " ".join(str(e) for e in chain).lower()
    if any(isinstance(e, (httpx.TimeoutException, TimeoutError, socket.timeout)) for e in chain):
        return f"Timed out after {timeout:g} s"
    if isinstance(exc, httpx.ProxyError):
        code = re.search(r"\b([1-5]\d\d)\b", text)
        if code and code.group(1) == "407":
            return "The proxy did not accept the username or password (HTTP 407)"
        return f"The proxy could not reach it (HTTP {code.group(1)})" if code else "The proxy could not reach it"
    if any(isinstance(e, ConnectionResetError) for e in chain) or any(
            w in text for w in ("reset by peer", "connection reset", "forcibly closed", "10054", "errno 104")):
        return "Connection reset"
    if any(isinstance(e, ssl.SSLCertVerificationError) for e in chain) or "certificate verify failed" in text:
        return "The certificate did not check out (someone in between?)"
    if any(isinstance(e, ssl.SSLError) for e in chain) or "eof occurred" in text or "unexpected_eof" in text:
        return "Connection cut during the secure handshake"
    if any(isinstance(e, socket.gaierror) for e in chain) or "name or service not known" in text \
            or "getaddrinfo failed" in text or "nodename nor servname" in text or "name resolution" in text:
        return "The proxy is not answering (name not found)" if via_proxy else "DNS found no address"
    if any(isinstance(e, ConnectionRefusedError) for e in chain) or "refused" in text:
        return "The proxy is not answering (connection refused)" if via_proxy else "Connection refused"
    if via_proxy and isinstance(exc, httpx.ConnectError):
        return "The proxy is not answering"
    return "Could not connect"


def reason_for_status(code: int) -> tuple[bool, str]:
    """Any answer below 500 came from the real source over checked HTTPS: reached."""
    if code < 500:
        return True, f"HTTP {code}, no key sent" if code in (401, 403) else f"HTTP {code}"
    return False, f"HTTP {code}"


def _private(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return not a.is_global


def check(net: Net, key: str, method: str, url: str, *, lan: bool = False,
          resolver: Resolver = socket.getaddrinfo) -> Result:
    """One request. Straight out to an internet source, the name is looked up first: a filter that
    poisons DNS answers with a private address is caught before anything is sent there."""
    via = net.via_proxy(url)
    route = net.route if via else "direct"
    name = SOURCE_NAMES.get(key, key)
    t0 = time.monotonic()

    def done(ok: bool, reason: str) -> Result:
        return Result(key, name, route, ok, int((time.monotonic() - t0) * 1000), reason)

    if not via and not lan:
        host = host_of(url)
        try:
            answers = resolver(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return done(False, "DNS found no address")
        except OSError:
            return done(False, "DNS did not answer")
        if any(_private(str(a[4][0])) for a in answers):
            return done(False, "DNS answered a private address")
    try:
        r = net.client_for(url).request(method, url)
    except httpx.HTTPError as e:
        return done(False, reason_for(e, via, net.timeout))
    except (OSError, ssl.SSLError) as e:
        return done(False, reason_for(e, via, net.timeout))
    ok, reason = reason_for_status(r.status_code)
    return done(ok, reason)


def run(net: Net, settings, resolver: Resolver = socket.getaddrinfo) -> list[Result]:
    """Every source at once, in page order. Jellyfin only when its address is set."""
    jobs: dict[str, tuple[str, str, bool]] = dict((k, (m, u, False)) for k, (m, u) in CHECKS.items())
    jf = str(settings.get("sources.jellyfin.url", "") or "").strip().rstrip("/")
    if jf.startswith(("http://", "https://")):
        jobs["jellyfin"] = ("GET", jf + "/System/Info/Public", True)
    results: dict[str, Result] = {}
    limit = net.timeout * 2 + 5
    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="nettest")
    try:
        futs = {k: pool.submit(check, net, k, m, u, lan=lan, resolver=resolver) for k, (m, u, lan) in jobs.items()}
        wait(list(futs.values()), timeout=limit)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # a stuck DNS lookup must not hold the page
    for k, f in futs.items():
        if f.done() and not f.cancelled() and f.exception() is None:
            results[k] = f.result()
        else:
            url = jobs[k][1]
            results[k] = Result(k, SOURCE_NAMES[k], net.route if net.via_proxy(url) else "direct", False,
                                int(limit * 1000), f"Timed out after {net.timeout:g} s")
    out = []
    for k in SOURCES:
        if k in results:
            out.append(results[k])
        elif k == "jellyfin":
            out.append(Result(k, SOURCE_NAMES[k], "direct", None, None, "No server address set"))
    log.info("Test connections: %s", ", ".join(f"{r.name} {r.outcome.lower()}" for r in out))
    return out
