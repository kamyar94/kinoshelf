"""The contract every lookup source follows."""

from __future__ import annotations

from pathlib import Path

from ..models import Candidate, Query
from ..net import Net


class Source:
    """A title lookup. Subclasses set `key` and `name` and override what they support.

    search() and by_id() raise LookupError_ when the source cannot be reached, and return
    an empty list when it was reached but knows nothing. Never return made-up data.
    """

    key = ""  # settings key, e.g. "tmdb"
    name = ""  # shown on the page, e.g. "TMDB"
    online = True  # False for sources that work without internet (offline IMDb copy)
    kinds = ("movie", "series")  # what it can identify; an empty answer about anything else says nothing

    def covers(self, kind: str) -> bool:
        """Whether an answer from this source counts for a file of this kind (movie, episode, unknown)."""
        if kind == "movie":
            return "movie" in self.kinds
        if kind == "episode":
            return "series" in self.kinds
        return True

    def __init__(self, cfg: dict, net: Net, data_dir: Path):
        self.cfg = cfg or {}
        self.net = net
        self.data_dir = data_dir

    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled"))

    def search(self, q: Query) -> list[Candidate]:
        """Candidates for a title (and year, season, episode when known), best first, at most ~8."""
        return []

    def by_id(self, q: Query) -> list[Candidate]:
        """Candidates for the ids in the query (imdb_id, tmdb_id + tmdb_kind, tvmaze_id)."""
        return []

    def details(self, c: Candidate, q: Query) -> Candidate:
        """Fill what search() left out (runtime, alternative titles, episode facts). Returns c."""
        return c
