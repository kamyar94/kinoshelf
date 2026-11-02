"""Filing rules: where a film or an episode goes, decided by an ordered list of rules the user edits in
Settings > Filing rules (or imports as YAML). The first enabled rule whose conditions fit an item files it.

A rule:  {name, enabled, when: {conditions}, root, folder, file, show_folder, season_folder, episode_file,
          tags: [{text, when}], collections: {existing, create, min_released}, policies: {...}}
Conditions (all must hold; a list means "any of"):
  type (film | episode | special), genres, keywords, original_language, countries, audio_languages,
  subtitle_languages, dubbed (a language code, or "any"), collection (true/false), quality (release-name word),
  picture (2160p, 1080p, ...), year {min, max}, runtime {min, max} (minutes), in_library (true/false),
  name (a regular expression on the release name), any: [blocks], not: {block}.
Templates: {Title} {OriginalTitle} {TranslatedTitle:ja} {TranslatedTitle:ja/JP} {Year} {Quality} {Picture} {Tags}
  {Collection} {Folder} {Series} {Season:00} {Episode:00} {Code} {EpisodeTitle} {Part} {VersionWords};
  {prefix|Token|suffix} shows prefix and suffix only when the token has a value.
"""

from __future__ import annotations

import copy
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import PurePath

from .paths import PathError, PathMap, pure

VIDEO_EXTS = {"mkv", "mp4", "m4v", "avi", "mov", "wmv", "mpg", "mpeg", "ts", "m2ts", "webm", "flv", "divx", "ogm",
              "vob", "iso", "dat", "3gp", "rmvb", "asf", "mts"}
CONDITIONS = {"type", "genres", "keywords", "original_language", "countries", "audio_languages", "subtitle_languages",
              "dubbed", "collection", "quality", "picture", "year", "runtime", "in_library", "name", "any", "not"}
RULE_KEYS = {"name", "enabled", "when", "root", "folder", "file", "show_folder", "season_folder", "episode_file",
             "tags", "collections", "policies"}
POLICIES = {
    "quality_from": "name-or-picture",  # name | picture | name-or-picture
    "second_copy": "version",  # a copy of a film already filed: version (in its folder) | folder (its own)
    "other_audio_copy": "folder",  # a copy whose audio differs (dubbed or not): folder | version
    "version_label": "{Picture}{ |VersionWords}",
    "trailers": "suffix",  # suffix (beside the film) | folder (a Trailers folder in it)
    "trailer_suffix": "-trailer",
    "trailer_max_minutes": 6,
    "part_label": " - part{Part}",
    "latin_titles": True,  # a Title in another script than its OriginalTitle's Latin one: use the original
    "dub_language": "",  # the language "dubbed" refers to for tags, versions and folder matching ("" = none)
    "dub_label": "Dubbed",  # the tag a dubbed copy's folder carries (matching existing folders by audio)
    "original_audio_label": "Original audio",
    "dub_words": ["dubbed", "dub"],  # release-name words that mark a dubbed copy
    "hardsub_label": "{Lang} Hardsub",
    "hold_copies": True,  # a copy of something already filed waits on the review page
}
PICTURE = ["2160p", "1080p", "720p", "576p", "480p"]
QUALITY_WORDS = [(re.compile(r"(?i)(2160p|\b4k\b|\buhd\b)"), "2160p"), (re.compile(r"(?i)1080[pi]"), "1080p"),
                 (re.compile(r"(?i)720p"), "720p"), (re.compile(r"(?i)576p"), "576p"), (re.compile(r"(?i)480p"), "480p"),
                 (re.compile(r"(?i)(dvd|video_ts|\.vob$|\.iso$)"), "DVD")]
TRAILER_WORDS = re.compile(r"(?i)(?<![a-z])(trailer|teaser)(?![a-z])")
PART_WORDS = re.compile(r"(?i)(?:^|[ ._\-\[(])(?:cd|part|pt|disc|disk)[ ._\-]?(\d{1,2})(?=[ ._\-\])]|$)")
THREE_D = re.compile(r"(?i)(?<![a-z])(hsbs|h-sbs|half-sbs|sbs|htab|h-tab|tab|fsbs|ftab|mvc)(?![a-z])")
THREE_D_NAMES = {"hsbs": "HSBS", "h-sbs": "HSBS", "half-sbs": "HSBS", "sbs": "SBS", "htab": "HTAB", "h-tab": "HTAB",
                 "tab": "TAB", "fsbs": "FSBS", "ftab": "FTAB", "mvc": "MVC"}
