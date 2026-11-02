"""Settings > Network: saving and loading, secrets that never come back, routes per source, the test
connections classification, and the move from the old single proxy setting.

Every address here is a placeholder under example.invalid or a local socket; nothing reaches the internet.
"""

from __future__ import annotations

import base64
import json
import logging
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
import yaml
from conftest import FakeIdentifier
from fastapi.testclient import TestClient
from test_web import signed_in

from kinoshelf import netcheck
from kinoshelf.__main__ import copy_settings_without_proxy
from kinoshelf.config import Settings
from kinoshelf.hooks import payload, run_command, run_folder
from kinoshelf.models import LookupError_
from kinoshelf.net import Net, parse_proxy_url, proxy_from_env, proxy_url
from kinoshelf.paths import PathMap
from kinoshelf.web import App, create_app

# Placeholder secrets. Each carries "zq7" so a leak of any part of one is easy to spot.
ADDR = "zq7-proxy.example.invalid:3128"
USER = "zq7user"
PASS = "zq7-pass-Word"
SECRETS = [ADDR, USER, PASS, "zq7"]


def no_secrets(text: str) -> bool:
    low = text.lower()
    return not any(s.lower() in low for s in SECRETS)


@pytest.fixture
def client(inst, monkeypatch):
    monkeypatch.delenv("KS_CF_ACCESS_TEAM", raising=False)
    monkeypatch.delenv("KS_CF_ACCESS_AUD", raising=False)
    core = App(ddir=inst["data"], start_worker=False, identifier=FakeIdentifier(inst["settings"], {}))
    c = TestClient(create_app(core), base_url="http://inbox.test")
    return c, core


def save_net(c, csrf, **body):
    return c.post("/api/settings/network", json=body, headers={"X-CSRF-Token": csrf})


# ---------------------------------------------------------------------- save and load

def test_page_works_with_no_proxy_at_all(client):
    c, core = client
    signed_in(c, core)
    html = c.get("/settings").text
    assert 'name="mode" value="direct" checked' in html
    assert 'data-set="yes"' not in html and "Test connections" in html
    assert Net.from_settings(core.settings).routed is False


def test_settings_save_and_load(client, inst):
    c, core = client
    csrf = signed_in(c, core)
    r = save_net(c, csrf, mode="proxy", kind="socks5", address=ADDR, username=USER, password=PASS,
                 use={"tmdb": True, "tvmaze": False, "jellyfin": False}, timeout_seconds=20)
    assert r.status_code == 200
    assert r.json()["set"] == {"address": True, "username": True, "password": True}
    again = Settings(inst["data"] / "config.yml")
    n = again.get("network")
    assert n["mode"] == "proxy" and n["timeout_seconds"] == 20
    assert n["proxy"] == {"kind": "socks5", "address": ADDR, "username": USER, "password": PASS}
    assert n["use"]["tmdb"] is True and n["use"]["tvmaze"] is False and n["use"]["omdb"] is True
    assert proxy_url(n) == f"socks5h://{USER}:{PASS}@{ADDR}"
    # Blank boxes keep what is saved; switching to direct keeps the proxy for later.
    save_net(c, csrf, mode="direct", kind="socks5", timeout_seconds=20)
    assert core.settings.get("network.proxy.password") == PASS
    assert Net.from_settings(core.settings).routed is False
    # Remove one secret.
    r = save_net(c, csrf, mode="proxy", kind="socks5", clear=["password"])
    assert r.json()["set"]["password"] is False and core.settings.get("network.proxy.password") == ""


def test_new_address_forgets_the_old_sign_in(client):
    c, core = client
    csrf = signed_in(c, core)
    save_net(c, csrf, mode="proxy", kind="http", address=ADDR, username=USER, password=PASS)
    r = save_net(c, csrf, mode="proxy", kind="http", address="other.example.invalid:8080")
    assert "forgotten" in r.json()["note"]
    assert core.settings.get("network.proxy.username") == "" and core.settings.get("network.proxy.password") == ""


def test_pasted_proxy_url_is_split(client):
    c, core = client
    csrf = signed_in(c, core)
    save_net(c, csrf, mode="proxy", kind="http", address=f"socks5://{USER}:{PASS}@{ADDR}")
    p = core.settings.get("network.proxy")
    assert p == {"kind": "socks5", "address": ADDR, "username": USER, "password": PASS}


