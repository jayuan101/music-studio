"""ReplayGain: measuring, tagging only the RG fields, and the library side."""

from __future__ import annotations

import math

import pytest

from musicstudio.core import convert, formats, replaygain
from musicstudio.core import tags as T
from musicstudio.core.convert import ConvertRequest
from musicstudio.db import Library, scan_into_library

from .conftest import make_tone, requires_ffmpeg

SAMPLE_SUMMARY = """\
[Parsed_ebur128_0 @ 0000026687a917c0] Summary:

  Integrated loudness:
    I:         -27.8 LUFS
    Threshold: -37.8 LUFS

  Loudness range:
    LRA:        20.0 LU

  True peak:
    Peak:      -24.1 dBFS
[out#0/null @ 0000026687a917c0] video:0KiB audio:258KiB
"""


def test_parse_summary():
    m = replaygain.parse_ebur128_summary(SAMPLE_SUMMARY)
    assert m.loudness_lufs == -27.8
    assert m.peak == pytest.approx(10 ** (-24.1 / 20))


def test_parse_summary_silence():
    text = SAMPLE_SUMMARY.replace("-27.8 LUFS", "-70.0 LUFS").replace("-24.1 dBFS", "-inf dBFS")
    m = replaygain.parse_ebur128_summary(text)
    assert m.peak == 0.0 and m.loudness_lufs == -70.0


def test_parse_summary_missing():
    with pytest.raises(ValueError):
        replaygain.parse_ebur128_summary("no summary here")


def test_formatting_round_trips():
    assert replaygain.format_gain(-6.2) == "-6.20 dB"
    assert replaygain.format_gain(1.3) == "+1.30 dB"
    assert replaygain.parse_gain("+1.30 dB") == 1.3
    assert replaygain.parse_gain("-6.2") == -6.2
    assert replaygain.parse_gain("") is None
    assert replaygain.parse_peak(replaygain.format_peak(0.98855)) == pytest.approx(0.98855)
    assert replaygain.parse_peak("junk") is None


def test_album_loudness_is_an_energy_mean():
    a = replaygain.Measurement(-10.0, 1.0)
    b = replaygain.Measurement(-20.0, 1.0)
    # Equal durations: mean energy of 10^-1 and 10^-2.
    assert replaygain.album_loudness([(a, 60), (b, 60)]) == pytest.approx(
        10 * math.log10((0.1 + 0.01) / 2)
    )
    # Silence does not drag the album down.
    silent = replaygain.Measurement(-math.inf, 0.0)
    assert replaygain.album_loudness([(a, 60), (silent, 60)]) == pytest.approx(-10.0)


@requires_ffmpeg
@pytest.mark.parametrize("profile_id", ["flac", "mp3", "aac", "opus", "wma", "wavpack"])
def test_write_fields_touches_only_those_fields(tone_flac, tmp_path, cover_png, profile_id):
    profile = formats.get_profile(profile_id)
    path = tmp_path / f"x{profile.extension}"
    convert.convert(ConvertRequest(tone_flac, path, profile, overwrite=True))
    T.write(path, T.TagSet(title="Keep me", artist="Me", comment="hi"),
            artwork=T.Artwork.from_bytes(cover_png))
    before = T.read(path)

    T.write_fields(path, replaygain_track_gain="-3.00 dB", replaygain_track_peak="0.500000")
    after = T.read(path)
    assert after.replaygain_track_gain == "-3.00 dB"
    assert after.replaygain_track_peak == "0.500000"
    assert (after.title, after.artist, after.comment) == ("Keep me", "Me", "hi")
    assert after.artwork.data == before.artwork.data

    T.write_fields(path, replaygain_track_gain="")
    assert T.read(path).replaygain_track_gain == ""
    assert T.read(path).replaygain_track_peak == "0.500000"