LATIN = re.compile(r"^[\x00-\x7f\u00c0-\u024f\u1e00-\u1eff]*$")
TOKEN = re.compile(r"\{([^{}]*)\}")
# Language codes as media files and release names write them (ISO 639-1 first).
LANGS = {
    "en": ("en", "eng", "english"), "fr": ("fr", "fre", "fra", "french"), "de": ("de", "ger", "deu", "german"),
    "es": ("es", "spa", "spanish"), "it": ("it", "ita", "italian"), "ja": ("ja", "jpn", "japanese"),
    "ko": ("ko", "kor", "korean"), "zh": ("zh", "chi", "zho", "chinese"), "ru": ("ru", "rus", "russian"),
    "tr": ("tr", "tur", "turkish"), "ar": ("ar", "ara", "arabic"), "fa": ("fa", "fas", "per", "persian", "farsi"),
    "hi": ("hi", "hin", "hindi"), "pt": ("pt", "por", "portuguese"), "nl": ("nl", "dut", "nld", "dutch"),
    "sv": ("sv", "swe", "swedish"), "pl": ("pl", "pol", "polish"), "he": ("he", "heb", "hebrew"),
}
LANG_NAMES = {k: v[-1].capitalize() for k, v in LANGS.items()}


class FilingError(ValueError):
    pass


@dataclass
class Filing:
    kind: str  # movie | episode
    dest: str  # shown path of the main file (DVD: of the title folder + "\VIDEO_TS")
    folder: str  # the title folder (films) or the show folder (series)
    folder_new: bool
    stem: str
    rule: str = ""
    category: str = ""  # the rule's root, for icons and reports
    collection: dict | None = None  # {"id", "name", "folder", "new", "members"}
    season_folder: str = ""
    tags: list[str] = field(default_factory=list)
    dubbed: bool = False
    version_of: str | None = None
    label: str = ""
    role: str = "feature"  # feature | trailer | part
    hold: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------------------------- small helpers
def safe(name: str) -> str:
    """A valid Windows file or folder name (":" becomes " -")."""
    s = str(name or "").replace(": ", " - ").replace(":", " -").replace("/", "-").replace("\\", "-").replace("|", "-")
    s = re.sub(r'[<>"?*\x00-\x1f]', "", s)
    s = re.sub(r"\s+", " ", s).strip().rstrip(".").strip()
    return s or "_"


def norm(s: str) -> str:
    return re.sub(r"[^\w]+", "", str(s or "").lower().replace("&", "and")).replace("_", "")


def lang_code(v: str) -> str:
    v = str(v or "").strip().lower()
    for code, names in LANGS.items():
        if v in names:
            return code
    return v


def quality_from_name(*names: str) -> str:
    for n in names:
        for rx, q in QUALITY_WORDS:
            if n and rx.search(n):
                return q
    return ""


def picture_class(width: int | None, height: int | None) -> str:
    """The quality word for a picture size; width counts too (1280 x 534 is a 720p release)."""
    w, h = int(width or 0), int(height or 0)
    if not w and not h:
        return ""
    if h >= 2000 or w >= 3200:
        return "2160p"
    if h >= 1000 or w >= 1800:
        return "1080p"
    if h >= 700 or w >= 1200:
        return "720p"
    if h >= 540:
        return "576p"
    if h >= 400 or w >= 640:
        return "480p"
    return f"{h}p" if h else ""


def episode_code(season: int, episodes: list[int]) -> str:
    def n(x: int) -> str:
        return f"{x:02d}" if x < 100 else str(x)
    eps = [e for e in episodes if e is not None] or [0]
    return f"S{n(season)}E{n(eps[0])}" + "".join(f"-E{n(e)}" for e in eps[1:])


def show_name_of(folder: str) -> str:
    """The name episodes carry: the show folder's name without its year, tags or copy number."""
    s = re.sub(r"\s*\(\d+\)\s*$", "", folder)
    s = re.sub(r"\s*\[[^\]]*\]", "", s)
    return re.sub(r"\s*\(\d{4}\)\s*$", "", s).strip()


