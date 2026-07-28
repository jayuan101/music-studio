"""Filter-graph construction.

These assert on the generated filter strings, so they are fast and need no
encoding. The end-to-end behaviour is covered in test_convert.py.
"""

from __future__ import annotations

import pytest

from musicstudio.core.edit import (
    ChannelMode,
    EditSpec,
    EqBand,
    GainMode,
    Region,
    SilenceMode,
    _atempo_factors,
    build_filter_chain,
    db_to_linear,
    semitones_to_ratio,
)
from musicstudio.core.probe import AudioInfo
from pathlib import Path


def make_info(**overrides) -> AudioInfo:
    defaults = dict(
        path=Path("x.flac"), container="flac", codec="flac", codec_long_name="FLAC",
        duration=60.0, sample_rate=48000, channels=2, channel_layout="stereo",
        bit_depth=24, bitrate=900_000, size_bytes=1_000_000, tags={},
    )
    defaults.update(overrides)
    return AudioInfo(**defaults)


INFO = make_info()


def chain_of(spec: EditSpec, info: AudioInfo = INFO, **kwargs) -> str:
    return ",".join(build_filter_chain(spec, info, **kwargs))


# ---------------------------------------------------------------------------
# Maths helpers
# ---------------------------------------------------------------------------


def test_db_to_linear_round_numbers():
    assert db_to_linear(0) == pytest.approx(1.0)
    assert db_to_linear(6) == pytest.approx(1.995, rel=1e-3)
    assert db_to_linear(20) == pytest.approx(10.0)
    assert db_to_linear(-6) == pytest.approx(0.501, rel=1e-3)


def test_semitones_to_ratio():
    assert semitones_to_ratio(0) == pytest.approx(1.0)
    assert semitones_to_ratio(12) == pytest.approx(2.0)
    assert semitones_to_ratio(-12) == pytest.approx(0.5)


@pytest.mark.parametrize(
    "tempo,expected_count",
    [(1.0, 1), (1.5, 1), (2.0, 1), (0.5, 1), (4.0, 2), (0.25, 2), (8.0, 3)],
)
def test_atempo_chains_beyond_single_filter_range(tempo, expected_count):
    factors = _atempo_factors(tempo)
    assert len(factors) == expected_count
    product = 1.0
    for factor in factors:
        assert 0.5 <= factor <= 2.0
        product *= factor
    assert product == pytest.approx(tempo)


def test_atempo_rejects_zero():
    with pytest.raises(ValueError):
        _atempo_factors(0)


# ---------------------------------------------------------------------------
# Spec semantics
# ---------------------------------------------------------------------------


def test_empty_spec_is_detected():
    assert EditSpec().is_empty
    assert not EditSpec(gain_db=3).is_empty
    assert not EditSpec(trim=Region(0, 5)).is_empty
    # A zeroed EQ band is not an edit.
    assert EditSpec(eq_bands=[EqBand(100, 0.0)]).is_empty


def test_region_rejects_backwards_and_negative():
    with pytest.raises(ValueError):
        Region(5, 2)
    with pytest.raises(ValueError):
        Region(-1)


def test_estimated_duration_accounts_for_trim_cuts_and_tempo():
    spec = EditSpec(trim=Region(10, 50), cuts=[Region(20, 25)], tempo=2.0)
    # 40s kept, minus a 5s cut, then halved by the tempo change.
    assert spec.estimated_duration(60.0) == pytest.approx(17.5)


def test_output_channels_follows_channel_mode():
    assert EditSpec().output_channels(2) == 2
    assert EditSpec(channel_mode=ChannelMode.MONO).output_channels(2) == 1
    assert EditSpec(channel_mode=ChannelMode.STEREO).output_channels(1) == 2
    assert EditSpec(channel_mode=ChannelMode.SWAP).output_channels(2) == 2


# ---------------------------------------------------------------------------
# Chain construction
# ---------------------------------------------------------------------------


def test_chain_always_works_in_float():
    assert build_filter_chain(EditSpec(gain_db=3), INFO)[0] == "aformat=sample_fmts=fltp"


def test_trim_uses_atrim_and_resets_timestamps():
    chain = chain_of(EditSpec(trim=Region(1.5, 4.0)))
    assert "atrim=start=1.500000:end=4.000000" in chain
    assert "asetpts=PTS-STARTPTS" in chain


def test_cuts_select_around_removed_regions():
    chain = chain_of(EditSpec(cuts=[Region(10, 20), Region(30, 35)]))
    assert "aselect=" in chain
    assert "between(t\\,10.000000\\,20.000000)" in chain
    assert "between(t\\,30.000000\\,35.000000)" in chain
    assert "asetpts=N/SR/TB" in chain


def test_cut_offsets_are_relative_to_the_trimmed_timeline():
    # With a trim starting at 10s, a cut at 15s must become 5s post-trim.
    chain = chain_of(EditSpec(trim=Region(10, 40), cuts=[Region(15, 20)]))
    assert "between(t\\,5.000000\\,10.000000)" in chain


# -- gain ------------------------------------------------------------------


def test_limit_mode_adds_a_limiter_at_the_ceiling():
    chain = chain_of(EditSpec(gain_db=24, gain_mode=GainMode.LIMIT, limiter_ceiling_db=-0.3))
    assert "volume=24.0000dB" in chain
    assert "alimiter=" in chain
    assert f"limit={db_to_linear(-0.3):.6f}" in chain


