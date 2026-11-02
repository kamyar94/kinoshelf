"""Windows folder icon for a new film, show or collection folder placed by the filing rules (docs/FOLDER-ICONS.md).

Writes FolderIcon_<id>.ico and desktop.ini into the new folder while it waits in the Inbox. Folders that are
already in the library are left alone. Windows only shows the icon once desktop.ini is Hidden + System and the
folder is ReadOnly; a Linux container cannot set those, so a small Windows-side step does it after the move
(scripts/windows/Set-FolderIconAttributes.ps1).

Options: {"collections": true} also makes the stacked icon of a new collection folder; {"frames": true} lets
a frame of the video stand in when TMDB has no wide picture (default true).
"""

from __future__ import annotations

from pathlib import Path

from . import run


def _settings():
    from ..config import Settings, data_dir

    return Settings(data_dir() / "config.yml")


def _write_icon(folder: Path, ico_name: str, ico: bytes) -> list[str]:
    folder.mkdir(parents=True, exist_ok=True)
    if any(p.name.lower().startswith("foldericon_") for p in folder.glob("*.ico")) or (folder / "desktop.ini").exists():
        return []
    from ..icons import desktop_ini

    (folder / ico_name).write_bytes(ico)
    (folder / "desktop.ini").write_bytes(desktop_ini(ico_name))
    return [str(folder / ico_name), str(folder / "desktop.ini")]


def handle(job: dict) -> dict:
    it = job["item"]
    opts = job.get("options") or {}
    filing = it.get("filing") or {}
    if not filing:
        return {"status": "skipped", "message": "Folder icons follow the filing rules"}
    from ..config import data_dir
    from ..icons import art as A
    from ..icons import icon_name, label_for, make_icon

    f = Path(it["file"])
    episode = it["kind"] == "episode"
    folder = f.parent.parent if episode else (f if it.get("is_dvd") else f.parent)
    coll = filing.get("collection") or {}
    want_title = bool(filing.get("folder_new"))
    want_coll = bool(coll.get("new")) and opts.get("collections", True) and not episode
    if not want_title and not want_coll:
        return {"status": "skipped", "message": "The folder is already in the library"}
    s = _settings()
    key = s.get("sources.tmdb.api_key", "") if s.get("sources.tmdb.enabled") else ""
    from ..net import proxy_from_env

    proxy = proxy_from_env(A.API)  # TMDB's route from Settings > Network (KS_PROXY, KS_PROXY_USE)
    tm = A.Tmdb(key, proxy=proxy, cache=data_dir() / "cache" / "icon-art")
    made: list[str] = []
    notes: list[str] = []
    try:
        if want_title:
            facts = it.get("facts") or {}
            ids = it.get("ids") or {}
            pic, cleans, kind = None, [], ""
            if ids.get("tmdb"):
                choice = A.choose(tm, "tv" if episode else "movie", ids["tmdb"], facts.get("language") or "")
                if choice:
                    pic, cleans = A.download(tm, choice)
                    kind = choice["type"]
            if pic is None and opts.get("frames", True) and not it.get("is_dvd"):
                pic = A.video_frame(f, (it.get("probe") or {}).get("duration_s"))
                kind = "frame"
            if pic is None:
                notes.append("no wide picture for the title")
            else:
                _png, ico, rep = make_icon(pic, kind, label_for(it.get("title") or "", it.get("original_title")), cleans)
                made += _write_icon(folder, icon_name(ids.get("imdb")), ico)
                notes.append(f"{kind} art, {rep['method']}")
        if want_coll:
            cfolder = folder.parent
            choice = A.collection_art(tm, coll.get("id")) if coll.get("id") else None
            if choice:
                pic, _ = A.download(tm, choice)
                if pic is not None:
                    name = coll.get("name") or cfolder.name
                    short = name[:-len(" Collection")] if name.endswith(" Collection") else name
                    n = int(coll.get("members") or 0) + 1
                    label = f"{short} · {n} FILM{'S' if n != 1 else ''}"
                    _png, ico, _rep = make_icon(pic, choice["type"], label.upper(), stack=2)
                    made += _write_icon(cfolder, icon_name(coll.get("id"), collection=True), ico)
                    notes.append("collection icon")
    finally:
        tm.close()
    if not made:
        return {"status": "skipped", "message": "No folder icon made: " + ("; ".join(notes) or "nothing to do")}
    return {"status": "done", "message": "Folder icon made (" + "; ".join(notes) + ")", "files": made}


if __name__ == "__main__":
    run(handle)
