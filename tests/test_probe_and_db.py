"""File inspection and the library index."""

from __future__ import annotations

import pytest

from musicstudio.core import formats, probe
from musicstudio.core import tags as T
from musicstudio.db import Library, find_audio_files, scan_into_library

from .conftest import make_tone, requires_ffmpeg

pytestmark = requires_ffmpeg


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_reads_technical_details(tone_flac):
    info = probe.probe(tone_flac)
    assert info.codec == "flac"
    assert info.sample_rate == 48000
    assert info.channels == 2
    assert info.duration == pytest.approx(3.0, abs=0.05)
    assert info.is_lossless


def test_bit_depth_reflects_stored_bits_not_the_decode_buffer(tone_flac):
    """24-bit FLAC decodes into a 32-bit buffer; reporting 32 would inflate
    every conversion of it."""
    assert probe.probe(tone_flac).bit_depth == 24


def test_lossy_codecs_report_no_bit_depth(tone_mp3):
    """Bit depth is not a property of an MP3 at all."""
    info = probe.probe(tone_mp3)
    assert not info.is_lossless
    assert info.bit_depth == 0


def test_describe_technical_omits_the_lossless_verdict(tone_flac):
    info = probe.probe(tone_flac)
    assert "lossless" not in info.describe_technical()
    assert "lossless" in info.describe()


def test_probe_raises_on_a_file_with_no_audio(tmp_path):
    junk = tmp_path / "x.flac"
    junk.write_bytes(b"definitely not audio")
    with pytest.raises((ValueError, OSError, Exception)):
        probe.probe(junk)


def test_try_probe_returns_none_instead_of_raising(tmp_path):
    junk = tmp_path / "x.flac"
    junk.write_bytes(b"nope")
    assert probe.try_probe(junk) is None


def test_probe_missing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        probe.probe(tmp_path / "absent.flac")


# ---------------------------------------------------------------------------
# Clipping measurement
# ---------------------------------------------------------------------------


def test_gain_raises_the_measured_peak(tone_flac):
    base = probe.measure_clipping(tone_flac, 0)
    boosted = probe.measure_clipping(tone_flac, 6)
    assert boosted.peak_dbfs == pytest.approx(base.peak_dbfs + 6, abs=0.2)


def test_no_clipping_reported_below_full_scale(tone_flac):
    report = probe.measure_clipping(tone_flac, 0)
    assert not report.clips
    assert report.clipped_samples == 0
    assert "headroom" in report.describe()


def test_extreme_gain_reports_clipped_samples(tone_flac):
    """This number is what the editor shows when you use raw gain."""
    report = probe.measure_clipping(tone_flac, 40)
    assert report.peak_dbfs > 0
    assert report.clips
    assert report.clipped_samples > 0
    assert 0 < report.clipped_fraction <= 1
    assert "clipped" in report.describe()


def test_peak_tracks_gain_across_the_whole_range(tone_flac):
    """Regression: ffmpeg prints astats summaries in reverse chain order, so
    reading the blocks by appearance swapped the float and clamped passes. The
    bug hid below 0 dBFS, where both passes happen to agree."""
    base = probe.measure_clipping(tone_flac, 0).peak_dbfs
    for gain in (12, 24, 36, 48):
        report = probe.measure_clipping(tone_flac, gain)
        assert report.peak_dbfs == pytest.approx(base + gain, abs=0.3)
        # Clipping must appear only once the peak actually exceeds full scale.
        assert report.clips == (base + gain > 0)


def _measure_with(path, spec):
    """Measure a file through a real edit chain, the way the editor does."""
    from musicstudio.core import edit as edit_module

    info = probe.probe(path)
    return probe.measure_clipping(
        path, filters=edit_module.build_filter_chain(spec, info)
    )


def test_limiter_mode_reports_no_clipping(tone_flac):
    """Regression: the measurement applied a bare gain and ignored the mode, so
    a limited boost was reported as clipping just as badly as a raw one."""
    from musicstudio.core.edit import EditSpec, GainMode

    report = _measure_with(
        tone_flac,
        EditSpec(gain_db=48, gain_mode=GainMode.LIMIT, limiter_ceiling_db=-0.3),
    )
    assert not report.clips
    assert report.clipped_samples == 0
    assert report.peak_dbfs == pytest.approx(-0.3, abs=0.2)


