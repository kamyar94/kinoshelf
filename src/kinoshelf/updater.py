"""Updates: the agent that runs in the updater container, the decisions it takes, and what the pages show.

How a new version travels (docs/UPDATES.md):
  1. The updater container (`kinoshelf update-agent`, this module) checks the registry on a schedule or when asked,
     along the route Settings > Network gives "Updates". It has no Docker access and cannot start anything.
  2. When the channel's image is newer, it fetches the image by digest, checks every part against its digest
     (and, when set up, its cosign signature) and leaves a `docker load` archive plus data/update/ready.env.
  3. The host step (scripts/update/apply.sh, run by a timer on the host) loads it, keeps the running image as
     :previous, restarts the services, checks that the app answers with the new version, and goes back to the
     previous image when it does not. It writes data/update/journal.jsonl and host.json.
"""

from __future__ import annotations

import calendar
import json
import logging
import os
import re
import shutil
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from . import __version__
from .net import Net, host_of, is_local_host
from .registry import Image, Registry, RegistryError, split_image

log = logging.getLogger(__name__)

CHANNEL_TAGS = {"latest": "latest", "stable": "stable"}
AGENT_STALE_SECONDS = 600
_SAFE = re.compile(r"^[A-Za-z0-9._:+-]*$")


# ---------------------------------------------------------------------- files in data/update
def update_dir(data_dir: Path) -> Path:
    d = Path(data_dir) / "update"
    d.mkdir(parents=True, exist_ok=True)
    return d


def read_json(p: Path) -> dict:
    try:
        v = json.loads(p.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json(p: Path, obj: dict) -> None:
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, p)


def rejected(d: Path) -> set[str]:
    try:
        return {x.strip() for x in (d / "rejected.txt").read_text(encoding="utf-8").splitlines() if x.strip()}
    except OSError:
        return set()


def journal(d: Path, limit: int = 6) -> list[dict]:
    try:
        lines = (d / "journal.jsonl").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
        if len(out) >= limit:
            break
    return out


def request(data_dir: Path, action: str) -> None:
    """The page asks the updater to check now, or to update now (even with updates off)."""
    if action not in ("check", "update"):
        raise ValueError("Unknown request")
    write_json(update_dir(data_dir) / "request.json", {"action": action, "at": time.time()})


# ---------------------------------------------------------------------- decisions
def version_key(v: str) -> tuple:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(x) for x in nums[:4])


def compare_versions(a: str, b: str) -> int:
    ka, kb = version_key(a), version_key(b)
    if not ka or not kb:
        return 0
    return (ka > kb) - (ka < kb)


RETRY_FIRST_SECONDS = 12 * 60  # a check that failed is tried again after 12 min, then 24, 48 ... up to the interval


def retry_wait(cfg: dict, failures: int) -> float:
    """Seconds until the next check: the interval, or after failed checks a back-off that starts at 12 minutes,
    doubles each time and never waits longer than the interval."""
    interval = float(cfg.get("interval_hours", 6) or 6) * 3600
    if failures <= 0:
        return interval
    return min(interval, RETRY_FIRST_SECONDS * 2 ** min(failures - 1, 16))


def due(cfg: dict, last_check_at: float, now: float, requested: str | None, failures: int = 0) -> bool:
    if requested or not last_check_at:
        return True
    return now - float(last_check_at or 0) >= retry_wait(cfg, failures)


