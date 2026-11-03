"""Updates: decisions, the registry client's digest checks, the updater agent, the host step's install and roll
back (against a fake docker), and the Settings and Week pages. Nothing here reaches a real registry."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest
from conftest import FakeIdentifier
from fastapi.testclient import TestClient
from test_web import signed_in

from kinoshelf import __version__, updater
from kinoshelf.config import Settings
from kinoshelf.registry import CANDIDATE_TAG, Image, Registry, RegistryError, split_image
from kinoshelf.updater import Agent, choose, compare_versions, due
from kinoshelf.web import App, create_app

ROOT = Path(__file__).resolve().parents[1]
CFG = {"enabled": True, "interval_hours": 6, "channel": "latest", "allow_downgrade": False}


# ---------------------------------------------------------------------- decisions
def test_versions_compare_by_number():
    assert compare_versions("0.10.0", "0.9.9") == 1 and compare_versions("0.2.1", "0.2.1") == 0
    assert compare_versions("0.1.2", "0.2.1") == -1 and compare_versions("", "0.1.0") == 0


def test_due_follows_the_interval_or_a_request():
    assert due(CFG, 0, 1000, None)
    assert not due(CFG, 1000, 1000 + 3600, None) and due(CFG, 1000, 1000 + 6 * 3600, None)
    assert due(CFG, 1000, 1001, "check")


def test_choose_installs_only_what_is_newer_and_wanted():
    cur = {"version": "0.1.2", "revision": "aaa", "digest": ""}
    new = {"version": "0.1.2", "revision": "bbb", "digest": "sha256:" + "b" * 64}
    assert choose(CFG, cur, new, set(), {}, None) == (True, "Newer image")  # a new commit, same version
    assert choose(CFG, cur, dict(new, revision="aaa"), set(), {}, None)[1] == "Up to date"
    assert "rolled back before" in choose(CFG, cur, new, {new["digest"]}, {}, None)[1]
    assert "waiting for the host step" in choose(CFG, cur, new, set(), {"digest": new["digest"]}, None)[1]
    older = dict(new, version="0.1.1")
    assert not choose(CFG, cur, older, set(), {}, None)[0]
    assert choose(dict(CFG, allow_downgrade=True), cur, older, set(), {}, None)[0]
    off = dict(CFG, enabled=False)
    assert not choose(off, cur, new, set(), {}, None)[0] and "Update now" in choose(off, cur, new, set(), {}, None)[1]
    assert choose(off, cur, new, set(), {}, "update")[0]
    assert choose(CFG, cur, None, set(), {}, None) == (False, "Nothing checked yet")


# ---------------------------------------------------------------------- a fake registry
def _sha(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _layer(name: str, data: bytes) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as t:
        info = tarfile.TarInfo(name)
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    return gzip.compress(raw.getvalue())


class FakeRegistry:
    """registry:2 in a dict, with ghcr-style anonymous tokens and an index pointing at amd64 and arm64."""

    def __init__(self, version="0.1.3", revision="c0ffee1", token=True):
        self.blobs, self.manifests, self.tags = {}, {}, {}
        self.token = token
        self.calls = []
        self.push("latest", version, revision)

    def push(self, tag, version, revision):
        layers = [_layer("app/one.txt", f"{version} one".encode()), _layer("app/two.txt", b"two" * 1000)]
        cfg = json.dumps({"created": "2026-11-02T10:00:00Z", "config": {"Labels": {
            "org.opencontainers.image.version": version, "org.opencontainers.image.revision": revision}},
            "rootfs": {"type": "layers", "diff_ids": []}}).encode()
        for b in layers + [cfg]:
            self.blobs[_sha(b)] = b
        man = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.manifest.v1+json",
                          "config": {"digest": _sha(cfg), "size": len(cfg)},
                          "layers": [{"mediaType": "application/vnd.oci.image.layer.v1.tar+gzip", "digest": _sha(b),
                                      "size": len(b)} for b in layers]}).encode()
        idx = json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json",
                          "manifests": [{"digest": _sha(man), "size": len(man),
                                         "platform": {"os": "linux", "architecture": "amd64"}},
                                        {"digest": "sha256:" + "0" * 64, "size": 1,
                                         "platform": {"os": "linux", "architecture": "arm64"}}]}).encode()
        self.manifests[_sha(man)] = man
        self.manifests[_sha(idx)] = idx
        self.tags[tag] = _sha(idx)
        return _sha(idx), _sha(man), _sha(cfg)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(str(request.url))
        if request.url.path == "/token":
            return httpx.Response(200, json={"token": "zq7-pull-token"})
        if self.token and request.headers.get("authorization") != "Bearer zq7-pull-token":
            return httpx.Response(401, headers={"www-authenticate":
                                                'Bearer realm="https://reg.example.invalid/token",service="reg",scope="x"'})
        m = re.match(r"^/v2/(.+)/(manifests|blobs)/(.+)$", request.url.path)
        if not m:
            return httpx.Response(404)
        kind, ref = m.group(2), m.group(3)
        if kind == "manifests":
            ref = self.tags.get(ref, ref)
            body = self.manifests.get(ref)
            if body is None:
                return httpx.Response(404)
            return httpx.Response(200, content=body, headers={"docker-content-digest": ref,
                                                              "content-type": json.loads(body)["mediaType"]})
        body = self.blobs.get(ref)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)

    def client(self):
        c = httpx.Client(transport=httpx.MockTransport(self.handler))
        return lambda url: c


def test_split_image():
    assert split_image("ghcr.io/kamyar94/kinoshelf") == ("ghcr.io", "kamyar94/kinoshelf")
    assert split_image("localhost:5000/e2e/kinoshelf") == ("localhost:5000", "e2e/kinoshelf")
    assert split_image("nginx") == ("registry-1.docker.io", "library/nginx")


def test_resolve_signs_in_picks_amd64_and_reads_labels():
    fr = FakeRegistry()
    img = Registry("reg.example.invalid/owner/kinoshelf", fr.client()).resolve("latest")
    assert (img.version, img.revision, img.digest) == ("0.1.3", "c0ffee1", fr.tags["latest"])
    assert img.manifest_digest != img.digest and img.size > 0
    assert any(u.endswith("/token?service=reg&scope=x") for u in fr.calls)


def test_fetch_writes_a_docker_load_archive(tmp_path):
    fr = FakeRegistry()
    reg = Registry("reg.example.invalid/owner/kinoshelf", fr.client())
    img = reg.resolve("latest")
    out = tmp_path / "img.tar"
    sha = reg.fetch(img, out)
    assert sha == hashlib.sha256(out.read_bytes()).hexdigest()
    with tarfile.open(out) as t:
        man = json.loads(t.extractfile("manifest.json").read())
        assert man[0]["RepoTags"] == [CANDIDATE_TAG] and len(man[0]["Layers"]) == 2
        for name in man[0]["Layers"] + [man[0]["Config"]]:
            assert _sha(t.extractfile(name).read()).split(":")[1] == name.split(".")[0]


def test_a_tampered_layer_or_manifest_is_refused(tmp_path):
    fr = FakeRegistry()
    reg = Registry("reg.example.invalid/owner/kinoshelf", fr.client())
    img = reg.resolve("latest")
    bad = img.manifest["manifest"]["layers"][1]["digest"]
    blob = bytearray(fr.blobs[bad])
    blob[len(blob) // 2] ^= 0xFF  # same size, different content
    fr.blobs[bad] = bytes(blob)
    with pytest.raises(RegistryError, match="does not match its digest"):
        reg.fetch(img, tmp_path / "img.tar")
    assert list(tmp_path.iterdir()) == []  # nothing half-written is left
    fr2 = FakeRegistry()
    fr2.manifests[fr2.tags["latest"]] += b" "
    with pytest.raises(RegistryError, match="manifest does not match"):
        Registry("reg.example.invalid/owner/kinoshelf", fr2.client()).resolve("latest")


# ---------------------------------------------------------------------- the agent
@pytest.fixture
def agent_env(tmp_path, monkeypatch):
    monkeypatch.setenv("KS_REVISION", "aaaaaaa")
    s = Settings(tmp_path / "config.yml")
    s.save()
    fr = FakeRegistry(version=__version__, revision="bbbbbbb")
    clock = [1_000_000.0]

    def factory(settings, cfg):
        return Registry("reg.example.invalid/owner/kinoshelf", fr.client()), _NoNet(), "direct"

    agent = Agent(tmp_path, s, registry_factory=factory, clock=lambda: clock[0])
    return agent, s, fr, clock, tmp_path / "update"


class _NoNet:
    def proxy_for(self, url):
        return None

    def close(self):
        pass


def test_agent_fetches_a_newer_image_once(agent_env):
    agent, s, fr, clock, d = agent_env
    st = agent.run_once()
    assert st["state"] == "ready" and st["last_check"]["ok"]
    ready = json.loads((d / "ready.json").read_text())
    env = dict(line.split("=", 1) for line in (d / "ready.env").read_text().splitlines())
    assert env["DIGEST"] == ready["digest"] == fr.tags["latest"] and env["SHA256"] == ready["sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", env["LAYERS"])
    assert all(re.fullmatch(r"[A-Za-z0-9._:+-]*", v) for v in env.values())
    assert (d / "incoming" / env["FILE"]).exists()
    clock[0] += 60
    assert agent.run_once()["state"] == "ready"  # not due again yet, nothing new fetched
    updater.request(d.parent, "check")
    assert "waiting for the host step" in agent.run_once()["message"]


def test_after_the_host_step_the_panel_says_what_runs(agent_env, monkeypatch):
    agent, s, fr, clock, d = agent_env
    assert agent.run_once()["state"] == "ready"
    for f in ("ready.json", "ready.env"):  # the host step installed it and cleaned up
        (d / f).unlink()
    monkeypatch.setenv("KS_REVISION", "bbbbbbb")  # the new image runs
    clock[0] += 30
    st = agent.run_once()
    assert st["state"] == "idle" and st["message"] == "Up to date"


def test_agent_respects_off_downgrade_and_rejected(agent_env):
    agent, s, fr, clock, d = agent_env
    s.values["update"]["enabled"] = False
    s.save()
    assert "Update now" in agent.run_once()["message"] and not (d / "ready.json").exists()
    updater.request(d.parent, "update")
    clock[0] += 1
    assert agent.run_once()["state"] == "ready"
    for f in ("ready.json", "ready.env"):
        (d / f).unlink()
    (d / "rejected.txt").write_text(fr.tags["latest"] + "\n")
    updater.request(d.parent, "update")
    assert "rolled back before" in agent.run_once()["message"]
    fr.push("latest", "0.0.9", "ccccccc")
    updater.request(d.parent, "check")
    s.values["update"]["enabled"] = True
    s.save()
    assert "older than the running" in agent.run_once()["message"]


def test_agent_reports_an_unreachable_registry(agent_env):
    agent, s, fr, clock, d = agent_env
    fr.manifests.clear()
    st = agent.run_once()
    assert st["last_check"]["ok"] is False and "has no latest" in st["last_check"]["reason"]
    assert "zq7" not in json.dumps(st)  # the pull token never lands in a file


def test_a_failed_check_is_tried_again_sooner_with_a_back_off(agent_env):
    from kinoshelf.updater import retry_wait, view

    agent, s, fr, clock, d = agent_env
    assert [retry_wait(CFG, n) / 60 for n in range(5)] == [360, 12, 24, 48, 96]
    assert retry_wait(CFG, 9) == 6 * 3600  # never longer than the interval
    good = dict(fr.manifests)
    fr.manifests.clear()
    t0 = clock[0]
    st = agent.run_once()
    assert st["failures"] == 1 and st["last_check"]["retry_at"] == t0 + 12 * 60
    clock[0] = t0 + 11 * 60
    assert agent.run_once()["last_check"]["at"] == t0  # not yet: 12 minutes after a failure
    clock[0] = t0 + 12 * 60
    st = agent.run_once()
    assert st["last_check"]["at"] == clock[0] and st["failures"] == 2  # tried again, failed again: 24 min next
    assert "Trying again in 24 min" in view(d.parent, s, now=clock[0])["text"]
    fr.manifests.update(good)
    clock[0] += 24 * 60
    st = agent.run_once()
    assert st["last_check"]["ok"] and st["failures"] == 0
    clock[0] += 60 * 60
    assert agent.run_once()["last_check"]["at"] == clock[0] - 60 * 60  # back to the 6-hour interval


def test_cosign_mode_needs_a_good_signature(agent_env):
    agent, s, fr, clock, d = agent_env
    s.values["update"]["verify"] = "cosign"
    s.save()
    agent.verify = lambda ref, identity, env: "The image's signature did not check out"
    assert agent.run_once()["message"] == "The image's signature did not check out"
    assert not (d / "ready.json").exists()
    seen = {}
    agent.verify = lambda ref, identity, env: seen.setdefault("ref", ref) and ""
    updater.request(d.parent, "check")
    assert agent.run_once()["state"] == "ready" and seen["ref"].endswith("@" + fr.tags["latest"])


# ---------------------------------------------------------------------- the host step (fake docker)
FAKE_DOCKER = r'''#!/bin/sh
S="$FAKE_STATE"
echo "$*" >> "$S/calls.log"
key() { echo "$1" | tr ':/' '__'; }
cmd=$1; shift
case "$cmd" in
  load) cat "$3.id" > "$S/tag_$(key kinoshelf:candidate)"; exit 0 ;;
  image)
    sub=$1; shift
    if [ "$sub" = rm ]; then rm -f "$S/tag_$(key "$1")"; exit 0; fi
    t="$S/tag_$(key "$3")"; [ -f "$t" ] || exit 1; id=$(cat "$t")
    case "$2" in
      *version*) cat "$S/version_$(key "$id")" ;;
      *revision*) cat "$S/revision_$(key "$id")" 2>/dev/null ;;
      *) echo "$id" ;;
    esac
    exit 0 ;;
  tag) cp "$S/tag_$(key "$1")" "$S/tag_$(key "$2")"; exit 0 ;;
  compose) cp "$S/tag_$(key kinoshelf:local)" "$S/running"; exit 0 ;;
  run) id=$(cat "$S/tag_$(key kinoshelf:candidate)"); cat > /dev/null
       if [ -f "$S/badconfig_$(key "$id")" ]; then echo "The filing method some-other-method is not in this version"
         echo "Settings this version cannot use"; exit 1; fi
       echo "Settings OK"; exit 0 ;;
  exec) id=$(cat "$S/running"); [ -f "$S/bad_$(key "$id")" ] && exit 1
        printf '%s|%s\n' "$(cat "$S/version_$(key "$id")")" "$(cat "$S/revision_$(key "$id")" 2>/dev/null)"; exit 0 ;;
esac
exit 0
'''


def _sh():
    for c in (shutil.which("sh"), r"C:\Program Files\Git\bin\sh.exe", r"C:\Program Files\Git\usr\bin\sh.exe"):
        if c and Path(c).exists():
            return c
    return None


@pytest.fixture
def host(tmp_path):
    sh = _sh()
    if not sh:
        pytest.skip("needs a POSIX sh")
    state = tmp_path / "state"
    state.mkdir()
    (tmp_path / "fakedocker").write_text(FAKE_DOCKER, encoding="utf-8", newline="\n")
    d = tmp_path / "data" / "update" / "incoming"
    d.mkdir(parents=True)
    (tmp_path / "data" / "config.yml").write_text("before: 1\n", encoding="utf-8")

    def key(x):
        return x.replace(":", "_").replace("/", "_")

    def image(id_, version, bad=False, revision=""):
        (state / f"version_{key(id_)}").write_text(version + "\n", encoding="utf-8", newline="\n")
        if revision:
            (state / f"revision_{key(id_)}").write_text(revision + "\n", encoding="utf-8", newline="\n")
        if bad:
            (state / f"bad_{key(id_)}").write_text("x")

    old = "sha256:" + "1" * 64
    image(old, "0.1.1", revision="rold")
    (state / f"tag_{key('kinoshelf:local')}").write_text(old, encoding="utf-8")
    (state / "running").write_text(old, encoding="utf-8")

    def offer(version, bad=False, badconfig=False):
        cfg = hashlib.sha256(version.encode()).hexdigest()
        image("sha256:" + cfg, version, bad, revision=f"r{version}")
        if badconfig:
            (state / f"badconfig_sha256_{cfg}").write_text("x")
        f = d / f"kinoshelf-{version}.tar"
        f.write_bytes(b"archive " + version.encode())
        (d / f"kinoshelf-{version}.tar.id").write_text("sha256:" + cfg, encoding="utf-8")
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        env = f"VERSION={version}\nREVISION=r{version}\nDIGEST=sha256:{'9' * 64}\nMANIFEST=sha256:{'8' * 64}\n" \
              f"CONFIG={cfg}\nFILE={f.name}\nSHA256={sha}\n"
        (d.parent / "ready.env").write_text(env, encoding="utf-8", newline="\n")
        (d.parent / "ready.json").write_text("{}", encoding="utf-8")
        return "sha256:" + cfg

    def run():
        env = dict(os.environ, KS_DIR=tmp_path.as_posix(), FAKE_STATE=state.as_posix(),
                   KS_DOCKER=f"sh {(tmp_path / 'fakedocker').as_posix()}", KS_HEALTH_SECONDS="2", KS_HEALTH_STEP="1")
        return subprocess.run([sh, str(ROOT / "scripts" / "update" / "apply.sh")], env=env, capture_output=True,
                              text=True, timeout=120)

    def running():
        return (state / "running").read_text().strip()

    def entries():
        p = d.parent / "journal.jsonl"
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    return {"offer": offer, "run": run, "running": running, "entries": entries, "dir": d.parent, "old": old,
            "root": tmp_path, "state": state}


def test_host_step_installs_and_keeps_the_previous_image(host):
    r = host["run"]()
    assert r.returncode == 0 and host["entries"]() == []  # nothing waiting: only the heartbeat
    assert json.loads((host["dir"] / "host.json").read_text())["seen"]
    new = host["offer"]("0.1.2")
    r = host["run"]()
    assert r.returncode == 0, r.stdout + r.stderr
    assert host["running"]() == new
    assert (host["state"] / "tag_kinoshelf_previous").read_text().strip() == host["old"]
    e = host["entries"]()[-1]
    assert e["event"] == "updated" and e["from"]["version"] == "0.1.1" and e["to"]["version"] == "0.1.2"
    assert not (host["dir"] / "ready.env").exists() and not list((host["dir"] / "incoming").glob("*.tar"))
    assert json.loads((host["dir"] / "host.json").read_text())["current"]["version"] == "0.1.2"


def test_host_step_rolls_back_a_version_that_does_not_answer(host):
    bad = host["offer"]("0.1.3", bad=True)
    (host["root"] / "data" / "config.yml").write_text("before: 1\n", encoding="utf-8")
    r = host["run"]()
    assert r.returncode == 1 and "Rolled back" in r.stdout
    assert host["running"]() == host["old"]  # back on the previous image
    e = host["entries"]()[-1]
    assert e["event"] == "rolled_back" and "previous version answers again" in e["reason"]
    assert (host["dir"] / "rejected.txt").read_text().strip() == "sha256:" + "9" * 64
    assert not (host["state"] / "tag_kinoshelf_candidate").exists()
    assert bad != host["old"]


def test_host_step_needs_the_updater_to_run_again(host):
    agent = host["dir"] / "agent.json"
    agent.write_text(json.dumps({"heartbeat": 1000.5, "state": "ready"}, indent=1), encoding="utf-8")
    host["offer"]("0.1.4")
    r = host["run"]()  # the app answers, but the updater never writes its heartbeat again
    assert r.returncode == 1 and host["running"]() == host["old"]
    assert "the updater did not start" in host["entries"]()[-1]["reason"]
    agent.write_text(json.dumps({"heartbeat": 9_999_999_999.5}, indent=1), encoding="utf-8")  # it runs
    host["offer"]("0.1.5")
    r = host["run"]()
    assert r.returncode == 0 and host["entries"]()[-1]["event"] == "updated", r.stdout + r.stderr


def test_host_step_refuses_a_version_that_cannot_use_the_settings(host):
    host["offer"]("0.3.0", badconfig=True)
    r = host["run"]()
    assert r.returncode == 1 and "Refused" in r.stdout
    assert host["running"]() == host["old"]  # nothing was switched or restarted
    e = host["entries"]()[-1]
    assert e["event"] == "refused" and "some-other-method is not in this version" in e["reason"]
    assert (host["dir"] / "rejected.txt").read_text().strip() == "sha256:" + "9" * 64
    assert "compose" not in (host["state"] / "calls.log").read_text()


def test_check_config_names_what_a_version_lacks():
    from kinoshelf.confcheck import problems

    assert problems("watcher: {enabled: false}\n") == []
    assert problems("network: {proxy: 'http://ks.example.invalid:3128'}\n") == []  # old settings still read
    found = problems("naming: {preset: some-other-method}\nfuture: {x: 1}\nhooks:\n  icon:\n"
                     "  - {name: Icon, kind: command, enabled: true, command: [python, -m, kinoshelf.default_hooks.nope]}\n")
    assert "The filing method some-other-method is not in this version" in found
    assert "Unknown settings section future" in found
    assert any("kinoshelf.default_hooks.nope is not in this version" in p for p in found)
    assert problems("hooks: {icon: [{name: x, kind: telepathy}]}\n") == [
        "Hook x: the kind telepathy is not in this version"]


def test_host_step_refuses_a_damaged_archive(host):
    host["offer"]("0.1.4")
    f = next((host["dir"] / "incoming").glob("*.tar"))
    f.write_bytes(b"changed")
    r = host["run"]()
    assert r.returncode == 1 and host["running"]() == host["old"]
    assert host["entries"]()[-1]["reason"] == "the archive does not match its checksum"


# ---------------------------------------------------------------------- pages
@pytest.fixture
def client(inst, monkeypatch):
    monkeypatch.delenv("KS_CF_ACCESS_TEAM", raising=False)
    monkeypatch.delenv("KS_CF_ACCESS_AUD", raising=False)
    core = App(ddir=inst["data"], start_worker=False, identifier=FakeIdentifier(inst["settings"], {}))
    return TestClient(create_app(core), base_url="http://inbox.test"), core


def test_updates_panel_settings_and_requests(client):
    c, core = client
    csrf = signed_in(c, core)
    html = c.get("/settings").text
    assert 'id="h-upd"' in html and "The updater is not running" in html and "Update now" in html
    assert 'name="use.updates"' in html  # the registry route is a Network toggle too
    r = c.post("/api/settings/update", json={"enabled": False, "interval_hours": 12, "channel": "stable"},
               headers={"X-CSRF-Token": csrf})
    assert r.status_code == 200 and core.settings.get("update.channel") == "stable"
    assert core.settings.get("update.enabled") is False and core.settings.get("update.interval_hours") == 12
    assert c.post("/api/settings/update", json={"channel": "nightly"}, headers={"X-CSRF-Token": csrf}).status_code == 400
    assert c.post("/api/update/update", headers={"X-CSRF-Token": csrf}).json()["ok"]
    req = json.loads((core.data_dir / "update" / "request.json").read_text())
    assert req["action"] == "update"
    assert c.post("/api/update/reboot", headers={"X-CSRF-Token": csrf}).status_code == 404


def test_panel_and_week_show_state_and_history(client):
    c, core = client
    signed_in(c, core)
    d = core.data_dir / "update"
    d.mkdir(exist_ok=True)
    now = time.time()
    (d / "agent.json").write_text(json.dumps({"heartbeat": now, "state": "idle", "message": "Up to date",
                                              "latest": {"version": __version__, "revision": "abcdef123"},
                                              "last_check": {"at": now, "ok": True, "route": "direct"}}))
    (d / "host.json").write_text(json.dumps({"seen": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))}))
    (d / "journal.jsonl").write_text(
        json.dumps({"at": "2026-11-01T18:03:00Z", "event": "updated", "from": {"version": "0.1.0"},
                    "to": {"version": "0.1.1", "revision": "9f37955aa"}, "reason": "healthy after 19 s"}) + "\n" +
        json.dumps({"at": "2026-11-01T21:15:00Z", "event": "rolled_back", "from": {"version": "0.1.1"},
                    "to": {"version": "0.1.2", "revision": "5d02b7e"}, "reason": "no answer"}) + "\n")
    panel = c.get("/api/update/panel").text
    assert "Up to date" in panel and "Rolled back" in panel and "0.1.0 to 0.1.1 · 9f37955" in panel
    assert panel.index("Rolled back") < panel.index("healthy after 19 s")  # newest first
    assert "Seen just now" in panel and "registry reached directly" in panel
    assert "Updates" in c.get("/week").text and "up to date" in c.get("/week").text
