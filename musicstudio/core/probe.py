"""Inspecting source files with ffprobe.

The :class:`AudioInfo` this produces is what every quality decision in the app
is based on: whether the source is lossless, what its real sample rate and bit
depth are, and therefore what a conversion can and cannot preserve.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from . import ffmpeg

#: Codecs that carry every original sample. Converting between any two of these
#: is a true bit-preserving operation; converting *into* one from anything else
#: is not, no matter what the file extension ends up saying.
LOSSLESS_CODECS = frozenset(
    {
        "flac",
        "alac",
        "wavpack",
        "tta",
        "tak",
        "ape",  # Monkey's Audio
        "mlp",
        "truehd",
        "als",
        "shorten",
        "wmalossless",
        "pcm_s16le", "pcm_s16be", "pcm_s24le", "pcm_s24be",
        "pcm_s32le", "pcm_s32be", "pcm_u8", "pcm_f32le", "pcm_f64le",
    }
)

#: Maps ffprobe's sample_fmt to the number of *meaningful* bits per sample.
_SAMPLE_FMT_BITS = {
    "u8": 8, "u8p": 8,
    "s16": 16, "s16p": 16,
    "s32": 32, "s32p": 32,
    "flt": 32, "fltp": 32,
    "dbl": 64, "dblp": 64,
}


@dataclass(frozen=True)
class AudioInfo:
    """Everything we need to know about a source file before touching it."""

    path: Path
    #: Container format short name, e.g. "flac", "mov,mp4,m4a,...".
    container: str
    #: Audio codec short name, e.g. "flac", "mp3", "aac".
    codec: str
    codec_long_name: str
    duration: float
    sample_rate: int
    channels: int
    channel_layout: str
    #: Bits per sample actually stored. 0 when the codec has no fixed depth
    #: (all lossy codecs), in which case bit depth is not a meaningful concept.
    bit_depth: int
    #: Stream bitrate in bits/second, falling back to the container's.
    bitrate: int
    size_bytes: int
    #: Tags ffprobe found, lowercased keys. Authoritative tag reads go through
    #: `tags.py`; this is only for quick display and library scanning.
    tags: dict[str, str]

    @property
    def is_lossless(self) -> bool:
        return self.codec in LOSSLESS_CODECS

    @property
    def bitrate_kbps(self) -> int:
        return round(self.bitrate / 1000) if self.bitrate else 0

    def describe_technical(self) -> str:
        """Codec, rate, depth, channels and bitrate -- with no lossless verdict.

        Used next to the lossless/lossy badge, which already says that part;
        including it here too reads as "LOSSY · … · lossy".
        """
        parts = [self.codec.upper()]
        if self.sample_rate:
            parts.append(f"{self.sample_rate / 1000:g} kHz")
        if self.bit_depth:
            parts.append(f"{self.bit_depth}-bit")
        if self.channels:
            parts.append(self.channel_layout or f"{self.channels}ch")
        if self.bitrate_kbps:
            parts.append(f"{self.bitrate_kbps} kbps")
        return " · ".join(parts)

    def describe(self) -> str:
        """A one-line technical summary, including whether it is lossless."""
        return f"{self.describe_technical()} · {'lossless' if self.is_lossless else 'lossy'}"


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bit_depth_from_stream(stream: dict) -> int:
    """Real stored bit depth, preferring the codec's own report.

    Returns 0 for lossy codecs, where bit depth is not a property of the file
    at all -- MP3 and AAC store frequency coefficients, not samples, and the
    32-bit float their decoders emit says nothing about the source.

    For lossless codecs, ``bits_per_raw_sample`` is what was actually stored;
    ``sample_fmt`` only describes the decoder's output buffer, which is often
    wider (24-bit FLAC decodes into s32). Trusting sample_fmt alone would make
    every 24-bit file look like 32-bit and quietly inflate it on conversion.
    """
    if stream.get("codec_name") not in LOSSLESS_CODECS:
        return 0
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        depth = _to_int(stream.get(key))
        if depth:
            return depth
    return _SAMPLE_FMT_BITS.get(stream.get("sample_fmt", ""), 0)


def probe(path: str | Path) -> AudioInfo:
    """Read technical details of ``path``.

    Raises :class:`ValueError` when the file holds no decodable audio stream.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"No such file: {path}")

    output = ffmpeg.run(
        [
            str(ffmpeg.ffprobe_path()),
            "-hide_banner",
            "-loglevel", "error",
            "-show_format",
            "-show_streams",
            "-select_streams", "a",
            "-of", "json",
            str(path),
        ],
        timeout=60,
    )
    data = json.loads(output or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise ValueError(f"No audio stream found in {path.name}")
    stream = streams[0]
    fmt = data.get("format") or {}

    # ffprobe will happily label 20 bytes of text as a FLAC stream, reporting
    # sample_rate 0 and channels 0. Accepting that would put a garbage row in
    # the library and fail confusingly later, so reject it here.
    if _to_int(stream.get("sample_rate")) <= 0 or _to_int(stream.get("channels")) <= 0:
        raise ValueError(f"{path.name} contains no decodable audio")

    raw_tags = {**(fmt.get("tags") or {}), **(stream.get("tags") or {})}
    tags = {str(k).lower(): str(v) for k, v in raw_tags.items()}

    duration = _to_float(stream.get("duration")) or _to_float(fmt.get("duration"))
    bitrate = _to_int(stream.get("bit_rate")) or _to_int(fmt.get("bit_rate"))

    return AudioInfo(
        path=path,
        container=str(fmt.get("format_name", "")),
        codec=str(stream.get("codec_name", "")),
        codec_long_name=str(stream.get("codec_long_name", "")),
        duration=duration,
        sample_rate=_to_int(stream.get("sample_rate")),
        channels=_to_int(stream.get("channels")),
        channel_layout=str(stream.get("channel_layout", "")),
        bit_depth=_bit_depth_from_stream(stream),
        bitrate=bitrate,
        size_bytes=_to_int(fmt.get("size")) or path.stat().st_size,
        tags=tags,
    )


def try_probe(path: str | Path) -> AudioInfo | None:
    """Like :func:`probe`, but returns None instead of raising.

    Used when scanning folders, where hitting one unreadable file must not
    abort the whole import.
    """
    try:
        return probe(path)
    except (OSError, ValueError, json.JSONDecodeError, ffmpeg.FFmpegError, ffmpeg.FFmpegNotFound):
        return None


@dataclass(frozen=True)
class LoudnessStats:
    """EBU R128 measurements from ffmpeg's ``loudnorm`` analysis pass."""

    input_i: float          # integrated loudness, LUFS
    input_tp: float         # true peak, dBTP
    input_lra: float        # loudness range, LU
    input_thresh: float
    target_offset: float

    def to_loudnorm_args(self) -> dict[str, float]:
        return {
            "measured_I": self.input_i,
            "measured_TP": self.input_tp,
            "measured_LRA": self.input_lra,
            "measured_thresh": self.input_thresh,
            "offset": self.target_offset,
        }


def parse_loudnorm_json(stderr: str) -> LoudnessStats | None:
    """Pull the JSON block ffmpeg's loudnorm prints to stderr after analysis.

    ffmpeg writes normal log lines before the JSON, so we scan backwards for
    the last ``{`` and parse from there.
    """
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
        return LoudnessStats(
            input_i=float(data["input_i"]),
            input_tp=float(data["input_tp"]),
            input_lra=float(data["input_lra"]),
            input_thresh=float(data["input_thresh"]),
            target_offset=float(data.get("target_offset", 0.0)),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


@dataclass(frozen=True)
class ClippingReport:
    """How hard a gain setting is driving the signal past full scale."""

    #: True peak after gain, in dBFS. Positive means it exceeds full scale.
    peak_dbfs: float
    #: Samples that would be flattened against the ceiling on encode.
    clipped_samples: int
    total_samples: int

    @property
    def clipped_fraction(self) -> float:
        return self.clipped_samples / self.total_samples if self.total_samples else 0.0

    @property
    def clips(self) -> bool:
        return self.clipped_samples > 0

    def describe(self) -> str:
        """Plain-language summary for the editor panel."""
        if not self.clips:
            return f"Peak {self.peak_dbfs:+.1f} dBFS · {-self.peak_dbfs:.1f} dB headroom left"
        return (
            f"Peak {self.peak_dbfs:+.1f} dBFS · "
            f"{self.clipped_samples:,} of {self.total_samples:,} samples clipped "
            f"({self.clipped_fraction:.1%})"
        )


_ASTATS_LABEL = re.compile(r"\[Parsed_astats_(\d+)\s*@")


def _parse_astats_blocks(stderr: str) -> list[dict[str, float]]:
    """Split ffmpeg stderr into one dict per astats instance, in *chain* order.

    ffmpeg does not print filter summaries in chain order -- it tears the graph
    down in reverse, so the last astats in the chain reports first. Ordering by
    the ``Parsed_astats_N`` index instead of by appearance is the only reliable
    way to tell which measurement came from which point in the chain.
    """
    blocks: dict[int, dict[str, float]] = {}
    current: dict[str, float] | None = None

    for raw in stderr.splitlines():
        match = _ASTATS_LABEL.search(raw)
        if match is None:
            continue
        index = int(match.group(1))
        line = raw.split("] ", 1)[-1].strip()

        if line == "Overall":
            current = blocks.setdefault(index, {})
            continue
        if ":" not in line:
            continue
        target = blocks.setdefault(index, {})
        # Per-channel sections repeat the keys; only the Overall block is set
        # as `current`, so ignore anything arriving before it.
        if current is not target:
            continue
        key, _, value = line.partition(":")
        target[key.strip()] = _to_float(value.strip(), 0.0)

    return [blocks[index] for index in sorted(blocks)]


def measure_clipping(
    path: str | Path,
    gain_db: float = 0.0,
    *,
    filters: list[str] | None = None,
) -> ClippingReport:
    """Measure the true peak and clipped-sample count of a processed signal.

    ffmpeg processes in 32-bit float, which never clips, so the true peak is
    read from a float-domain ``astats``. The signal is then clamped to a fixed
    integer format and measured again -- samples pinned to full scale there are
    exactly the ones the encoder would flatten.

    ``filters`` is the processing to measure *through*, normally the editor's
    real filter chain from ``edit.build_filter_chain``. Passing only ``gain_db``
    measures a bare gain, which is a different signal: a boost with the limiter
    engaged does not clip, and reporting that it does steers people away from
    the mode they should be using. Whatever the caller wants the number to
    describe has to actually be in the chain.

    Callers pass a prebuilt chain rather than an EditSpec so this module keeps
    no dependency on ``edit`` -- ``edit`` already imports from here.
    """
    path = Path(path)
    if filters:
        prefix = ",".join(filters) + ","
    elif gain_db:
        prefix = f"volume={gain_db:.4f}dB,"
    else:
        prefix = ""
    measures = "Peak_level+Abs_Peak_count+Number_of_samples"
    chain = (
        f"{prefix}"
        f"astats=measure_perchannel=none:measure_overall={measures},"
        f"aformat=sample_fmts=s32,"
        f"astats=measure_perchannel=none:measure_overall={measures}"
    )
    stderr = ffmpeg.run_with_progress(
        [
            *ffmpeg.base_args(),
            "-i", str(path),
            "-af", chain,
            "-f", "null", "-",
            *ffmpeg.progress_args(),
        ]
    )

    blocks = _parse_astats_blocks(stderr)
    if len(blocks) < 2:
        return ClippingReport(peak_dbfs=0.0, clipped_samples=0, total_samples=0)

    float_stats, clamped_stats = blocks[0], blocks[1]
    peak_dbfs = float_stats.get("Peak level dB", 0.0)
    total = int(clamped_stats.get("Number of samples", 0))
    # Below full scale nothing is pinned, so the peak count is just the
    # waveform's own maxima -- not clipping. Only count when we actually exceed.
    clipped = int(clamped_stats.get("Abs Peak count", 0)) if peak_dbfs > 0 else 0
    return ClippingReport(peak_dbfs=peak_dbfs, clipped_samples=clipped, total_samples=total)
