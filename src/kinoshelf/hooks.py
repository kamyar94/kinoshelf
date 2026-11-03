"""Optional steps (subtitles, device check, icon, NFO) run as hooks: a command, an HTTP service, or a
job file in a folder that another program answers. See docs/HOOKS.md for the protocol."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from .models import LookupError_
from .net import Net
from .paths import PathError, PathMap

log = logging.getLogger(__name__)

PROTOCOL = 1
STAGES = ["playable", "subtitles", "nfo", "icon"]  # run in this order before an item is ready
STATUSES = {"done", "skipped", "pending", "failed"}


def payload(stage: str, item: dict, pm: PathMap, settings, options: dict) -> dict:
    """What a hook receives. Paths come in both forms: container (for hooks inside the container)
    and shown (for programs on the host, such as a Windows script answering a folder hook)."""
    cand = item.get("chosen_cand") or {}
    parsed = item.get("parsed") or {}

    def shown(p):
        try:
            return pm.to_shown(p) if p else None
        except PathError:
            return None

    dest_c = None
    if item.get("dest"):
        try:
            dest_c = str(pm.to_container(item["dest"]))
        except PathError:
            pass
    return {
        "protocol": PROTOCOL,
        "stage": stage,
        "item": {
            "id": item["id"],
            "kind": "episode" if cand.get("kind") == "series" else "movie",
            "is_dvd": bool(item.get("is_dvd")),
            "file": item.get("staged"),
            "file_shown": shown(item.get("staged")),
            "sidecars": item.get("sidecars") or [],
            "dest_file": dest_c,
            "dest_shown": item.get("dest"),
            "title": cand.get("title"),
            "original_title": cand.get("original_title"),
            "year": cand.get("year"),
            "season": cand.get("season") or parsed.get("season"),
            "episode": cand.get("episode") or parsed.get("episode"),
            "episodes": parsed.get("episodes") or [],
            "episode_title": cand.get("episode_title"),
            "ids": cand.get("ids") or {},
            "poster_url": cand.get("poster_url"),
            "people": cand.get("people"),
            "probe": item.get("probe") or {},
            "playable": item.get("playable") or {},
            "facts": cand.get("facts") or {},  # record details (TMDB): genres, countries, collection, plot ...
            "filing": cand.get("filing") or {},  # where the filing rules put it: folder, folder_new, collection, tags
        },
        "settings": {
            "subtitle_languages": settings.get("subtitles.languages", []),
            "subtitle_mark_default": settings.get("subtitles.mark_default", True),
            "device": settings.get("device", {}),
            "playback": settings.get("playback", {}),
        },
        "options": options or {},
    }


def _parse_answer(text: str) -> dict:
    """The last line of output that is a JSON object."""
    for line in reversed((text or "").strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                ans = json.loads(line)
                if isinstance(ans, dict):
                    return ans
            except ValueError:
                continue
    return {}


def _norm_answer(ans: dict, fallback: str) -> dict:
    status = ans.get("status")
    if status not in STATUSES:
        return {"status": "failed", "message": fallback or "The hook gave no answer"}
    out = {"status": status, "message": str(ans.get("message") or "")[:500]}
    for k in ("file", "files", "replaced", "retry_after_minutes", "playable"):
        if ans.get(k) is not None:
            out[k] = ans[k]
    return out


def run_command(hook: dict, body: dict, data_dir: Path, extra_env: dict | None = None) -> dict:
    """extra_env: KS_PROXY and KS_PROXY_USE when lookups take a proxy (see Net.hook_env). Command
    hooks run inside the container, so the route reaches them only through their environment."""
    cmd = list(hook.get("command") or [])
    if not cmd:
        return {"status": "failed", "message": "No command set"}
    if cmd[0] in ("python", "python3"):
        cmd[0] = sys.executable
    env = {k: v for k, v in os.environ.items() if k not in ("KS_PROXY", "KS_PROXY_USE")}
    env.update(extra_env or {}, KS_DATA_DIR=str(data_dir), KS_HOOK_STAGE=body["stage"],
               PYTHONIOENCODING="utf-8")
    try:
        p = subprocess.run(cmd, input=json.dumps(body, ensure_ascii=False), capture_output=True, text=True,
                           encoding="utf-8", timeout=int(hook.get("timeout_minutes", 240)) * 60, env=env)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "message": "The hook ran out of time"}
    except OSError as e:
        return {"status": "failed", "message": f"Could not start the hook: {e}"}
    tail = (p.stderr or "").strip().splitlines()[-1:] or [f"exit code {p.returncode}"]
    return _norm_answer(_parse_answer(p.stdout), tail[0][:300])


def run_http(hook: dict, body: dict, net: Net) -> dict:
    """Your own service, normally on the same host or network: reached directly, never by proxy."""
    url = hook.get("url")
    if not url:
        return {"status": "failed", "message": "No URL set"}
    headers = {str(k): str(v) for k, v in (hook.get("headers") or {}).items()}
    try:
        ans = net.post_json(url, body, headers=headers, timeout=float(hook.get("timeout_seconds", 120)))
    except LookupError_ as e:
        return {"status": "pending", "message": f"Service not reachable ({e})", "retry_after_minutes": 30}
    return _norm_answer(ans or {}, "The service gave no answer")


def run_folder(hook: dict, body: dict, pm: PathMap) -> dict:
    """Write <id>-<stage>.job.json; the answer arrives as <id>-<stage>.result.json. Pending until then."""
    folder = hook.get("folder")
    if not folder:
        return {"status": "failed", "message": "No folder set"}
    try:
        base = pm.to_container(folder)
    except PathError:
        base = Path(folder)
    base.mkdir(parents=True, exist_ok=True)
    name = f"{body['item']['id']}-{body['stage']}"
    job, result = base / f"{name}.job.json", base / f"{name}.result.json"
    if result.exists():
        try:
            ans = json.loads(result.read_text(encoding="utf-8-sig"))
        except ValueError:
            return {"status": "failed", "message": f"{result.name} is not valid JSON"}
        done = base / "done"
        done.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for f in (job, result):
            if f.exists():
                os.replace(f, done / f"{stamp}-{f.name}")
        return _norm_answer(ans, "The answer file had no status")
    if not job.exists():
        tmp = job.with_suffix(".tmp")
        tmp.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, job)
    return {"status": "pending", "message": f"Waiting for {result.name}"}


def run_hook(hook: dict, body: dict, pm: PathMap, net: Net, data_dir: Path) -> dict:
    kind = hook.get("kind", "command")
    if kind == "command":
        return run_command(hook, body, data_dir, net.hook_env() if hasattr(net, "hook_env") else None)
    if kind == "http":
        return run_http(hook, body, net)
    if kind == "folder":
        return run_folder(hook, body, pm)
    return {"status": "failed", "message": f"Unknown hook kind {kind!r}"}