def test_raw_mode_never_limits():
    """Raw gain is the one mode that must be allowed to clip."""
    chain = chain_of(EditSpec(gain_db=24, gain_mode=GainMode.RAW))
    assert "volume=24.0000dB" in chain
    assert "alimiter" not in chain
    assert "acompressor" not in chain


def test_compress_mode_compresses_before_boosting():
    chain = chain_of(EditSpec(gain_db=24, gain_mode=GainMode.COMPRESS))
    assert chain.index("acompressor") < chain.index("volume=")
    assert "alimiter" in chain


def test_gain_can_far_exceed_full_scale():
    """The whole point of the boost feature: +45 dB must be expressible."""
    chain = chain_of(EditSpec(gain_db=45, gain_mode=GainMode.LIMIT))
    assert "volume=45.0000dB" in chain


def test_no_limiter_without_gain():
    assert "alimiter" not in chain_of(EditSpec())


def test_dynaudnorm_uses_altboundary():
    """Without altboundary=1 the filter silently does nothing to short files."""
    chain = chain_of(EditSpec(dynamic_normalize=True))
    assert "dynaudnorm=" in chain
    assert "altboundary=1" in chain


# -- loudness --------------------------------------------------------------


def test_loudnorm_single_pass_without_measurements():
    chain = chain_of(EditSpec(normalize=True, normalize_target_lufs=-16))
    assert "loudnorm=I=-16" in chain
    assert "measured_I" not in chain


def test_loudnorm_second_pass_uses_measurements():
    from musicstudio.core.probe import LoudnessStats

    stats = LoudnessStats(-22.5, -3.1, 7.2, -33.0, 0.5)
    chain = chain_of(EditSpec(normalize=True), measured_loudness=stats)
    assert "measured_I=-22.5" in chain
    assert "measured_TP=-3.1" in chain
    assert "linear=true" in chain


# -- spectrum --------------------------------------------------------------


def test_eq_emits_one_band_per_nonzero_gain():
    chain = chain_of(EditSpec(eq_bands=[EqBand(60, 6), EqBand(1000, 0), EqBand(8000, -3)]))
    assert "equalizer=f=60:t=q:w=1:g=6" in chain
    assert "equalizer=f=8000" in chain
    assert "f=1000" not in chain


def test_pitch_prefers_rubberband_when_available(monkeypatch):
    monkeypatch.setattr("musicstudio.core.ffmpeg.has_filter", lambda name: True)
    chain = chain_of(EditSpec(pitch_semitones=5))
    assert "rubberband=pitch=" in chain
    assert "asetrate" not in chain


def test_pitch_falls_back_without_rubberband(monkeypatch):
    monkeypatch.setattr("musicstudio.core.ffmpeg.has_filter", lambda name: name != "rubberband")
    chain = chain_of(EditSpec(pitch_semitones=12))
    assert "rubberband" not in chain
    assert "asetrate=96000" in chain  # one octave up from 48 kHz
    assert "atempo=" in chain


# -- routing ---------------------------------------------------------------


def test_mono_fold_averages_rather_than_sums():
    """Summing would add 6 dB and clip; the fold must halve each channel."""
    assert "pan=mono|c0=0.5*c0+0.5*c1" in chain_of(EditSpec(channel_mode=ChannelMode.MONO))


def test_channel_swap():
    assert "pan=stereo|c0=c1|c1=c0" in chain_of(EditSpec(channel_mode=ChannelMode.SWAP))


def test_silence_trim_uses_stop_periods_minus_one():
    """stop_periods=-1 strips the tail; a positive value would gut the middle."""
    chain = chain_of(EditSpec(trim_silence=SilenceMode.BOTH))
    assert "silenceremove=" in chain
    assert "stop_periods=-1" in chain
    assert "start_periods=1" in chain


def test_leading_only_does_not_touch_the_tail():
    chain = chain_of(EditSpec(trim_silence=SilenceMode.LEADING))
    assert "start_periods=1" in chain
    assert "stop_periods" not in chain


# -- rate and depth --------------------------------------------------------


def test_resample_uses_soxr_at_high_precision():
    chain = chain_of(EditSpec(), target_sample_rate=44100)
    assert "aresample=44100:resampler=soxr:precision=28" in chain


def test_no_resample_when_rate_already_matches():
    assert "aresample" not in chain_of(EditSpec(), target_sample_rate=48000)


def test_dither_applied_only_when_reducing_depth():
    reducing = chain_of(EditSpec(), target_sample_rate=44100, target_bit_depth=16)
    assert "dither_method=triangular" in reducing
    keeping = chain_of(EditSpec(), target_sample_rate=44100, target_bit_depth=24)
    assert "dither_method" not in keeping


def test_chain_rechunks_to_protect_the_encoder():
    """loudnorm emits frames larger than FLAC's 65535-sample block limit."""
    chain = chain_of(EditSpec(normalize=True))
    assert chain.endswith("asetnsamples=n=4096:p=0")


def test_no_rechunk_when_nothing_is_applied():
    assert build_filter_chain(EditSpec(), INFO) == ["aformat=sample_fmts=fltp"]