def _join(shown: str, name: str) -> str:
    return str(pure(shown) / name)


def render(template: str, values: dict) -> str:
    """Fill a template; {pre|Token|post} adds pre and post only around a value."""
    def one(m):
        parts = m.group(1).split("|")
        pre, tok, post = (["", parts[0], ""] if len(parts) == 1 else
                          [parts[0], parts[1], ""] if len(parts) == 2 else parts[:3])
        name, _, arg = tok.partition(":")
        if name in ("Season", "Episode", "Part") and arg and set(arg) == {"0"}:
            v = values.get(name)
            v = f"{int(v):0{len(arg)}d}" if v not in (None, "") else ""
        elif name == "TranslatedTitle":
            v = values["_translated"](arg)
        else:
            v = values.get(name, None)
            if v is None:
                raise FilingError(f"Unknown token {{{tok}}}")
        v = str(v or "")
        return f"{pre}{v}{post}" if v else ""
    return TOKEN.sub(one, template or "")


# ---------------------------------------------------------------------------------------------- facts of one item
class Item:
    """What the conditions and tokens read, gathered once."""

    def __init__(self, it: dict, cand: dict, pol: dict):
        self.it, self.cand, self.pol = it, cand, pol
        self.facts = cand.get("facts") or {}
        self.probe = it.get("probe") or {}
        src = str(it.get("src_rel") or "").replace("\\", "/")
        self.names = [PurePath(src).name, *PurePath(src).parent.parts[-2:]]
        parsed = it.get("parsed") or {}
        self.kind = "film" if cand.get("kind") != "series" else (
            "special" if (parsed.get("season") if parsed.get("season") is not None else cand.get("season")) == 0
            else "episode")
        self.language = lang_code(self.facts.get("language") or "")
        self.audio = {lang_code(a.get("language")) for a in self.probe.get("audio") or [] if a.get("language")}
        self.subs = {lang_code(s.get("language")) for s in self.probe.get("subtitles") or [] if s.get("language")}
        self.in_library = False

    def dubbed(self, lang: str) -> bool:
        words = self.pol.get("dub_words") or []
        if any(re.search(rf"(?i)(?<![a-z]){re.escape(w)}(?![a-z])" if re.fullmatch(r"[\w ]+", w) else re.escape(w), n or "")
               for w in words for n in self.names):
            return True
        if not lang or not self.language:
            return False
        if lang == "any":
            return bool(self.audio - {self.language, "und", ""})
        return self.language != lang_code(lang) and lang_code(lang) in self.audio

    def quality(self) -> str:
        if self.it.get("is_dvd"):
            return "DVD"
        mode = self.pol.get("quality_from", "name-or-picture")
        q = quality_from_name(*self.names) if mode in ("name", "name-or-picture") else ""
        if not q and mode in ("picture", "name-or-picture"):
            q = picture_class(self.probe.get("width"), self.probe.get("height"))
        return q

    def translated(self, arg: str) -> str:
        title = self.title()
        for part in (arg or "").split("/"):
            if not part:
                continue
            if part.isupper():  # a country: its alternative title
                v = next(iter((self.facts.get("alt_titles") or {}).get(part) or []), "")
            else:
                v = (self.facts.get("translations") or {}).get(part) or (self.facts.get(f"{part}_title") or "")
            if v and norm(v) != norm(title):
                return safe(v)
            if v:
                return ""
        return ""

    def title(self) -> str:
        t = self.cand.get("title") or ""
        o = self.cand.get("original_title") or ""
        if self.pol.get("latin_titles", True) and not LATIN.match(t) and o and LATIN.match(o):
            return safe(o)
        return safe(t)


def _anyof(have, want) -> bool:
    want = want if isinstance(want, list) else [want]
    have = {str(h).lower() for h in have}
    return any(str(w).lower() in have for w in want)


