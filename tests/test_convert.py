"""Conversion: the quality policy, command construction, and real encodes."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from musicstudio.core import convert, ffmpeg, formats, probe
from musicstudio.core.convert import ConvertRequest, Severity, resolve_output
from musicstudio.core.edit import ChannelMode, EditSpec, GainMode, Region

from .conftest import make_tone, requires_ffmpeg
from .test_edit import make_info

pytestmark = requires_ffmpeg


# ---------------------------------------------------------------------------
# Quality policy (no encoding required)
# ---------------------------------------------------------------------------


def _titles(output) -> list[str]:
    return [note.title for note in output.notes]


def test_lossy_to_lossless_is_warned_not_blocked():
    output = resolve_output(make_info(codec="mp3", bit_depth=0), formats.FLAC)
    assert "Lossy source, lossless target" in _titles(output)
    assert any(n.severity is Severity.WARNING for n in output.notes)


def test_lossy_to_lossy_warns_about_generation_loss():
    output = resolve_output(make_info(codec="mp3", bit_depth=0), formats.OPUS)
    assert "Re-encoding lossy audio" in _titles(output)


def test_lossless_to_lossless_is_reported_as_bit_perfect():
    output = resolve_output(make_info(codec="flac"), formats.WAVPACK)
    assert "Bit-perfect conversion" in _titles(output)
    assert all(n.severity is Severity.INFO for n in output.notes if "Bit-perfect" in n.title)


def test_source_rate_and_depth_are_preserved_by_default():
    output = resolve_output(make_info(sample_rate=96000, bit_depth=24), formats.FLAC)
    assert output.sample_rate == 96000
    assert output.bit_depth == 24


def test_format_rate_limit_forces_a_downsample_and_says_so():
    """Opus cannot store 96 kHz, so it must resample and explain why."""
    output = resolve_output(make_info(sample_rate=96000), formats.OPUS)
    assert output.sample_rate == 48000
    assert "Sample rate changed" in _titles(output)


def test_never_upsamples_to_reach_a_supported_rate():
    output = resolve_output(make_info(sample_rate=44100), formats.MP3)
    assert output.sample_rate == 44100


def test_requested_downsample_is_warned():
    output = resolve_output(make_info(sample_rate=96000), formats.FLAC, sample_rate=44100)
    assert output.sample_rate == 44100
    assert "Sample rate changed by request" in _titles(output)


def test_padding_bit_depth_is_called_out_as_pointless():
    output = resolve_output(make_info(bit_depth=16), formats.FLAC, bit_depth=24)
    assert "Bit depth padded" in _titles(output)


def test_alac_cannot_hold_32_bit_and_warns():
    output = resolve_output(make_info(bit_depth=32), formats.ALAC)
    assert output.bit_depth == 24
    assert "Bit depth reduced" in _titles(output)


def test_lossy_defaults_to_vbr_where_supported():
    output = resolve_output(make_info(), formats.MP3)
    assert output.vbr_quality == "0"
    assert output.bitrate is None


def test_lossy_without_vbr_uses_the_default_bitrate():
    output = resolve_output(make_info(), formats.AAC)
    assert output.bitrate == 256


def test_low_bitrate_is_flagged():
    output = resolve_output(make_info(), formats.AAC, bitrate=96)
    assert "Low bitrate" in _titles(output)


def test_channel_override_is_respected():
    output = resolve_output(make_info(channels=2), formats.FLAC, channels=1)
    assert output.channels == 1


def test_wav_reports_that_it_cannot_carry_artwork():
    assert "No embedded cover art" in _titles(resolve_output(make_info(), formats.WAV))


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def test_command_encoder_flags_for_flac(tmp_path):
    info = make_info()
    output = resolve_output(info, formats.FLAC)
    request = ConvertRequest(Path("in.flac"), tmp_path / "out.flac", formats.FLAC)
    command = convert.build_command(request, info, output)

    assert "-c:a" in command and command[command.index("-c:a") + 1] == "flac"
    assert "-compression_level" in command and "8" in command
    assert command[command.index("-sample_fmt") + 1] == "s32"
    assert command[command.index("-bits_per_raw_sample") + 1] == "24"
    assert "-vn" in command  # never carry a video/art stream into the encoder


def test_alac_gets_planar_sample_format(tmp_path):
    """ALAC accepts s32p, not s32; the packed form fails the whole encode."""
    info = make_info()
    output = resolve_output(info, formats.ALAC)
    command = convert.build_command(
        ConvertRequest(Path("in.flac"), tmp_path / "o.m4a", formats.ALAC), info, output
    )
    assert command[command.index("-sample_fmt") + 1] == "s32p"


def test_pcm_targets_pick_a_depth_specific_encoder(tmp_path):
    info = make_info(bit_depth=16)
    output = resolve_output(info, formats.WAV)
    command = convert.build_command(
        ConvertRequest(Path("in.flac"), tmp_path / "o.wav", formats.WAV), info, output
    )
    assert command[command.index("-c:a") + 1] == "pcm_s16le"
    assert "-sample_fmt" not in command


def test_encoder_rate_matches_the_filter_chain(tmp_path):
    """A mismatch makes ffmpeg resample twice and can break the encoder."""
    info = make_info(sample_rate=96000)
    request = ConvertRequest(
        Path("in.flac"), tmp_path / "o.opus", formats.OPUS, edits=EditSpec(sample_rate=44100)
    )
    output = resolve_output(info, formats.OPUS, sample_rate=44100)
    command = convert.build_command(request, info, output)
    encoder_rate = command[command.index("-ar") + 1]
    filters = command[command.index("-af") + 1]
    assert f"aresample={encoder_rate}:" in filters


def test_metadata_mapping_can_be_disabled(tmp_path):
    info = make_info()
    output = resolve_output(info, formats.FLAC)
    request = ConvertRequest(
        Path("in.flac"), tmp_path / "o.flac", formats.FLAC, copy_metadata=False
    )
    command = convert.build_command(request, info, output)
    assert command[command.index("-map_metadata") + 1] == "-1"


# ---------------------------------------------------------------------------
# Real encodes
# ---------------------------------------------------------------------------

ALL_IDS = ["flac", "alac", "wav", "aiff", "wavpack", "mp3", "aac", "opus", "vorbis", "wma"]


@pytest.mark.parametrize("profile_id", ALL_IDS)
def test_every_output_format_encodes(tone_flac, tmp_path, profile_id):
    profile = formats.get_profile(profile_id)
    result = convert.convert(
        ConvertRequest(tone_flac, tmp_path / f"out{profile.extension}", profile, overwrite=True)
    )
    assert result.destination.exists()
    assert result.destination.stat().st_size > 0
    assert result.result_info is not None
    assert result.result_info.duration == pytest.approx(3.0, abs=0.15)


@pytest.mark.parametrize("profile_id", ["flac", "alac", "wav", "aiff", "wavpack"])
def test_lossless_targets_preserve_rate_depth_and_channels(tone_flac, tmp_path, profile_id):
    profile = formats.get_profile(profile_id)
    result = convert.convert(
        ConvertRequest(tone_flac, tmp_path / f"o{profile.extension}", profile, overwrite=True)
    )
    info = result.result_info
    assert info.sample_rate == 48000
    assert info.channels == 2
    assert info.bit_depth == 24
    assert info.is_lossless


def _pcm_digest(path: Path) -> str:
    """Hash the decoded samples, so container differences do not matter."""
    proc = subprocess.run(
        [str(ffmpeg.ffmpeg_path()), "-hide_banner", "-loglevel", "error",
         "-i", str(path), "-f", "s32le", "-"],
        capture_output=True, check=True,
    )
    return hashlib.md5(proc.stdout).hexdigest()


def test_lossless_round_trip_is_bit_identical(tone_flac, tmp_path):
    """FLAC -> WAV -> FLAC must return the exact original samples."""
    wav = tmp_path / "rt.wav"
    back = tmp_path / "rt.flac"
    convert.convert(ConvertRequest(tone_flac, wav, formats.WAV, overwrite=True))
    convert.convert(ConvertRequest(wav, back, formats.FLAC, overwrite=True))
    assert _pcm_digest(tone_flac) == _pcm_digest(wav) == _pcm_digest(back)


def test_lossy_output_is_smaller_than_lossless(tone_flac, tmp_path):
    result = convert.convert(
        ConvertRequest(tone_flac, tmp_path / "o.opus", formats.OPUS, overwrite=True)
    )
    assert result.size_change < 1.0


def test_existing_file_is_not_overwritten_by_default(tone_flac, tmp_path):
    destination = tmp_path / "taken.flac"
    destination.write_bytes(b"not audio")
    result = convert.convert(
        ConvertRequest(tone_flac, destination, formats.FLAC, overwrite=False)
    )
    assert result.destination != destination
    assert destination.read_bytes() == b"not audio"


def test_converting_onto_the_source_writes_beside_it(tone_flac):
    """Encoding onto the input would truncate it mid-read."""
    result = convert.convert(
        ConvertRequest(tone_flac, tone_flac, formats.FLAC, overwrite=True)
    )
    assert result.destination != tone_flac
    assert tone_flac.exists()


# ---------------------------------------------------------------------------
# Edits applied through a real conversion
# ---------------------------------------------------------------------------


def test_trim_produces_the_expected_duration(tone_flac, tmp_path):
    result = convert.convert(
        ConvertRequest(
            tone_flac, tmp_path / "t.flac", formats.FLAC,
            edits=EditSpec(trim=Region(0.5, 2.0)), overwrite=True,
        )
    )
    assert result.result_info.duration == pytest.approx(1.5, abs=0.05)


def test_cut_shortens_by_the_removed_span(tone_flac, tmp_path):
    result = convert.convert(
        ConvertRequest(
            tone_flac, tmp_path / "c.flac", formats.FLAC,
            edits=EditSpec(cuts=[Region(1.0, 1.5)]), overwrite=True,
        )
    )
    assert result.result_info.duration == pytest.approx(2.5, abs=0.06)


def test_tempo_change_scales_duration(tone_flac, tmp_path):
    result = convert.convert(
        ConvertRequest(
            tone_flac, tmp_path / "s.flac", formats.FLAC,
            edits=EditSpec(tempo=2.0), overwrite=True,
        )
    )
    assert result.result_info.duration == pytest.approx(1.5, abs=0.1)


def test_mono_fold_reaches_the_output_file(tone_flac, tmp_path):
    result = convert.convert(
        ConvertRequest(
            tone_flac, tmp_path / "m.flac", formats.FLAC,
            edits=EditSpec(channel_mode=ChannelMode.MONO), overwrite=True,
        )
    )
    assert result.result_info.channels == 1


def test_edit_sample_rate_reaches_the_output_file(tone_flac, tmp_path):
    result = convert.convert(
        ConvertRequest(
            tone_flac, tmp_path / "r.flac", formats.FLAC,
            edits=EditSpec(sample_rate=44100), overwrite=True,
        )
    )
    assert result.result_info.sample_rate == 44100


def test_limited_boost_stops_at_the_ceiling(tone_flac, tmp_path):
    """A huge boost with the limiter on must land on the ceiling, not clip."""
    destination = tmp_path / "loud.flac"
    convert.convert(
        ConvertRequest(
            tone_flac, destination, formats.FLAC,
            edits=EditSpec(gain_db=45, gain_mode=GainMode.LIMIT, limiter_ceiling_db=-0.3),
            overwrite=True,
        )
    )
    report = probe.measure_clipping(destination)
    assert report.peak_dbfs == pytest.approx(-0.3, abs=0.35)
    assert not report.clips


def test_raw_boost_is_allowed_to_clip(tone_flac, tmp_path):
    """Raw mode is the user's explicit choice to accept distortion."""
    destination = tmp_path / "raw.flac"
    convert.convert(
        ConvertRequest(
            tone_flac, destination, formats.FLAC,
            edits=EditSpec(gain_db=45, gain_mode=GainMode.RAW), overwrite=True,
        )
    )
    assert probe.measure_clipping(destination).peak_dbfs >= -0.05


