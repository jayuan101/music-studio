"""Auto-trim: detecting and removing music-video intro/outro noise."""

from __future__ import annotations

import subprocess

import pytest

from musicstudio.config import Settings
from musicstudio.core import autotrim
from musicstudio.core import ffmpeg
from musicstudio.core import tags as T
from musicstudio.db import Library

from .conftest import make_tone, requires_ffmpeg

pytestmark = requires_ffmpeg


# ---------------------------------------------------------------------------
# AutoTrimSettings.from_settings
# ---------------------------------------------------------------------------


def test_from_settings_maps_every_field():
    settings = Settings(
        auto_trim_silence_threshold_db=-42.0,
        auto_trim_max_intro_s=7.0,
        auto_trim_max_outro_s=9.0,
        auto_trim_detect_speech=True,
    )
    trim_settings = autotrim.AutoTrimSettings.from_settings(settings)
    assert trim_settings.threshold_db == -42.0
    assert trim_settings.max_intro_s == 7.0
    assert trim_settings.max_outro_s == 9.0
    assert trim_settings.detect_speech is True


# ---------------------------------------------------------------------------
# parse_silencedetect
# ---------------------------------------------------------------------------


def test_parse_silencedetect_pairs_start_and_end():
    stderr = (
        "[silencedetect @ 0x1] silence_start: 0\n"
        "[silencedetect @ 0x1] silence_end: 3.5 | silence_duration: 3.5\n"
        "[silencedetect @ 0x1] silence_start: 96.2\n"
        "[silencedetect @ 0x1] silence_end: 99.9 | silence_duration: 3.7\n"
    )
    spans = autotrim.parse_silencedetect(stderr)
    assert spans == [
        autotrim.SilenceSpan(0.0, 3.5),
        autotrim.SilenceSpan(96.2, 99.9),
    ]


def test_parse_silencedetect_handles_a_trailing_unmatched_start():
    """A silence that runs to end-of-file never gets a silence_end line."""
    stderr = (
        "[silencedetect @ 0x1] silence_start: 90.0\n"
    )
    spans = autotrim.parse_silencedetect(stderr)
    assert spans == [autotrim.SilenceSpan(90.0, None)]


def test_parse_silencedetect_ignores_unrelated_lines():
    stderr = "size=  1024kB time=00:01:40.00 bitrate= 838.9kbits/s speed=42x\n"
    assert autotrim.parse_silencedetect(stderr) == []


# ---------------------------------------------------------------------------
# compute_trim_region
# ---------------------------------------------------------------------------


def test_leading_silence_only_trims_the_start():
    region = autotrim.compute_trim_region(
        [autotrim.SilenceSpan(0.0, 5.0)],
        100.0, max_intro_s=12.0, max_outro_s=12.0, min_trim_s=1.0,
    )
    assert region.start == pytest.approx(5.0)
    assert region.end is None


def test_trailing_silence_only_trims_the_end():
    region = autotrim.compute_trim_region(
        [autotrim.SilenceSpan(90.0, None)],
        100.0, max_intro_s=12.0, max_outro_s=12.0, min_trim_s=1.0,
    )
    assert region.start == 0.0
    assert region.end == pytest.approx(90.0)


def test_both_ends_trimmed_together():
    region = autotrim.compute_trim_region(
        [autotrim.SilenceSpan(0.0, 3.0), autotrim.SilenceSpan(95.0, None)],
        100.0, max_intro_s=12.0, max_outro_s=12.0, min_trim_s=1.0,
    )
    assert region.start == pytest.approx(3.0)
    assert region.end == pytest.approx(95.0)


def test_internal_silence_is_never_touched():
    """A silent passage in the middle of the song must be left alone."""
    region = autotrim.compute_trim_region(
        [autotrim.SilenceSpan(40.0, 45.0)],
        100.0, max_intro_s=12.0, max_outro_s=12.0, min_trim_s=1.0,
    )
    assert region is None


def test_no_silence_at_all_returns_none():
    assert autotrim.compute_trim_region(
        [], 100.0, max_intro_s=12.0, max_outro_s=12.0, min_trim_s=1.0
    ) is None


def test_silence_longer_than_the_cap_is_capped_not_fully_removed():
    """A 30-second logo bumper is only ever cut back to the cap."""
    region = autotrim.compute_trim_region(
        [autotrim.SilenceSpan(0.0, 30.0)],
        100.0, max_intro_s=12.0, max_outro_s=12.0, min_trim_s=1.0,
    )
    assert region.start == pytest.approx(12.0)


def test_silence_shorter_than_min_trim_is_ignored():
    region = autotrim.compute_trim_region(
        [autotrim.SilenceSpan(0.0, 0.5)],
        100.0, max_intro_s=12.0, max_outro_s=12.0, min_trim_s=1.0,
    )
    assert region is None


