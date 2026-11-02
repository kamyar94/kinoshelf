"""Writes Kodi/Jellyfin NFO files: <name>.nfo beside a film or episode, and tvshow.nfo for a new series."""

from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from . import run


def _ids(ids: dict) -> str:
    out = []
    for i, key in enumerate(k for k in ("imdb", "tmdb", "tvmaze") if ids.get(k)):
        default = ' default="true"' if i == 0 else ""
        out.append(f'  <uniqueid type="{key}"{default}>{escape(str(ids[key]))}</uniqueid>')
    return "\n".join(out)


def _write(path: Path, xml: str) -> str:
    path.write_text('<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n' + xml + "\n", encoding="utf-8")
    return str(path)


def handle(job: dict) -> dict:
    it = job["item"]
    f = Path(it["file"])
    ids = it.get("ids") or {}
    made = []
    if it.get("is_dvd"):
        target = f / "movie.nfo"
    else:
        target = f.with_suffix(".nfo")
    if it["kind"] == "episode":
        made.append(_write(target, "\n".join([
            "<episodedetails>",
            f"  <title>{escape(it.get('episode_title') or '')}</title>",
            f"  <showtitle>{escape(it.get('title') or '')}</showtitle>",
            f"  <season>{int(it.get('season') or 0)}</season>",
            f"  <episode>{int(it.get('episode') or 0)}</episode>",
            "</episodedetails>"])))
        series_dir = f.parent.parent
        lib_series = Path(it["dest_file"]).parent.parent if it.get("dest_file") else None
        show_nfo = series_dir / "tvshow.nfo"
        if not show_nfo.exists() and not (lib_series and (lib_series / "tvshow.nfo").exists()):
            made.append(_write(show_nfo, "\n".join(x for x in [
                "<tvshow>", f"  <title>{escape(it.get('title') or '')}</title>",
                f"  <year>{it['year']}</year>" if it.get("year") else "", _ids(ids), "</tvshow>"] if x)))
    else:
        made.append(_write(target, "\n".join(x for x in [
            "<movie>", f"  <title>{escape(it.get('title') or '')}</title>",
            f"  <originaltitle>{escape(it.get('original_title') or it.get('title') or '')}</originaltitle>",
            f"  <year>{it['year']}</year>" if it.get("year") else "", _ids(ids), "</movie>"] if x)))
    return {"status": "done", "message": "Details file written", "files": made}


if __name__ == "__main__":
    run(handle)