def choose(cfg: dict, current: dict, latest: dict | None, bad: set[str], ready: dict,
           requested: str | None) -> tuple[bool, str]:
    """Fetch the latest image or not, and why. Pure: every input is passed in."""
    if not latest or not latest.get("digest"):
        return False, "Nothing checked yet"
    if latest["digest"] in bad:
        return False, "This image was rolled back before; waiting for a newer one"
    if ready.get("digest") == latest["digest"]:
        return False, "Already fetched; waiting for the host step"
    if current.get("digest") and current["digest"] in (latest["digest"], latest.get("manifest_digest")):
        return False, "Up to date"
    if latest.get("revision") and latest.get("revision") == current.get("revision"):
        return False, "Up to date"
    if compare_versions(latest.get("version", ""), current.get("version", "")) < 0 and not cfg.get("allow_downgrade"):
        return False, f"{latest.get('version')} is older than the running {current.get('version')}; not installed"
    if not cfg.get("enabled", True) and requested != "update":
        return False, "A newer image is out; updates are off, so it waits for Update now"
    return True, "Newer image"


# ---------------------------------------------------------------------- the agent
def current_image(d: Path) -> dict:
    host = read_json(d / "host.json").get("current") or {}
    return {"version": __version__, "revision": os.environ.get("KS_REVISION", ""),
            "digest": host.get("digest", "") if host.get("version") == __version__ else ""}


def make_registry(settings, cfg: dict) -> tuple[Registry, Net, str]:
    """A registry client on the route Settings > Network gives "Updates"; plain http only for a local registry."""
    net = Net.from_settings(settings)
    image = str(cfg.get("image") or "ghcr.io/kamyar94/kinoshelf")
    host = split_image(image)[0]
    insecure = bool(cfg.get("insecure_registry")) and is_local_host(host.rsplit(":", 1)[0])
    reg = Registry(image, net.client_for, insecure=insecure)
    probe = f"{reg.scheme}://{host}/v2/"
    route = {"proxy": "through the proxy", "route": "through the sidecar"}.get(net.route, "") \
        if net.via_proxy(probe) else "direct"
    return reg, net, route


def cosign_verify(ref: str, identity: str, env: dict, run: Callable = subprocess.run) -> str:
    """'' when the signature checks out, else why not. Needs the cosign program and Sigstore reachable."""
    exe = shutil.which("cosign")
    if not exe:
        return "Signature check is on, but cosign is not installed for the updater"
    try:
        p = run([exe, "verify", ref, "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
                 "--certificate-identity-regexp", identity], capture_output=True, text=True, timeout=300, env=env)
    except (OSError, subprocess.TimeoutExpired):
        return "The signature check did not finish"
    return "" if p.returncode == 0 else "The image's signature did not check out"


def _safe(v: str) -> str:
    return v if _SAFE.match(v or "") else ""


