"""Ask every enabled source about a file, merge what they say, and score the result."""

from __future__ import annotations

import logging
from pathlib import Path

from rapidfuzz import fuzz

from .models import Candidate, HashHit, LookupError_, Query
from .net import Net
from .scoring import score_candidates
from .textnorm import norm

log = logging.getLogger(__name__)

# Which source's value wins when several know the same thing.
PRIORITY = ["TMDB", "IMDb", "TVmaze", "OMDb"]


def build_sources(settings, net: Net, data_dir: Path) -> dict:
    from .sources.imdb import ImdbSource
    from .sources.jellyfin import JellyfinClient
    from .sources.omdb import OmdbSource
    from .sources.opensubtitles import OpenSubtitlesHash
    from .sources.tmdb import TmdbSource
    from .sources.tvmaze import TvmazeSource

    cfg = settings.get("sources", {})
    return {
        "titles": [
            TmdbSource(cfg.get("tmdb", {}), net, data_dir),
            ImdbSource(cfg.get("imdb", {}), net, data_dir),
            TvmazeSource(cfg.get("tvmaze", {}), net, data_dir),
            OmdbSource(cfg.get("omdb", {}), net, data_dir),
        ],
        "hash": OpenSubtitlesHash(cfg.get("opensubtitles", {}), net),
        "jellyfin": JellyfinClient(cfg.get("jellyfin", {}), net),
    }


def _rank(name: str) -> int:
    return PRIORITY.index(name) if name in PRIORITY else len(PRIORITY)


def _same(a: Candidate, b: Candidate) -> bool:
    ia, ib = a.ids.get("imdb"), b.ids.get("imdb")
    if ia and ib:
        return ia == ib
    if a.kind != b.kind:
        return False
    if a.year and b.year and abs(a.year - b.year) > 1:
        return False
    return norm(a.title) == norm(b.title) or (
        bool(a.original_title) and norm(a.original_title) == norm(b.original_title or b.title))


def merge(group: list[Candidate]) -> Candidate:
    """One candidate from several sources' views of the same title."""
    g = sorted(group, key=lambda c: min((_rank(s) for s in c.sources), default=9))

    def first(attr):
        for c in g:
            v = getattr(c, attr)
            if v not in (None, "", []):
                return v
        return None

    out = Candidate(kind=g[0].kind, title=g[0].title)
    for attr in ("year", "original_title", "runtime_min", "poster_url", "people", "season", "episode",
                 "episode_title", "episode_runtime_min"):
        setattr(out, attr, first(attr))
    seen = {norm(out.title)}
    for c in g:
        for t in [c.title, c.original_title, *c.alt_titles]:
            if t and norm(t) not in seen:
                seen.add(norm(t))
                out.alt_titles.append(t)
        for k, v in c.ids.items():
            out.ids.setdefault(k, v)
        for s in c.sources:
            if s not in out.sources:
                out.sources.append(s)
        out.hash_match = out.hash_match or c.hash_match
    flags = [c.episode_exists for c in g if c.episode_exists is not None]
    out.episode_exists = True if True in flags else (False if flags else None)
    out.sources.sort(key=_rank)
    return out


def _group(cands: list[Candidate]) -> list[list[Candidate]]:
    groups: list[list[Candidate]] = []
    for c in cands:
        for g in groups:
            if any(_same(c, x) for x in g):
                g.append(c)
                break
        else:
            groups.append([c])
    # A later imdb id (from details) can join two groups that looked different.
    merged = True
    while merged:
        merged = False
        for i in range(len(groups)):
            for j in range(i + 1, len(groups)):
                ids_i = {c.ids.get("imdb") for c in groups[i]} - {None}
                ids_j = {c.ids.get("imdb") for c in groups[j]} - {None}
                if ids_i & ids_j:
                    groups[i] += groups.pop(j)
                    merged = True
                    break
            if merged:
                break
    return groups