def holds(when: dict | None, x: Item) -> bool:
    """Do all the conditions of this block hold for the item?"""
    for k, v in (when or {}).items():
        if k == "any":
            if not any(holds(b, x) for b in v or []):
                return False
        elif k == "not":
            if holds(v, x):
                return False
        elif k == "type":
            if x.kind not in (v if isinstance(v, list) else [v]) and not (x.kind == "special" and "episode" in
                                                                           (v if isinstance(v, list) else [v])):
                return False
        elif k == "genres":
            if not _anyof(x.facts.get("genres") or [], v):
                return False
        elif k == "keywords":
            if not _anyof(x.facts.get("keywords") or [], v):
                return False
        elif k == "original_language":
            if lang_code(x.facts.get("language") or "") not in {lang_code(w) for w in (v if isinstance(v, list) else [v])}:
                return False
        elif k == "countries":
            if not _anyof(x.facts.get("countries") or [], v):
                return False
        elif k == "audio_languages":
            if not x.audio & {lang_code(w) for w in (v if isinstance(v, list) else [v])}:
                return False
        elif k == "subtitle_languages":
            if not x.subs & {lang_code(w) for w in (v if isinstance(v, list) else [v])}:
                return False
        elif k == "dubbed":
            if bool(x.dubbed("any" if v is True else str(v))) != (v is not False):
                return False
        elif k == "collection":
            if bool((x.facts.get("collection") or {}).get("id")) != bool(v):
                return False
        elif k == "quality":
            if quality_from_name(*x.names) not in (v if isinstance(v, list) else [v]):
                return False
        elif k == "picture":
            if picture_class(x.probe.get("width"), x.probe.get("height")) not in (v if isinstance(v, list) else [v]):
                return False
        elif k in ("year", "runtime"):
            n = x.cand.get("year") if k == "year" else ((x.probe.get("duration_s") or 0) / 60 or None)
            if n is None or (v.get("min") is not None and n < v["min"]) or (v.get("max") is not None and n > v["max"]):
                return False
        elif k == "in_library":
            if x.in_library != bool(v):
                return False
        elif k == "name":
            if not any(re.search(str(v), n or "", re.I) for n in x.names):
                return False
        else:
            raise FilingError(f"Unknown condition {k}")
    return True


def validate(cfg: dict) -> list[str]:
    """Problems in a rules set: unknown conditions, keys or policies, or templates that do not render."""
    out = []
    for i, r in enumerate((cfg or {}).get("rules") or [], 1):
        if not isinstance(r, dict):
            out.append(f"Rule {i} is not a mapping")
            continue
        out += [f"Rule {i}: unknown setting {k}" for k in r if k not in RULE_KEYS]
        stack = [r.get("when") or {}] + [t.get("when") or {} for t in r.get("tags") or [] if isinstance(t, dict)]
        while stack:
            b = stack.pop()
            for k, v in (b or {}).items():
                if k not in CONDITIONS:
                    out.append(f"Rule {i}: the condition {k} is not in this version")
                elif k == "any":
                    stack += list(v or [])
                elif k == "not":
                    stack.append(v)
        out += [f"Rule {i}: unknown policy {k}" for k in (r.get("policies") or {}) if k not in POLICIES]
        if not r.get("root") and r.get("enabled", True):
            out.append(f"Rule {i}: no root folder")
    out += [f"Unknown policy {k}" for k in (cfg or {}).get("policies") or {} if k not in POLICIES]
    return out


# ---------------------------------------------------------------------------------------------- the library's folders
@dataclass
class Folder:
    shown: str
    name: str
    root: str
    collection: str | None = None
    ids: dict = field(default_factory=dict)
    files: list[str] = field(default_factory=list)


ICON_ID = re.compile(r"^FolderIcon_(tt\d+|tmdbcoll\d+)\.ico$", re.I)
MOVIE_FOLDER = re.compile(r"^(?P<title>.+?) \((?P<year>\d{4})\)(?P<tags>(?: \[[^\]]+\])*)(?P<dup> \(\d+\))?$")


def _list_dir(p) -> list[tuple[str, bool]]:
    try:
        with os.scandir(p) as it:
            return [(e.name, e.is_dir()) for e in it]
    except OSError:
        return []


def _ids(files: list[str]) -> dict:
    for f in files:
        m = ICON_ID.match(f)
        if m:
            v = m.group(1)
            return {"imdb": v} if v.startswith("tt") else {"tmdbcoll": v[8:]}
    return {}


