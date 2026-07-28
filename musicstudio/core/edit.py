"""Audio editing: an effect stack compiled into a single ffmpeg filter graph.

Nothing here touches a file. An :class:`EditSpec` is a description of what the
user wants; :func:`build_filter_chain` turns it into filter syntax, and
``convert.py`` runs it as part of the one and only encode. Keeping it that way
means a trim, a normalize and a format change all cost exactly one generation
of encoding rather than three.

Everything runs in 32-bit float internally, so intermediate stages have
effectively unlimited headroom -- you can boost by +30 dB, then pull back with a
limiter, and only the final encode quantises.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

from . import ffmpeg
from .probe import AudioInfo, LoudnessStats

#: ffmpeg's atempo filter accepts 0.5..100 per instance, but stays cleanest in
#: the 0.5..2.0 range, so larger changes are chained.
_ATEMPO_MIN = 0.5
_ATEMPO_MAX = 2.0


class GainMode(str, Enum):
    """How to handle a gain that pushes the signal past full scale."""

    #: Apply gain, then a brickwall true-peak limiter. Loud and clean.
    LIMIT = "limit"
    #: Compress first, then make up gain and limit. Loudest perceived result.
    COMPRESS = "compress"
    #: Raw gain, clipping allowed. Honest, and sometimes what you want.
    RAW = "raw"


class ChannelMode(str, Enum):
    KEEP = "keep"
    MONO = "mono"
    STEREO = "stereo"
    SWAP = "swap"


class SilenceMode(str, Enum):
    NONE = "none"
    LEADING = "leading"
    TRAILING = "trailing"
    BOTH = "both"


@dataclass(frozen=True)
class Region:
    """A span of the timeline, in seconds. ``end=None`` means to the end."""

    start: float = 0.0
    end: float | None = None

    def __post_init__(self) -> None:
        if self.start < 0:
            raise ValueError("Region start cannot be negative")
        if self.end is not None and self.end <= self.start:
            raise ValueError(f"Region end ({self.end}) must be after start ({self.start})")

    @property
    def duration(self) -> float | None:
        return None if self.end is None else self.end - self.start


@dataclass(frozen=True)
class EqBand:
    """One peaking-EQ band."""

    frequency: float
    gain_db: float
    q: float = 1.0


#: A sensible 10-band graphic EQ layout, matching what most players show.
DEFAULT_EQ_FREQUENCIES = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)


@dataclass
class EditSpec:
    """A complete, non-destructive description of the edits to apply."""

    # -- Time ----------------------------------------------------------
    #: Keep only this span. Applied before cuts.
    trim: Region | None = None
    #: Spans to remove from the timeline, joining what remains.
    cuts: list[Region] = field(default_factory=list)

    # -- Level ---------------------------------------------------------
    #: Gain in dB. May exceed 0 dB freely -- see ``gain_mode`` for what
    #: happens when the result would exceed full scale.
    gain_db: float = 0.0
    gain_mode: GainMode = GainMode.LIMIT
    #: True-peak ceiling for the limiter, in dBFS. -0.3 leaves room for the
    #: intersample peaks that lossy encoders and DACs can generate.
    limiter_ceiling_db: float = -0.3
    #: Compressor settings used by GainMode.COMPRESS.
    compress_threshold_db: float = -18.0
    compress_ratio: float = 4.0

    #: EBU R128 loudness normalization to a fixed target. Mutually exclusive
    #: with a manual gain in practice; if both are set, normalize runs first.
    normalize: bool = False
    normalize_target_lufs: float = -14.0
    #: Aggressive "make everything loud" dynamic normalizer.
    dynamic_normalize: bool = False

    fade_in: float = 0.0
    fade_out: float = 0.0

    # -- Spectrum & speed ----------------------------------------------
    #: Playback rate multiplier. 1.0 is unchanged; pitch is preserved.
    tempo: float = 1.0
    #: Pitch shift in semitones, tempo preserved.
    pitch_semitones: float = 0.0
    eq_bands: list[EqBand] = field(default_factory=list)

    # -- Cleanup & routing ---------------------------------------------
    trim_silence: SilenceMode = SilenceMode.NONE
    silence_threshold_db: float = -50.0
    channel_mode: ChannelMode = ChannelMode.KEEP
    #: Target sample rate. None keeps the source's.
    sample_rate: int | None = None

    # ------------------------------------------------------------------
    @property
    def is_empty(self) -> bool:
        """True when applying this spec would leave the audio untouched."""
        return not any(
            (
                self.trim is not None,
                self.cuts,
                abs(self.gain_db) > 1e-9,
                self.normalize,
                self.dynamic_normalize,
                self.fade_in > 0,
                self.fade_out > 0,
                abs(self.tempo - 1.0) > 1e-9,
                abs(self.pitch_semitones) > 1e-9,
                any(abs(b.gain_db) > 1e-9 for b in self.eq_bands),
                self.trim_silence is not SilenceMode.NONE,
                self.channel_mode is not ChannelMode.KEEP,
                self.sample_rate is not None,
            )
        )

    def output_channels(self, source_channels: int) -> int:
        """Channel count after this spec is applied.

        The encoder's ``-ac`` flag must agree with what the channel filters
        actually produce, or ffmpeg silently inserts a second conversion.
        """
        if self.channel_mode is ChannelMode.MONO:
            return 1
        if self.channel_mode is ChannelMode.STEREO:
            return 2
        return source_channels

    @property
    def needs_loudness_analysis(self) -> bool:
        """Whether a measurement pass must run before the real encode."""
        return self.normalize

    @property
    def changes_duration(self) -> bool:
        """Whether the output length differs from the input's."""
        return bool(
            self.trim is not None
            or self.cuts
            or abs(self.tempo - 1.0) > 1e-9
            or self.trim_silence is not SilenceMode.NONE
        )

    def estimated_duration(self, source_duration: float) -> float:
        """Best-effort output length, for progress reporting.

        Silence trimming is not predictable, so it is ignored here; progress
        may finish slightly early on files with long silent tails.
        """
        duration = source_duration
        if self.trim is not None:
            end = self.trim.end if self.trim.end is not None else duration
            duration = max(0.0, min(end, duration) - self.trim.start)
        for cut in self.cuts:
            cut_end = cut.end if cut.end is not None else duration
            duration -= max(0.0, min(cut_end, duration) - cut.start)
        if self.tempo > 0:
            duration /= self.tempo
        return max(0.0, duration)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def db_to_linear(db: float) -> float:
    """Convert decibels to a linear amplitude multiplier."""
    return 10.0 ** (db / 20.0)