def test_proxy_without_address_stays_direct(client):
    c, core = client
    csrf = signed_in(c, core)
    r = save_net(c, csrf, mode="proxy", kind="http")
    assert r.status_code == 200 and "direct until" in r.json()["note"]
    assert Net.from_settings(core.settings).routed is False


def test_route_mode_uses_the_sidecar(client):
    c, core = client
    csrf = signed_in(c, core)
    save_net(c, csrf, mode="route", kind="http", address=ADDR, password=PASS)
    net = Net.from_settings(core.settings)
    assert net.routed and net.route == "route"
    assert net.hook_env()["KS_PROXY"] == "http://route:10809"  # the proxy's own sign-in is not used


def test_bad_values_are_refused_without_repeating_them(client):
    c, core = client
    csrf = signed_in(c, core)
    for body in ({"mode": "proxy", "address": "zq7 not an address"},
                 {"mode": "proxy", "address": "ftp://zq7.example.invalid:21"},
                 {"mode": "proxy", "address": "zq7.example.invalid:99999"},
                 {"mode": "zq7"}, {"mode": "proxy", "kind": "zq7"}):
        r = save_net(c, csrf, **body)
        assert r.status_code == 400 and no_secrets(r.text), body


# ---------------------------------------------------------------------- secrets stay hidden

