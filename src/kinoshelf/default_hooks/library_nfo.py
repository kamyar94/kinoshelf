"""Full details files for the filing rules (Jellyfin/Kodi NFO, UTF-8, LF line ends):

  movie.nfo           in a new film folder: title, original title (when it differs), year, release date, running
                      time, plot, genres, up to three countries, IMDb and TMDB ids (IMDb the default), the TMDB
                      collection as <set>, and the rule's tags (other than a quality word) as <tag>
  tvshow.nfo          in a new show folder: the same without running time and countries, TMDB the default id
  <episode>.nfo       beside each episode: title, show title, season, episode, plot, air date, year, rating, ids

A copy that becomes a version of a film already in the library gets no new movie.nfo: the folder has one.
"""

from __future__ import annotations

import re

from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from . import run

HEAD = '<?xml version="1.0" encoding="utf-8" standalone="yes"?>'


def _lines(tag: str, value) -> list[str]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return []
    return [f"  <{tag}>{escape(str(value))}</{tag}>"]


def _year(it: dict, facts: dict):
    return it.get("year") or (facts.get("release_date") or "")[:4] or None


def movie_nfo(it: dict) -> str:
    facts = it.get("facts") or {}
    filing = it.get("filing") or {}
    ids = it.get("ids") or {}
    L = [HEAD, "<movie>"]
    L += _lines("title", it.get("title"))
    if it.get("original_title") and it.get("original_title") != it.get("title"):
        L += _lines("originaltitle", it["original_title"])
    L += _lines("year", _year(it, facts))
    L += _lines("premiered", facts.get("release_date"))
    if facts.get("runtime"):
        L += _lines("runtime", int(facts["runtime"]))
    L += _lines("plot", facts.get("overview"))
    for g in facts.get("genres") or []:
        L += _lines("genre", g)
    for c in (facts.get("country_names") or [])[:3]:
        L += _lines("country", c)
    imdb, tmdb = ids.get("imdb"), ids.get("tmdb")
    if imdb:
        L.append(f'  <uniqueid type="imdb" default="true">{escape(imdb)}</uniqueid>')
    if tmdb:
        default = "" if imdb else ' default="true"'
        L.append(f'  <uniqueid type="tmdb"{default}>{escape(str(tmdb))}</uniqueid>')
    L += _lines("imdbid", imdb)
    L += _lines("tmdbid", tmdb)
    coll = facts.get("collection") or {}
    if coll.get("name"):
        L += ["  <set>", f"    <name>{escape(coll['name'])}</name>", "  </set>"]
    for tag in filing.get("tags") or []:
        if not re.fullmatch(r"\d+p|DVD|4K", str(tag)):
            L += _lines("tag", tag)
    L.append("</movie>")
    return "\n".join(L) + "\n"


def show_nfo(it: dict) -> str:
    facts = it.get("facts") or {}
    ids = it.get("ids") or {}
    L = [HEAD, "<tvshow>"]
    L += _lines("title", it.get("title"))
    if it.get("original_title") and it.get("original_title") != it.get("title"):
        L += _lines("originaltitle", it["original_title"])
    L += _lines("year", _year(it, facts))
    L += _lines("premiered", facts.get("release_date"))
    L += _lines("plot", facts.get("overview"))
    for g in facts.get("genres") or []:
        L += _lines("genre", g)
    imdb, tmdb = ids.get("imdb"), ids.get("tmdb")
    if imdb:
        L.append(f'  <uniqueid type="imdb">{escape(imdb)}</uniqueid>')
    if tmdb:
        L.append(f'  <uniqueid type="tmdb" default="true">{escape(str(tmdb))}</uniqueid>')
    L += _lines("imdbid", imdb)
    L += _lines("tmdbid", tmdb)
    L.append("</tvshow>")
    return "\n".join(L) + "\n"


def episode_nfo(it: dict) -> str:
    facts = it.get("facts") or {}
    ep = facts.get("episode") or {}
    eps = [e for e in it.get("episodes") or [] if e is not None] or [it.get("episode")]
    L = [HEAD, "<episodedetails>"]
    L += _lines("title", it.get("episode_title"))
    L += _lines("showtitle", it.get("title"))
    L += _lines("season", int(it.get("season") or 0))
    L += _lines("episode", int(eps[0] or 0))
    if len(eps) > 1:
        L += _lines("episodenumberend", int(eps[-1]))
    L += _lines("plot", ep.get("overview"))
    if ep.get("air_date"):
        L += _lines("aired", ep["air_date"])
        L += _lines("premiered", ep["air_date"])
        L += _lines("year", ep["air_date"][:4])
    if ep.get("rating"):
        L += _lines("rating", "%g" % float(ep["rating"]))
    ids = {k: v for k, v in (ep.get("ids") or {}).items() if v}
    default = "tvdb" if "tvdb" in ids else ("tmdb" if "tmdb" in ids else None)
    for k in [k for k in ("tvdb", "tmdb", "imdb") if k in ids]:
        L.append("  <uniqueid type=%s%s>%s</uniqueid>" % (quoteattr(k), ' default="true"' if k == default else "",
                                                         escape(str(ids[k]))))
    L.append("</episodedetails>")
    return "\n".join(L) + "\n"


def _write(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return str(path)


def handle(job: dict) -> dict:
    it = job["item"]
    f = Path(it["file"])
    filing = it.get("filing") or {}
    made: list[str] = []
    if it["kind"] == "episode":
        made.append(_write(f.with_suffix(".nfo"), episode_nfo(it)))
        show_dir = f.parent.parent
        if filing.get("folder_new", True) and not (show_dir / "tvshow.nfo").exists():
            made.append(_write(show_dir / "tvshow.nfo", show_nfo(it)))
        return {"status": "done", "message": "Details files written", "files": made}
    if filing and not filing.get("folder_new", True):
        return {"status": "skipped", "message": "The film's folder already has its details file"}
    folder = f if it.get("is_dvd") else f.parent
    target = folder / "movie.nfo"
    if target.exists():
        return {"status": "skipped", "message": "movie.nfo is already there"}
    made.append(_write(target, movie_nfo(it)))
    return {"status": "done", "message": "Details file written", "files": made}


if __name__ == "__main__":
    run(handle)