def linear_to_db(linear: float) -> float:
    if linear <= 0:
        return float("-inf")
    return 20.0 * math.log10(linear)


def semitones_to_ratio(semitones: float) -> float:
    """Frequency ratio for a pitch shift of ``semitones``."""
    return 2.0 ** (semitones / 12.0)


def _atempo_factors(tempo: float) -> list[float]:
    """Split a tempo change into chained factors each within atempo's range.

    A single atempo instance handles 0.5..2.0 cleanly; going to 0.25x or 4x
    needs two, and the artefacts are far milder chained than forced.
    """
    if tempo <= 0:
        raise ValueError("Tempo must be positive")
    factors: list[float] = []
    remaining = tempo
    while remaining > _ATEMPO_MAX:
        factors.append(_ATEMPO_MAX)
        remaining /= _ATEMPO_MAX
    while remaining < _ATEMPO_MIN:
        factors.append(_ATEMPO_MIN)
        remaining /= _ATEMPO_MIN
    if abs(remaining - 1.0) > 1e-9:
        factors.append(remaining)
    return factors or [1.0]


def _escape(value: str) -> str:
    """Escape a value for use inside filter syntax."""
    return value.replace("\\", "\\\\").replace(":", r"\:").replace("'", r"\'")


# ---------------------------------------------------------------------------
# Filter chain construction
# ---------------------------------------------------------------------------


