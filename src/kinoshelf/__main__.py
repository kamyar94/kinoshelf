"""Command line: serve the app, back up, run or undo a move, reset the password."""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import shutil
import sqlite3
import sys
import tarfile
import time
from pathlib import Path

from .config import data_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kinoshelf")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the web app and the background worker")
    s.add_argument("--host", default=os.environ.get("KS_HOST", "0.0.0.0"))
    # The port inside the container stays 8484; KS_PORT in .env is the host side only.
    s.add_argument("--port", type=int, default=int(os.environ.get("KS_LISTEN_PORT", "8484")))
    b = sub.add_parser("backup", help="copy the database, settings and journals into one archive")
    b.add_argument("--to", default=None, help="folder for the archive (default: <data>/backups)")
    m = sub.add_parser("move", help="run the weekly move now")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--for-real", action="store_true", help="move even when dry run is on in Settings")
    u = sub.add_parser("undo", help="put back every file of a run")
    u.add_argument("run_id")
    p = sub.add_parser("set-password", help="set a new password (and create the account if none exists)")
    p.add_argument("username")
    sub.add_parser("imdb-update", help="download and index the IMDb datasets now")
    c = sub.add_parser("check-config", help="can this version carry out these settings? (exit 1 when not)")
    c.add_argument("path", nargs="?", default="-", help="a config.yml, or - for standard input")
    a = sub.add_parser("update-agent", help="check for and fetch new images (runs in the updater container)")
    a.add_argument("--once", action="store_true", help="check once and stop")
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("KS_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.cmd == "serve":
        import uvicorn

        from .web import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True,
                    forwarded_allow_ips="*", log_level="info", access_log=False)
        return 0
    if args.cmd == "backup":
        print(backup(Path(args.to) if args.to else None))
        return 0
    if args.cmd == "check-config":
        from .confcheck import problems

        text = sys.stdin.read() if args.path == "-" else Path(args.path).read_text(encoding="utf-8")
        found = problems(text)
        for p in found:
            print(p)
        print("Settings this version cannot use" if found else "Settings OK")
        return 1 if found else 0
    if args.cmd == "update-agent":
        from .config import Settings
        from .updater import Agent

        agent = Agent(data_dir(), Settings(data_dir() / "config.yml"))
        if args.once:
            print(json.dumps(agent.run_once(), indent=1))
        else:
            agent.loop()
        return 0

    from .web import App

    core = App(start_worker=False)
    if args.cmd == "move":
        dry = None if not (args.dry_run or args.for_real) else (args.dry_run and not args.for_real)
        print(json.dumps(core.mover.run(dry_run=dry), indent=1))
    elif args.cmd == "undo":
        from .mover import undo

        print(json.dumps(undo(core.engine, args.run_id), indent=1))
    elif args.cmd == "set-password":
        pw = getpass.getpass("New password: ")
        err = core.auth.set_password(args.username, pw)
        print(err or "Password set.")
        return 1 if err else 0
    elif args.cmd == "imdb-update":
        src = next(x for x in core.engine.identifier.sources["titles"] if x.key == "imdb")
        print(src.update(force=True))
    return 0


def backup(dest: Path | None = None) -> str:
    """Online copy of the database plus settings and journals, as one .tar.gz. API keys are inside:
    keep backups where only you can read them. The proxy's address and sign-in are left out."""
    d = data_dir()
    dest = dest or d / "backups"
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    work = dest / f".tmp-{stamp}"
    work.mkdir()
    try:
        src = sqlite3.connect(str(d / "kinoshelf.sqlite"))
        out = sqlite3.connect(str(work / "kinoshelf.sqlite"))
        with out:
            src.backup(out)
        src.close()
        out.close()
        if (d / "config.yml").exists():
            copy_settings_without_proxy(d / "config.yml", work / "config.yml")
        if (d / "journal").exists():
            shutil.copytree(d / "journal", work / "journal")
        archive = dest / f"kinoshelf-{stamp}.tar.gz"
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(work, arcname=f"kinoshelf-{stamp}")
        try:
            os.chmod(archive, 0o600)
        except OSError:
            pass
        return str(archive)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def copy_settings_without_proxy(src: Path, dest: Path) -> None:
    """config.yml with the proxy's address, username and password blanked (set them again after a
    restore). The route sidecar's server details were never in it."""
    import yaml

    raw = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    net = raw.get("network") if isinstance(raw, dict) else None
    if isinstance(net, dict):
        if isinstance(net.get("proxy"), dict):
            for k in ("address", "username", "password"):
                if k in net["proxy"]:
                    net["proxy"][k] = ""
        elif isinstance(net.get("proxy"), str):
            net["proxy"] = ""
    dest.write_text("# Kinoshelf settings from a backup. The proxy's address and sign-in are left out.\n"
                    + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
