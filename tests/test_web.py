"""Web app: first account, sign-in, CSRF, lockout, Cloudflare Access, pages and settings."""

import re

import pytest
from conftest import FakeIdentifier
from fastapi.testclient import TestClient

from kinoshelf.web import App, create_app


@pytest.fixture
def client(inst, monkeypatch):
    monkeypatch.delenv("KS_CF_ACCESS_TEAM", raising=False)
    monkeypatch.delenv("KS_CF_ACCESS_AUD", raising=False)
    core = App(ddir=inst["data"], start_worker=False, identifier=FakeIdentifier(inst["settings"], {}))
    c = TestClient(create_app(core), base_url="http://inbox.test")
    return c, core


def signed_in(c, core):
    code = core.auth.setup_code
    r = c.post("/setup", data={"code": code, "username": "owner", "password": "correct horse 1"})
    assert r.status_code in (200, 303)
    r = c.post("/login", data={"username": "owner", "password": "correct horse 1"}, follow_redirects=False)
    assert r.status_code == 303 and "ks_session" in r.cookies
    page = c.get("/review").text
    return re.search(r'name="csrf" content="([^"]+)"', page).group(1)


def test_first_start_needs_the_setup_code(client):
    c, core = client
    assert c.get("/review", follow_redirects=False).headers["location"] == "/setup"
    r = c.post("/setup", data={"code": "WRONG", "username": "owner", "password": "correct horse 1"})
    assert "not right" in r.text
    assert c.get("/api/health").json()["ok"] is True


def test_sign_in_csrf_and_security_headers(client):
    c, core = client
    csrf = signed_in(c, core)
    r = c.get("/review")
    assert r.status_code == 200 and "Nothing needs you" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert c.post("/api/settings/rule", json={"min_score": 90, "max_runtime_diff_pct": 5, "min_margin": 10}
                  ).status_code == 403
    r = c.post("/api/settings/rule", json={"min_score": 90, "max_runtime_diff_pct": 5, "min_margin": 10,
                                           "dry_run": True}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and core.settings.get("rule.min_score") == 90
    r = c.post("/api/settings/naming", json={"movie": "X:\\{Nope}", "episode": "X:\\{Series}.{ext}"},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 400 and "Unknown token" in r.json()["error"]
    r = c.post("/api/settings/rule", json={}, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_keys_are_never_sent_back(client):
    c, core = client
    csrf = signed_in(c, core)
    c.post("/api/settings/sources", json={"tmdb": {"enabled": True, "api_key": "secret-key-123"}},
           headers={"X-CSRF-Token": csrf})
    assert core.settings.get("sources.tmdb.api_key") == "secret-key-123"
    c.post("/api/settings/sources", json={"tmdb": {"enabled": True, "api_key": ""}}, headers={"X-CSRF-Token": csrf})
    assert core.settings.get("sources.tmdb.api_key") == "secret-key-123"  # blank keeps it
    assert "secret-key-123" not in c.get("/settings").text


def test_lockout_after_five_wrong_tries(client):
    c, core = client
    signed_in(c, core)
    c.cookies.clear()
    for _ in range(5):
        c.post("/login", data={"username": "owner", "password": "nope nope nope"})
    r = c.post("/login", data={"username": "owner", "password": "correct horse 1"})
    assert "Too many wrong tries" in r.text


def test_cloudflare_requests_need_access(client, monkeypatch):
    c, core = client
    r = c.get("/api/health", headers={"CF-Ray": "abc", "CF-Connecting-IP": "203.0.113.5"})
    assert r.status_code == 403 and "Cloudflare Access is not set up" in r.text
    monkeypatch.setattr(core.cf, "team", "team.cloudflareaccess.com")
    monkeypatch.setattr(core.cf, "aud", "aud123")
    r = c.get("/api/health", headers={"CF-Ray": "abc"})
    assert r.status_code == 403 and "token missing" in r.text


def test_shortcut_file(client):
    c, core = client
    csrf = signed_in(c, core)
    assert c.get("/shortcut.url?target=lan").status_code == 400
    c.post("/api/settings/server", json={"public_url": "", "lan_url": "http://192.0.2.10:8484"},
           headers={"X-CSRF-Token": csrf})
    r = c.get("/shortcut.url?target=lan")
    assert r.status_code == 200 and "URL=http://192.0.2.10:8484/review" in r.text