def _time_filters(spec: EditSpec, duration: float) -> list[str]:
    """Trim and cut, producing a continuous timeline."""
    filters: list[str] = []

    if spec.trim is not None:
        args = [f"start={spec.trim.start:.6f}"]
        if spec.trim.end is not None:
            args.append(f"end={spec.trim.end:.6f}")
        filters.append(f"atrim={':'.join(args)}")
        filters.append("asetpts=PTS-STARTPTS")

    if spec.cuts:
        # aselect drops the sample ranges we do not want; asetpts then closes
        # the resulting gaps so the output plays as one continuous piece.
        # Offsets are relative to the already-trimmed timeline.
        offset = spec.trim.start if spec.trim is not None else 0.0
        conditions = []
        for cut in spec.cuts:
            start = max(0.0, cut.start - offset)
            end = (cut.end if cut.end is not None else duration) - offset
            conditions.append(f"between(t\\,{start:.6f}\\,{end:.6f})")
        expression = "+".join(conditions)
        filters.append(f"aselect='not({expression})'")
        filters.append("asetpts=N/SR/TB")

    return filters


def _silence_filters(spec: EditSpec) -> list[str]:
    """Strip silence from the start and/or end."""
    if spec.trim_silence is SilenceMode.NONE:
        return []

    threshold = f"{spec.silence_threshold_db:g}dB"
    args = ["detection=peak"]
    if spec.trim_silence in (SilenceMode.LEADING, SilenceMode.BOTH):
        args += ["start_periods=1", "start_threshold=" + threshold, "start_silence=0"]
    if spec.trim_silence in (SilenceMode.TRAILING, SilenceMode.BOTH):
        # stop_periods=-1 tells silenceremove to strip trailing silence rather
        # than every internal silent passage, which would gut the music.
        args += ["stop_periods=-1", "stop_threshold=" + threshold, "stop_silence=0"]
    return [f"silenceremove={':'.join(args)}"]


def _eq_filters(spec: EditSpec) -> list[str]:
    """One peaking-EQ instance per active band."""
    return [
        f"equalizer=f={band.frequency:g}:t=q:w={band.q:g}:g={band.gain_db:g}"
        for band in spec.eq_bands
        if abs(band.gain_db) > 1e-9
    ]


def _speed_pitch_filters(spec: EditSpec, sample_rate: int) -> list[str]:
    """Tempo and pitch, preferring rubberband when the build has it."""
    filters: list[str] = []
    tempo_changed = abs(spec.tempo - 1.0) > 1e-9
    pitch_changed = abs(spec.pitch_semitones) > 1e-9

    if pitch_changed and ffmpeg.has_filter("rubberband"):
        # rubberband handles both at once with far better quality than the
        # asetrate workaround, and keeps formants sane on vocals.
        args = [f"pitch={semitones_to_ratio(spec.pitch_semitones):.9f}"]
        if tempo_changed:
            args.append(f"tempo={spec.tempo:.9f}")
        filters.append(f"rubberband={':'.join(args)}")
        return filters

    if pitch_changed:
        # Fallback: resample to shift pitch (which also shifts speed), then
        # correct the speed back with atempo.
        ratio = semitones_to_ratio(spec.pitch_semitones)
        filters.append(f"asetrate={int(round(sample_rate * ratio))}")
        filters.append(f"aresample={sample_rate}")
        for factor in _atempo_factors(1.0 / ratio):
            filters.append(f"atempo={factor:.9f}")

    if tempo_changed:
        for factor in _atempo_factors(spec.tempo):
            filters.append(f"atempo={factor:.9f}")

    return filters


def _loudness_filters(spec: EditSpec, measured: LoudnessStats | None) -> list[str]:
    """EBU R128 normalization, using measurements when we have them."""
    if not spec.normalize:
        return []

    args = [
        f"I={spec.normalize_target_lufs:g}",
        "TP=-1.5",
        "LRA=11",
    ]
    if measured is not None:
        # Second pass: hand loudnorm the real measurements so it applies a
        # precise linear gain. Without these it runs in dynamic single-pass
        # mode, which is less accurate and can pump.
        stats = measured.to_loudnorm_args()
        args += [
            f"measured_I={stats['measured_I']:g}",
            f"measured_TP={stats['measured_TP']:g}",
            f"measured_LRA={stats['measured_LRA']:g}",
            f"measured_thresh={stats['measured_thresh']:g}",
            f"offset={stats['offset']:g}",
            "linear=true",
        ]
    return [f"loudnorm={':'.join(args)}"]