def test_secrets_are_never_rendered_back_or_logged(client, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    c, core = client
    csrf = signed_in(c, core)
    r = save_net(c, csrf, mode="proxy", kind="http", address=ADDR, username=USER, password=PASS)
    assert no_secrets(r.text)
    html = c.get("/settings").text
    assert no_secrets(html)
    assert html.count('data-set="yes"') == 3 and html.count(">set<") == 3
    # A test run whose errors carry the proxy URL in their text: the page and the log show neither.
    leaky = f"502 Bad Gateway from http://{USER}:{PASS}@{ADDR}"

    class Leaky:
        def request(self, method, url, **kw):
            raise httpx.ProxyError(leaky)

        def get(self, url, **kw):
            raise httpx.ProxyError(leaky)

    monkeypatch.setattr(Net, "client_for", lambda self, url: Leaky())
    out = c.post("/api/network/test", headers={"X-CSRF-Token": csrf}).text
    assert "The proxy could not reach it (HTTP 502)" in out and no_secrets(out)
    # Lookups that fail through the proxy name only the source's host.
    with pytest.raises(LookupError_) as e:
        core.engine.net.get_json("https://api.themoviedb.org/3/configuration")
    assert no_secrets(str(e.value)) and e.value.__cause__ is None
    assert no_secrets(caplog.text)
    assert no_secrets(repr(core.engine.net))


def test_real_requests_through_a_proxy_log_nothing_secret(caplog):
    """httpx logs request URLs at INFO and httpcore the proxy host at DEBUG; both are silenced."""
    caplog.set_level(logging.DEBUG)
    with FakeProxy() as fp:
        net = Net(f"http://{USER}:{PASS}@{fp.address}", timeout=3)
        with pytest.raises(LookupError_):
            net.get_json("https://api.tvmaze.com/shows/1", {"api_key": "zq7-key"})
        net.close()
    assert fp.seen and no_secrets(caplog.text)


def test_hook_jobs_and_backups_carry_no_proxy(client, inst, tmp_path):
    c, core = client
    csrf = signed_in(c, core)
    save_net(c, csrf, mode="proxy", kind="http", address=ADDR, username=USER, password=PASS)
    item = {"id": 3, "staged": "/x/a.mkv", "chosen_cand": {"kind": "movie", "title": "A"}, "parsed": {}}
    body = payload("icon", item, PathMap([]), core.settings, {})
    assert no_secrets(json.dumps(body)) and "proxy" not in body["settings"]
    pm = PathMap([{"shown": "X:\\", "container": str(tmp_path)}])
    run_folder({"kind": "folder", "folder": "X:\\jobs"}, body, pm)
    assert no_secrets((tmp_path / "jobs" / "3-icon.job.json").read_text(encoding="utf-8"))
    copy_settings_without_proxy(inst["data"] / "config.yml", tmp_path / "backup.yml")
    kept = (tmp_path / "backup.yml").read_text(encoding="utf-8")
    assert no_secrets(kept) and yaml.safe_load(kept)["network"]["mode"] == "proxy"


def test_command_hooks_get_the_route_in_their_environment(tmp_path):
    script = tmp_path / "hook.py"
    script.write_text(
        "import json,os,sys\njob=json.load(sys.stdin)\n"
        "ok=os.environ.get('KS_PROXY')=='http://route:10809' and os.environ.get('KS_PROXY_USE')=='tmdb'\n"
        "print(json.dumps({'status':'done' if ok else 'failed','message':'checked'}))\n")
    net = Net("http://route:10809", use={"tmdb": True, "imdb": False, "tvmaze": False, "omdb": False,
                                         "opensubtitles": False})
    body = {"protocol": 1, "stage": "icon", "item": {"id": 1}, "settings": {}, "options": {}}
    assert run_command({"command": [sys.executable, str(script)]}, body, tmp_path, net.hook_env())["status"] == "done"
    assert run_command({"command": [sys.executable, str(script)]}, body, tmp_path, None)["status"] == "failed"
    net.close()


# ---------------------------------------------------------------------- routes per source

def test_each_source_takes_its_own_route():
    net = Net("http://route:10809", use={"tmdb": True, "imdb": True, "tvmaze": False, "omdb": True,
                                         "opensubtitles": True, "jellyfin": False},
              hosts={"jellyfin.example.invalid": "jellyfin"})
    assert net.via_proxy("https://api.themoviedb.org/3/search/movie")
    assert net.via_proxy("https://image.tmdb.org/t/p/w185/x.jpg")  # posters follow their source
    assert net.via_proxy("https://datasets.imdbws.com/title.basics.tsv.gz")
    assert not net.via_proxy("https://api.tvmaze.com/search/shows")
    assert not net.via_proxy("http://jellyfin.example.invalid:8096/System/Info")
    assert not net.via_proxy("http://hooks.example.invalid:9000/hook")  # your own services go direct
    assert net.client_for("https://api.themoviedb.org/3") is not net.client_for("https://api.tvmaze.com")
    env = {"KS_PROXY": "http://route:10809", "KS_PROXY_USE": "tmdb,omdb"}
    assert proxy_from_env("https://image.tmdb.org/t/p/w185/x.jpg", env) == "http://route:10809"
    assert proxy_from_env("https://static.tvmaze.com/x.jpg", env) is None
    assert proxy_from_env("https://image.tmdb.org/x.jpg", {}) is None
    net.close()


def test_local_places_always_go_direct():
    """Jellyfin on the LAN, on this host or as a Docker service never takes the proxy, even when its
    toggle is on; one on a public name does when asked."""
    on = {"jellyfin": True}
    for host in ("192.168.0.20", "10.0.0.5", "127.0.0.1", "[::1]", "localhost", "jellyfin", "media.lan",
                 "nas.local", "box.home.arpa"):
        net = Net("http://route:10809", use=on, hosts={host.strip("[]"): "jellyfin"})
        assert not net.via_proxy(f"http://{host}:8096/System/Info"), host
        net.close()
    net = Net("http://route:10809", use=on, hosts={"jellyfin.example.invalid": "jellyfin"})
    assert net.via_proxy("https://jellyfin.example.invalid/System/Info")
    net.close()


def test_migrated_proxy_keeps_jellyfin_direct(tmp_path):
    cfg = tmp_path / "config.yml"
    cfg.write_text(yaml.safe_dump({"network": {"proxy": "http://sidecar.example.invalid:10809"},
                                   "sources": {"jellyfin": {"enabled": True, "url": "http://192.168.0.20:8096"}}}),
                   encoding="utf-8")
    net = Net.from_settings(Settings(cfg))
    assert net.routed and net.via_proxy("https://api.themoviedb.org/3/configuration")
    assert not net.via_proxy("http://192.168.0.20:8096/System/Info")
    net.close()


def test_routed_and_direct_requests_really_split():
    """A local proxy sees the routed source (with its sign-in) and never the direct one."""
    with FakeProxy() as fp, LocalSite() as site:
        net = Net(f"http://{USER}:{PASS}@{fp.address}", timeout=3,
                  use={"tmdb": False, "tvmaze": True}, hosts={"127.0.0.1": "jellyfin"})
        with pytest.raises(LookupError_):
            net.get_json("https://api.tvmaze.com/shows/1")
        assert net.get_json(f"http://127.0.0.1:{site.port}/System/Info/Public") == {"ok": True}
        net.close()
    assert fp.seen == [("CONNECT", "api.tvmaze.com:443")]
    assert fp.auth == [base64.b64encode(f"{USER}:{PASS}".encode()).decode()]


# ---------------------------------------------------------------------- test connections

class FakeResp:
    def __init__(self, code):
        self.status_code = code


class FakeClient:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def request(self, method, url):
        self.calls.append((method, url))
        if isinstance(self.answer, BaseException):
            raise self.answer
        return FakeResp(self.answer)


def fixed_net(answer, proxy=None):
    net = Net(proxy, timeout=15)
    fc = FakeClient(answer)
    net.client_for = lambda url: fc  # type: ignore[method-assign]
    return net, fc


def public_dns(*a, **k):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.1.1.1", 443))]


def test_dns_answering_a_private_address_is_caught_before_connecting():
    net, fc = fixed_net(200)
    poisoned = lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.66", 443))]  # noqa: E731
    r = netcheck.check(net, "tmdb", "GET", netcheck.CHECKS["tmdb"][1], resolver=poisoned)
    assert (r.ok, r.reason, r.route, r.outcome) == (False, "DNS answered a private address", "direct", "Blocked")
    assert fc.calls == []  # nothing was sent to the filter's address
    gone = lambda *a, **k: (_ for _ in ()).throw(socket.gaierror(-2, "Name or service not known"))  # noqa: E731
    assert netcheck.check(net, "tmdb", "GET", "https://api.themoviedb.org/3", resolver=gone).reason == \
        "DNS found no address"


def test_reset_timeout_and_status_are_told_apart():
    reset = httpx.ConnectError("[Errno 104] Connection reset by peer")
    reset.__cause__ = ConnectionResetError(104, "Connection reset by peer")
    cases = [
        (reset, False, "Connection reset"),
        (httpx.ReadError("[WinError 10054] An existing connection was forcibly closed"), False,
         "Connection reset"),
        (httpx.ConnectError("unexpected"), False, "Could not connect"),
        (httpx.ConnectTimeout("timed out"), False, "Timed out after 15 s"),
        (httpx.ReadTimeout("The read operation timed out"), False, "Timed out after 15 s"),
        (200, True, "HTTP 200"),
        (401, True, "HTTP 401, no key sent"),
        (503, False, "HTTP 503"),
    ]
    for answer, ok, reason in cases:
        net, _ = fixed_net(answer)
        r = netcheck.check(net, "tvmaze", "GET", "https://api.tvmaze.com/shows/1", resolver=public_dns)
        assert (r.ok, r.reason) == (ok, reason), answer
        assert r.ms is not None and r.ms >= 0


def test_proxy_failures_are_named_without_the_proxy():
    via = f"http://{USER}:{PASS}@{ADDR}"
    cases = [
        (httpx.ProxyError(f"407 Proxy Authentication Required at {ADDR}"),
         "The proxy did not accept the username or password (HTTP 407)"),
        (httpx.ProxyError("502 Bad Gateway"), "The proxy could not reach it (HTTP 502)"),
        (httpx.ConnectError(f"[Errno 111] Connection refused ({ADDR})"),
         "The proxy is not answering (connection refused)"),
        (httpx.ConnectError(f"[Errno -2] Name or service not known: {ADDR}"),
         "The proxy is not answering (name not found)"),
    ]
    for exc, reason in cases:
        net, _ = fixed_net(exc, proxy=via)
        r = netcheck.check(net, "tmdb", "GET", netcheck.CHECKS["tmdb"][1])
        assert (r.ok, r.reason, r.route_text) == (False, reason, "through the proxy")
        assert no_secrets(r.reason)


def test_run_tests_every_source_in_page_order(client, monkeypatch):
    c, core = client
    csrf = signed_in(c, core)
    seen = []

    def fake_check(net, key, method, url, *, lan=False, resolver=None):
        seen.append((key, method, url, lan))
        return netcheck.Result(key, key, "direct", key != "omdb", 12, "HTTP 200" if key != "omdb" else "Connection reset")

    monkeypatch.setattr(netcheck, "check", fake_check)
    html = c.post("/api/network/test", headers={"X-CSRF-Token": csrf}).text
    assert html.index("tmdb") < html.index("tvmaze") < html.index("opensubtitles")
    assert "No server address set" in html and "4 of 5 reached" in html
    assert all("api_key" not in u and "apikey" not in u for _, _, u, _ in seen)  # no key in any test
    c.post("/api/settings/sources", json={"jellyfin": {"url": "http://jellyfin.example.invalid:8096"}},
           headers={"X-CSRF-Token": csrf})
    seen.clear()
    c.post("/api/network/test", headers={"X-CSRF-Token": csrf})
    assert ("jellyfin", "GET", "http://jellyfin.example.invalid:8096/System/Info/Public", True) in seen


# ---------------------------------------------------------------------- migration from 0.1.0

def write_old(path, proxy, **extra):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"network": dict({"proxy": proxy, "timeout_seconds": 20}, **extra)}),
                    encoding="utf-8")


