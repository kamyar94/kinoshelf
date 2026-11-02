"""The Inbox: find settled files, identify them, stage them under _Working, run hooks, mark them ready.

Folders inside each Inbox:
  _Needs review\\  files the app is not sure about (review page)
  _Working\\       files being finished by hooks, already under their final names
  _Ready\\         finished files, waiting for the weekly move into the library
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path, PurePath

from . import filing, media, sidecars
from .clean import is_junk_file, parse_name
from .config import SUBTITLE_EXTS, VIDEO_EXTS
from .db import Db
from .fsops import move, prune_empty_dirs, set_aside, unique
from .hooks import STAGES, payload, run_hook
from .identify import Identifier
from .models import Candidate, LookupError_, Query
from .naming import TemplateError, fields_for, render
from .net import Net
from .paths import PathError, PathMap, pure
from .scoring import decide
from .textnorm import norm

log = logging.getLogger(__name__)

WORKING, READY, REVIEW = "_Working", "_Ready", "_Needs review"
SPECIAL = {WORKING, READY, REVIEW}
ACTIVE = ("settling", "queued", "review", "kept", "working", "ready")

_LANG_WORDS = {
    "english": "en", "eng": "en", "en": "en", "persian": "fa", "farsi": "fa", "fa": "fa", "per": "fa", "fas": "fa",
    "arabic": "ar", "ara": "ar", "ar": "ar", "french": "fr", "fre": "fr", "fr": "fr", "german": "de", "ger": "de",
    "de": "de", "spanish": "es", "spa": "es", "es": "es", "turkish": "tr", "tur": "tr", "tr": "tr",
}


def _guess_lang(name: str) -> str:
    for tok in reversed(re.split(r"[\s._\-\[\]()]+", PurePath(name).stem.lower())):
        if tok in _LANG_WORDS:
            return _LANG_WORDS[tok]
    return "und"


class Engine:
    def __init__(self, settings, db: Db, data_dir: Path, identifier: Identifier | None = None):
        self.settings = settings
        self.db = db
        self.data_dir = data_dir
        self.lock = threading.RLock()
        self._identifier = identifier
        self._rules: filing.Rules | None = None
        self.net: Net | None = None
        self.reload()

    # ---------------------------------------------------------------- setup
    def reload(self) -> None:
        """Rebuild the network client and sources after the settings change."""
        if self.net is not None:
            self.net.close()
        self.net = Net.from_settings(self.settings)
        if self._identifier is None or not getattr(self._identifier, "fixed", False):
            self.identifier = Identifier(self.settings, self.net, self.data_dir)
        else:
            self.identifier = self._identifier

    def rules(self) -> filing.Rules | None:
        """The filing rules when Settings > Filing rules is on (the folder index is kept between calls)."""
        cfg = self.settings.get("filing", {}) or {}
        if not cfg.get("enabled") or not cfg.get("rules"):
            return None
        pm = self.pm()
        if (self._rules is None or self._rules.cfg != cfg
                or [vars(e) for e in self._rules.pm.entries] != [vars(e) for e in pm.entries]):
            self._rules = filing.Rules(cfg, pm)
        return self._rules

    def filing_for(self, it: dict, cand: dict) -> dict | None:
        """Where the rules file this candidate (folder, version, tags, collection), or None with the templates."""
        rules = self.rules()
        if rules is None:
            return None
        try:
            f = rules.resolve(it, cand)
        except filing.FilingError as e:
            raise TemplateError(str(e)) from None
        self.pm().relative_to_root(f.dest)  # raises PathError when outside the mapped folders
        return f.to_dict()

    def pm(self) -> PathMap:
        return PathMap(self.settings.get("paths.map", []))

    def inbox_roots(self) -> list[tuple[str, Path]]:
        pm, out = self.pm(), []
        for shown in self.settings.get("paths.inbox", []):
            try:
                out.append((shown, pm.to_container(shown)))
            except PathError as e:
                log.warning("Inbox %s skipped: %s", shown, e)
        return out

    def _root_of(self, item: dict) -> Path:
        return self.pm().to_container(item["inbox"])

    # ---------------------------------------------------------------- scanning
    def scan(self, now: float | None = None) -> dict:
        """One pass over every Inbox: new files, settled files, junk, files that vanished."""
        now = now or time.time()
        settle = float(self.settings.get("watcher.settle_seconds", 120))
        junk_cfg = self.settings.get("junk", {})
        aside_name = PurePath(self.settings.get("paths.set_aside", "_Set aside").replace("\\", "/")).parts[0]
        counts = {"new": 0, "settled": 0, "junk": 0}
        seen: set[str] = set()
        for shown, root in self.inbox_roots():
            if not root.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(root):
                d = Path(dirpath)
                if d == root:
                    dirnames[:] = [x for x in dirnames if x not in SPECIAL and x != aside_name and not x.startswith(".")]
                else:
                    dirnames[:] = [x for x in dirnames if not x.startswith(".")]
                vts = [x for x in dirnames if x.lower() == "video_ts"]
                if vts and d != root:
                    dirnames[:] = []  # a DVD folder is one item
                    vobs = [p for p in (d / vts[0]).glob("*") if p.suffix.lower() == ".vob"]
                    if vobs:
                        sig = (sum(p.stat().st_size for p in vobs), max(p.stat().st_mtime for p in vobs), "")
                        rel = str(d.relative_to(root) / vts[0] / max(vobs, key=lambda p: p.stat().st_size).name)
                        self._track(d, shown, rel, sig, now, settle, counts, seen, is_dvd=True)
                    continue
                for fn in filenames:
                    p = d / fn
                    ext = p.suffix.lower().lstrip(".")
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    if fn.endswith(".ks-part"):
                        continue
                    if is_junk_file(fn, st.st_size, junk_cfg, VIDEO_EXTS):
                        if junk_cfg.get("auto_set_aside", True) and now - st.st_mtime > settle:
                            self._set_aside_junk(p, root, shown)
                            counts["junk"] += 1
                        continue
                    if ext not in VIDEO_EXTS or ext == "vob":
                        continue
                    try:
                        sig = (st.st_size, st.st_mtime, media.tail_sum(p))
                    except OSError:
                        continue
                    self._track(p, shown, str(p.relative_to(root)), sig, now, settle, counts, seen)
        # Files that disappeared while waiting (moved away by hand).
        for it in self.db.items(["settling", "queued", "review", "kept"]):
            if it["path"] not in seen and not Path(it["path"]).exists():
                self.db.update_item(it["id"], state="gone")
                self.db.event("gone", _title(it), "No longer in the Inbox", item_id=it["id"])
        return counts

    def _track(self, p: Path, shown: str, rel: str, sig: tuple, now: float, settle: float, counts: dict,
               seen: set, is_dvd: bool = False) -> None:
        seen.add(str(p))
        it = self.db.item_by_path(str(p))
        if it is None:
            self.db.add_item(inbox=shown, src_rel=rel, path=str(p), is_dvd=int(is_dvd), size=sig[0], mtime=sig[1],
                             tail=sig[2], seen_at=now, settled_at=now, state="settling",
                             group_key=f"{shown}|{PurePath(rel).parent}")
            counts["new"] += 1
            return
        if it["state"] != "settling":
            return
        if (it["size"], it["mtime"], it["tail"] or "") != sig:
            self.db.update_item(it["id"], size=sig[0], mtime=sig[1], tail=sig[2], settled_at=now)
        elif now - (it["settled_at"] or now) >= settle:
            self.db.update_item(it["id"], state="queued")
            counts["settled"] += 1

    def _set_aside_junk(self, p: Path, root: Path, shown: str) -> None:
        with self.lock:
            try:
                dst = set_aside(p, "junk", self.pm(), self.settings)
            except (OSError, PathError) as e:
                log.warning("Could not set aside %s: %s", p, e)
                return
            self.db.event("junk", p.name, "Site advert or junk file", self._shown(dst))

    def _shown(self, p: Path | str) -> str:
        try:
            return self.pm().to_shown(p)
        except PathError:
            return str(p)

    # ---------------------------------------------------------------- identify
    def main_file(self, it: dict) -> Path:
        p = Path(it["path"])
        if it.get("is_dvd"):
            vobs = [x for x in p.rglob("*") if x.suffix.lower() == ".vob"]
            return max(vobs, key=lambda x: x.stat().st_size) if vobs else p
        return p

    def identify_item(self, item_id: int) -> None:
        it = self.db.item(item_id)
        if not it or it["state"] != "queued":
            return
        parsed = parse_name(it["src_rel"], self.settings.get("junk.release_words", []))
        main = self.main_file(it)
        probe = media.probe(main)
        oshash = None if it["is_dvd"] else _try(lambda: media.oshash(main))
        dur = probe.get("duration_s")
        q = Query(kind=parsed.kind, title=parsed.title, year=parsed.year, season=parsed.season,
                  episode=parsed.episode, duration_min=(dur / 60 if dur and not it["is_dvd"] else None),
                  oshash=oshash, size=probe.get("size"))
        self.db.update_item(item_id, parsed=parsed.to_dict(), probe=probe, oshash=oshash)
        try:
            cands, notes = self.identifier.identify(q) if q.title else ([], [])
        except LookupError_ as e:
            tries = (it["tries"] or 0) + 1
            wait = min(6 * 3600, 900 * 2 ** (tries - 1))
            self.db.update_item(item_id, tries=tries, retry_at=time.time() + wait, notes=[str(e)],
                                reason="Lookups could not be reached; trying again later")
            return
        rules = self.rules()
        if rules is not None and cands:
            # The rules know the library's folders: a copy of something already there waits for the user.
            for c in cands[:3]:
                if c.kind == "series":
                    c.in_library = None  # a new episode of a show in the library is not "already there"
                try:
                    f = rules.resolve(dict(it, parsed=parsed.to_dict(), probe=probe), c.to_dict())
                except (filing.FilingError, PathError) as e:
                    if c is cands[0]:
                        c.checks.append({"key": "filing", "ok": None, "text": str(e)})
                    continue
                if f.hold:
                    c.in_library = c.in_library or f.hold
                    if not any(ck.get("key") == "library" for ck in c.checks):
                        c.checks.append({"key": "library", "ok": None, "text": f.hold})
        confident, reason = decide(cands, q, self.settings.get("rule", {}), parsed.kind)
        if rules is not None and cands and confident:
            try:
                rules.resolve(dict(it, parsed=parsed.to_dict(), probe=probe), cands[0].to_dict())
            except (filing.FilingError, PathError) as e:
                confident, reason = False, f"Filing rules: {e}"
        if it["is_dvd"]:
            confident, reason = False, "DVD folder: pick the title by hand" if cands else "No name to go on"
        play = {"status": "dvd", "text": "DVD folder"} if it["is_dvd"] else media.playable(
            probe, self.settings.get("device", {}))
        self.db.update_item(item_id, candidates=[c.to_dict() for c in cands], confident=int(confident),
                            reason=reason, notes=notes, playable=play, tries=0, retry_at=None,
                            sidecars=[str(s) for s in self.find_sidecars(Path(it["path"]), it)])
        if confident:
            self.choose(item_id, cands[0].to_dict(), by="auto")
        else:
            self.to_review(item_id)

    def find_sidecars(self, main: Path, it: dict) -> list[Path]:
        """Subtitles that belong to this file: same name, or any subtitle in a one-video folder."""
        if it.get("is_dvd"):
            return []
        folder, out = main.parent, []
        for p in folder.iterdir():
            if p != main and p.is_file() and p.name.startswith(main.stem + ".") \
                    and p.suffix.lower().lstrip(".") in SUBTITLE_EXTS:
                out.append(p)
        try:
            root = self._root_of(it)
        except PathError:
            return out
        if folder != root:
            videos = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower().lstrip(".") in VIDEO_EXTS
                      and "sample" not in p.name.lower()]
            if len(videos) == 1:
                out += [p for p in folder.rglob("*") if p.is_file() and p not in out
                        and p.suffix.lower().lstrip(".") in SUBTITLE_EXTS]
                if self.rules() is not None and (self.settings.get("filing.policies", {}) or {}).get("artwork", True):
                    out += [p for p in folder.iterdir() if p.is_file() and p not in out  # pictures that came with it
                            and p.suffix.lower().lstrip(".") in sidecars.IMAGE_EXTS]
        return out

    # ---------------------------------------------------------------- staging
    def _area_path(self, it: dict, area: str, rel: str | None = None) -> Path:
        return self._root_of(it) / area / Path((rel or it["src_rel"]).replace("\\", "/"))

    def _move_with_sidecars(self, it: dict, new_main: Path, rename_sidecars: bool,
                            filed: dict | None = None) -> tuple[Path, list[str]]:
        """Move the main file (or DVD folder) and its sidecars; returns new paths."""
        old_main = Path(it["path"])
        new_main = unique(new_main) if not it.get("is_dvd") else new_main
        move(old_main, new_main)
        if rename_sidecars and filed is not None:
            return new_main, self._move_sidecars_named(it, old_main, new_main, filed)
        new_sides = []
        for s in it.get("sidecars") or []:
            sp = Path(s)
            if not sp.exists():
                continue
            if rename_sidecars:
                suffix = sp.name[len(old_main.stem):] if sp.name.startswith(old_main.stem + ".") \
                    else f".{_guess_lang(sp.name)}{sp.suffix.lower()}"
                target = new_main.parent / (new_main.stem + suffix)
            else:
                target = new_main.parent / sp.name
            target = unique(target)
            move(sp, target)
            new_sides.append(str(target))
        return new_main, new_sides

    def _tidy_after(self, old_main: Path, it: dict) -> None:
        """After a file leaves a folder: junk goes aside, and an emptied folder is set aside too."""
        root = self._root_of(it)
        folder = old_main.parent
        for area in SPECIAL:
            if (root / area) in folder.parents or folder == root / area:
                prune_empty_dirs(folder, root / area)
                return
        if not self.settings.get("junk.auto_set_aside", True):
            return  # junk and emptied folders stay where they are
        while folder != root and root in folder.parents:
            if not folder.exists():
                folder = folder.parent
                continue
            files = [p for p in folder.rglob("*") if p.is_file()]
            for p in files:
                if is_junk_file(p.name, p.stat().st_size, self.settings.get("junk", {}), VIDEO_EXTS):
                    self._set_aside_junk(p, root, it["inbox"])
            if any(p.is_file() for p in folder.rglob("*")):
                return
            try:
                set_aside(folder, "empty", self.pm(), self.settings)
            except (OSError, PathError):
                return
            folder = folder.parent

    def to_review(self, item_id: int) -> None:
        with self.lock:
            it = self.db.item(item_id)
            old = Path(it["path"])
            target = self._area_path(it, REVIEW)
            if old != target:
                try:
                    new_main, sides = self._move_with_sidecars(it, target, rename_sidecars=False)
                except OSError as e:
                    log.warning("Could not move %s to review: %s", old, e)
                    new_main, sides = old, it.get("sidecars") or []
                else:
                    self._tidy_after(old, it)
            else:
                new_main, sides = old, it.get("sidecars") or []
            self.db.update_item(item_id, path=str(new_main), sidecars=sides, state="review", staged=None)
        self.make_frames(item_id)

    def make_frames(self, item_id: int) -> list[Path]:
        it = self.db.item(item_id)
        if not it:
            return []
        dur = (it.get("probe") or {}).get("duration_s")
        return media.frames(self.main_file(it), self.data_dir / "frames" / str(item_id), dur)

    def _move_sidecars_named(self, it: dict, old_main: Path, new_main: Path, filed: dict) -> list[str]:
        """Sidecars named as sidecars.py describes; pictures only for a new film folder."""
        try:
            from PIL import Image  # only needed here; without it pictures keep their place
        except ImportError:
            Image = None

        folder = new_main if it.get("is_dvd") else new_main.parent
        taken = {p.name.lower() for p in folder.iterdir()} if folder.is_dir() else set()
        other = folder / "Other Files"
        if other.is_dir():
            taken |= {("Other Files\\" + p.name).lower() for p in other.iterdir()}
        have_art = {re.sub(r"\.(jpe?g|png)$", "", n) for n in taken}
        out: list[str] = []
        stem = filed.get("stem") or new_main.stem
        for s in it.get("sidecars") or []:
            sp = Path(s)
            if not sp.exists():
                continue
            if sp.suffix.lower().lstrip(".") in sidecars.IMAGE_EXTS:
                if filed.get("kind") != "movie" or not filed.get("folder_new") or it.get("is_dvd"):
                    continue  # pictures stay in the Inbox for the user
                try:
                    with Image.open(sp) as im:
                        size = im.size
                except (OSError, AttributeError):
                    size = None
                name = sidecars.artwork_name(sp, size, have_art)
                if not name:
                    continue
                have_art.add(name.rsplit(".", 1)[0])
                target = folder / name
            else:
                got = sidecars.sidecar_name(sp, old_main.stem, stem, taken)
                if got is None:
                    continue
                rel, picture = got
                target = folder / Path(rel.replace("\\", "/"))
                if picture:
                    marker = target.parent / ".ignore"
                    if not marker.exists():
                        marker.parent.mkdir(parents=True, exist_ok=True)
                        marker.write_bytes(b"")
                        out.append(str(marker))
            target = unique(target)
            move(sp, target)
            out.append(str(target))
        return out

    def dest_for(self, it: dict, cand: dict) -> str:
        f = self.filing_for(it, cand)
        if f is not None:
            return f["dest"]
        kind = "episode" if cand.get("kind") == "series" else "movie"
        template = self.settings.get(f"naming.{kind}", "")
        main = self.main_file(it)
        ext = "" if it.get("is_dvd") else main.suffix
        dest = render(template, fields_for(kind, cand, it.get("parsed") or {}, ext, it.get("probe")))
        self.pm().relative_to_root(dest)  # raises PathError when outside the mapped folders
        return dest

    def staged_target(self, it: dict, dest: str, area: str) -> Path:
        entry, rel = self.pm().relative_to_root(dest)
        p = self._root_of(it) / area / entry.label / Path(*rel)
        return p.parent if it.get("is_dvd") else p

    def choose(self, item_id: int, cand: dict, by: str, replace: bool = False) -> str | None:
        """File this item as cand: work out the final name and stage it under _Working."""
        with self.lock:
            it = self.db.item(item_id)
            try:
                f = self.filing_for(it, cand)
                dest = f["dest"] if f else self.dest_for(it, cand)
            except (TemplateError, PathError) as e:
                self.db.update_item(item_id, chosen_cand=cand, reason=f"Naming: {e}")
                if it["state"] not in ("review", "kept"):
                    self.to_review(item_id)
                return str(e)
            if f is not None:
                cand = dict(cand, filing=f)  # hooks (folder icons, details files) read where and why
            target = self.staged_target(it, dest, WORKING)
            old = Path(it["path"])
            try:
                new_main, sides = self._move_with_sidecars(it, target, rename_sidecars=True, filed=f)
            except OSError as e:
                return f"Could not move the file: {e}"
            self._tidy_after(old, it)
            self.db.update_item(item_id, chosen_cand=cand, dest=dest, decided_by=by, decided_at=time.time(),
                                state="working", path=str(new_main), staged=str(new_main), sidecars=sides,
                                hooks={}, replace_existing=int(replace), tries=0, retry_at=None)
            if by != "auto":
                self.db.event("approved", Candidate.from_dict(cand).label, "Approved on the review page",
                              dest, item_id=item_id)
        return None

    # ---------------------------------------------------------------- hooks
    def run_hooks(self, item_id: int) -> str:
        """Run the next hooks of a working item. Returns ready, pending, failed or skipped."""
        it = self.db.item(item_id)
        if not it or it["state"] != "working":
            return "skipped"
        state = dict(it.get("hooks") or {})
        now = time.time()
        for stage in STAGES:
            for i, hook in enumerate(self.settings.get(f"hooks.{stage}", []) or []):
                if not hook.get("enabled"):
                    continue
                key = f"{stage}:{i}:{hook.get('name', '')}"
                st = state.get(key, {})
                if st.get("status") in ("done", "skipped"):
                    continue
                if st.get("retry_at", 0) > now and hook.get("kind") != "folder":
                    return "pending"
                if it.get("is_dvd") and stage in ("playable", "subtitles"):
                    state[key] = {"status": "skipped", "message": "DVD folder"}
                    continue
                body = payload(stage, it, self.pm(), self.settings, hook.get("options", {}))
                res = run_hook(hook, body, self.pm(), self.net, self.data_dir)
                with self.lock:
                    it = self.db.item(item_id)
                    if not it or it["state"] != "working":
                        return "skipped"
                    if res["status"] in ("done", "skipped"):
                        it = self._apply_result(it, res)
                        state[key] = {"status": res["status"], "message": res.get("message", ""), "at": now}
                        self.db.update_item(item_id, hooks=state)
                        continue
                    if res["status"] == "pending":
                        wait = float(res.get("retry_after_minutes") or 10) * 60
                        state[key] = {"status": "pending", "message": res.get("message", ""), "retry_at": now + wait}
                        self.db.update_item(item_id, hooks=state)
                        return "pending"
                    tries = int(st.get("tries", 0)) + 1
                    state[key] = {"status": "failed", "message": res.get("message", ""), "tries": tries,
                                  "retry_at": now + 1800}
                    if tries >= 3:
                        self.db.update_item(item_id, hooks=state,
                                            reason=f"{hook.get('name', stage)} failed: {res.get('message', '')}")
                        self.to_review(item_id)
                        return "failed"
                    self.db.update_item(item_id, hooks=state)
                    return "failed"
        self.promote(item_id)
        return "ready"

    def _apply_result(self, it: dict, res: dict) -> dict:
        pm = self.pm()

        def cpath(p: str) -> Path:
            try:
                return pm.to_container(p) if ":" in p[:3] or p.startswith("\\\\") else Path(p)
            except PathError:
                return Path(p)

        fields: dict = {}
        for r in res.get("replaced") or []:
            rp = cpath(r)
            if rp.exists():
                try:
                    set_aside(rp, "replaced", pm, self.settings)
                except (OSError, PathError) as e:
                    log.warning("Could not set aside %s: %s", rp, e)
        if res.get("file"):
            nf = cpath(res["file"])
            if nf.exists() and str(nf) != it["staged"]:
                fields.update(path=str(nf), staged=str(nf))
                if it.get("dest"):
                    fields["dest"] = str(pure(it["dest"]).with_suffix(nf.suffix))
        sides = list(it.get("sidecars") or [])
        for f in res.get("files") or []:
            fp = str(cpath(f))
            if fp not in sides and fp != (fields.get("staged") or it["staged"]):
                sides.append(fp)
        fields["sidecars"] = sides
        if res.get("playable"):
            fields["playable"] = res["playable"]
        self.db.update_item(it["id"], **fields)
        return self.db.item(it["id"])

    def promote(self, item_id: int) -> None:
        """_Working -> _Ready, same names."""
        with self.lock:
            it = self.db.item(item_id)
            root = self._root_of(it)
            moved_main = None
            new_sides = []
            for p in [it["staged"]] + list(it.get("sidecars") or []):
                src = Path(p)
                if not src.exists():
                    continue
                try:
                    rel = src.relative_to(root / WORKING)
                except ValueError:
                    new_sides.append(p)
                    continue
                dst = root / READY / rel
                if dst.exists():
                    if p != it["staged"]:
                        # A shared file (tvshow.nfo, the series poster) another episode already brought.
                        set_aside(src, "replaced", self.pm(), self.settings)
                        continue
                    dst = unique(dst)
                move(src, dst)
                prune_empty_dirs(src.parent, root / WORKING)
                if p == it["staged"]:
                    moved_main = dst
                else:
                    new_sides.append(str(dst))
            if moved_main is None:
                self.db.update_item(item_id, state="gone", reason="The staged file is missing")
                return
            self.db.update_item(item_id, state="ready", staged=str(moved_main), path=str(moved_main),
                                sidecars=new_sides)
            self.db.event("ready", _title(it), "Ready for the weekly move", it.get("dest") or "", item_id=item_id)

    # ---------------------------------------------------------------- review actions
    def siblings(self, it: dict) -> list[dict]:
        """Other waiting episodes of the same show from the same subfolder (never the Inbox root)."""
        if str(PurePath(it["src_rel"].replace("\\", "/")).parent) in (".", ""):
            return []
        title = norm((it.get("parsed") or {}).get("title"))
        return [x for x in self.db.items(["review", "kept"])
                if x["id"] != it["id"] and x["group_key"] == it["group_key"]
                and (x.get("parsed") or {}).get("kind") == "episode"
                and norm((x.get("parsed") or {}).get("title")) == title]

    def approve(self, item_id: int, cand: dict, replace: bool = False, group: bool = False) -> str | None:
        it = self.db.item(item_id)
        if not it or it["state"] not in ("review", "kept"):
            return "This item is no longer waiting for review"
        sibs = self.siblings(it) if group and cand.get("kind") == "series" else []
        err = self.choose(item_id, cand, by="user", replace=replace)
        if err:
            return err
        for sib in sibs:
            p = sib.get("parsed") or {}
            ids = cand.get("ids") or {}
            q = Query(kind="episode", title=cand.get("title", ""), season=p.get("season"), episode=p.get("episode"),
                      duration_min=((sib.get("probe") or {}).get("duration_s") or 0) / 60 or None,
                      tmdb_id=ids.get("tmdb"), tmdb_kind="tv" if ids.get("tmdb") else None,
                      imdb_id=None if ids.get("tmdb") else ids.get("imdb"),
                      tvmaze_id=ids.get("tvmaze") if not ids.get("tmdb") and not ids.get("imdb") else None)
            try:
                cands, _ = self.identifier.identify(q)
            except LookupError_:
                continue
            match = next((c for c in cands if c.kind == "series" and (c.ids.get("tmdb") == ids.get("tmdb")
                          or c.ids.get("imdb") == ids.get("imdb"))), None)
            if match is not None:
                self.db.update_item(sib["id"], candidates=[c.to_dict() for c in cands])
                self.choose(sib["id"], match.to_dict(), by="user-group")
        return None

    def keep(self, item_id: int) -> None:
        self.db.update_item(item_id, state="kept")

    def junk(self, item_id: int) -> str | None:
        with self.lock:
            it = self.db.item(item_id)
            if not it or it["state"] not in ("review", "kept"):
                return "This item is no longer waiting for review"
            pm, place = self.pm(), ""
            for p in [it["path"]] + list(it.get("sidecars") or []):
                if Path(p).exists():
                    try:
                        dst = set_aside(Path(p), "junk", pm, self.settings)
                        place = place or self._shown(dst)
                    except (OSError, PathError) as e:
                        return f"Could not set it aside: {e}"
            self._tidy_after(Path(it["path"]), it)
            self.db.update_item(item_id, state="junk")
            self.db.event("junk", Path(it["src_rel"]).name, "Set aside as junk on the review page", place,
                          item_id=item_id)
        return None

    def search(self, item_id: int, text: str, only: str | None = None) -> tuple[list[Candidate], list[str]]:
        """Search box on the review page: free text, or a pasted IMDb / TMDB / TVmaze link."""
        it = self.db.item(item_id) or {}
        parsed = it.get("parsed") or {}
        dur = (it.get("probe") or {}).get("duration_s")
        q = Query(kind=parsed.get("kind", "unknown"), title=text.strip(), season=parsed.get("season"),
                  episode=parsed.get("episode"), duration_min=(dur / 60 if dur and not it.get("is_dvd") else None))
        if m := re.search(r"(tt\d{6,10})", text):
            q.imdb_id, q.title = m.group(1), ""
        elif m := re.search(r"themoviedb\.org/(movie|tv)/(\d+)", text):
            q.tmdb_kind, q.tmdb_id, q.title = m.group(1), m.group(2), ""
        elif m := re.search(r"tvmaze\.com/shows/(\d+)", text):
            q.tvmaze_id, q.title = m.group(1), ""
        else:
            if m := re.search(r"\(?\b(19\d\d|20\d\d)\b\)?\s*$", q.title):
                q.year = int(m.group(1))
                q.title = q.title[: m.start()].strip()
            if q.kind == "unknown" and (q.season or re.search(r"\bS\d{1,2}E\d{1,3}\b", text, re.I)):
                q.kind = "episode"
        cands, notes = self.identifier.identify(q, only=only)
        if cands:
            self.db.update_item(item_id, candidates=[c.to_dict() for c in cands])
        return cands, notes

    # ---------------------------------------------------------------- worker step
    def step(self) -> dict:
        """One worker pass: scan, identify settled files, advance hooks."""
        if not self.settings.get("watcher.enabled", True):
            return {"paused": True}  # watching is off: nothing is picked up, renamed or moved
        counts = self.scan()
        now = time.time()
        for it in self.db.items(["queued"]):
            if (it["retry_at"] or 0) <= now:
                try:
                    self.identify_item(it["id"])
                except Exception:  # one bad file must not stop the rest
                    log.exception("Identify failed for item %s", it["id"])
                    self.db.update_item(it["id"], retry_at=now + 3600, reason="Unexpected error; see the log")
        for it in self.db.items(["working"]):
            try:
                self.run_hooks(it["id"])
            except Exception:
                log.exception("Hooks failed for item %s", it["id"])
        return counts


def _try(fn):
    try:
        return fn()
    except OSError:
        return None


def _title(it: dict) -> str:
    c = it.get("chosen_cand") or {}
    if c:
        lab = Candidate.from_dict(c).label
        if c.get("kind") == "series":
            p = it.get("parsed") or {}
            if p.get("season") and p.get("episode"):
                lab = f"{c.get('title')} S{p['season']:02d}E{p['episode']:02d}"
        return lab
    return (it.get("parsed") or {}).get("clean") or Path(it.get("src_rel", "")).name
