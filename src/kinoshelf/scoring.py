"""Score candidates against the file, and decide whether the best one is safe to file without asking."""

from __future__ import annotations

from rapidfuzz import fuzz

from .models import Candidate, Query
from .textnorm import norm, skeleton


def _title_score(q_title: str, c: Candidate, rules) -> tuple[float, str]:
    """Best similarity (0-1) over title, original title and alternative titles, with which one matched."""
    qn, qs = norm(q_title), skeleton(q_title, rules)
    best, which = 0.0, "title"
    names = [("title", c.title), ("original", c.original_title)] + [("alt", t) for t in c.alt_titles]
    for kind, t in names:
        if not t:
            continue
        tn = norm(t)
        s = max(fuzz.ratio(qn, tn), fuzz.token_sort_ratio(qn, tn), fuzz.ratio(qs, skeleton(t, rules)))
        if s > best * 100 + 0.01:
            best, which = s / 100, kind
    return best, which


def _runtime(c: Candidate) -> float | None:
    if c.kind == "series":
        return c.episode_runtime_min or c.runtime_min
    return c.runtime_min


def runtime_diff_pct(file_min: float | None, c: Candidate) -> float | None:
    r = _runtime(c)
    if not file_min or not r:
        return None
    if abs(file_min - r) <= 1.5:  # databases round to whole minutes
        return 0.0
    return abs(file_min - r) / r * 100


def score_candidates(cands: list[Candidate], q: Query, scoring: dict, hash_checked: bool = False) -> list[Candidate]:
    w = scoring.get("weights", {})
    wt, wr, wy = float(w.get("title", 35)), float(w.get("runtime", 30)), float(w.get("year", 15))
    rules = scoring.get("translit", [])
    alt_only: dict[int, bool] = {}
    for c in cands:
        checks: list[dict] = []
        num = den = 0.0

        ts, which = _title_score(q.title, c, rules)
        alt_only[id(c)] = which != "title"
        num += wt * ts
        den += wt
        if ts >= 0.99:
            text = {"title": "Title matches exactly", "original": "Original title matches",
                    "alt": "Another title of it matches"}[which]
            checks.append({"key": "title", "ok": True, "text": text})
        elif ts >= 0.8:
            checks.append({"key": "title", "ok": True, "text": f"Title is close ({ts:.0%})"})
        else:
            checks.append({"key": "title", "ok": False, "text": f"Title differs ({ts:.0%} alike)"})

        if q.year and c.year:
            d = abs(q.year - c.year)
            ys = 1.0 if d == 0 else 0.6 if d == 1 else 0.0
            num += wy * ys
            den += wy
            checks.append({"key": "year", "ok": d <= 1,
                           "text": "Year matches" if d == 0 else "Year is one off" if d == 1
                           else f"Year differs ({c.year}, file says {q.year})"})
        elif c.year and not q.year:
            num += wy * float(scoring.get("missing_year", 0.7))
            den += wy
        if c.years:
            told = ", ".join(f"{s} {y}" for s, y in c.years.items())
            checks.append({"key": "year_sources", "ok": None, "text": f"Sources disagree on the year ({told}); using {c.year}"})

        diff = runtime_diff_pct(q.duration_min, c)
        r = _runtime(c)
        if diff is not None:
            rs = 1.0 if diff <= 2 else max(0.0, 1 - (diff - 2) / 13)
            num += wr * rs
            den += wr
            delta = r - q.duration_min
            what = "Episode length" if c.kind == "series" else "Running time"
            if diff <= 2:
                checks.append({"key": "runtime", "ok": True,
                               "text": f"{what} matches within {max(1, round(abs(delta)))} minute"
                                       + ("s" if round(abs(delta)) > 1 else "")})
            else:
                way = "longer" if delta > 0 else "shorter"
                checks.append({"key": "runtime", "ok": diff <= 5,
                               "text": f"{what} is {abs(delta):.0f} minutes {way}"})
        elif q.duration_min and not r:
            checks.append({"key": "runtime", "ok": None, "text": "Running time unknown for this match"})

        score = num / den * 100 if den else 0.0

        if c.kind == "series" and q.season and q.episode:
            if c.episode_exists is True:
                checks.append({"key": "episode", "ok": True,
                               "text": f"Season {q.season} has an episode {q.episode}"})
            elif c.episode_exists is False:
                checks.append({"key": "episode", "ok": False,
                               "text": f"No episode {q.episode} in season {q.season}"})
                score = min(score, 50.0)
        if (q.kind == "movie" and c.kind == "series") or (q.kind == "episode" and c.kind == "movie"):
            score *= 0.6
            checks.append({"key": "kind", "ok": False,
                           "text": "This is a series" if c.kind == "series" else "This is a film"})

        if c.hash_match:
            score = max(score, float(scoring.get("hash_floor", 90)))
            checks.insert(0, {"key": "hash", "ok": True,
                              "text": "File hash matches this " + ("episode" if c.kind == "series" else "film")})
        elif hash_checked:
            checks.append({"key": "hash", "ok": False, "text": "No file hash match"})
        if c.in_library:
            checks.append({"key": "library", "ok": None, "text": f"Already in your library: {c.in_library}"})

        c.score = round(score)
        c.checks = checks
    # Ties: a match on the main title first, then the title more sources know, then the older one
    # (remakes and local versions are usually newer).
    cands.sort(key=lambda x: (-x.score, alt_only[id(x)], -len(x.sources), x.year or 9999))
    return cands


def decide(cands: list[Candidate], q: Query, rule: dict, parsed_kind: str) -> tuple[bool, str]:
    """(confident, reason). The reason is what the review page shows when it is not confident."""
    reasons: list[str] = []
    if parsed_kind == "unknown" and not q.title:
        return False, "No name to go on"
    if not cands:
        return False, "Nothing matched by name or by file hash"
    best = cands[0]
    if not q.year and best.kind == "movie" and not best.hash_match:
        same = [c for c in cands[1:] if c.kind == "movie" and norm(c.title) == norm(best.title)]
        if same:
            reasons.append(f"No year in the name; {len(same) + 1} films share the title")
    if best.score < float(rule.get("min_score", 85)):
        reasons.append(f"Best match only {best.score}%")
    diff = runtime_diff_pct(q.duration_min, best)
    if q.duration_min:
        if diff is None:
            reasons.append("Running time unknown for the best match")
        elif diff > float(rule.get("max_runtime_diff_pct", 5)):
            reasons.append(f"Running time differs by {diff:.0f}%")
    if len(cands) > 1 and best.score - cands[1].score < float(rule.get("min_margin", 10)):
        reasons.append(f"{cands[1].label} scores close ({cands[1].score}%)")
    if best.kind == "series" and q.episode and best.episode_exists is not True:
        reasons.append("Episode not confirmed by any source")
    if best.in_library:
        reasons.append("Already in your library")
    if parsed_kind == "unknown":
        reasons.append("Could not tell film from episode")
    # Only blocking reasons make it unsure; the year note alone does not when the rest holds.
    blocking = [r for r in reasons if not r.startswith("No year in the name")]
    if blocking:
        return False, "; ".join(reasons)
    return True, "; ".join(reasons) or "Confident"
