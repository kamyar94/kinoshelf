import struct

from conftest import fake_file, make_video, needs_ffmpeg

from kinoshelf import media
from kinoshelf.config import DEFAULTS


def _reference_hash(path):
    data = path.read_bytes()
    h = len(data)
    for chunk in (data[:65536], data[-65536:]):
        for (v,) in struct.iter_unpack("<Q", chunk):
            h = (h + v) % 2 ** 64
    return f"{h:016x}"


def test_oshash_matches_the_published_method(tmp_path):
    f = fake_file(tmp_path / "a.bin", 1_000_003 - 3)
    assert media.oshash(f) == _reference_hash(f)
    assert media.oshash(fake_file(tmp_path / "small.bin", 1000)) is None


def test_tail_sum_changes_when_the_end_changes(tmp_path):
    f = fake_file(tmp_path / "b.bin", 200_000)
    before = media.tail_sum(f)
    with open(f, "r+b") as fh:
        fh.seek(-10, 2)
        fh.write(b"0123456789")
    assert media.tail_sum(f) != before


def test_playable_verdicts():
    dev = DEFAULTS["device"]
    base = {"video_codec": "h264", "audio": [{"codec": "aac"}], "height": 1080}
    assert media.playable(dict(base, container="mp4"), dev)["status"] == "ok"
    assert media.playable(dict(base, container="avi"), dev)["status"] == "rewrap"
    assert media.playable(dict(base, container="mkv", audio=[{"codec": "dts"}]), dev)["status"] == "convert"
    assert media.playable({}, dev)["status"] == "unknown"


@needs_ffmpeg
def test_probe_frames_and_describe(tmp_path):
    v = make_video(tmp_path / "clip.mkv", 4)
    info = media.probe(v)
    assert 3.5 < info["duration_s"] < 4.5
    assert info["video_codec"] in ("h264", "mpeg4") and info["width"] == 160
    d = media.describe(info)
    assert d["Length"] == "0:00:04" and "160x90" in d["Picture"]
    frames = media.frames(v, tmp_path / "frames", info["duration_s"])
    assert len(frames) == 4 and all(f.stat().st_size > 0 for f in frames)