def test_gain_actually_raises_the_level(tone_flac, tmp_path):
    before = probe.measure_clipping(tone_flac).peak_dbfs
    destination = tmp_path / "g.flac"
    convert.convert(
        ConvertRequest(
            tone_flac, destination, formats.FLAC,
            edits=EditSpec(gain_db=12, gain_mode=GainMode.RAW), overwrite=True,
        )
    )
    assert probe.measure_clipping(destination).peak_dbfs == pytest.approx(before + 12, abs=0.3)


def test_dynamic_normalize_lifts_a_quiet_file(tone_flac, tmp_path):
    """Regression: without altboundary=1 this silently did nothing."""
    before = probe.measure_clipping(tone_flac).peak_dbfs
    destination = tmp_path / "dyn.flac"
    convert.convert(
        ConvertRequest(
            tone_flac, destination, formats.FLAC,
            edits=EditSpec(dynamic_normalize=True), overwrite=True,
        )
    )
    assert probe.measure_clipping(destination).peak_dbfs > before + 3


def test_loudness_normalization_runs_two_passes(tone_flac, tmp_path):
    destination = tmp_path / "n.flac"
    result = convert.convert(
        ConvertRequest(
            tone_flac, destination, formats.FLAC,
            edits=EditSpec(normalize=True, normalize_target_lufs=-14), overwrite=True,
        )
    )
    assert result.destination.exists()
    # A pure tone normalises to close to its target peak; just assert it moved.
    assert probe.measure_clipping(destination).peak_dbfs > -30


def test_stacked_edits_into_a_lossy_format(tone_flac, tmp_path):
    """The combination that previously broke the encoder with a bad block size."""
    result = convert.convert(
        ConvertRequest(
            tone_flac, tmp_path / "combo.mp3", formats.MP3,
            edits=EditSpec(
                trim=Region(0.5, 2.5), normalize=True, gain_db=6,
                fade_in=0.2, fade_out=0.2, channel_mode=ChannelMode.MONO,
                sample_rate=44100,
            ),
            overwrite=True,
        )
    )
    info = result.result_info
    assert info.channels == 1
    assert info.sample_rate == 44100
    assert info.duration == pytest.approx(2.0, abs=0.12)