def test_old_single_proxy_setting_moves_over(tmp_path):
    cfg = tmp_path / "config.yml"
    write_old(cfg, "http://zq7%40user:p%3Ass@old-proxy.example.invalid:3128")
    s = Settings(cfg)
    n = s.get("network")
    assert n["mode"] == "proxy" and n["timeout_seconds"] == 20
    assert n["proxy"] == {"kind": "http", "address": "old-proxy.example.invalid:3128", "username": "zq7@user",
                          "password": "p:ss"}
    assert n["use"]["jellyfin"] is False and n["use"]["tmdb"] is True
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))["network"]
    assert isinstance(saved["proxy"], dict) and saved["mode"] == "proxy"  # written back once, new shape
    assert Settings(cfg).get("network") == n


def test_old_sidecar_and_socks_and_empty_values(tmp_path):
    cases = [("http://xray:10809", "proxy", "http", "xray:10809"),
             ("socks5h://127.0.0.1:1080", "proxy", "socks5", "127.0.0.1:1080"),
             ("socks5://[::1]", "proxy", "socks5", "[::1]:1080"),
             ("", "direct", "http", "")]
    for i, (old, mode, kind, addr) in enumerate(cases):
        cfg = tmp_path / str(i) / "config.yml"
        write_old(cfg, old)
        n = Settings(cfg).get("network")
        assert (n["mode"], n["proxy"]["kind"], n["proxy"]["address"]) == (mode, kind, addr), old


