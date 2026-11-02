"""Title normalisation shared by the parser, the offline IMDb index and scoring."""

from __future__ import annotations

import re
from functools import lru_cache

from anyascii import anyascii

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def norm(text: str | None) -> str:
    """Lower-case Latin form with punctuation turned into single spaces: "Amélie & Co." -> "amelie and co"."""
    if not text:
        return ""
    s = anyascii(text).lower().replace("&", " and ").replace("'", "").replace("`", "")
    return _NON_ALNUM.sub(" ", s).strip()


@lru_cache(maxsize=64)
def _compiled(rules: tuple[tuple[str, str], ...]) -> list[tuple[re.Pattern, str]]:
    return [(re.compile(p), r) for p, r in rules]


def skeleton(text: str | None, rules: list | tuple = ()) -> str:
    """norm() plus transliteration rules, so "Jodaeiye" and "Jodaeie" or "Ghermez" and "Qermez" meet."""
    s = norm(text)
    for pat, rep in _compiled(tuple(tuple(r) for r in rules)):
        s = pat.sub(rep, s)
    return re.sub(r"\s+", " ", s).strip()