class Index:
    """Folder names under the rules' roots (and the ids in their icon file names), read once and reused."""

    def __init__(self, pm: PathMap, film_roots: list[str], show_roots: list[str], minutes: float = 60,
                 lister: Callable = _list_dir):
        self.pm, self.film_roots, self.show_roots, self.minutes, self.lister = pm, film_roots, show_roots, minutes, lister
        self.titles: list[Folder] = []
        self.collections: list[tuple[Folder, int]] = []
        self.shows: list[Folder] = []
        self.built = 0.0
        self.lock = threading.Lock()

    def _cont(self, shown: str):
        try:
            return self.pm.to_container(shown)
        except PathError:
            return None

    def fresh(self) -> Index:
        with self.lock:
            if time.time() - self.built > self.minutes * 60:
                self._build()
        return self

    def _build(self) -> None:
        titles, colls, shows = [], [], []
        for root in dict.fromkeys(self.film_roots):
            c = self._cont(root)
            for name, is_dir in (self.lister(c) if c is not None else []):
                if not is_dir or name.startswith((".", "$", "_")):
                    continue
                shown, entries = _join(root, name), self.lister(c / name)
                files = [n for n, d in entries if not d]
                subdirs = [n for n, d in entries if d]
                if not any(f.rsplit(".", 1)[-1].lower() in VIDEO_EXTS for f in files) and subdirs \
                        and not MOVIE_FOLDER.match(name):
                    for sub in subdirs:
                        sfiles = [n for n, d in self.lister(c / name / sub) if not d]
                        titles.append(Folder(_join(shown, sub), sub, root, name, _ids(sfiles), sfiles))
                    colls.append((Folder(shown, name, root, None, _ids(files), files), len(subdirs)))
                else:
                    titles.append(Folder(shown, name, root, None, _ids(files), files))
        for root in dict.fromkeys(self.show_roots):
            c = self._cont(root)
            for name, is_dir in (self.lister(c) if c is not None else []):
                if is_dir and not name.startswith((".", "$", "_")):
                    files = [n for n, d in self.lister(c / name) if not d]
                    shows.append(Folder(_join(root, name), name, root, None, _ids(files), files))
        self.titles, self.collections, self.shows, self.built = titles, colls, shows, time.time()

    def films(self, imdb: str | None, titles: list[str], year: int | None) -> list[Folder]:
        hits = [t for t in self.titles if imdb and t.ids.get("imdb") == imdb]
        if hits:
            return hits
        keys = {f"{norm(x)}|{year}" for x in titles if x and year}
        out = []
        for t in self.titles:
            m = MOVIE_FOLDER.match(t.name)
            if m and (f"{norm(m.group('title'))}|{m.group('year')}" in keys or any(
                    f"{norm(p)}|{m.group('year')}" in keys for p in m.group("title").split(" - "))):
                out.append(t)
        return out

    def collection(self, cid: str | None, name: str | None, root: str) -> tuple[Folder, int] | None:
        for f, n in self.collections:
            if f.root == root and cid and f.ids.get("tmdbcoll") == str(cid):
                return f, n
        for f, n in self.collections:
            if f.root == root and name and norm(f.name) == norm(safe(name)):
                return f, n
        return None

    def show(self, imdb: str | None, titles: list[str], year: int | None) -> Folder | None:
        for s in self.shows:
            if imdb and s.ids.get("imdb") == imdb:
                return s
        names = {norm(x) for x in titles if x}
        for s in self.shows:
            m = re.match(r"^(?P<t>.+?)(?: \((?P<y>\d{4})\))?(?: \[[^\]]+\])*(?: \(\d+\))?$", s.name)
            if m and any(norm(p) in names for p in [m.group("t"), *m.group("t").split(" - ")]) and (
                    not year or not m.group("y") or abs(int(m.group("y")) - year) <= 1):
                return s
        return None