class Agent:
    def __init__(self, data_dir: Path, settings, registry_factory: Callable = make_registry,
                 clock: Callable[[], float] = time.time, verify: Callable = cosign_verify):
        self.data_dir = Path(data_dir)
        self.settings = settings
        self.registry_factory = registry_factory
        self.clock = clock
        self.verify = verify

    def run_once(self) -> dict:
        d = update_dir(self.data_dir)
        self.settings.load()
        cfg = dict(self.settings.get("update", {}) or {})
        st = read_json(d / "agent.json")
        now = self.clock()
        req_file = d / "request.json"
        requested = read_json(req_file).get("action")
        req_file.unlink(missing_ok=True)
        current = current_image(d)
        st.update(heartbeat=now, current=current, enabled=bool(cfg.get("enabled", True)))
        if not (d / "ready.json").exists():
            # The host step installed (or refused) what was fetched: say what runs now, not "Update ready" or
            # "waiting for the host step".
            latest = st.get("latest") or {}
            if latest.get("revision") and latest.get("revision") == current.get("revision"):
                st.update(state="idle", message="Up to date")
            elif st.get("state") in ("ready", "fetching"):
                st.update(state="idle", message="The fetched image was not installed; see the history")
        last = st.get("last_check") or {}
        failures = int(st.get("failures") or 0) if last and not last.get("ok") else 0
        if not due(cfg, last.get("at", 0), now, requested, failures):
            write_json(d / "agent.json", st)
            return st
        net = None
        try:
            reg, net, route = self.registry_factory(self.settings, cfg)
            img: Image = reg.resolve(CHANNEL_TAGS.get(cfg.get("channel"), "latest"))
            latest = {"version": img.version, "revision": img.revision, "digest": img.digest,
                      "manifest_digest": img.manifest_digest, "created": img.created, "size": img.size}
            st.update(latest=latest, last_check={"at": now, "ok": True, "route": route, "reason": ""}, failures=0)
            go, why = choose(cfg, current, latest, rejected(d), read_json(d / "ready.json"), requested)
            if go and cfg.get("verify") == "cosign":
                env = dict(os.environ)
                proxy = net.proxy_for(f"https://{reg.host}/v2/")
                if proxy:
                    env["HTTPS_PROXY"] = proxy
                why = self.verify(f"{cfg.get('image')}@{img.digest}",
                                  str(cfg.get("cosign_identity") or "^https://github.com/kamyar94/kinoshelf/"), env)
                go = not why
            if go:
                st.update(state="fetching", message=f"Fetching {img.version or 'the new image'} and checking it")
                write_json(d / "agent.json", st)
                self._fetch(d, reg, img)
                st.update(state="ready", message=f"{img.version} is fetched and checked; the host step switches over")
            else:
                st.update(state="idle", message=why)
        except RegistryError as e:
            st.update(state="idle", message=str(e), failures=failures + 1,
                      last_check={"at": now, "ok": False, "reason": str(e), "retry_at": now + retry_wait(cfg, failures + 1),
                                  "route": (st.get("last_check") or {}).get("route", "")})
        except OSError as e:
            st.update(state="idle", message=f"Could not write the update ({type(e).__name__})", failures=failures + 1,
                      last_check={"at": now, "ok": False, "reason": "Could not write the update",
                                  "retry_at": now + retry_wait(cfg, failures + 1)})
        finally:
            if net is not None:
                net.close()
        log.info("Update check: %s", st.get("message"))
        write_json(d / "agent.json", st)
        return st

    def _fetch(self, d: Path, reg: Registry, img: Image) -> None:
        inc = d / "incoming"
        inc.mkdir(exist_ok=True)
        for old in inc.glob("*.tar*"):  # an earlier image that never got applied
            old.unlink(missing_ok=True)
        name = f"kinoshelf-{_safe(img.version) or 'new'}-{img.manifest_digest.split(':')[-1][:12]}.tar"
        sha = reg.fetch(img, inc / name)
        ready = {"version": _safe(img.version), "revision": _safe(img.revision), "digest": _safe(img.digest),
                 "manifest": _safe(img.manifest_digest), "config": _safe(img.config_digest.split(":")[-1]),
                 "layers": _safe(img.layers_sha),
                 "file": name, "sha256": sha, "at": self.clock()}
        write_json(d / "ready.json", ready)
        env = "".join(f"{k.upper()}={v}\n" for k, v in ready.items() if k != "at")
        tmp = d / "ready.env.tmp"
        tmp.write_text(env, encoding="utf-8")
        os.replace(tmp, d / "ready.env")

    def loop(self, every: float = 30.0) -> None:
        while True:
            try:
                self.run_once()
            except Exception:  # keep running; the page shows the last good state
                log.exception("Update check failed")
            time.sleep(every)


# ---------------------------------------------------------------------- what the pages show
def _when(t: float | None) -> str:
    if not t:
        return "never"
    lt = time.localtime(float(t))
    today = time.localtime()
    if (lt.tm_year, lt.tm_yday) == (today.tm_year, today.tm_yday):
        return "Today " + time.strftime("%H:%M", lt)
    return f"{lt.tm_mday} {time.strftime('%b %H:%M', lt)}"


def _ago(t: float | None, now: float) -> str:
    if not t:
        return "never"
    s = max(0, int(now - float(t)))
    if s < 90:
        return "just now"
    if s < 5400:
        return f"{s // 60} min ago"
    if s < 172800:
        return f"{s // 3600} h ago"
    return f"{s // 86400} days ago"


