from pathlib import Path

import pytest

from kinoshelf.naming import TemplateError, fields_for, render, safe_value
from kinoshelf.paths import PathError, PathMap

MOVIE_T = "X:\\Films\\{Title} ({Year})\\{Title} ({Year}).{ext}"
EP_T = "Y:\\Shows\\{Series} ({Year})\\Season {SS}\\{Series} S{SS}E{EE} - {Episode}.{ext}"


def test_movie_template():
    f = fields_for("movie", {"title": "A Separation", "year": 2011}, {}, ".MKV")
    assert render(MOVIE_T, f) == "X:\\Films\\A Separation (2011)\\A Separation (2011).mkv"


def test_episode_template_and_double_episode():
    cand = {"title": "The Office", "year": 2005, "season": 2, "episode_title": "The Dundies"}
    f = fields_for("episode", cand, {"season": 2, "episodes": [1]}, "avi")
    assert render(EP_T, f) == ("Y:\\Shows\\The Office (2005)\\Season 02\\"
                               "The Office S02E01 - The Dundies.avi")
    f2 = fields_for("episode", cand, {"season": 5, "episodes": [1, 2]}, "mkv")
    assert render(EP_T, f2).endswith("S05E01-E02 - The Dundies.mkv")


def test_missing_values_are_tidied():
    f = fields_for("episode", {"title": "Friends", "year": None, "season": 1}, {"season": 1, "episodes": [3]}, "mkv")
    assert render(EP_T, f) == "Y:\\Shows\\Friends\\Season 01\\Friends S01E03.mkv"


def test_unsafe_characters_and_reserved_names():
    f = fields_for("movie", {"title": 'Mission: Impossible / "Fallout"?', "year": 2018}, {}, "mkv")
    out = render(MOVIE_T, f)
    assert "Mission - Impossible - 'Fallout'" in out and "?" not in out
    assert render("X:\\{Title}\\{Title}.{ext}", {"Title": "CON", "ext": "mkv"}) == "X:\\_CON\\_CON.mkv"
    assert safe_value("a\\b|c<d>e*") == "a-b-cde"


def test_persian_title_kept():
    f = fields_for("movie", {"title": "جدایی نادر از سیمین", "year": 2011}, {}, "mkv")
    assert "جدایی نادر از سیمین (2011)" in render(MOVIE_T, f)


def test_unknown_token():
    with pytest.raises(TemplateError):
        render("X:\\{Nope}.{ext}", {"ext": "mkv"})


def test_posix_template():
    f = fields_for("movie", {"title": "Up", "year": 2009}, {}, "mp4")
    assert render("/library/movies/{Title} ({Year})/{Title} ({Year}).{ext}", f) == \
        "/library/movies/Up (2009)/Up (2009).mp4"


def test_path_map_round_trip(tmp_path):
    pm = PathMap([{"shown": "M:\\", "container": "/media/m"}, {"shown": "M:\\Movies\\Kids", "container": "/kids"},
                  {"shown": "/srv/films", "container": "/media/films", "label": "films"}])
    assert pm.to_container("m:\\Movies\\Up (2009)\\Up.mp4") == Path("/media/m", "Movies", "Up (2009)", "Up.mp4")
    assert pm.to_container("M:\\Movies\\Kids\\Up.mp4") == Path("/kids", "Up.mp4")  # longest prefix wins
    assert pm.to_shown("/media/m/Movies/Up.mp4") == "M:\\Movies\\Up.mp4"
    assert pm.to_shown("/kids/Up.mp4") == "M:\\Movies\\Kids\\Up.mp4"
    assert pm.to_container("/srv/films/A/a.mkv") == Path("/media/films", "A", "a.mkv")
    entry, rel = pm.relative_to_root("M:\\Movies\\X\\x.mkv")
    assert entry.label == "M" and rel == ("Movies", "X", "x.mkv")
    with pytest.raises(PathError):
        pm.to_container("Q:\\Other\\x.mkv")
