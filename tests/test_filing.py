"""Filing rules: templates, conditions, rule order, films (tags, collections, copies, trailers, parts) and episodes,
checked against a made-up library tree. Public film titles only; any language could stand where Japanese does."""

from __future__ import annotations

import pytest

from kinoshelf.filing import Filing, FilingError, Item, Rules, holds, preset, render, validate
from kinoshelf.paths import PathMap

PM = PathMap([{"shown": "X:\\", "container": "/x"}])


def tree(paths: list[str]):
    """A lister over a made-up folder tree: 'Movies/Films/Heat (1995)/Heat (1995).mkv' style paths."""
    kids: dict[str, dict[str, bool]] = {}
    for p in paths:
        parts = p.split("/")
        for i in range(len(parts)):
            parent = "/x/" + "/".join(parts[:i]) if i else "/x"
            kids.setdefault(parent.rstrip("/"), {})[parts[i]] = i < len(parts) - 1
    return lambda path: sorted(kids.get(str(path).replace("\\", "/").rstrip("/"), {}).items())


RULES = {
    "policies": {"dub_language": "ja", "dub_label": "Japanese Dubbed", "quality_from": "name"},
    "rules": [
        {"name": "Animation", "when": {"type": "film", "genres": ["Animation"]}, "root": "X:\\Movies\\Animation",
         "folder": "{Title} ({Year}){ |Tags}", "tags": ["{Quality}"]},
        {"name": "Japanese", "when": {"type": "film", "any": [{"original_language": "ja"}, {"countries": ["JP"]}],
                                      "not": {"dubbed": "ja"}},
         "root": "X:\\Movies\\Japanese", "folder": "{Title}{ - |TranslatedTitle:ja/JP} ({Year}){ |Tags}",
         "tags": ["{Quality}"]},
        {"name": "Films", "when": {"type": "film"}, "root": "X:\\Movies\\Films", "folder": "{Title} ({Year}){ |Tags}",
         "tags": ["{Quality}", {"text": "Japanese Dubbed", "when": {"dubbed": "ja"}}],
         "collections": {"existing": True, "create": True, "min_released": 2}},
        {"name": "Off", "enabled": False, "when": {"type": "film"}, "root": "X:\\Nowhere"},
        {"name": "Series", "when": {"type": "episode"}, "root": "X:\\Series",
         "show_folder": "{Title}{ - |TranslatedTitle:ja} ({Year}){ |Tags}",
         "tags": [{"text": "Japanese Dubbed", "when": {"dubbed": "ja"}}]},
    ],
}


def film(title, year, src, genres=(), lang="en", countries=(), audio=(), ids=None, coll=None, translations=None,
         width=1920, height=1080, minutes=110, original=None):
    facts = {"genres": list(genres), "language": lang, "countries": list(countries), "translations": translations or {}}
    if coll:
        facts["collection"] = coll
    it = {"src_rel": src, "probe": {"width": width, "height": height, "duration_s": minutes * 60,
                                    "audio": [{"language": a} for a in audio]}, "parsed": {}}
    cand = {"kind": "movie", "title": title, "original_title": original or title, "year": year, "ids": ids or {},
            "facts": facts}
    return it, cand


def engine(paths=(), rules=RULES) -> Rules:
    r = Rules(rules, PM)
    r.index.lister = tree(list(paths))
    return r


def test_templates_fill_tokens_and_optional_parts():
    v = {"Title": "Heat", "Year": 1995, "Tags": "", "Season": 2, "Episode": 7, "_translated": lambda a: ""}
    assert render("{Title} ({Year}){ |Tags}", v) == "Heat (1995)"
    assert render("S{Season:00}E{Episode:000}", v) == "S02E007"
    assert render("{Title}{ - |TranslatedTitle:ja}", dict(v, _translated=lambda a: "ヒート")) == "Heat - ヒート"
    with pytest.raises(FilingError, match="Unknown token"):
        render("{Nope}", v)


