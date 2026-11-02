"""Files the filing rules make: full details files, folder icons and desktop.ini, and a file filed end to end
through the Inbox with a rule."""

from __future__ import annotations

import io
import struct
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from kinoshelf.default_hooks import library_nfo as N
from kinoshelf.icons import desktop_ini, detect, icon_name, label_for, make_icon, template

from conftest import FakeIdentifier, make_video, needs_ffmpeg


def test_movie_nfo_matches_the_library_format():
    it = {"title": "The Mummy Returns", "original_title": "The Mummy Returns", "year": 2001,
          "ids": {"imdb": "tt0209163", "tmdb": "1734"},
          "facts": {"release_date": "2001-05-04", "runtime": 130, "overview": "Plot & more.",
                    "genres": ["Adventure", "Action", "Fantasy"], "country_names": ["United States of America"],
                    "collection": {"id": "1733", "name": "The Mummy Collection"}},
          "filing": {"tags": ["1080p"], "dubbed": False}}
    assert N.movie_nfo(it) == (
        '<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n<movie>\n'
        "  <title>The Mummy Returns</title>\n  <year>2001</year>\n  <premiered>2001-05-04</premiered>\n"
        "  <runtime>130</runtime>\n  <plot>Plot &amp; more.</plot>\n  <genre>Adventure</genre>\n"
        "  <genre>Action</genre>\n  <genre>Fantasy</genre>\n  <country>United States of America</country>\n"
        '  <uniqueid type="imdb" default="true">tt0209163</uniqueid>\n  <uniqueid type="tmdb">1734</uniqueid>\n'
        "  <imdbid>tt0209163</imdbid>\n  <tmdbid>1734</tmdbid>\n"
        "  <set>\n    <name>The Mummy Collection</name>\n  </set>\n</movie>\n")


def test_movie_nfo_original_title_and_rule_tags():
    it = {"title": "22 Bullets", "original_title": "L'Immortel", "year": 2010, "ids": {"imdb": "tt1167638", "tmdb": "37645"},
          "facts": {"country_names": ["France"]}, "filing": {"dubbed": True, "tags": ["720p", "Dubbed"]}}
    text = N.movie_nfo(it)
    assert "  <originaltitle>L'Immortel</originaltitle>\n" in text
    assert text.endswith("  <tag>Dubbed</tag>\n</movie>\n") and "<tag>720p</tag>" not in text
    ja = N.movie_nfo({"title": "Seven Samurai", "original_title": "七人の侍", "year": 1954, "ids": {"tmdb": "346"}})
    assert "<originaltitle>七人の侍</originaltitle>" in ja
    assert '<uniqueid type="tmdb" default="true">346</uniqueid>' in ja


def test_show_and_episode_nfo():
    it = {"title": "Dracula", "original_title": "دراکولا", "year": 2021, "ids": {"imdb": "tt14261264", "tmdb": "194814"},
          "season": 1, "episode": 4, "episodes": [4], "episode_title": "Episode 4",
          "facts": {"release_date": "2021-05-02", "genres": ["Drama", "Comedy"],
                    "episode": {"overview": "", "air_date": "2021-05-23", "rating": 7.5,
                                "ids": {"tmdb": "3000001", "tvdb": "8000001"}}}}
    show = N.show_nfo(it)
    assert '  <uniqueid type="imdb">tt14261264</uniqueid>\n  <uniqueid type="tmdb" default="true">194814</uniqueid>' in show
    assert "<originaltitle>دراکولا</originaltitle>" in show
    e = N.episode_nfo(it)
    assert e.splitlines()[1:5] == ["<episodedetails>", "  <title>Episode 4</title>", "  <showtitle>Dracula</showtitle>",
                                   "  <season>1</season>"]
    assert '  <uniqueid type="tvdb" default="true">8000001</uniqueid>\n  <uniqueid type="tmdb">3000001</uniqueid>' in e
    assert "  <rating>7.5</rating>" in e and "  <aired>2021-05-23</aired>" in e
    assert "episodenumberend" in N.episode_nfo(dict(it, episodes=[1, 2]))