def test_raw_mode_reports_heavy_clipping(tone_flac):
    from musicstudio.core.edit import EditSpec, GainMode

    report = _measure_with(tone_flac, EditSpec(gain_db=48, gain_mode=GainMode.RAW))
    assert report.clips
    assert report.clipped_samples > 0
    assert report.peak_dbfs > 0


def test_the_gain_modes_disagree(tone_flac):
    """The assertion that would have caught the bug: whatever the numbers are,
    limiting and not limiting cannot produce the same answer."""
    from musicstudio.core.edit import EditSpec, GainMode

    raw = _measure_with(tone_flac, EditSpec(gain_db=48, gain_mode=GainMode.RAW))
    limited = _measure_with(tone_flac, EditSpec(gain_db=48, gain_mode=GainMode.LIMIT))
    assert raw.clipped_samples != limited.clipped_samples
    assert raw.peak_dbfs > limited.peak_dbfs


def test_measurement_sees_effects_other_than_gain(tone_flac):
    """Normalisation moves the peak, so the measurement must reflect it."""
    from musicstudio.core.edit import EditSpec

    plain = probe.measure_clipping(tone_flac)
    normalised = _measure_with(tone_flac, EditSpec(normalize=True))
    assert normalised.peak_dbfs != pytest.approx(plain.peak_dbfs, abs=1.0)


def test_bare_gain_still_works_without_a_chain(tone_flac):
    """The simple call signature stays supported for non-editor callers."""
    base = probe.measure_clipping(tone_flac).peak_dbfs
    assert probe.measure_clipping(tone_flac, 6).peak_dbfs == pytest.approx(base + 6, abs=0.2)


def test_astats_blocks_are_ordered_by_filter_index():
    stderr = (
        "[Parsed_astats_3 @ 0x1] Overall\n"
        "[Parsed_astats_3 @ 0x1] Peak level dB: 0.000000\n"
        "[Parsed_astats_1 @ 0x2] Overall\n"
        "[Parsed_astats_1 @ 0x2] Peak level dB: 5.500000\n"
    )
    blocks = probe._parse_astats_blocks(stderr)
    # astats_1 comes first in the chain even though it printed last.
    assert blocks[0]["Peak level dB"] == pytest.approx(5.5)
    assert blocks[1]["Peak level dB"] == pytest.approx(0.0)


def test_loudnorm_json_parsing():
    stderr = (
        "some ffmpeg log line\n"
        '{ "input_i" : "-22.50", "input_tp" : "-3.10", "input_lra" : "7.20",'
        '  "input_thresh" : "-33.00", "target_offset" : "0.50" }'
    )
    stats = probe.parse_loudnorm_json(stderr)
    assert stats is not None
    assert stats.input_i == pytest.approx(-22.5)
    assert stats.input_tp == pytest.approx(-3.1)


def test_loudnorm_json_parsing_handles_garbage():
    assert probe.parse_loudnorm_json("no json here") is None
    assert probe.parse_loudnorm_json("{ not valid }") is None


# ---------------------------------------------------------------------------
# Format registry
# ---------------------------------------------------------------------------


def test_every_profile_is_retrievable_by_id():
    for profile in formats.ALL_PROFILES:
        assert formats.get_profile(profile.id) is profile


def test_unknown_profile_id_lists_the_valid_ones():
    with pytest.raises(ValueError, match="Available"):
        formats.get_profile("mp9")


def test_lossless_profiles_are_offered_first():
    """The first entries drive the default choice, so quality leads."""
    assert formats.ALL_PROFILES[0].lossless


def test_profile_lookup_by_extension():
    assert formats.profile_for_extension(".flac") is formats.FLAC
    assert formats.profile_for_extension("ogg") is formats.VORBIS
    assert formats.profile_for_extension(".aif") is formats.AIFF
    assert formats.profile_for_extension(".xyz") is None


