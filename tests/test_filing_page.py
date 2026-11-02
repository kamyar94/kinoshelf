"""Settings > Filing rules: the page, saving with checks, preview, presets, YAML in and out."""

import pytest
import yaml
from conftest import FakeIdentifier
from fastapi.testclient import TestClient
from test_web import signed_in

from kinoshelf.web import App, create_app

RULE = {"name": "Films", "when": {"type": "film"}, "root": "X:\\Movies", "folder": "{Title} ({Year}){ |Tags}",
        "tags": ["{Quality}"]}


@pytest.fixture
def client(inst, monkeypatch):
    monkeypatch.delenv("KS_CF_ACCESS_TEAM", raising=False)
    monkeypatch.delenv("KS_CF_ACCESS_AUD", raising=False)
    core = App(ddir=inst["data"], start_worker=False, identifier=FakeIdentifier(inst["settings"], {}))
    return TestClient(create_app(core), base_url="http://inbox.test"), core


def test_page_save_preview_and_checks(client):
    c, core = client
    csrf = signed_in(c, core)
    assert "Open the filing rules" in c.get("/settings").text
    assert 'id="filing-data"' in c.get("/settings/filing").text
    r = c.post("/api/filing", json={"filing": {"enabled": True, "rules": [RULE], "policies": {"quality_from": "name"}}},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and core.settings.get("filing.enabled") is True
    rows = {p["item"]: p for p in r.json()["preview"]}
    assert rows["Interstellar (2014)"]["dest"] == "X:\\Movies\\Interstellar (2014) [2160p]\\Interstellar (2014) [2160p].mkv"
    assert rows["The Office S02E01"]["ok"] is False and "No filing rule fits" in rows["The Office S02E01"]["dest"]
    bad = c.post("/api/filing", json={"filing": {"rules": [dict(RULE, when={"mood": "calm"})]}},
                 headers={"X-CSRF-Token": csrf})
    assert bad.status_code == 400 and "mood is not in this version" in bad.json()["error"]
    assert core.settings.get("filing.rules")[0]["when"] == {"type": "film"}  # nothing saved


def test_presets_import_and_export(client):
    c, core = client
    csrf = signed_in(c, core)
    r = c.post("/api/filing/preset", json={"name": "by-language", "language": "ko"}, headers={"X-CSRF-Token": csrf})
    names = [x["name"] for x in r.json()["filing"]["rules"]]
    assert names == ["Original language: Korean", "Films", "Series"]
    text = yaml.safe_dump({"filing": {"enabled": True, "rules": [RULE]}})
    r = c.post("/api/filing/import", json={"yaml": text}, headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and core.settings.get("filing.rules")[0]["name"] == "Films"
    out = c.get("/api/filing/export")
    assert out.status_code == 200 and yaml.safe_load(out.text)["filing"]["rules"][0]["root"] == "X:\\Movies"
    assert c.post("/api/filing/import", json={"yaml": "filing: [1, 2"}, headers={"X-CSRF-Token": csrf}).status_code == 400