# ---------------------------------------------------------------------------------------------- the engine
class Rules:
    def __init__(self, cfg: dict, pm: PathMap, index: Index | None = None):
        self.cfg = copy.deepcopy(cfg or {})
        self.pm = pm
        self.rules = [r for r in self.cfg.get("rules") or [] if isinstance(r, dict) and r.get("enabled", True)]
        film_roots = [r["root"] for r in self.rules if r.get("root") and _types(r) & {"film"}]
        show_roots = [r["root"] for r in self.rules if r.get("root") and _types(r) & {"episode", "special"}]
        self.index = index or Index(pm, film_roots, show_roots, float(self.cfg.get("index_minutes", 60)))

    def policies(self, rule: dict) -> dict:
        return {**POLICIES, **(self.cfg.get("policies") or {}), **(rule.get("policies") or {})}

    def pick(self, it: dict, cand: dict, in_library: bool = False) -> tuple[dict, Item]:
        for r in self.rules:
            x = Item(it, cand, self.policies(r))
            x.in_library = in_library
            if holds(r.get("when"), x) and x.kind in (_types(r) or {"film", "episode", "special"}) | (
                    {"special"} if "episode" in _types(r) else set()):
                return r, x
        raise FilingError("No filing rule fits this item")

    def resolve(self, it: dict, cand: dict) -> Filing:
        kind = "film" if cand.get("kind") != "series" else "episode"
        index = self.index.fresh()
        ids = cand.get("ids") or {}
        names = [cand.get("title") or "", cand.get("original_title") or ""]
        if kind == "film":
            existing = index.films(ids.get("imdb"), names, cand.get("year"))
        else:
            existing = [s] if (s := index.show(ids.get("imdb"), names, cand.get("year"))) else []
        rule, x = self.pick(it, cand, in_library=bool(existing))
        return self.film(rule, x, existing) if kind == "film" else self.episode(rule, x, existing[0] if existing else None)

    def _values(self, x: Item, **extra) -> dict:
        c = x.cand
        v = {"Title": x.title(), "OriginalTitle": safe(c.get("original_title") or c.get("title") or ""),
             "Year": c.get("year") or "", "Quality": x.quality(),
             "Picture": picture_class(x.probe.get("width"), x.probe.get("height")) or "version",
             "Collection": safe((x.facts.get("collection") or {}).get("name") or "") if x.facts.get("collection") else "",
             "EpisodeTitle": safe(c.get("episode_title") or "") if c.get("episode_title") else "",
             "Tags": "", "Folder": "", "Series": "", "Season": None, "Episode": None, "Code": "", "Part": None,
             "VersionWords": "", "_translated": x.translated}
        v.update(extra)
        return v

    def _tags(self, rule: dict, x: Item, values: dict) -> list[str]:
        out = []
        for t in rule.get("tags") or []:
            t = t if isinstance(t, dict) else {"text": t}
            if holds(t.get("when"), x):
                s = render(str(t.get("text") or ""), values).strip()
                if s:
                    out.append(s)
        return out

    def film(self, rule: dict, x: Item, existing: list[Folder]) -> Filing:
        pol, it, cand = x.pol, x.it, x.cand
        if not cand.get("year"):
            raise FilingError("No year known for this title")
        ext = "" if it.get("is_dvd") else PurePath(str(it.get("src_rel") or "").replace("\\", "/")).suffix.lower()
        role, part = _role(x)
        dub = pol.get("dub_language") or ""
        dubbed = x.dubbed(dub) if dub else False
        values = self._values(x)
        tags = self._tags(rule, x, values)
        values["Tags"] = " ".join(f"[{t}]" for t in tags)
        folder_name = safe(render(rule.get("folder") or "{Title} ({Year})", values))
        values["Folder"] = folder_name
        notes: list[str] = []
        root = rule["root"]
        if existing:
            label = pol.get("dub_label") or ""
            same = [t for t in existing if (label and f"[{label}]" in t.name) == dubbed]
            target = None
            if role != "feature":
                target = (same or existing)[0]
            elif same and pol.get("second_copy") == "version":
                target = same[0]
            elif not same and pol.get("other_audio_copy") == "version":
                target = existing[0]
            if target is not None:
                return self._into(rule, x, target, ext, role, part, dubbed, values)
            notes.append("This film is already in the library as " + ", ".join(t.name for t in existing))
        coll, parent = None, root
        c = x.facts.get("collection") or {}
        cp = {"existing": True, "create": False, "min_released": 2, **(rule.get("collections") or {})}
        if c.get("id") and c.get("name") and (cp["existing"] or cp["create"]):
            found = self.index.collection(c.get("id"), c.get("name"), root) if cp["existing"] else None
            if found is not None:
                coll = {"id": c["id"], "name": c["name"], "folder": found[0].shown, "new": False, "members": found[1]}
                parent = found[0].shown
            elif cp["create"] and int(c.get("released") or 0) >= int(cp["min_released"]):
                coll = {"id": c["id"], "name": c["name"], "folder": _join(root, safe(c["name"])), "new": True,
                        "members": 0}
                parent = coll["folder"]
        folder = _join(parent, folder_name)
        if role == "trailer":
            stem = folder_name + pol.get("trailer_suffix", "-trailer")
            if pol.get("trailers") == "folder":
                folder = _join(folder, "Trailers")
        elif part:
            stem = folder_name + render(pol.get("part_label", " - part{Part}"), dict(values, Part=part))
        else:
            stem = safe(render(rule.get("file") or "{Folder}", values))
        dest = _join(folder, "VIDEO_TS") if it.get("is_dvd") else _join(folder, stem + ext)
        f = Filing(kind="movie", dest=dest, folder=folder, folder_new=True, stem=stem, rule=rule.get("name", ""),
                   category=root, collection=coll, tags=tags, dubbed=dubbed, role="part" if part else role, notes=notes)
        if existing and pol.get("hold_copies", True):
            f.hold = "Another copy of this film is already in the library"
        elif role == "trailer":
            f.hold = "A trailer waits for its film"
        return f

    def _into(self, rule, x: Item, t: Folder, ext, role, part, dubbed, values) -> Filing:
        pol = x.pol
        files = {f.lower() for f in t.files}
        common = dict(kind="movie", folder=t.shown, folder_new=False, rule=rule.get("name", ""), category=t.root,
                      version_of=t.shown, dubbed=dubbed)
        if role == "trailer":
            sfx = pol.get("trailer_suffix", "-trailer")
            stem, n = f"{t.name}{sfx}", 2
            while any(f.startswith(stem.lower() + ".") for f in files):
                stem, n = f"{t.name} ({n}){sfx}", n + 1
            return Filing(dest=_join(t.shown, stem + ext), stem=stem, role="trailer",
                          hold="A trailer for a film already in the library", **common)
        if part:
            stem = t.name + render(pol.get("part_label", " - part{Part}"), dict(values, Part=part))
            return Filing(dest=_join(t.shown, stem + ext), stem=stem, role="part",
                          hold="A part of a film already in the library", **common)
        words = []
        joined = " ".join(x.names)
        label = pol.get("dub_label") or ""
        lang = pol.get("dub_language") or ""
        t_dubbed = bool(label) and f"[{label}]" in t.name
        if t_dubbed and lang and lang_code(lang) not in x.audio:
            words.append(pol.get("original_audio_label", "Original audio"))
        elif dubbed and not t_dubbed and label:
            words.append(label)
        if re.search(r"(?i)extended", joined):
            words.append("Extended")
        if re.search(r"(?i)hard-?sub", joined):
            hl = next((LANG_NAMES[c] for c, ns in LANGS.items() if any(re.search(rf"(?i)(?<![a-z]){n}(?![a-z])", joined)
                                                                       for n in ns if len(n) > 3)), "")
            words.append(render(pol.get("hardsub_label", "{Lang} Hardsub"), {"Lang": hl}).strip() if hl else "Hardsub")
        m3 = THREE_D.search(joined) if re.search(r"(?i)(?<![a-z0-9])3d(?![a-z])", joined) else None
        if m3:
            words.append("3D " + THREE_D_NAMES[m3.group(1).lower()])
        base = render(pol.get("version_label", "{Picture}{ |VersionWords}"), dict(values, VersionWords=" ".join(words)))
        tagq = next((v for v in re.findall(r"\[([^\]]+)\]", t.name) if re.fullmatch(r"\d+p|DVD", v)), "")
        n = 2 if (not words and base == tagq) else 1
        while True:
            full = base if n == 1 else f"{base} version {n}"
            stem = f"{t.name} - {full}"
            if not any(f.startswith(stem.lower() + ".") for f in files):
                break
            n += 1
        return Filing(dest=_join(t.shown, stem + ext), stem=stem, label=full,
                      hold="Another copy of this film is already in the library; it becomes a version of it"
                      if pol.get("hold_copies", True) else "", **common)

    def episode(self, rule: dict, x: Item, show: Folder | None) -> Filing:
        pol, it, cand = x.pol, x.it, x.cand
        parsed = it.get("parsed") or {}
        season = parsed.get("season") if parsed.get("season") is not None else cand.get("season")
        eps = parsed.get("episodes") or ([cand.get("episode")] if cand.get("episode") is not None else [])
        if season is None or not eps:
            raise FilingError("No season and episode number")
        dub = pol.get("dub_language") or ""
        dubbed = x.dubbed(dub) if (dub and x.facts) else False
        values = self._values(x, Season=int(season), Episode=int(eps[0]), Code=episode_code(int(season), [int(e) for e in eps]))
        tags = self._tags(rule, x, values)
        values["Tags"] = " ".join(f"[{t}]" for t in tags)
        if show is not None:
            folder, folder_new, show_folder = show.shown, False, show.name
        else:
            show_folder = safe(render(rule.get("show_folder") or rule.get("folder") or "{Title} ({Year})", values))
            folder, folder_new = _join(rule["root"], show_folder), True
        values.update(Series=show_name_of(show_folder), Folder=show_folder)
        sf = safe(render(rule.get("season_folder") or "Season {Season:00}", values))
        stem = safe(render(rule.get("episode_file") or "{Series} {Code}{ - |EpisodeTitle}", values))
        ext = PurePath(str(it.get("src_rel") or "").replace("\\", "/")).suffix.lower()
        f = Filing(kind="episode", dest=_join(_join(folder, sf), stem + ext), folder=folder, folder_new=folder_new,
                   stem=stem, rule=rule.get("name", ""), category=rule["root"], season_folder=sf, tags=tags,
                   dubbed=dubbed)
        if not folder_new:
            cs = self.index._cont(_join(folder, sf))
            pre = f"{values['Series']} {values['Code']}".lower()
            if cs is not None and any(n.lower().startswith(pre) and n.rsplit(".", 1)[-1].lower() in VIDEO_EXTS
                                      for n, d in self.index.lister(cs) if not d):
                f.hold = "This episode is already in the library"
        return f


