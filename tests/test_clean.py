import pytest

from kinoshelf.clean import is_junk_file, parse_name, strip_junk
from kinoshelf.config import DEFAULTS, VIDEO_EXTS

JUNK = DEFAULTS["junk"]["release_words"]


@pytest.mark.parametrize("name, kind, title, year, season, episode, removed", [
    ("Solaris.DVDRip.XviD-[Iran-Film].avi", "movie", "Solaris", None, None, None, ["[Iran-Film]", "DVDRip", "XviD"]),
    ("Little.Women.720p.WEB-DL.x264.FardaDownload.mkv", "movie", "Little Women", None, None, None,
     ["FardaDownload", "720p", "WEB-DL", "x264"]),
    ("Jodaeiye.Nader.az.Simin.2011.DVDRip.mkv", "movie", "Jodaeiye Nader az Simin", 2011, None, None, ["DVDRip"]),
    ("the.office.s02e01.hdtv.avi", "episode", "The Office", None, 2, 1, ["hdtv"]),
    ("Interstellar.2014.1080p.BluRay.x264.YIFY.mp4", "movie", "Interstellar", 2014, None, None,
     ["YIFY", "1080p", "BluRay", "x264"]),
    ("www.YTS.MX - The Matrix (1999) [1080p].mkv", "movie", "The Matrix", 1999, None, None, ["www.YTS.MX"]),
    ("Breaking Bad/Season 2/E03.mkv", "episode", "Breaking Bad", None, 2, 3, []),
    ("Shahrzad (2015)/Season 1/Shahrzad.S01E03.720p.mkv", "episode", "Shahrzad", None, 1, 3, ["720p"]),
    ("Movie.Name.2019.AAC2.0.x264.mkv", "movie", "Movie Name", 2019, None, None, ["AAC2.0", "x264"]),
    ("Mission.Impossible.Dead.Reckoning.Part.One.2023.2160p.WEB-DL.DDP5.1.Atmos.HEVC-FLUX.mkv", "movie",
     "Mission Impossible Dead Reckoning Part One", 2023, None, None, ["2160p", "WEB-DL", "DDP5.1", "HEVC"]),
])
def test_parse(name, kind, title, year, season, episode, removed):
    p = parse_name(name, JUNK)
    assert p.kind == kind
    assert p.title == title
    assert p.year == year
    assert p.season == season
    assert p.episode == episode
    for r in removed:
        assert r in p.removed, (r, p.removed)


def test_double_episode():
    p = parse_name("Friends Season 5/Friends.S05E01.E02.720p.mkv", JUNK)
    assert p.episodes == [1, 2]
    assert p.clean == "Friends S05E01-E02"


def test_dvd_folder_named_after_its_folder():
    p = parse_name("DVD_VIDEO_3/VIDEO_TS/VTS_01_1.VOB", JUNK)
    assert p.kind == "unknown"
    assert p.clean == "DVD VIDEO 3"
    assert "VTS_01_1" in p.removed


def test_strip_junk_keeps_title_words():
    rest, removed = strip_junk("Par30dl.The.Salesman.2016", JUNK)
    assert "Salesman" in rest and "Par30dl" in removed


@pytest.mark.parametrize("name, size, junk", [
    ("RARBG.txt", 100, True),
    ("WWW.YTS.MX.jpg", 50_000, True),
    ("Torrent downloaded from Demonoid.com.txt", 80, True),
    ("movie-sample.mkv", 20 * 1024 * 1024, True),
    ("movie-sample.mkv", 900 * 1024 * 1024, False),
    ("Movie (2011).mkv", 900 * 1024 * 1024, False),
    ("Movie (2011).fa.srt", 40_000, False),
])
def test_junk_files(name, size, junk):
    assert is_junk_file(name, size, DEFAULTS["junk"], VIDEO_EXTS) is junk