def test_conditions_and_rule_order():
    r = engine()
    it, cand = film("Spirited Away", 2001, "Inbox/Spirited.Away.2001.1080p.mkv", genres=["Animation"], lang="ja")
    assert r.resolve(it, cand).rule == "Animation"  # first rule that fits wins
    it, cand = film("Seven Samurai", 1954, "Inbox/Seven.Samurai.1954.mkv", lang="ja",
                    translations={"ja": "七人の侍"})
    f = r.resolve(it, cand)
    assert f.rule == "Japanese" and f.folder == "X:\\Movies\\Japanese\\Seven Samurai - 七人の侍 (1954)"
    it, cand = film("Ran", 1985, "Inbox/Ran.1985.720p.mkv", lang="fr", countries=["JP"])  # any: country fits
    assert r.resolve(it, cand).rule == "Japanese"
    it, cand = film("Heat", 1995, "Inbox/Heat.1995.mkv", lang="en", audio=["en", "jpn"])  # dubbed into Japanese
    f = r.resolve(it, cand)
    assert f.rule == "Films" and f.dubbed and f.folder.endswith("Heat (1995) [Japanese Dubbed]")
    it, cand = film("Tokyo Story", 1953, "Inbox/Tokyo.Story.1953.DUBBED.mkv", lang="ja")  # dub word in the name
    assert r.resolve(it, cand).rule == "Films"
    with pytest.raises(FilingError, match="No filing rule fits"):
        Rules({"rules": [RULES["rules"][0]]}, PM).resolve(*film("Heat", 1995, "Inbox/Heat.mkv"))


def test_a_rule_can_hold_items_without_details():
    rules = {"rules": [{"name": "Wait", "when": {"type": "film", "not": {"details": True}},
                        "hold": "No details for this title yet"}, *RULES["rules"]]}
    it, cand = film("Heat", 1995, "Inbox/Heat.1995.mkv")
    cand["facts"] = {}
    with pytest.raises(FilingError, match="No details for this title yet"):
        engine([], rules).resolve(it, cand)
    assert engine([], rules).resolve(*film("Heat", 1995, "Inbox/Heat.1995.mkv")).rule == "Films"
    assert validate(rules) == []


def test_tmdb_facts_are_generic():
    from kinoshelf.sources.tmdb import _facts

    f = _facts({"original_language": "ja", "genres": [{"name": "Drama"}], "production_countries": [{"iso_3166_1": "JP"}],
                "translations": {"translations": [{"iso_639_1": "ja", "data": {"title": "七人の侍"}},
                                                  {"iso_639_1": "en", "data": {"title": "Seven Samurai"}}]},
                "alternative_titles": {"titles": [{"iso_3166_1": "US", "title": "The Magnificent Seven"}]}}, "movie")
    assert f["translations"] == {"ja": "七人の侍", "en": "Seven Samurai"}
    assert f["alt_titles"] == {"US": ["The Magnificent Seven"]} and f["countries"] == ["JP"]


def test_holds_reads_every_condition():
    it, cand = film("Heat", 1995, "Inbox/Heat.1995.720p.mkv", genres=["Crime"], audio=["en"], minutes=170,
                    coll={"id": "1", "name": "X"})
    x = Item(it, cand, {"dub_words": []})
    assert holds({"genres": "crime", "quality": "720p", "picture": "1080p", "collection": True,
                  "year": {"min": 1990, "max": 1999}, "runtime": {"min": 160}, "audio_languages": ["eng"],
                  "name": r"heat\.1995", "in_library": False}, x)
    assert not holds({"keywords": ["heist"]}, x) and not holds({"subtitle_languages": "en"}, x)
    with pytest.raises(FilingError):
        holds({"colour": "red"}, x)


def test_quality_tags_collections_trailers_and_parts():
    r = engine(["Movies/Films/The Matrix Collection/The Matrix (1999)/The Matrix (1999).mkv"])
    it, cand = film("The Matrix Reloaded", 2003, "Inbox/The.Matrix.Reloaded.2003.1080p.mkv",
                    coll={"id": "2344", "name": "The Matrix Collection", "released": 4})
    f = r.resolve(it, cand)
    assert f.collection["new"] is False and f.dest == ("X:\\Movies\\Films\\The Matrix Collection\\"
                                                      "The Matrix Reloaded (2003) [1080p]\\The Matrix Reloaded (2003) [1080p].mkv")
    it, cand = film("Toy Story 2", 1999, "Inbox/Toy.Story.2.1999.mkv", coll={"id": "10194", "name": "Toy Story Collection",
                                                                            "released": 1})
    f = r.resolve(it, cand)
    assert f.collection is None and f.folder == "X:\\Movies\\Films\\Toy Story 2 (1999)"  # too few released
    it, cand = film("Heat", 1995, "Inbox/Heat.1995.Trailer.mp4", minutes=2)
    f = r.resolve(it, cand)
    assert f.role == "trailer" and f.stem == "Heat (1995)-trailer" and f.hold
    it, cand = film("Heat", 1995, "Inbox/Heat.1995.CD2.avi")
    assert r.resolve(it, cand).stem == "Heat (1995) - part2"