def _types(rule: dict) -> set[str]:
    t = (rule.get("when") or {}).get("type")
    return set(t if isinstance(t, list) else [t]) if t else set()


def _role(x: Item) -> tuple[str, int | None]:
    dur = (x.probe.get("duration_s") or 0) / 60
    name = x.names[0]
    if TRAILER_WORDS.search(name) and (not dur or dur <= float(x.pol.get("trailer_max_minutes", 6))):
        return "trailer", None
    m = PART_WORDS.search(PurePath(name).stem)
    if m and int(m.group(1)) >= 1:
        return "feature", int(m.group(1))
    return "feature", None


# ---------------------------------------------------------------------------------------------- starter presets
def preset(name: str, root_films: str = "X:\\Movies", root_series: str = "X:\\Series", language: str = "") -> dict:
    """Neutral starting points; the user edits them on the page."""
    films = {"name": "Films", "when": {"type": "film"}, "root": root_films,
             "folder": "{Title} ({Year})", "file": "{Folder}", "collections": {"existing": True, "create": False}}
    series = {"name": "Series", "when": {"type": "episode"}, "root": root_series, "show_folder": "{Title} ({Year})",
              "season_folder": "Season {Season:00}", "episode_file": "{Series} {Code}{ - |EpisodeTitle}"}
    if name == "standard":  # the Jellyfin, Plex and Kodi layout
        return {"rules": [films, series], "policies": {}}
    if name == "by-language":  # films in the chosen original language get a folder of their own
        lang = language or "xx"
        own = {"name": f"Original language: {LANG_NAMES.get(lang, lang)}", "when": {"type": "film", "original_language": [lang]},
               "root": f"{root_films}\\{LANG_NAMES.get(lang, lang)}", "folder": f"{{Title}}{{ - |TranslatedTitle:{lang}}} ({{Year}})",
               "file": "{Folder}"}
        return {"rules": [own, dict(films, root=f"{root_films}\\Films"), series], "policies": {}}
    raise FilingError(f"Unknown preset {name}")