def _gain_filters(spec: EditSpec) -> list[str]:
    """Gain, including boosts well past full scale.

    This is where "louder than the file allows" is handled. The signal is
    already in float, so the gain itself never clips; what matters is how we
    bring it back under the ceiling before the encoder quantises.
    """
    filters: list[str] = []
    has_gain = abs(spec.gain_db) > 1e-9

    if spec.gain_mode is GainMode.COMPRESS and has_gain:
        # Squash the peaks first so the makeup gain lifts the whole track
        # rather than just slamming transients into the limiter.
        filters.append(
            "acompressor="
            f"threshold={db_to_linear(spec.compress_threshold_db):.6f}:"
            f"ratio={spec.compress_ratio:g}:"
            "attack=20:release=250:makeup=1:knee=2.8:detection=rms"
        )

    if has_gain:
        filters.append(f"volume={spec.gain_db:.4f}dB")

    if spec.dynamic_normalize:
        # Frame-based normalization: brings quiet passages up to meet loud
        # ones. Heavy-handed on music, excellent on speech and old recordings.
        #
        # altboundary=1 is not optional. The gaussian window spans
        # gausssize * framelen = 15.5 seconds, and in the default boundary mode
        # any file shorter than that is treated as all-boundary and comes out
        # completely unchanged -- which silently breaks the feature for clips
        # and voice memos, exactly the files that need it most.
        filters.append(
            "dynaudnorm=framelen=500:gausssize=31:peak=0.95:maxgain=30:altboundary=1"
        )

    needs_limiter = spec.gain_mode in (GainMode.LIMIT, GainMode.COMPRESS) and (
        has_gain or spec.dynamic_normalize
    )
    if needs_limiter:
        ceiling = db_to_linear(spec.limiter_ceiling_db)
        if ffmpeg.has_filter("alimiter"):
            # A lookahead limiter catches peaks before they arrive, so it
            # ducks smoothly instead of chopping the waveform flat.
            filters.append(
                "alimiter="
                "level_in=1:level_out=1:"
                f"limit={ceiling:.6f}:"
                "attack=5:release=50:level=disabled"
            )
        else:
            # Hard clip is the honest fallback -- distorted, but never louder
            # than the ceiling.
            filters.append(f"acompressor=threshold={ceiling:.6f}:ratio=20:attack=0.01:release=50")

    return filters


def _fade_filters(spec: EditSpec, output_duration: float) -> list[str]:
    filters: list[str] = []
    if spec.fade_in > 0:
        filters.append(f"afade=t=in:st=0:d={spec.fade_in:.6f}:curve=tri")
    if spec.fade_out > 0 and output_duration > 0:
        start = max(0.0, output_duration - spec.fade_out)
        filters.append(f"afade=t=out:st={start:.6f}:d={spec.fade_out:.6f}:curve=tri")
    return filters


def _channel_filters(spec: EditSpec, channels: int) -> list[str]:
    """Channel folding and swapping."""
    if spec.channel_mode is ChannelMode.KEEP:
        return []
    if spec.channel_mode is ChannelMode.MONO:
        if channels <= 1:
            return []
        # Average rather than sum, or the fold would gain 6 dB and clip.
        return ["pan=mono|c0=0.5*c0+0.5*c1"] if channels == 2 else ["aformat=channel_layouts=mono"]
    if spec.channel_mode is ChannelMode.STEREO:
        if channels == 1:
            return ["pan=stereo|c0=c0|c1=c0"]
        return ["aformat=channel_layouts=stereo"]
    if spec.channel_mode is ChannelMode.SWAP:
        if channels < 2:
            return []
        return ["pan=stereo|c0=c1|c1=c0"]
    return []