class Identifier:
    def __init__(self, settings, net: Net, data_dir: Path, sources: dict | None = None):
        self.settings = settings
        self.net = net
        self.data_dir = data_dir
        self.sources = sources or build_sources(settings, net, data_dir)

    def title_sources(self, only: str | None = None):
        return [s for s in self.sources["titles"] if s.enabled() and (only in (None, "all", s.key))]

    def identify(self, q: Query, only: str | None = None, details_top: int = 6) -> tuple[list[Candidate], list[str]]:
        """Scored candidates, best first, plus notes about sources that failed.

        Raises LookupError_ when nothing could be asked at all (every source failed), so the
        caller retries later instead of treating the file as unknown.
        """
        notes: list[str] = []
        found: list[tuple] = []  # (source, candidate)
        asked = failed = 0
        by_id = bool(q.imdb_id or q.tmdb_id or q.tvmaze_id)
        for src in self.title_sources(only):
            asked += 1
            try:
                res = src.by_id(q) if by_id else src.search(q)
            except LookupError_ as e:
                failed += 1
                notes.append(f"{src.name} could not be reached ({e})")
                continue
            found += [(src, c) for c in res]

        hits: list[HashHit] = []
        hsrc = self.sources.get("hash")
        hash_checked = False
        if hsrc is not None and hsrc.enabled() and q.oshash and not by_id:
            asked += 1
            try:
                hits = hsrc.lookup(q.oshash)
                hash_checked = True
            except LookupError_ as e:
                failed += 1
                notes.append(f"OpenSubtitles could not be reached ({e})")
        if asked and failed == asked:
            raise LookupError_("; ".join(notes))

        # Hash hits for titles no search found: look them up by id.
        for h in hits:
            want = h.parent_imdb_id if h.kind == "episode" else h.imdb_id
            if want and not any(c.ids.get("imdb") == want for _, c in found):
                hq = Query(kind=q.kind, title=q.title, season=h.season or q.season,
                           episode=h.episode or q.episode, duration_min=q.duration_min, imdb_id=want)
                for src in self.title_sources():
                    try:
                        found += [(src, c) for c in src.by_id(hq)]
                    except LookupError_:
                        pass

        # Details for the most promising titles only.
        def pre(c: Candidate) -> float:
            s = max(fuzz.token_sort_ratio(norm(q.title), norm(t)) for t in [c.title, c.original_title or ""])
            if q.year and c.year:
                s += 10 if q.year == c.year else -10
            return s

        cands_all = [c for _, c in found]
        groups = _group(cands_all)
        groups.sort(key=lambda g: -max(pre(c) for c in g))
        keep = groups[:details_top]
        src_of = {id(c): s for s, c in found}
        for g in keep:
            for c in g:
                try:
                    src_of[id(c)].details(c, q)
                except LookupError_ as e:
                    notes.append(f"{src_of[id(c)].name}: {e}")
        merged = [merge(g) for g in _group([c for g in keep for c in g])]

        for c in merged:
            for h in hits:
                want = h.parent_imdb_id if h.kind == "episode" else h.imdb_id
                if want and c.ids.get("imdb") == want:
                    c.hash_match = True
        jf = self.sources.get("jellyfin")
        if jf is not None and jf.enabled():
            for c in merged:
                try:
                    c.in_library = jf.find(c)
                except LookupError_ as e:
                    notes.append(f"Jellyfin: {e}")
                    break
        scored = score_candidates(merged, q, self.settings.get("scoring", {}), hash_checked=hash_checked)
        return scored[:8], sorted(set(notes))

    def status(self) -> list[dict]:
        """Lookup chips for the top of the review page."""
        out = []
        for s in self.sources["titles"]:
            if s.enabled():
                note = "offline list" if not s.online else "online"
                out.append({"name": s.name, "note": note, "ok": True})
        if self.sources["hash"].enabled():
            out.append({"name": "OpenSubtitles hash", "note": "online", "ok": True})
        if self.sources["jellyfin"].enabled():
            out.append({"name": "Jellyfin library", "note": "local", "ok": True})
        return out