def test_nfo_hook_skips_a_version(tmp_path):
    f = tmp_path / "Film (2010) [720p]" / "Film (2010) [720p] - 1080p.mkv"
    f.parent.mkdir()
    f.write_bytes(b"x")
    job = {"item": {"kind": "movie", "file": str(f), "filing": {"folder_new": False}, "ids": {}, "title": "Film"}}
    assert N.handle(job)["status"] == "skipped"
    job["item"]["filing"] = {"folder_new": True}
    out = N.handle(job)
    assert out["status"] == "done" and (f.parent / "movie.nfo").exists()
    assert b"\r\n" not in (f.parent / "movie.nfo").read_bytes()


# ---------------------------------------------------------------------------------------------- icons
def test_folder_icon_hook_takes_tmdb_route(tmp_path, monkeypatch):
    """The job carries no proxy; the hook finds TMDB's route in its environment (Settings > Network)."""
    from kinoshelf.default_hooks import folder_icon
    from kinoshelf.icons import art

    class Stop(Exception):
        pass

    seen = {}

    def fake_tmdb(key, proxy=None, cache=None, timeout=20):
        seen["proxy"] = proxy
        raise Stop

    monkeypatch.setenv("KS_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(art, "Tmdb", fake_tmdb)
    job = {"item": {"file": str(tmp_path / "Film (2001)" / "Film (2001).mkv"), "kind": "movie",
                    "filing": {"folder_new": True}}, "settings": {}, "options": {}}
    for env, want in (({"KS_PROXY": "http://route:10809", "KS_PROXY_USE": "tmdb,tvmaze"}, "http://route:10809"),
                      ({"KS_PROXY": "http://route:10809", "KS_PROXY_USE": "tvmaze"}, None), ({}, None)):
        monkeypatch.delenv("KS_PROXY", raising=False)
        monkeypatch.delenv("KS_PROXY_USE", raising=False)
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        with pytest.raises(Stop):
            folder_icon.handle(job)
        assert seen["proxy"] == want


def test_icon_names_and_desktop_ini():
    assert icon_name("tt0120338") == "FolderIcon_tt0120338.ico"
    assert icon_name(None) == "FolderIcon_tt-unknown.ico"
    assert icon_name("9518", collection=True) == "FolderIcon_tmdbcoll9518.ico"
    assert icon_name(None, collection=True) == "FolderIcon_noid.ico"
    ini = desktop_ini("FolderIcon_tt3173594.ico")
    assert ini == (b"[.ShellClassInfo]\r\nIconResource=FolderIcon_tt3173594.ico,0\r\n"
                   b"IconFile=FolderIcon_tt3173594.ico\r\nIconIndex=0\r\n")
    assert len(ini) == 108  # the size of the library's own files for 7-digit ids
    assert label_for("A Separation") == "A SEPARATION"


def _picture(text: bool = True) -> Image.Image:
    im = Image.new("RGB", (1280, 720))
    d = ImageDraw.Draw(im)
    for y in range(720):
        d.line([(0, y), (1279, y)], fill=(30 + y // 6, 60, 140 - y // 8))
    d.ellipse((520, 180, 760, 480), fill=(220, 170, 140))
    if text:
        d.rectangle((60, 600, 600, 680), fill=(250, 250, 250))
    return im


def test_icon_pipeline_smoke():
    png, ico, rep = make_icon(_picture(), "keyart", "A TEST TITLE")
    assert png.size == (256, 256) and png.mode == "RGBA"
    count = struct.unpack("<HHH", ico[:6])[2]
    assert count == 6
    sizes = sorted((ico[6 + 16 * i] or 256) for i in range(count))
    assert sizes == [16, 32, 48, 64, 128, 256]
    assert ico[struct.unpack("<I", ico[6 + 12:6 + 16])[0]:][:4] == b"\x89PNG"  # PNG frames, like the library's icons
    # transparent outside the folder, the picture inside the window, the tab coloured
    assert png.getpixel((2, 2))[3] == 0
    assert png.getpixel((128, 150))[3] == 255
    assert png.getpixel((60, 30))[3] == 255
    assert rep["method"] in ("cover", "fill", "cover-cut")
    Image.open(io.BytesIO(ico)).load()


def test_placement_keeps_text_inside_the_window():
    a = {"w": 1280, "h": 720, "text": [[20, 300, 300, 360, 0.9]], "faces": [], "diff": [], "sal": None}
    p = detect.place(a, "keyart")
    x0, y0, x1, y1 = p["box"]
    s = (x1 - x0) / 1280
    assert x0 + 20 * s >= 0 and y0 + 300 * s >= template.PANEL[1]
    centred = detect.place({"w": 1280, "h": 720, "text": [], "faces": [], "diff": [], "sal": None})
    assert centred["method"] == "cover" and round(centred["box"][1]) == 112


@pytest.mark.skipif(not detect.models_ready(), reason="detection models not downloaded (scripts/fetch-icon-assets.sh)")
def test_detectors_find_text_and_a_face():
    im = _picture()
    d = ImageDraw.Draw(im)
    d.rectangle((60, 600, 600, 680), fill=(0, 0, 0))
    try:
        font = template._font(56)
    except OSError:
        pytest.skip("no font")
    d.text((80, 605), "HELLO WORLD", font=font, fill=(255, 255, 255))
    boxes = detect.text_boxes(im)
    assert any(b[1] >= 560 and b[3] <= 720 for b in boxes)


# ---------------------------------------------------------------------------------------------- end to end
@needs_ffmpeg
def test_rules_file_a_film_with_its_subtitles_end_to_end(inst):
    from kinoshelf.pipeline import Engine

    s = inst["settings"]
    s.values["filing"] = {"enabled": True, "policies": {"quality_from": "name"}, "rules": [
        {"name": "Japanese", "when": {"type": "film", "original_language": ["ja"]}, "root": "X:\\Movies\\Japanese",
         "folder": "{Title}{ - |TranslatedTitle:ja} ({Year}){ |Tags}", "tags": ["{Quality}"]},
        {"name": "Films", "when": {"type": "film"}, "root": "X:\\Movies\\Films"}], "index_minutes": 0}
    s.values["hooks"]["nfo"] = [{"name": "NFO", "kind": "command", "enabled": True,
                                 "command": ["python", "-m", "kinoshelf.default_hooks.library_nfo"]}]
    s.save()
    folder = inst["inbox"] / "Seven.Samurai.1954.720p.BluRay"
    make_video(folder / "Seven.Samurai.1954.720p.BluRay.x264.mkv", seconds=3)
    (folder / "Seven.Samurai.1954.720p.BluRay.x264.English.srt").write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nThis is the English subtitle written for this test.\n", encoding="utf-8")
    table = {"seven samurai": [{"kind": "movie", "title": "Seven Samurai", "year": 1954, "runtime_min": 0.05,
                                "original_title": "七人の侍", "ids": {"imdb": "tt0047478", "tmdb": "346"},
                                "facts": {"source": "TMDB", "language": "ja", "countries": ["JP"], "genres": ["Drama"],
                                          "translations": {"ja": "七人の侍"}, "release_date": "1954-04-26",
                                          "country_names": ["Japan"]}}]}
    e = Engine(s, inst["db"], inst["data"], identifier=FakeIdentifier(s, table))
    e.scan(now=1e12)
    e.scan(now=1e12 + 1)
    for it in inst["db"].items(["queued"]):
        e.identify_item(it["id"])
    it = inst["db"].items()[0]
    assert it["state"] == "working", it["reason"]
    name = "Seven Samurai - 七人の侍 (1954) [720p]"
    staged = inst["inbox"] / "_Working" / "X" / "Movies" / "Japanese" / name
    assert (staged / f"{name}.mkv").exists()
    assert (staged / f"{name}.en.srt").exists()
    assert it["dest"] == f"X:\\Movies\\Japanese\\{name}\\{name}.mkv"
    assert e.run_hooks(it["id"]) == "ready", inst["db"].item(it["id"])["hooks"]
    ready = inst["inbox"] / "_Ready" / "X" / "Movies" / "Japanese" / name
    nfo = (ready / "movie.nfo").read_text(encoding="utf-8")
    assert "<originaltitle>七人の侍</originaltitle>" in nfo and "<country>Japan</country>" in nfo
