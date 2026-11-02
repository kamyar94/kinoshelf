"""Saves the poster as poster.jpg in the film's folder, or in the series folder for a new series."""

from __future__ import annotations

from pathlib import Path

import httpx

from ..net import proxy_from_env
from . import run


def handle(job: dict) -> dict:
    it = job["item"]
    url = it.get("poster_url")
    if not url:
        return {"status": "skipped", "message": "No poster known"}
    f = Path(it["file"])
    folder = f if it.get("is_dvd") else (f.parent.parent if it["kind"] == "episode" else f.parent)
    lib = Path(it["dest_file"]) if it.get("dest_file") else None
    lib_folder = None if lib is None else (lib if it.get("is_dvd") else
                                           (lib.parent.parent if it["kind"] == "episode" else lib.parent))
    names = ("poster.jpg", "folder.jpg", "cover.jpg")
    if any((folder / n).exists() for n in names) or (lib_folder and any((lib_folder / n).exists() for n in names)):
        return {"status": "skipped", "message": "A poster is already there"}
    proxy = proxy_from_env(url)  # the poster's source takes the route Settings > Network gives it
    out = folder / "poster.jpg"
    # A poster is a nice-to-have: when it cannot be fetched (filtered network), the file goes on without it.
    try:
        with httpx.Client(proxy=proxy, trust_env=False, timeout=20, follow_redirects=True) as c:
            r = c.get(url)
    except httpx.HTTPError as e:
        return {"status": "skipped", "message": f"Poster not reachable ({type(e).__name__})"}
    if r.status_code != 200 or not r.content:
        return {"status": "skipped", "message": f"Poster download failed (HTTP {r.status_code})"}
    folder.mkdir(parents=True, exist_ok=True)
    out.write_bytes(r.content)
    return {"status": "done", "message": "Poster saved", "files": [str(out)]}


if __name__ == "__main__":
    run(handle)