def test_opus_declares_its_48k_limit():
    assert 96000 not in (formats.OPUS.supported_sample_rates or ())
    assert 48000 in (formats.OPUS.supported_sample_rates or ())


# ---------------------------------------------------------------------------
# Library index
# ---------------------------------------------------------------------------


@pytest.fixture
def library(tmp_path):
    return Library(tmp_path / "test.db")


@pytest.fixture
def music_folder(tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    make_tone(folder / "a.flac", codec="flac", duration=1.0)
    make_tone(folder / "b.mp3", codec="libmp3lame", duration=1.0)
    nested = folder / "album"
    nested.mkdir()
    make_tone(nested / "c.flac", codec="flac", duration=1.0)
    (folder / "notes.txt").write_text("not audio")
    (folder / "cover.jpg").write_bytes(b"not audio either")
    return folder


def test_find_audio_files_recurses_and_ignores_non_audio(music_folder):
    found = find_audio_files([music_folder])
    names = sorted(p.name for p in found)
    assert names == ["a.flac", "b.mp3", "c.flac"]


def test_find_audio_files_deduplicates(music_folder):
    found = find_audio_files([music_folder, music_folder, music_folder / "a.flac"])
    assert len(found) == 3


def test_scan_indexes_every_track(library, music_folder):
    imported, skipped = scan_into_library(library, [music_folder])
    assert imported == 3
    assert library.count() == 3


def test_rescan_skips_unchanged_files(library, music_folder):
    scan_into_library(library, [music_folder])
    imported, skipped = scan_into_library(library, [music_folder])
    assert imported == 0
    assert skipped == 3


def test_rescan_picks_up_a_modified_file(library, music_folder):
    scan_into_library(library, [music_folder])
    target = music_folder / "a.flac"
    T.write(target, T.TagSet(title="Changed Title", artist="New Artist"))

    imported, _ = scan_into_library(library, [music_folder])
    assert imported == 1
    assert library.get(target).title == "Changed Title"


def test_scan_survives_an_unreadable_file(library, music_folder):
    (music_folder / "broken.flac").write_bytes(b"corrupt")
    imported, skipped = scan_into_library(library, [music_folder])
    assert imported == 3
    assert skipped >= 1


def test_tags_are_indexed(library, music_folder):
    target = music_folder / "a.flac"
    T.write(target, T.TagSet(title="Song", artist="Band", album="Record", track_number=4))
    scan_into_library(library, [target])

    row = library.get(target)
    assert row.title == "Song"
    assert row.artist == "Band"
    assert row.track_number == 4
    assert row.is_lossless


def test_search_matches_across_fields(library, music_folder):
    T.write(music_folder / "a.flac", T.TagSet(title="Unique Song", artist="Findable"))
    scan_into_library(library, [music_folder])

    assert len(library.search("Unique")) == 1
    assert len(library.search("Findable")) == 1
    assert len(library.search("nothing here")) == 0
    assert len(library.search("")) == 3


def test_stats_summarise_the_library(library, music_folder):
    scan_into_library(library, [music_folder])
    stats = library.stats()
    assert stats["tracks"] == 3
    assert stats["lossless"] == 2  # two FLACs, one MP3
    assert stats["size"] > 0


def test_prune_removes_rows_for_deleted_files(library, music_folder):
    scan_into_library(library, [music_folder])
    (music_folder / "b.mp3").unlink()
    assert library.prune_missing() == 1
    assert library.count() == 2


def test_upsert_updates_rather_than_duplicating(library, music_folder):
    target = music_folder / "a.flac"
    scan_into_library(library, [target])
    scan_into_library(library, [target], force=True)
    assert library.count() == 1


def test_artwork_presence_is_indexed(library, music_folder, cover_png):
    target = music_folder / "a.flac"
    T.write(target, T.TagSet(title="With Art"), artwork=T.Artwork.from_bytes(cover_png))
    scan_into_library(library, [target])

    row = library.get(target)
    assert row.has_artwork
    assert row.artwork_width == 240
