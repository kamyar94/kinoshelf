"""Shared data shapes: what a lookup is asked, and what a source answers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass
class Query:
    """What we know about one file when we ask the sources about it."""

    kind: str  # "movie", "episode" or "unknown"
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    duration_min: float | None = None  # running time of the file itself
    oshash: str | None = None  # OpenSubtitles hash, 16 hex digits
    size: int | None = None
    # Set when the user pasted a link or picked "same series as ..."
    imdb_id: str | None = None
    tmdb_id: str | None = None
    tmdb_kind: str | None = None  # "movie" or "tv" for a pasted TMDB link
    tvmaze_id: str | None = None


@dataclass
class Candidate:
    """One possible identity for a file. Sources fill what they know; scoring fills the rest."""

    kind: str  # "movie" or "series"
    title: str
    year: int | None = None
    original_title: str | None = None
    alt_titles: list[str] = field(default_factory=list)
    runtime_min: float | None = None  # film length, or typical episode length for a series
    ids: dict[str, str] = field(default_factory=dict)  # imdb "tt0111161", tmdb "278", tvmaze "123"
    sources: list[str] = field(default_factory=list)  # display names: "TMDB", "IMDb", ...
    poster_url: str | None = None
    people: str | None = None  # one short line: director, or network and country
    # Episode facts, for series candidates when the file names a season and episode
    season: int | None = None
    episode: int | None = None
    episode_title: str | None = None
    episode_runtime_min: float | None = None
    episode_exists: bool | None = None
    # Filled by identify and scoring
    hash_match: bool = False
    in_library: str | None = None  # path in the media server when the title is already there
    score: float = 0.0
    checks: list[dict] = field(default_factory=list)  # {"key", "ok" (True/False/None), "text"}

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Candidate":
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    @property
    def label(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title


@dataclass
class HashHit:
    """A file-hash match from OpenSubtitles."""

    kind: str  # "movie" or "episode"
    title: str
    year: int | None = None
    imdb_id: str | None = None  # the film, or the episode itself
    tmdb_id: str | None = None
    parent_imdb_id: str | None = None  # the series, for an episode
    parent_tmdb_id: str | None = None
    parent_title: str | None = None
    season: int | None = None
    episode: int | None = None


class LookupError_(Exception):
    """A source could not be reached or answered with an error. The lookup should be retried later."""