@requires_ffmpeg
def test_write_fields_keeps_the_id3_version(tone_flac, tmp_path):
    from mutagen.id3 import ID3

    path = tmp_path / "x.mp3"
    convert.convert(ConvertRequest(tone_flac, path, formats.get_profile("mp3"), overwrite=True))
    T.write(path, T.TagSet(title="t"))
    ID3(str(path)).save(str(path), v2_version=4)
    T.write_fields(path, replaygain_track_gain="-1.00 dB")
    assert ID3(str(path)).version[:2] == (2, 4)


def test_write_fields_rejects_other_fields(tmp_path):
    with pytest.raises(ValueError):
        T.write_fields(tmp_path / "x.flac", title="nope")


@requires_ffmpeg
def test_scan_tags_an_album_and_a_single(tmp_path):
    folder = tmp_path / "music"
    quiet = make_tone(folder / "quiet.flac", extra=["-af", "volume=-24dB"])
    loud = make_tone(folder / "loud.flac", extra=["-af", "volume=-6dB"])
    single = make_tone(folder / "single.flac")
    for path, n in ((quiet, 1), (loud, 2)):
        T.write(path, T.TagSet(title=path.stem, artist="A", album="LP", track_number=n))
    T.write(single, T.TagSet(title="single", artist="B"))

    library = Library(tmp_path / "t.db")
    scan_into_library(library, [folder])

    # Selecting one album track still measures the whole album.
    result = replaygain.scan_library([quiet, single], library=library)
    assert not result.failed
    assert len(result.updated) == 3

    q, lo, s = (library.get(p) for p in (quiet, loud, single))
    assert q.rg_track_gain > lo.rg_track_gain
    assert q.rg_track_gain - lo.rg_track_gain == pytest.approx(18, abs=0.5)
    assert lo.rg_track_gain < q.rg_album_gain < q.rg_track_gain
    assert q.rg_album_gain == lo.rg_album_gain
    assert q.rg_album_peak == pytest.approx(lo.rg_track_peak)
    assert s.rg_track_gain is not None and s.rg_album_gain is None
    assert T.read(loud).replaygain_album_gain == replaygain.format_gain(lo.rg_album_gain)

    # A second pass has nothing to do.
    again = replaygain.scan_library([quiet, loud, single], library=library)
    assert not again.updated and len(again.skipped) == 3


def test_has_replaygain_rule(tmp_path):
    from musicstudio.core.probe import AudioInfo

    library = Library(tmp_path / "t.db")
    for name, gain in (("a.flac", "-3.00 dB"), ("b.flac", "")):
        path = tmp_path / name
        path.write_bytes(b"x")
        info = AudioInfo(path, "flac", "flac", "", 1.0, 44100, 2, "", 16, 0, 1, {})
        library.upsert(path, info, T.TagSet(title=name, replaygain_track_gain=gain))
    rows = library.query_rules({"rules": [{"field": "has_replaygain", "op": "is_false"}]})
    assert [r.title for r in rows] == ["b.flac"]


class _Row:
    def __init__(self, tg=None, tp=None, ag=None, ap=None):
        self.rg_track_gain, self.rg_track_peak = tg, tp
        self.rg_album_gain, self.rg_album_peak = ag, ap


def test_playback_gain_modes():
    row = _Row(tg=-6.0, tp=0.9, ag=-4.0, ap=0.95)
    assert replaygain.playback_gain_db(row, mode="off") == 0.0
    assert replaygain.playback_gain_db(row, mode="track") == -6.0
    assert replaygain.playback_gain_db(row, mode="album") == -4.0
    assert replaygain.playback_gain_db(_Row(tg=-6.0, tp=0.9), mode="album") == -6.0
    assert replaygain.playback_gain_db(_Row(), mode="track") == 0.0
    assert replaygain.playback_gain_db(None, mode="track") == 0.0
    assert replaygain.playback_gain_db(row, mode="track", preamp_db=2.0) == -4.0


def test_playback_gain_never_clips():
    # +6 dB on a track peaking at 0.5 (-6 dBFS) would land exactly at full
    # scale; +10 is cut back to that.
    row = _Row(tg=10.0, tp=0.5)
    assert replaygain.playback_gain_db(row, mode="track") == pytest.approx(6.02, abs=0.01)
