"""Can this version carry out these settings? The update host step asks a new image this before it takes over
(`kinoshelf check-config -` with data/config.yml on stdin), so a version that lacks a feature the settings rely
on (a filing method, a hook, a rule kind) is refused instead of installed."""

from __future__ import annotations

import copy
import importlib.util

import yaml

from .config import DEFAULTS, deep_merge, migrate

HOOK_KINDS = ("command", "http", "folder")


def presets() -> set[str]:
    """Filing methods this version knows ("" is the naming templates)."""
    out = {""}
    try:
        from . import library  # a build that carries an extra filing method
        out.add(getattr(library, "PRESET", ""))
    except ImportError:
        pass
    return out


def problems(text: str) -> list[str]:
    """Plain sentences, one per thing this version cannot do with the settings; empty when it can."""
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        return ["The settings file is not valid YAML"]
    if not isinstance(raw, dict):
        return ["The settings file is not a mapping"]
    values = deep_merge(DEFAULTS, raw)
    migrate(values, copy.deepcopy(raw))
    out = [f"Unknown settings section {k}" for k in raw if k not in DEFAULTS]
    preset = str((values.get("naming") or {}).get("preset", "") or "")
    if preset not in presets():
        out.append(f"The filing method {preset} is not in this version")
    net = values.get("network") or {}
    if net.get("mode", "direct") not in ("direct", "proxy", "route"):
        out.append(f"The network route {net.get('mode')} is not in this version")
    upd = values.get("update") or {}
    if upd.get("channel", "latest") not in ("latest", "stable"):
        out.append(f"The update channel {upd.get('channel')} is not in this version")
    if upd.get("verify", "digest") not in ("digest", "cosign"):
        out.append(f"The update check {upd.get('verify')} is not in this version")
    for stage, hooks in (values.get("hooks") or {}).items():
        for h in hooks or []:
            if not isinstance(h, dict):
                out.append(f"A hook in {stage} is not a mapping")
                continue
            if h.get("kind", "command") not in HOOK_KINDS:
                out.append(f"Hook {h.get('name')}: the kind {h.get('kind')} is not in this version")
                continue
            cmd = h.get("command") or []
            if h.get("enabled") and len(cmd) >= 3 and cmd[1] == "-m" and str(cmd[2]).startswith("kinoshelf."):
                try:
                    found = importlib.util.find_spec(cmd[2]) is not None
                except (ImportError, ValueError):
                    found = False
                if not found:
                    out.append(f"Hook {h.get('name')}: {cmd[2]} is not in this version")
    return out