def test_a_second_copy_becomes_a_version_or_waits():
    lib = ["Movies/Films/Heat (1995) [720p]/Heat (1995) [720p].mkv",
           "Movies/Films/Heat (1995) [720p]/Heat (1995) [720p] - 1080p.mkv"]
    r = engine(lib)
    it, cand = film("Heat", 1995, "Inbox/Heat.1995.EXTENDED.mkv", width=1920, height=800)
    f = r.resolve(it, cand)
    assert f.version_of and f.stem == "Heat (1995) [720p] - 1080p Extended" and f.hold
    it, cand = film("Heat", 1995, "Inbox/Heat.1995.mkv", width=1920, height=800)
    assert r.resolve(it, cand).stem == "Heat (1995) [720p] - 1080p version 2"  # " - 1080p" is taken
    it, cand = film("Heat", 1995, "Inbox/Heat.1995.mkv", audio=["en", "ja"])  # other audio: its own folder
    f = r.resolve(it, cand)
    assert f.folder_new and f.folder.endswith("[Japanese Dubbed]") and "already in the library" in f.hold
    own = dict(RULES, policies=dict(RULES["policies"], second_copy="folder"))
    it, cand = film("Heat", 1995, "Inbox/Heat.1995.1080p.mkv")
    assert engine(lib, own).resolve(it, cand).folder_new


def test_episodes_new_and_existing_shows():
    r = engine(["Series/The Office (2005)/Season 02/The Office S02E01 - The Dundies.mkv"])
    it = {"src_rel": "Inbox/the.office.s02e01.mkv", "parsed": {"season": 2, "episodes": [1]}, "probe": {}}
    cand = {"kind": "series", "title": "The Office", "year": 2005, "ids": {}, "episode_title": "The Dundies", "facts": {}}
    f = r.resolve(it, cand)
    assert not f.folder_new and f.stem == "The Office S02E01 - The Dundies" and f.hold  # already there
    it = dict(it, src_rel="Inbox/the.office.s02e02e03.mkv", parsed={"season": 2, "episodes": [2, 3]})
    f = r.resolve(it, dict(cand, episode_title="Sexual Harassment"))
    assert f.stem == "The Office S02E02-E03 - Sexual Harassment" and not f.hold
    it = {"src_rel": "Inbox/shogun.s01e03.mkv", "parsed": {"season": 1, "episodes": [3]}, "probe": {}}
    cand = {"kind": "series", "title": "Shogun", "year": 2024, "ids": {}, "facts": {"language": "en",
                                                                                   "translations": {"ja": "将軍"}}}
    f = r.resolve(it, cand)
    assert f.dest == "X:\\Series\\Shogun - 将軍 (2024)\\Season 01\\Shogun - 将軍 S01E03.mkv"  # {Series} is the show folder's name


def test_validate_names_what_is_unknown():
    assert validate(RULES) == []
    bad = {"rules": [{"name": "x", "root": "X:\\", "when": {"colour": "red", "any": [{"mood": 1}]}, "speed": 1,
                      "policies": {"telepathy": True}}], "policies": {"nope": 1}}
    found = validate(bad)
    assert "Rule 1: the condition colour is not in this version" in found
    assert "Rule 1: the condition mood is not in this version" in found
    assert "Rule 1: unknown setting speed" in found and "Unknown policy nope" in found


def test_presets_are_neutral_and_work():
    std = preset("standard")
    assert validate(std) == [] and {r["name"] for r in std["rules"]} == {"Films", "Series"}
    by = preset("by-language", language="ko")
    assert by["rules"][0]["when"]["original_language"] == ["ko"] and by["rules"][0]["root"].endswith("Korean")
    f = engine([], by).resolve(*film("Oldboy", 2003, "Inbox/Oldboy.2003.mkv", lang="ko", translations={"ko": "올드보이"}))
    assert isinstance(f, Filing) and f.folder == "X:\\Movies\\Korean\\Oldboy - 올드보이 (2003)"