def test_unreadable_old_proxy_is_dropped_quietly(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    cfg = tmp_path / "config.yml"
    write_old(cfg, "zq7-garbage-without-scheme")
    n = Settings(cfg).get("network")
    assert n["mode"] == "direct" and n["proxy"]["address"] == ""
    assert "could not be read" in caplog.text and no_secrets(caplog.text)


def test_parse_proxy_url_refuses_other_schemes():
    with pytest.raises(ValueError):
        parse_proxy_url("ftp://zq7.example.invalid:21")
    assert parse_proxy_url("http://proxy.example.invalid")["address"] == "proxy.example.invalid:80"


# ---------------------------------------------------------------------- local helpers

class _ProxyHandler(BaseHTTPRequestHandler):
    def do_CONNECT(self):  # noqa: N802
        self.server.seen.append(("CONNECT", self.path))
        auth = self.headers.get("Proxy-Authorization", "")
        if auth.startswith("Basic "):
            self.server.auth.append(auth[6:])
        self.send_response(502)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class FakeProxy:
    """An HTTP proxy on 127.0.0.1 that records CONNECT targets and refuses them (HTTP 502)."""

    def __enter__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
        self.srv.seen, self.srv.auth = [], []
        self.seen, self.auth = self.srv.seen, self.srv.auth
        self.address = f"127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()


class _SiteHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = b'{"ok": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class LocalSite:
    def __enter__(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), _SiteHandler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()

