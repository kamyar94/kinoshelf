"""The web app: Review, This week, Settings, sign-in."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import __version__, netcheck
from .auth import Auth, CloudflareAccess
from .config import DEFAULTS, Settings, data_dir
from .db import Db
from .identify import Identifier
from .models import Candidate, LookupError_
from .mover import DAYS, Mover, next_due, undo
from .naming import TOKENS, TemplateError, fields_for, render
from .net import DEFAULT_USE, SOURCE_NAMES, SOURCES, check_address, parse_proxy_url
from .paths import PathError, PathMap, pure
from .pipeline import Engine
from . import filing, playback, updater
from .worker import Worker

log = logging.getLogger(__name__)
HERE = Path(__file__).parent / "web"
COOKIE = "ks_session"
POSTER_HOSTS = {"image.tmdb.org", "static.tvmaze.com", "m.media-amazon.com", "ia.media-imdb.com"}
DEFAULT_PROXY = DEFAULTS["network"]["proxy"]
SECTIONS = ["paths", "naming", "rule", "schedule", "subtitles", "sources", "network", "device", "playback", "hooks",
            "junk", "server", "watcher", "update"]


class App:
    """Everything one instance needs, built once at start."""

    def __init__(self, ddir: Path | None = None, start_worker: bool = True, identifier: Identifier | None = None):
        self.data_dir = ddir or data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.settings = Settings(self.data_dir / "config.yml")
        if not (self.data_dir / "config.yml").exists():
            self.settings.save()
        self.db = Db(self.data_dir / "kinoshelf.sqlite")
        self.auth = Auth(self.db)
        self.cf = CloudflareAccess()
        self.engine = Engine(self.settings, self.db, self.data_dir, identifier=identifier)
        self.mover = Mover(self.engine)
        self.worker = Worker(self.engine, self.mover)
        if start_worker:
            self.worker.start()


def create_app(core: App | None = None) -> FastAPI:
    core = core or App()
    app = FastAPI(title="Kinoshelf", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.core = core
    tpl = Jinja2Templates(directory=str(HERE / "templates"))
    tpl.env.globals.update(version=__version__)
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    # ------------------------------------------------------------ middleware
    @app.middleware("http")
    async def guard(request: Request, call_next):
        why = core.cf.check(request.headers)
        if why:
            return _plain(403, why)
        path = request.url.path
        open_paths = path.startswith("/static/") or path in ("/login", "/setup", "/api/health", "/licence")
        sess = core.auth.session(request.cookies.get(COOKIE))
        request.state.session = sess
        if not open_paths and sess is None:
            if path.startswith("/api/") or request.method != "GET":
                return JSONResponse({"error": "Sign in first"}, status_code=401)
            return RedirectResponse("/setup" if not core.auth.has_users() else "/login", status_code=303)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != request.headers.get("host"):
                return _plain(403, "Request from another site refused.")
            if not open_paths and request.headers.get("x-csrf-token") != (sess or {}).get("csrf"):
                return JSONResponse({"error": "Page expired; reload it"}, status_code=403)
        resp = await call_next(request)
        resp.headers.update({
            "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                       "script-src 'self'; font-src 'self'; frame-ancestors 'none'; "
                                       "base-uri 'none'; form-action 'self'",
            "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "same-origin",
            "X-Frame-Options": "DENY",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Cache-Control": resp.headers.get("Cache-Control", "no-store"),
        })
        return resp

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        sess = request.state.session or {}
        return tpl.TemplateResponse(request, name, {"csrf": sess.get("csrf", ""), "user": sess.get("username"),
                                                     "nav": name, **ctx})

    def secure(request: Request) -> bool:
        return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"

    # ------------------------------------------------------------ sign-in
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "revision": os.environ.get("KS_REVISION", ""),
                "worker": core.worker.is_alive()}

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request):
        if core.auth.has_users():
            return RedirectResponse("/login", status_code=303)
        return page(request, "setup.html", error=None)

    @app.post("/setup")
    async def setup_post(request: Request):
        f = await request.form()
        err = core.auth.create_first_user(str(f.get("code", "")), str(f.get("username", "")),
                                          str(f.get("password", "")))
        if err:
            return page(request, "setup.html", error=err)
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request):
        if not core.auth.has_users():
            return RedirectResponse("/setup", status_code=303)
        return page(request, "login.html", error=None)

    @app.post("/login")
    async def login_post(request: Request):
        f = await request.form()
        ip = request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")
        token, err = core.auth.login(str(f.get("username", "")), str(f.get("password", "")), ip)
        if err:
            return page(request, "login.html", error=err)
        r = RedirectResponse("/review", status_code=303)
        r.set_cookie(COOKIE, token, max_age=30 * 86400, httponly=True, samesite="strict", secure=secure(request))
        return r

    @app.post("/logout")
    def logout(request: Request):
        core.auth.logout(request.cookies.get(COOKIE))
        r = RedirectResponse("/login", status_code=303)
        r.delete_cookie(COOKIE)
        return r

    @app.get("/")
    def root():
        return RedirectResponse("/review", status_code=303)

    @app.get("/licence")
    def licence():
        for p in (Path(__file__).resolve().parents[2] / "LICENSE.md", Path("/app/LICENSE.md")):
            if p.exists():
                return _plain(200, p.read_text(encoding="utf-8"))
        return _plain(200, "KF Source-Available License 1.0. Created by Kamyar Fatemifar "
                           "(https://github.com/kamyar94).")

    # ------------------------------------------------------------ review
    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request, item: int | None = None):
        queue_items = core.db.items(["review"], order="id") + core.db.items(["kept"], order="id")
        min_score = float(core.settings.get("rule.min_score", 85))
        queue = [_queue_row(it, min_score) for it in queue_items]
        cur = None
        if queue_items:
            sel = next((it for it in queue_items if it["id"] == item), queue_items[0])
            cur = _current(core, sel)
        return page(request, "review.html", queue=queue, cur=cur, sources=core.engine.identifier.status(),
                    left=len([q for q in queue if q["state"] == "review"]), total=len(queue),
                    search_sources=[(s.key, s.name) for s in core.engine.identifier.title_sources()])

    @app.post("/api/items/{item_id}/approve")
    async def approve(item_id: int, request: Request):
        body = await request.json()
        it = core.db.item(item_id)
        if not it:
            return JSONResponse({"error": "Not found"}, status_code=404)
        cands = it.get("candidates") or []
        idx = int(body.get("index", 0))
        if not 0 <= idx < len(cands):
            return JSONResponse({"error": "Pick a match first"}, status_code=400)
        err = core.engine.approve(item_id, cands[idx], replace=bool(body.get("replace")),
                                  group=bool(body.get("group")))
        core.worker.wake.set()
        return JSONResponse({"error": err} if err else {"next": _next_url(core, item_id)},
                            status_code=400 if err else 200)

    @app.post("/api/items/{item_id}/keep")
    def keep(item_id: int):
        core.engine.keep(item_id)
        return {"next": _next_url(core, item_id)}

    @app.post("/api/items/{item_id}/junk")
    def junk(item_id: int):
        err = core.engine.junk(item_id)
        return JSONResponse({"error": err} if err else {"next": _next_url(core, item_id)},
                            status_code=400 if err else 200)

    @app.post("/api/items/{item_id}/lookup")
    def lookup_again(item_id: int):
        err = core.engine.lookup_again(item_id)
        if not err:
            core.worker.wake.set()
        return JSONResponse({"error": err} if err else {"next": _next_url(core, item_id)},
                            status_code=400 if err else 200)

    @app.post("/api/items/{item_id}/playback/undo")
    def playback_undo(item_id: int):
        try:
            return core.engine.undo_playback(item_id)
        except (ValueError, OSError, PathError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    @app.get("/api/items/{item_id}/search", response_class=HTMLResponse)
    def search(item_id: int, request: Request, q: str = "", source: str = "all"):
        if not q.strip():
            return HTMLResponse('<p class="empty">Type a name, or paste an IMDb or TMDB link.</p>')
        try:
            cands, notes = core.engine.search(item_id, q, only=None if source == "all" else source)
        except LookupError_ as e:
            return HTMLResponse(f'<p class="empty">The sources could not be reached: {_esc(str(e))}</p>')
        it = core.db.item(item_id)
        return tpl.TemplateResponse(request, "_cards.html", {"cur": _current(core, it), "notes": notes})

    @app.get("/frames/{item_id}/{n}.jpg")
    def frame(item_id: int, n: int):
        p = core.data_dir / "frames" / str(item_id) / f"{n}.jpg"
        if not p.exists():
            core.engine.make_frames(item_id)
        if not p.exists():
            return Response(status_code=404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    @app.get("/poster")
    def poster(u: str):
        host = urlparse(u).hostname or ""
        if urlparse(u).scheme != "https" or host not in POSTER_HOSTS:
            return Response(status_code=400)
        cache = core.data_dir / "posters"
        cache.mkdir(exist_ok=True)
        p = cache / (hashlib.sha1(u.encode()).hexdigest() + ".jpg")
        if not p.exists():
            try:
                core.engine.net.download(u, p.with_suffix(".part"))
                p.with_suffix(".part").replace(p)
            except (LookupError_, OSError):
                return Response(status_code=404)
        return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})

    # ------------------------------------------------------------ week
    @app.get("/week", response_class=HTMLResponse)
    def week(request: Request, panel: str = "moved"):
        since = time.time() - 7 * 86400
        ev = [dict(r) for r in core.db.q("SELECT * FROM events WHERE at>=? ORDER BY at DESC", (since,))]
        sched = core.settings.get("schedule", {})
        nd = next_due(sched)
        when = f"{nd.strftime('%A')} {nd.strftime('%H:%M')}" if sched.get("enabled", True) else "off"
        from .mover import _what

        ready = core.db.items(["ready"])
        moved = [it for it in core.db.items(["moved"], order="moved_at DESC") if (it["moved_at"] or 0) >= since]
        will = "would move" if core.settings.get("rule.dry_run", True) else "moves"
        panels = {
            "moved": ("Moved into the library this week, and ready to move",
                      [{"item": _label(it), "what": _what(it, not _play_view(it)) or "Moved", "where": it.get("dest") or "",
                        "play": _play_view(it)} for it in moved]
                      + [{"item": _label(it), "what": " · ".join(x for x in (f"Ready · {will} {when}",
                                                                            _what(it, not _play_view(it))) if x),
                          "where": it.get("dest") or "", "play": _play_view(it)} for it in ready]),
            "unsure": ("Waiting for you",
                       [{"item": _label(it), "what": it.get("reason") or "", "where": core.engine._shown(it["path"])}
                        for it in core.db.items(["review", "kept"])]),
            "junk": ("Set aside as junk (you decide later)",
                     [{"item": e["title"], "what": e["text"], "where": e["place"]} for e in ev if e["kind"] == "junk"]),
            "working": ("Still working",
                        [{"item": _label(it), "what": _working_text(it), "where": core.engine._shown(it["path"])}
                         for it in core.db.items(["settling", "queued", "working"])]),
        }
        if panel not in panels:
            panel = "moved"
        stats = [{"key": k, "n": len(panels[k][1]), "label": lab} for k, lab in
                 [("moved", "Moved or ready to move"), ("unsure", "Need you"), ("junk", "Set aside as junk"),
                  ("working", "Still working")]]
        runs = [dict(r) for r in core.db.q("SELECT * FROM runs ORDER BY started DESC LIMIT 5")]
        for r in runs:
            r["undone"] = bool(json.loads(r.get("summary") or "{}").get("undone"))
        return page(request, "week.html", stats=stats, panel=panel, title=panels[panel][0], rows=panels[panel][1],
                    when=when, rules=_rules(core), runs=runs, unsure=len(panels["unsure"][1]),
                    dry=core.settings.get("rule.dry_run", True), worker_error=core.worker.last_error,
                    updates=updater.view(core.data_dir, core.settings)["week"],
                    watching=core.settings.get("watcher.enabled", True))

    @app.post("/api/run/move")
    async def run_move(request: Request):
        body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
        res = core.mover.run(dry_run=body.get("dry_run"))
        return res

    @app.post("/api/run/scan")
    def run_scan():
        core.worker.wake.set()
        return {"ok": True}

    @app.post("/api/run/undo/{run_id}")
    def run_undo(run_id: str):
        if not re.fullmatch(r"[0-9]{8}-[0-9]{6}", run_id):
            return JSONResponse({"error": "Unknown run"}, status_code=400)
        try:
            return undo(core.engine, run_id)
        except FileNotFoundError:
            return JSONResponse({"error": "No undo file for that run"}, status_code=404)

    # ------------------------------------------------------------ settings
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request):
        s = core.settings.values
        imdb = next((x for x in core.engine.identifier.sources["titles"] if x.key == "imdb"), None)
        return page(request, "settings.html", s=s, keys=_key_state(s), tokens=TOKENS, days=DAYS,
                    hooks_yaml=yaml.safe_dump(s.get("hooks", {}), allow_unicode=True, sort_keys=False),
                    profiles=_profiles(), dev=playback.device_profile(s.get("device")),
                    pb=playback.options(s.get("playback")), x264_presets=X264_PRESETS,
                    imdb=_try_status(imdb), preview=_previews(core), net=_net_view(s),
                    upd=updater.view(core.data_dir, core.settings))

    @app.post("/api/settings/{section}")
    async def save_section(section: str, request: Request):
        if section not in SECTIONS:
            return JSONResponse({"error": "Unknown section"}, status_code=404)
        body = await request.json()
        try:
            value, note = _validate(core, section, body)
        except (ValueError, TemplateError, PathError, yaml.YAMLError) as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        core.settings.set_section(section, value)
        if section in ("sources", "network"):
            core.engine.reload()
        out = {"ok": True, "note": note, "preview": _previews(core) if section in ("naming", "paths") else None}
        if section == "network":
            out["set"] = _net_view(core.settings.values)["set"]  # which secrets are saved, never what
        return out

    # ------------------------------------------------------------ filing rules
    @app.get("/settings/filing", response_class=HTMLResponse)
    def filing_page(request: Request):
        return page(request, "filing.html", cfg=core.settings.get("filing", {}) or {},
                    policies=filing.POLICIES, preview=_filing_preview(core, core.settings.get("filing", {}) or {}))

    @app.get("/api/filing")
    def filing_get():
        return {"filing": core.settings.get("filing", {}) or {}, "policies": filing.POLICIES}

    @app.post("/api/filing")
    async def filing_save(request: Request):
        try:
            cfg = _filing_clean((await request.json()).get("filing"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        core.settings.set_section("filing", cfg)
        return {"ok": True, "preview": _filing_preview(core, cfg)}

    @app.post("/api/filing/preview")
    async def filing_try(request: Request):
        try:
            cfg = _filing_clean((await request.json()).get("filing"))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return {"preview": _filing_preview(core, cfg)}

    @app.get("/api/filing/export")
    def filing_export():
        text = yaml.safe_dump({"filing": core.settings.get("filing", {}) or {}}, allow_unicode=True, sort_keys=False)
        return Response(text, media_type="application/x-yaml; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="kinoshelf-filing-rules.yml"'})

    @app.post("/api/filing/import")
    async def filing_import(request: Request):
        try:
            data = yaml.safe_load(str((await request.json()).get("yaml") or "")) or {}
            cfg = _filing_clean(data.get("filing", data) if isinstance(data, dict) else None)
        except (ValueError, yaml.YAMLError) as e:
            return JSONResponse({"error": f"Not imported: {e}"}, status_code=400)
        core.settings.set_section("filing", cfg)
        return {"ok": True, "filing": cfg, "preview": _filing_preview(core, cfg)}

    @app.post("/api/filing/preset")
    async def filing_preset(request: Request):
        body = await request.json()
        movie = pure(str(core.settings.get("naming.movie", "") or "X:\\Movies\\x"))
        series = pure(str(core.settings.get("naming.episode", "") or "X:\\Series\\x"))
        try:
            cfg = filing.preset(str(body.get("name")), str(type(movie)(*movie.parts[:2])), str(type(series)(*series.parts[:2])),
                                str(body.get("language") or "").strip().lower())
        except filing.FilingError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        cfg["enabled"] = bool((core.settings.get("filing", {}) or {}).get("enabled"))
        return {"filing": cfg, "preview": _filing_preview(core, cfg)}

    @app.post("/api/update/{action}")
    def update_request(action: str):
        try:
            updater.request(core.data_dir, action)
        except ValueError:
            return JSONResponse({"error": "Unknown request"}, status_code=404)
        return {"ok": True, "note": "Asked the updater. It answers within a minute."
                if action == "check" else "Asked the updater to fetch the newest image; the host step installs it."}

    @app.get("/api/update/panel", response_class=HTMLResponse)
    def update_panel(request: Request):
        return tpl.TemplateResponse(request, "_updates.html", {"upd": updater.view(core.data_dir, core.settings)})

    @app.post("/api/network/test", response_class=HTMLResponse)
    def network_test(request: Request):
        results = netcheck.run(core.engine.net, core.settings)
        return tpl.TemplateResponse(request, "_nettest.html", {
            "results": results, "at": time.strftime("%H:%M"),
            "reached": sum(1 for r in results if r.ok), "tested": sum(1 for r in results if r.ok is not None)})

    @app.post("/api/test/{source}")
    def test_source(source: str):
        ident = core.engine.identifier
        try:
            if source == "jellyfin":
                info = ident.sources["jellyfin"].test()
                return {"ok": True, "note": f"Reached {info.get('name')} {info.get('version')}"}
            if source == "opensubtitles":
                ident.sources["hash"].lookup("8e245d9679d31e12")
                return {"ok": True, "note": "Reached OpenSubtitles"}
            src = next(x for x in ident.sources["titles"] if x.key == source)
            from .models import Query
            res = src.search(Query(kind="movie", title="Interstellar", year=2014))
            return {"ok": True, "note": f"Reached {src.name}: {len(res)} results for a test search"}
        except StopIteration:
            return JSONResponse({"error": "Unknown source"}, status_code=404)
        except LookupError_ as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    @app.post("/api/imdb/update")
    def imdb_update():
        core.db.set_meta("imdb_attempt_at", "0")
        core.worker.wake.set()
        return {"ok": True, "note": "Started. Building the offline copy takes a few minutes."}

    @app.post("/api/account/password")
    async def change_password(request: Request):
        body = await request.json()
        sess = request.state.session
        tok, err = core.auth.login(sess["username"], str(body.get("current", "")),
                                   request.client.host if request.client else "?")
        if err:
            return JSONResponse({"error": "The current password is not right"}, status_code=400)
        core.auth.logout(tok)
        err = core.auth.set_password(sess["username"], str(body.get("new", "")))
        return JSONResponse({"error": err}, status_code=400) if err else {"ok": True, "note": "Changed. Sign in again."}

    @app.get("/shortcut.url")
    def shortcut(target: str = "public"):
        url = core.settings.get("server.public_url" if target == "public" else "server.lan_url", "")
        if not re.match(r"^https?://[^\s\"'<>]+$", url or ""):
            return _plain(400, "Set the address first in Settings, Desktop shortcut.")
        body = f"[InternetShortcut]\r\nURL={url.rstrip('/')}/review\r\n"
        return Response(body, media_type="application/octet-stream",
                        headers={"Content-Disposition": 'attachment; filename="Kinoshelf.url"'})

    return app


# ---------------------------------------------------------------------- view helpers
def _plain(code: int, text: str) -> Response:
    return Response(text, status_code=code, media_type="text/plain; charset=utf-8")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _label(it: dict) -> str:
    from .pipeline import _title
    return _title(it)


def _profiles() -> list[dict]:
    """The device presets for Settings > Playback, with a one-line summary each."""
    out = []
    for key, p in playback.PROFILES.items():
        sub = (f"{', '.join(c.upper() for c in p['containers'][:3])} · "
               f"{', '.join(_CODEC_NAMES.get(v, v) for v in p['video_codecs'])} · up to {p['max_height']}p")
        out.append({"key": key, "label": p["label"], "sub": sub, **{k: p[k] for k in playback.LIST_KEYS},
                    "max_height": p["max_height"], "ten_bit": p["ten_bit"]})
    out.append({"key": "custom", "label": "Custom", "sub": "Your own lists"})
    return out


_CODEC_NAMES = {"h264": "H.264", "hevc": "HEVC", "vp9": "VP9", "av1": "AV1", "mpeg4": "MPEG-4"}


def _play_view(it: dict) -> dict | None:
    """What the playable stage did to an item, for the week page: the headline, why, the checks, where the original
    went and whether it can be put back."""
    rec = (it.get("playable") or {}).get("playback") or {}
    if rec.get("action") not in ("rewrapped", "reencoded", "kept"):
        return None
    tone = "ok" if rec["action"] in ("rewrapped", "reencoded") else "warn"
    result = {"rewrapped": "Rewrapped", "reencoded": "Re-encoded", "kept": "Original kept"}[rec["action"]]
    if rec.get("undone"):
        result, tone = "Original put back", "plain"
    return {"id": it["id"], "headline": rec.get("headline") or "", "why": rec.get("why") or "", "result": result,
            "tone": tone, "checks": rec.get("checks") or [], "facts": rec.get("facts") or [],
            "aside": rec.get("original_aside_shown") or "", "copy_aside": rec.get("copy_aside_shown") or "",
            "undone": bool(rec.get("undone")),
            "can_undo": rec["action"] in ("rewrapped", "reencoded") and bool(rec.get("original_aside"))
            and not rec.get("undone")}


def _working_text(it: dict) -> str:
    if it["state"] == "settling":
        return "Still arriving (copy in progress)"
    if it["state"] == "queued":
        return it.get("reason") or "Waiting to be looked up"
    pend = [v.get("message") for v in (it.get("hooks") or {}).values() if v.get("status") in ("pending", "failed")]
    return pend[0] if pend and pend[0] else "Finishing (subtitles, checks)"


def _queue_row(it: dict, min_score: float) -> dict:
    cands = it.get("candidates") or []
    top = cands[0]["score"] if cands else 0
    if it["state"] == "kept":
        badge, cls = "Kept", "kept"
    else:
        badge, cls = (f"best {top}%" if cands else "no match"), ("good" if top >= min_score else "warn")
    guess = Candidate.from_dict(cands[0]).label if cands else (it.get("parsed") or {}).get("clean") or "Unknown"
    return {"id": it["id"], "guess": guess, "file": Path(it["src_rel"]).name, "reason": it.get("reason") or "",
            "badge": badge, "cls": cls, "state": it["state"]}


def _current(core: App, it: dict) -> dict:
    parsed = it.get("parsed") or {}
    probe = it.get("probe") or {}
    from .media import describe

    facts = [("Type", {"movie": "Film", "episode": "Episode"}.get(parsed.get("kind"), "Unknown"))]
    if parsed.get("kind") == "episode":
        facts.append(("Season / episode", f"{parsed.get('season')} / {parsed.get('episode')}"))
    else:
        facts.append(("Year", parsed.get("year") or "not in the name"))
    facts += list(describe(probe).items())
    dur = probe.get("duration_s")
    cands = []
    for i, c in enumerate(it.get("candidates") or []):
        cc = Candidate.from_dict(c)
        rt = cc.episode_runtime_min or cc.runtime_min
        run = (f"{'about ' if cc.kind == 'series' else ''}{rt:.0f} min" if rt else "length unknown")
        if dur and not it.get("is_dvd"):
            run += f" · file is {dur / 60:.1f} min"
        sub = " · ".join(x for x in [cc.people, "Series" if cc.kind == "series" else "Film"] if x)
        if cc.kind == "series" and cc.season and cc.episode:
            sub += f" · S{cc.season:02d}E{cc.episode:02d}" + (f" {cc.episode_title}" if cc.episode_title else "")
        if cc.original_title and cc.original_title != cc.title:
            sub += f" · original title {cc.original_title}"
        try:
            dest = core.engine.dest_for(it, c)
        except (TemplateError, PathError) as e:
            dest = f"Cannot name it: {e}"
        cands.append({"i": i, "label": cc.label, "sub": sub, "runtime": run, "score": cc.score,
                      "good": cc.score >= float(core.settings.get("rule.min_score", 85)),
                      "sources": cc.sources, "checks": cc.checks, "dest": dest,
                      "poster": cc.poster_url})
    hashes = [ck for c in (it.get("candidates") or []) for ck in c.get("checks", []) if ck.get("key") == "hash"]
    if any(ck.get("ok") for ck in hashes):
        hash_text = "OpenSubtitles file hash: match found"
    elif hashes:
        hash_text = "OpenSubtitles file hash: no match"
    else:
        hash_text = "File hash not checked" if not it.get("is_dvd") else "DVD folder"
    play = it.get("playable") or {}
    extras = _extras(core, it)
    siblings = len(core.engine.siblings(it))
    nframes = 0 if it.get("is_dvd") and not dur else 4
    return {"id": it["id"], "file": it["src_rel"], "clean": parsed.get("clean") or "", "state": it["state"],
            "removed": parsed.get("removed") or [], "facts": facts, "frames": list(range(nframes)) if dur else [],
            "hash": hash_text, "candidates": cands, "tv": play.get("text") or "Device check not run",
            "tv_ok": play.get("status") == "ok", "dest": cands[0]["dest"] if cands else
            "Stays in the Inbox until you pick a match", "extras": extras,
            "is_episode": parsed.get("kind") == "episode", "siblings": siblings,
            "reason": it.get("reason") or "", "notes": it.get("notes") or []}


def _extras(core: App, it: dict) -> list[str]:
    out = []
    hooks = core.settings.get("hooks", {}) or {}
    if any(h.get("enabled") for h in hooks.get("subtitles", []) or []):
        langs = ", ".join(core.settings.get("subtitles.languages", []))
        out.append(f"Subtitles: {langs}")
    if any(h.get("enabled") for h in hooks.get("nfo", []) or []):
        out.append("Details file (NFO) written")
    if any(h.get("enabled") for h in hooks.get("icon", []) or []):
        out.append("Poster or folder icon made")
    play = (it.get("playable") or {}).get("status")
    pb = playback.options(core.settings.get("playback"))
    on = any(h.get("enabled") for h in hooks.get("playable", []) or [])
    if play == "rewrap":
        out.append("Rewrap for the device, checked picture by picture" if on and pb["rewrap"]
                   else "Will not play on the device as it is")
    elif play == "convert":
        out.append("Re-encode for the device, if it passes the quality check" if on and pb["reencode"]
                   else "Will not play on the device as it is")
    return out


def _next_url(core: App, item_id: int) -> str:
    rest = [it["id"] for it in core.db.items(["review"], order="id") if it["id"] != item_id]
    after = [i for i in rest if i > item_id]
    nxt = after[0] if after else (rest[0] if rest else None)
    return f"/review?item={nxt}" if nxt else "/review"


def _rules(core: App) -> list[tuple[str, str, bool]]:
    s = core.settings
    r = s.get("rule", {})
    return [
        ("Films go to", s.get("naming.movie", ""), True),
        ("Episodes go to", s.get("naming.episode", ""), True),
        ("Moves without asking when", f"Best match at least {r.get('min_score')}% and length within "
                                      f"{r.get('max_runtime_diff_pct')}%, {r.get('min_margin')} points ahead", False),
        ("Subtitles", ", ".join(s.get("subtitles.languages", [])) or "none", False),
    ]


FILING_SAMPLES = [  # public titles with made-up release names, for the rules preview
    ({"src_rel": "Inbox/Spirited.Away.2001.1080p.BluRay.mkv", "probe": {"width": 1920, "height": 1040}},
     {"kind": "movie", "title": "Spirited Away", "original_title": "千と千尋の神隠し", "year": 2001,
      "facts": {"source": "TMDB", "genres": ["Animation", "Family"], "language": "ja", "countries": ["JP"],
                "translations": {"ja": "千と千尋の神隠し"}}}),
    ({"src_rel": "Inbox/Seven.Samurai.1954.720p.mkv", "probe": {"width": 1280, "height": 720}},
     {"kind": "movie", "title": "Seven Samurai", "original_title": "七人の侍", "year": 1954,
      "facts": {"source": "TMDB", "genres": ["Drama", "Action"], "language": "ja", "countries": ["JP"],
                "translations": {"ja": "七人の侍"}}}),
    ({"src_rel": "Inbox/March.of.the.Penguins.2005.DVDRip.avi", "probe": {"width": 720, "height": 576}},
     {"kind": "movie", "title": "March of the Penguins", "original_title": "La Marche de l'empereur", "year": 2005,
      "facts": {"source": "TMDB", "genres": ["Documentary"], "language": "fr", "countries": ["FR"]}}),
    ({"src_rel": "Inbox/Interstellar.2014.2160p.UHD.mkv", "probe": {"width": 3840, "height": 1600}},
     {"kind": "movie", "title": "Interstellar", "year": 2014,
      "facts": {"source": "TMDB", "genres": ["Science Fiction", "Drama"], "language": "en", "countries": ["US"],
                "collection": {}}}),
    ({"src_rel": "Inbox/the.office.us.s02e01.mkv", "parsed": {"season": 2, "episodes": [1]}, "probe": {}},
     {"kind": "series", "title": "The Office", "year": 2005, "episode_title": "The Dundies",
      "facts": {"source": "TMDB", "genres": ["Comedy"], "language": "en", "countries": ["US"]}}),
]


def _filing_clean(cfg) -> dict:
    """A rules set as the page sends it, checked; ValueError with every problem."""
    if not isinstance(cfg, dict) or not isinstance(cfg.get("rules", []), list):
        raise ValueError("Filing rules are a list of rules")
    out = {"enabled": bool(cfg.get("enabled")), "rules": cfg.get("rules") or [],
           "policies": cfg.get("policies") or {}}
    problems = filing.validate(out)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return out


def _filing_preview(core: App, cfg: dict) -> list[dict]:
    """Where the sample items and the items now in the Inbox would go with these rules."""
    if not (cfg or {}).get("rules"):
        return []
    rules = filing.Rules(cfg, core.engine.pm())
    rows = []
    inbox = [(it, (it.get("candidates") or [None])[0]) for it in core.db.items(["review", "kept", "working", "ready"])][:8]
    for src, (it, cand) in [("sample", x) for x in FILING_SAMPLES] + [("inbox", x) for x in inbox if x[1]]:
        label = (cand.get("title") or "") + (f" ({cand['year']})" if cand.get("year") and cand.get("kind") != "series"
                                               else "")
        if cand.get("kind") == "series":
            p = it.get("parsed") or {}
            label += f" S{int(p.get('season') or 0):02d}E{int((p.get('episodes') or [0])[0]):02d}"
        try:
            f = rules.resolve(it, cand)
            rows.append({"item": label, "from": src, "rule": f.rule, "dest": f.dest, "hold": f.hold, "ok": True})
        except (filing.FilingError, PathError) as e:
            rows.append({"item": label, "from": src, "rule": "", "dest": str(e), "hold": "", "ok": False})
    return rows


SOURCE_HINTS = {"tmdb": "titles and posters", "imdb": "the offline list download", "tvmaze": "series and posters",
                "omdb": "second opinion", "opensubtitles": "file hash",
                "jellyfin": "a local address always goes direct", "updates": "new versions, for the updater"}
SECRET_FIELDS = ("address", "username", "password")


def _net_view(s: dict) -> dict:
    """What the Network panel shows. Secrets appear only as set or not set, never as text."""
    n = s.get("network") or {}
    p = n.get("proxy") if isinstance(n.get("proxy"), dict) else {}
    use = dict(DEFAULT_USE, **{k: bool(v) for k, v in (n.get("use") or {}).items() if k in DEFAULT_USE})
    return {"mode": n.get("mode", "direct"), "kind": p.get("kind", "http"),
            "set": {f: bool(str(p.get(f) or "").strip()) for f in SECRET_FIELDS},
            "use": [{"key": k, "name": SOURCE_NAMES[k], "hint": SOURCE_HINTS[k], "on": use[k]} for k in SOURCES],
            "timeout": n.get("timeout_seconds", 15)}


def _key_state(s: dict) -> dict:
    out = {}
    for name, cfg in (s.get("sources") or {}).items():
        out[name] = bool(cfg.get("api_key"))
    return out


def _try_status(src) -> dict:
    try:
        return src.status() if src is not None else {}
    except Exception as e:
        return {"error": str(e)}


SAMPLE_MOVIE = {"kind": "movie", "title": "A Separation", "original_title": "Jodaeiye Nader az Simin",
                "year": 2011, "ids": {"imdb": "tt1832382", "tmdb": "60243"}}
SAMPLE_EPISODE = {"kind": "series", "title": "The Office", "year": 2005, "season": 2, "episode": 1,
                  "episode_title": "The Dundies", "ids": {}}


def _previews(core: App) -> dict:
    out = {}
    for kind, cand, parsed in (("movie", SAMPLE_MOVIE, {}),
                               ("episode", SAMPLE_EPISODE, {"season": 2, "episode": 1, "episodes": [1]})):
        try:
            p = render(core.settings.get(f"naming.{kind}", ""), fields_for(kind, cand, parsed, ".mkv", {"height": 1080}))
            try:
                PathMap(core.settings.get("paths.map", [])).relative_to_root(p)
                out[kind] = {"ok": True, "text": p}
            except PathError as e:
                out[kind] = {"ok": False, "text": f"{p}  ({e})"}
        except TemplateError as e:
            out[kind] = {"ok": False, "text": str(e)}
    return out


X264_PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]


def _num(v, lo, hi, name) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not lo <= x <= hi:
        raise ValueError(f"{name} must be between {lo} and {hi}")
    return int(x) if x == int(x) else x


def _lines(v) -> list[str]:
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").splitlines() if x.strip()]


def _validate(core: App, section: str, body: dict):
    s = core.settings
    cur = json.loads(json.dumps(s.get(section, {})))
    note = ""
    if section == "paths":
        rows = [r for r in body.get("map", []) if str(r.get("shown", "")).strip() or str(r.get("container", "")).strip()]
        for r in rows:
            if not str(r.get("shown", "")).strip() or not str(r.get("container", "")).strip().startswith("/"):
                raise ValueError("Each folder needs how it looks to you and a container path starting with /")
        pm = PathMap(rows)
        inbox = _lines(body.get("inbox"))
        missing = []
        for x in inbox:
            pm.relative_to_root(x)
            if not pm.to_container(x).is_dir():
                missing.append(x)
        cur.update(map=[{"shown": r["shown"].strip(), "container": r["container"].strip(),
                         "label": str(r.get("label", "")).strip()} for r in rows],
                   inbox=inbox, set_aside=str(body.get("set_aside") or cur.get("set_aside")).strip())
        gone = [r["shown"] for r in rows if not Path(r["container"].strip()).is_dir()]
        if gone or missing:
            note = "Saved. Not found inside the container yet: " + ", ".join(gone + missing)
        return cur, note
    if section == "naming":
        for kind in ("movie", "episode"):
            t = str(body.get(kind, "")).strip()
            if not t:
                raise ValueError("Both templates are needed")
            render(t, fields_for(kind, SAMPLE_MOVIE if kind == "movie" else SAMPLE_EPISODE,
                                 {"season": 2, "episode": 1, "episodes": [1]}, ".mkv"))
            cur[kind] = t
        return cur, note
    if section == "rule":
        cur.update(min_score=_num(body.get("min_score"), 50, 100, "Best match"),
                   max_runtime_diff_pct=_num(body.get("max_runtime_diff_pct"), 0, 50, "Running time"),
                   min_margin=_num(body.get("min_margin"), 0, 50, "Lead over the next match"),
                   dry_run=bool(body.get("dry_run")))
        return cur, note
    if section == "schedule":
        day = str(body.get("day", "")).lower()
        tm = str(body.get("time", ""))
        if day not in DAYS or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", tm):
            raise ValueError("Pick a day and a time like 03:00")
        cur.update(enabled=bool(body.get("enabled")), day=day, time=tm)
        return cur, note
    if section == "subtitles":
        langs = [x.strip().lower() for x in re.split(r"[,\s]+", str(body.get("languages", ""))) if x.strip()]
        if any(not re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})?", x) for x in langs):
            raise ValueError("Use language codes like fa, en, de")
        cur.update(languages=langs, mark_default=bool(body.get("mark_default")))
        return cur, note
    if section == "sources":
        for name, cfg in (body or {}).items():
            if name not in cur:
                continue
            for k, v in cfg.items():
                if k == "api_key" and not str(v).strip():
                    continue  # blank keeps the saved key
                if k == "enabled":
                    cur[name][k] = bool(v)
                elif k in ("refresh_days",):
                    cur[name][k] = int(_num(v, 1, 365, "Refresh days"))
                elif k == "include_episodes":
                    cur[name][k] = bool(v)
                elif k == "clear_key" and v:
                    cur[name]["api_key"] = ""
                elif k in cur[name] and k != "path_map":
                    cur[name][k] = str(v).strip()
        return cur, note
    if section == "network":
        # Error messages here never repeat what was typed: the values are secrets.
        mode = str(body.get("mode", cur.get("mode", "direct")))
        if mode not in ("direct", "proxy", "route"):
            raise ValueError("Pick direct, a proxy or the route sidecar")
        p = dict(DEFAULT_PROXY, **(cur.get("proxy") if isinstance(cur.get("proxy"), dict) else {}))
        kind = str(body.get("kind", p.get("kind", "http")))
        if kind not in ("http", "socks5"):
            raise ValueError("A proxy is HTTP or SOCKS5")
        p["kind"] = kind
        for f in body.get("clear") or []:
            if f in SECRET_FIELDS:
                p[f] = ""
        addr = str(body.get("address") or "").strip()
        if addr:
            parts = parse_proxy_url(addr) if "://" in addr else {"address": check_address(addr)}
            if parts["address"] != p.get("address") and (p.get("username") or p.get("password")):
                # A sign-in belongs to one server: never send the old one to a new address.
                p["username"] = p["password"] = ""
                note = "Saved. The old username and password were forgotten with the old address."
            p.update({k: v for k, v in parts.items() if v})
        for f in ("username", "password"):
            v = str(body.get(f) or "").strip()
            if v:
                p[f] = v
        use = dict(DEFAULT_USE, **{k: bool(v) for k, v in (cur.get("use") or {}).items() if k in DEFAULT_USE})
        for k, v in (body.get("use") or {}).items():
            if k in use:
                use[k] = bool(v)
        cur.update(mode=mode, proxy=p, use=use,
                   timeout_seconds=_num(body.get("timeout_seconds", cur.get("timeout_seconds", 15)), 3, 120,
                                        "Time limit"))
        cur.setdefault("route_url", "http://route:10809")
        if mode == "proxy" and not p["address"]:
            note = "Saved. Lookups go direct until the proxy's address is set."
        return cur, note
    if section == "device":
        prof = str(body.get("profile") or "custom")
        if prof not in playback.PROFILES and prof != "custom":
            raise ValueError(f"Unknown device profile {prof}")
        cur.update(profile=prof, name=str(body.get("name", "")).strip() or "TV",
                   rewrap_to=(str(body.get("rewrap_to", "mp4")).strip().lower().lstrip(".") or "mp4"))
        if prof == "custom":
            cur.update(containers=[x.lower().lstrip(".") for x in _lines(str(body.get("containers", "")).replace(",", "\n"))],
                       video_codecs=[x.lower() for x in _lines(str(body.get("video_codecs", "")).replace(",", "\n"))],
                       audio_codecs=[x.lower() for x in _lines(str(body.get("audio_codecs", "")).replace(",", "\n"))],
                       max_height=int(_num(body.get("max_height", 2160), 240, 8640, "Largest picture")),
                       ten_bit=bool(body.get("ten_bit", True)))
        else:
            pr = playback.PROFILES[prof]
            cur.update({k: list(pr[k]) for k in playback.LIST_KEYS}, max_height=pr["max_height"], ten_bit=pr["ten_bit"])
        return cur, note
    if section == "playback":
        out = playback.options(cur)
        out.update(rewrap=bool(body.get("rewrap")), reencode=bool(body.get("reencode")))
        for k, lo, hi, label in (("audio_offset_ms", 1, 500, "Sound offset"),
                                 ("duration_tolerance_s", 0.01, 5, "Length difference"),
                                 ("vmaf_mean", 0, 100, "VMAF mean"), ("vmaf_low1", 0, 100, "VMAF 1% low"),
                                 ("vmaf_min", 0, 100, "Lowest stretch"), ("vmaf_window_s", 0, 3600, "Stretch length"),
                                 ("crf_start", 0, 51, "Start CRF"), ("crf_min", 0, 51, "Lowest CRF"),
                                 ("crf_max", 0, 51, "Highest CRF")):
            if k in body:
                out[k] = _num(body[k], lo, hi, label)
        preset = str(body.get("x264_preset", out["x264_preset"]))
        if preset not in X264_PRESETS:
            raise ValueError(f"Unknown x264 speed {preset}")
        out["x264_preset"] = preset
        if not out["crf_min"] <= out["crf_start"] <= out["crf_max"]:
            raise ValueError("The start CRF must lie between the lowest and the highest")
        return out, note
    if section == "hooks":
        data = yaml.safe_load(str(body.get("yaml", ""))) or {}
        if not isinstance(data, dict):
            raise ValueError("Hooks must be a list per stage")
        for stage, hooks in data.items():
            if stage not in ("playable", "subtitles", "nfo", "icon", "after_move") or not isinstance(hooks, list):
                raise ValueError(f"Unknown stage {stage!r}")
            for h in hooks:
                if not isinstance(h, dict) or h.get("kind") not in ("command", "http", "folder"):
                    raise ValueError(f"Each hook in {stage} needs kind: command, http or folder")
        return data, note
    if section == "junk":
        cur.update(release_words=_lines(body.get("release_words")), files=_lines(body.get("files")),
                   sample_max_mb=int(_num(body.get("sample_max_mb", 150), 1, 5000, "Sample size")),
                   auto_set_aside=bool(body.get("auto_set_aside")))
        return cur, note
    if section == "watcher":
        cur.update(enabled=bool(body.get("enabled")),
                   settle_seconds=int(_num(body.get("settle_seconds", 120), 10, 3600, "Wait before picking up")),
                   poll_seconds=int(_num(body.get("poll_seconds", 30), 5, 600, "Look every")))
        return cur, note
    if section == "update":
        channel = str(body.get("channel", cur.get("channel", "latest")))
        if channel not in updater.CHANNEL_TAGS:
            raise ValueError("Pick a channel: every change or releases only")
        cur.update(enabled=bool(body.get("enabled")), channel=channel,
                   interval_minutes=int(_num(body.get("interval_minutes", cur.get("interval_minutes", 30)),
                                             updater.MIN_INTERVAL_MINUTES, 10080, "Check every (minutes)")))
        return cur, note
    if section == "server":
        for k in ("public_url", "lan_url"):
            v = str(body.get(k, "")).strip()
            if v and not re.match(r"^https?://[^\s\"'<>]+$", v):
                raise ValueError("Addresses look like https://inbox.example.net or http://192.0.2.20:8484")
            cur[k] = v
        return cur, note
    raise ValueError("Unknown section")