def build_filter_chain(
    spec: EditSpec,
    info: AudioInfo,
    *,
    measured_loudness: LoudnessStats | None = None,
    target_sample_rate: int | None = None,
    target_bit_depth: int | None = None,
) -> list[str]:
    """Compile ``spec`` into an ordered list of ffmpeg filters.

    Order is deliberate:

    1. Convert to float, so every later stage has headroom.
    2. Time edits (trim, cuts) -- cheapest to do on the smallest signal.
    3. Silence trimming, before level changes alter what counts as silent.
    4. EQ and speed/pitch, which change the spectrum and therefore the peaks.
    5. Loudness normalization, then manual gain and dynamics.
    6. Fades, which must see the final level.
    7. Channel routing, then resampling and dither as the very last step.
    """
    filters: list[str] = []

    # 1. Work in 32-bit float from here on.
    filters.append("aformat=sample_fmts=fltp")

    # 2-4. Structure and spectrum.
    filters += _time_filters(spec, info.duration)
    filters += _silence_filters(spec)
    filters += _eq_filters(spec)
    filters += _speed_pitch_filters(spec, info.sample_rate or 44100)

    # 5. Level.
    filters += _loudness_filters(spec, measured_loudness)
    filters += _gain_filters(spec)

    # 6. Fades need the post-edit duration to place the out-fade correctly.
    filters += _fade_filters(spec, spec.estimated_duration(info.duration))

    # 7. Routing and final rate/depth conversion.
    filters += _channel_filters(spec, info.channels or 2)

    rate = target_sample_rate or spec.sample_rate
    resample_args = []
    if rate and rate != info.sample_rate:
        # soxr at precision 28 is transparent; the default resampler is not.
        resample_args += [str(rate), "resampler=soxr", "precision=28"]
    if target_bit_depth and info.bit_depth and target_bit_depth < info.bit_depth:
        # Truncating instead of dithering adds correlated quantisation noise
        # that is far more audible than the dither itself.
        resample_args += ["dither_method=triangular"]
    if resample_args:
        filters.append("aresample=" + ":".join(resample_args))

    # 8. Re-chunk into encoder-sized buffers.
    #
    # Some filters hand downstream a single enormous frame -- loudnorm is the
    # worst offender, because it internally resamples to 192 kHz and buffers
    # aggressively. FLAC rejects any block larger than 65535 samples outright
    # ("invalid block size"), which fails the whole conversion. Re-chunking
    # costs nothing and does not alter a single sample value.
    if len(filters) > 1:
        filters.append("asetnsamples=n=4096:p=0")

    return filters


def build_filter_string(spec: EditSpec, info: AudioInfo, **kwargs) -> str:
    """:func:`build_filter_chain` joined into a single ``-af`` argument."""
    return ",".join(build_filter_chain(spec, info, **kwargs))


# ---------------------------------------------------------------------------
# Loudness measurement pass
# ---------------------------------------------------------------------------


def build_loudness_analysis_command(path, spec: EditSpec, info: AudioInfo) -> list[str]:
    """Command for the loudnorm measurement pass.

    The measurement must see the audio exactly as the edits will leave it --
    measuring the raw source and then trimming half of it away would target the
    wrong loudness. So the same chain runs, minus normalization itself.
    """
    measure_spec = _spec_without_normalization(spec)
    chain = build_filter_chain(measure_spec, info)
    chain.append(
        f"loudnorm=I={spec.normalize_target_lufs:g}:TP=-1.5:LRA=11:print_format=json"
    )
    return [
        *ffmpeg.base_args(),
        "-i", str(path),
        "-af", ",".join(chain),
        "-f", "null", "-",
        *ffmpeg.progress_args(),
    ]


def _spec_without_normalization(spec: EditSpec) -> EditSpec:
    """A copy of ``spec`` with loudness normalization disabled."""
    from dataclasses import replace as dataclass_replace

    return dataclass_replace(spec, normalize=False)