# ---------------------------------------------------------------------------
# looks_like_video_source
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "source_url, title, filename, expected",
    [
        ("https://www.youtube.com/watch?v=abc", "", "", True),
        ("https://youtu.be/abc", "", "", True),
        ("https://soundcloud.com/someone/a-song", "", "", True),
        ("", "Some Song (Official Video)", "", True),
        ("", "", "Some Song [Official Audio].flac", True),
        ("", "Normal Song", "track.flac", False),
        ("https://example.com/track.flac", "Normal Song", "track.flac", False),
    ],
)
def test_looks_like_video_source(source_url, title, filename, expected):
    assert (
        autotrim.looks_like_video_source(source_url=source_url, title=title, filename=filename)
        is expected
    )


# ---------------------------------------------------------------------------
# End-to-end: analyse() / autotrim_track() against real audio
# ---------------------------------------------------------------------------


def _make_tone_with_leading_silence(path, *, silence_s: float, tone_s: float):
    """A tone preceded by true digital silence, built with ffmpeg's anullsrc."""
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        str(ffmpeg.ffmpeg_path()),
        "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"anullsrc=r=48000:cl=stereo:d={silence_s}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={tone_s}:sample_rate=48000",
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[out]",
        "-map", "[out]",
        "-c:a", "flac",
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path


def test_analyse_detects_injected_leading_silence(tmp_path):
    path = _make_tone_with_leading_silence(tmp_path / "silent_intro.flac", silence_s=4.0, tone_s=3.0)
    region = autotrim.analyse(path, settings=autotrim.AutoTrimSettings(max_intro_s=12.0))
    assert region is not None
    assert region.start == pytest.approx(4.0, abs=0.3)


def test_autotrim_track_shrinks_duration_and_keeps_tags(tmp_path):
    from musicstudio.core.probe import probe as probe_fn

    path = _make_tone_with_leading_silence(tmp_path / "video_rip.flac", silence_s=4.0, tone_s=3.0)
    T.write(path, T.TagSet(title="Cool Song (Official Video)", artist="Band"))
    before = probe_fn(path).duration

    outcome = autotrim.autotrim_track(path, force=True)

    assert outcome.updated
    after = probe_fn(path).duration
    assert after < before - 3.0  # most of the injected silence is gone
    tags_after = T.read(path)
    assert tags_after.title == "Cool Song (Official Video)"
    assert tags_after.artist == "Band"


def test_autotrim_track_leaves_non_video_sourced_tracks_alone(tone_flac):
    """A normal studio track must never be touched, even with force=False."""
    T.write(tone_flac, T.TagSet(title="Normal Song", artist="Band"))
    outcome = autotrim.autotrim_track(tone_flac)
    assert outcome.state == autotrim.AutoTrimState.NOT_APPLICABLE
    assert not outcome.updated


def test_autotrim_track_skips_when_no_silence_found(tone_flac):
    """A short plain tone with no injected silence has nothing to trim."""
    T.write(tone_flac, T.TagSet(title="Song (Official Video)", artist="Band"))
    outcome = autotrim.autotrim_track(tone_flac, force=True)
    assert outcome.state == autotrim.AutoTrimState.SKIPPED
    assert not outcome.updated


# ---------------------------------------------------------------------------
# Library integration: candidate filtering and state persistence
# ---------------------------------------------------------------------------


def test_autotrim_candidates_filters_by_state_and_heuristic(tmp_path):
    from musicstudio.db import scan_into_library

    library = Library(tmp_path / "test.db")
    folder = tmp_path / "music"
    folder.mkdir()

    video_track = make_tone(folder / "video.flac", duration=1.0)
    T.write(video_track, T.TagSet(title="Song (Official Video)", artist="Band"))
    normal_track = make_tone(folder / "normal.flac", duration=1.0)
    T.write(normal_track, T.TagSet(title="Normal Song", artist="Band"))

    scan_into_library(library, [folder])
    candidates = {c.path.name for c in library.autotrim_candidates()}
    assert candidates == {"video.flac"}

    library.set_auto_trim_state(video_track, "applied")
    assert library.autotrim_candidates() == []


def test_upsert_does_not_reset_auto_trim_state(tmp_path):
    from musicstudio.core import probe as probe_module
    from musicstudio.db import scan_into_library

    library = Library(tmp_path / "test.db")
    track = make_tone(tmp_path / "video.flac", duration=1.0)
    T.write(track, T.TagSet(title="Song (Official Video)", artist="Band"))

    scan_into_library(library, [track])
    library.set_auto_trim_state(track, "applied")

    # Simulate a rescan picking the file back up.
    scan_into_library(library, [track], force=True)
    assert library.get(track).auto_trim_state == "applied"