def _iso(t: str) -> float:
    try:
        return float(calendar.timegm(time.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")))
    except (TypeError, ValueError):
        return 0.0


def _label(v: dict | None) -> str:
    v = v or {}
    rev = (v.get("revision") or "")[:7]
    return " · ".join(x for x in (v.get("version") or "", rev) if x) or "unknown"


def view(data_dir: Path, settings, now: float | None = None) -> dict:
    """The Updates panel and the Week page line, from the files the agent and the host step keep."""
    now = time.time() if now is None else now
    d = update_dir(data_dir)
    cfg = settings.get("update", {}) or {}
    agent, host, ready = read_json(d / "agent.json"), read_json(d / "host.json"), read_json(d / "ready.json")
    cur = {"version": __version__, "revision": os.environ.get("KS_REVISION", "")}
    latest = agent.get("latest")
    check = agent.get("last_check") or {}
    host_seen = _iso(str(host.get("seen") or ""))
    hist = []
    for e in journal(d):
        ev = e.get("event")
        frm, to = e.get("from") or {}, e.get("to") or {}
        hist.append({"when": _when(_iso(str(e.get("at") or ""))),
                     "result": {"updated": "Updated", "rolled_back": "Rolled back", "refused": "Refused"}.get(ev, "Failed"),
                     "ok": ev == "updated", "what": f"{frm.get('version') or '?'} to {_label(to)}",
                     "why": str(e.get("reason") or "")[:200]})
    if not agent or now - float(agent.get("heartbeat") or 0) > AGENT_STALE_SECONDS:
        state = ("The updater is not running", False,
                 "Start it with docker compose up -d updater. See docs/UPDATES.md.")
    elif agent.get("state") in ("ready", "fetching"):
        state = ("Update ready" if agent.get("state") == "ready" else "Fetching an update", True,
                 agent.get("message") or "")
    elif check and not check.get("ok"):
        retry = float(check.get("retry_at") or 0)
        again = (f" Trying again in {max(1, int((retry - now) // 60))} min." if retry > now else " Trying again soon.")
        state = ("The last check failed", False, (check.get("reason") or "The registry could not be reached.") + again)
    elif not check:
        state = ("Checking soon", True, "The updater has not checked yet.")
    elif not cfg.get("enabled", True):
        state = ("Updates are off", False, agent.get("message") or "Press Update now to install a newer image.")
    else:  # checked, nothing to fetch: say why when it is more than "up to date"
        msg = agent.get("message") or ""
        state = ("Up to date", True, msg if msg and msg != "Up to date" else
                 "Running the newest image of the channel.")
    if ready and not host_seen:
        state = (state[0], False, state[2] + " The host step has not run yet: set it up as docs/UPDATES.md shows.")
    check_text = "never"
    if check:
        route = {"direct": "directly"}.get(check.get("route") or "direct", check.get("route"))
        check_text = _when(check.get("at")) + (f" · registry reached {route}" if check.get("ok")
                                               else " · " + (check.get("reason") or "failed"))
    facts = [("Running", _label(cur)), ("Latest", _label(latest) if latest else "not checked yet"),
             ("Last check", check_text),
             ("Host step", f"Seen {_ago(host_seen, now)}" if host_seen else "Not seen yet")]
    last_update = next((h for h in hist if h["ok"]), None)
    week = ("On" if cfg.get("enabled", True) else "Off") + " · " + state[0].lower() + \
        f" · running {_label(cur)}" + (f" · checked {_when(check.get('at'))}" if check else "")
    return {"title": state[0], "ok": state[1], "text": state[2], "facts": facts, "history": hist,
            "last_update": last_update, "week": week, "image": cfg.get("image", ""),
            "enabled": bool(cfg.get("enabled", True)), "interval": cfg.get("interval_hours", 6),
            "channel": cfg.get("channel", "latest")}
