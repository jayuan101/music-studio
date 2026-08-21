"""Format conversion, with an explicit quality policy.

The rule this module exists to enforce: **one encode generation, and never a
silent downgrade.** Edits and format changes are composed into a single ffmpeg
invocation, the source's sample rate and bit depth survive unless the target
format genuinely cannot carry them, and anything that costs quality is reported
back to the caller as a warning rather than happening quietly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from . import edit, ffmpeg, formats, probe
from .edit import EditSpec
from .formats import FormatProfile
from .probe import AudioInfo


#: CD quality. Used as the normalisation target when the user turns off
#: "preserve source rate/depth" -- the sensible interpretation of not
#: preserving is "bring everything down to a standard", not "do nothing".
STANDARD_SAMPLE_RATE = 44100
STANDARD_BIT_DEPTH = 16


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"


@dataclass(frozen=True)
class QualityNote:
    """Something the user should know before this conversion runs."""

    severity: Severity
    title: str
    detail: str

    def __str__(self) -> str:
        return f"{self.title}: {self.detail}"


@dataclass(frozen=True)
class OutputSpec:
    """The resolved technical shape of the output file."""

    profile: FormatProfile
    encoder: str
    sample_rate: int
    bit_depth: int
    channels: int
    #: ffmpeg sample format for lossless targets, e.g. "s32".
    sample_fmt: str | None
    #: Bitrate in kbps for CBR lossy encodes.
    bitrate: int | None = None
    #: VBR quality value when the encoder supports it.
    vbr_quality: str | None = None
    notes: tuple[QualityNote, ...] = field(default_factory=tuple)

    @property
    def resamples(self) -> bool:
        return self.sample_rate > 0


@dataclass
class ConvertRequest:
    """Everything needed to produce one output file."""

    source: Path
    destination: Path
    profile: FormatProfile
    edits: EditSpec = field(default_factory=EditSpec)
    #: Override the source's sample rate. None keeps it where the format allows.
    sample_rate: int | None = None
    #: Override the source's bit depth (lossless targets only).
    bit_depth: int | None = None
    #: CBR bitrate in kbps for lossy targets. None uses the profile default.
    bitrate: int | None = None
    #: VBR quality for encoders that support it. Takes precedence over bitrate.
    vbr_quality: str | None = None
    #: Copy tags from the source into the output.
    copy_metadata: bool = True
    overwrite: bool = False


# ---------------------------------------------------------------------------
# Quality policy
# ---------------------------------------------------------------------------


def _nearest_supported_rate(rate: int, supported: tuple[int, ...]) -> int:
    """The highest supported rate that does not exceed ``rate``.

    Downsampling loses the top octave; upsampling to reach a supported rate
    would only inflate the file, so we never round up past the source.
    """
    at_or_below = [r for r in supported if r <= rate]
    return max(at_or_below) if at_or_below else min(supported)


def resolve_output(
    info: AudioInfo,
    profile: FormatProfile,
    *,
    sample_rate: int | None = None,
    bit_depth: int | None = None,
    bitrate: int | None = None,
    vbr_quality: str | None = None,
    channels: int | None = None,
    preserve_rate: bool = True,
    preserve_depth: bool = True,
) -> OutputSpec:
    """Work out the best output settings, and what they cost.

    This is the heart of the quality policy. Every decision either preserves
    the source or produces a note explaining what was given up and why.
    """
    notes: list[QualityNote] = []

    # -- Generation loss ------------------------------------------------
    if not info.is_lossless and profile.lossless:
        notes.append(
            QualityNote(
                Severity.WARNING,
                "Lossy source, lossless target",
                f"{info.codec.upper()} has already discarded detail permanently. "
                f"Converting to {profile.label} preserves exactly what is left -- "
                f"it cannot restore anything, and the file will get several times "
                f"larger for no gain in sound quality.",
            )
        )
    elif not info.is_lossless and profile.is_lossy:
        notes.append(
            QualityNote(
                Severity.WARNING,
                "Re-encoding lossy audio",
                f"Going {info.codec.upper()} to {profile.label} decodes and "
                f"re-encodes, so a second round of compression artefacts is added "
                f"on top of the first. Keep the original if you can.",
            )
        )
    elif info.is_lossless and profile.is_lossy:
        notes.append(
            QualityNote(
                Severity.INFO,
                "Lossless to lossy",
                f"{profile.label} at the settings below is generally considered "
                f"transparent, but this step cannot be undone. Keep the "
                f"{info.codec.upper()} original as your master.",
            )
        )
    elif info.is_lossless and profile.lossless:
        notes.append(
            QualityNote(
                Severity.INFO,
                "Bit-perfect conversion",
                f"Both {info.codec.upper()} and {profile.label} are lossless, so "
                f"every sample survives intact.",
            )
        )

    # -- Sample rate ----------------------------------------------------
    source_rate = info.sample_rate or STANDARD_SAMPLE_RATE
    if sample_rate:
        target_rate = sample_rate
    elif preserve_rate:
        target_rate = source_rate
    else:
        # "Don't preserve" means normalise down to CD standard -- the point of
        # turning it off is shrinking a hi-res library for a phone or car.
        # Never round *up*: upsampling adds bytes and no detail.
        target_rate = min(source_rate, STANDARD_SAMPLE_RATE)
        if target_rate < source_rate:
            notes.append(
                QualityNote(
                    Severity.WARNING,
                    "Sample rate normalised",
                    f"Preferences are set to normalise rather than preserve, so "
                    f"{source_rate / 1000:g} kHz is being resampled down to "
                    f"{target_rate / 1000:g} kHz. Turn on 'keep source sample rate' "
                    f"to leave it alone.",
                )
            )

    if profile.supported_sample_rates and target_rate not in profile.supported_sample_rates:
        adjusted = _nearest_supported_rate(target_rate, profile.supported_sample_rates)
        if adjusted != target_rate:
            notes.append(
                QualityNote(
                    Severity.WARNING if adjusted < target_rate else Severity.INFO,
                    "Sample rate changed",
                    f"{profile.label} cannot store {target_rate / 1000:g} kHz, so the "
                    f"audio is resampled to {adjusted / 1000:g} kHz using the "
                    f"high-precision soxr resampler."
                    + (
                        f" Content above {adjusted / 2000:g} kHz is discarded."
                        if adjusted < target_rate
                        else ""
                    ),
                )
            )
        target_rate = adjusted
    elif sample_rate and sample_rate != source_rate:
        notes.append(
            QualityNote(
                Severity.WARNING if sample_rate < source_rate else Severity.INFO,
                "Sample rate changed by request",
                f"Resampling {source_rate / 1000:g} kHz to {sample_rate / 1000:g} kHz. "
                + (
                    "Downsampling permanently removes the highest frequencies."
                    if sample_rate < source_rate
                    else "Upsampling adds no detail and only makes the file bigger."
                ),
            )
        )

    # -- Bit depth ------------------------------------------------------
    sample_fmt: str | None = None
    target_depth = 0
    if profile.lossless and profile.supported_bit_depths:
        source_depth = info.bit_depth or STANDARD_BIT_DEPTH
        if bit_depth:
            wanted = bit_depth
        elif preserve_depth:
            wanted = source_depth
        else:
            # Same reasoning as the sample rate: normalise down to 16-bit, but
            # never pad a 16-bit source up to 24.
            wanted = min(source_depth, STANDARD_BIT_DEPTH)
        # Pick the smallest supported depth that still holds every source bit.
        candidates = sorted(profile.supported_bit_depths)
        fitting = [d for d in candidates if d >= wanted]
        target_depth = fitting[0] if fitting else max(candidates)

        if target_depth < wanted:
            notes.append(
                QualityNote(
                    Severity.WARNING,
                    "Bit depth reduced",
                    f"{profile.label} tops out at {target_depth}-bit, below the "
                    f"source's {wanted}-bit. Triangular dither is applied to keep "
                    f"the quantisation noise inaudible.",
                )
            )
        elif bit_depth and bit_depth > source_depth:
            notes.append(
                QualityNote(
                    Severity.INFO,
                    "Bit depth padded",
                    f"Storing {source_depth}-bit audio in a {bit_depth}-bit file "
                    f"adds zeros, not detail. The file grows; the sound does not change.",
                )
            )
        sample_fmt = _sample_fmt_for(profile, target_depth)

    # -- Encoder & lossy settings ---------------------------------------
    encoder = ffmpeg.best_aac_encoder() if profile.encoder == "aac" else profile.encoder
    if profile.encoder == "aac" and encoder == "aac":
        notes.append(
            QualityNote(
                Severity.INFO,
                "Using ffmpeg's native AAC encoder",
                "This build has no libfdk_aac. The native encoder is good but "
                "slightly behind libfdk at the same bitrate.",
            )
        )

    out_bitrate: int | None = None
    out_vbr: str | None = None
    if profile.is_lossy:
        if vbr_quality is not None and profile.supports_vbr:
            out_vbr = vbr_quality
        elif bitrate is not None:
            out_bitrate = bitrate
        elif profile.supports_vbr:
            out_vbr = profile.default_vbr_quality
        else:
            out_bitrate = profile.default_bitrate

        if out_bitrate and out_bitrate < 192:
            notes.append(
                QualityNote(
                    Severity.WARNING,
                    "Low bitrate",
                    f"{out_bitrate} kbps is below the transparency threshold for "
                    f"{profile.label}; compression artefacts may be audible on "
                    f"cymbals, applause and reverb tails.",
                )
            )

    # -- Channels -------------------------------------------------------
    channels = channels or info.channels or 2
    if channels > profile.max_channels:
        notes.append(
            QualityNote(
                Severity.WARNING,
                "Channels reduced",
                f"{profile.label} supports at most {profile.max_channels} channels; "
                f"the source's {channels} will be downmixed.",
            )
        )
        channels = profile.max_channels

    # -- Artwork --------------------------------------------------------
    if not profile.supports_artwork:
        notes.append(
            QualityNote(
                Severity.INFO,
                "No embedded cover art",
                f"{profile.label} files cannot reliably carry artwork. Tags and "
                f"art are still written where the format allows.",
            )
        )

    return OutputSpec(
        profile=profile,
        encoder=encoder,
        sample_rate=target_rate,
        bit_depth=target_depth,
        channels=channels,
        sample_fmt=sample_fmt,
        bitrate=out_bitrate,
        vbr_quality=out_vbr,
        notes=tuple(notes),
    )


def _sample_fmt_for(profile: FormatProfile, depth: int) -> str | None:
    """The ffmpeg sample format that stores ``depth`` bits for this encoder.

    Each encoder accepts a different, small set of formats -- and crucially some
    want planar ('s32p') where others want packed ('s32'). Handing an encoder a
    format it does not accept fails the whole conversion, so these lists mirror
    ``ffmpeg -h encoder=<name>`` exactly.

    24-bit audio has no dedicated sample format in these codecs: it travels in a
    32-bit buffer with the true depth recorded via ``-bits_per_raw_sample``.
    """
    if profile.id in ("wav", "aiff"):
        # PCM depth is chosen by picking the encoder itself, not a sample format.
        return None
    if profile.id == "flac":
        return "s16" if depth <= 16 else "s32"
    if profile.id == "alac":
        return "s16p" if depth <= 16 else "s32p"
    if profile.id == "wavpack":
        if depth <= 8:
            return "u8p"
        return "s16p" if depth <= 16 else "s32p"
    return None


def _pcm_encoder_for(profile: FormatProfile, depth: int) -> str:
    """WAV/AIFF encode straight to a depth-specific PCM codec."""
    if profile.id == "wav":
        return {8: "pcm_u8", 16: "pcm_s16le", 24: "pcm_s24le", 32: "pcm_s32le"}.get(depth, "pcm_s24le")
    return {8: "pcm_s8", 16: "pcm_s16be", 24: "pcm_s24be", 32: "pcm_s32be"}.get(depth, "pcm_s24be")


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def build_command(
    request: ConvertRequest,
    info: AudioInfo,
    output: OutputSpec,
    *,
    measured_loudness: probe.LoudnessStats | None = None,
) -> list[str]:
    """Assemble the single ffmpeg command that does the whole job.

    Edits, resampling, dither and encoding all happen in this one pass -- the
    audio is decoded once and encoded once, no matter how many effects are
    stacked on it.
    """
    # OutputSpec is the single source of truth for rate and depth here: it has
    # already reconciled what the user asked for with what the format allows.
    filters = edit.build_filter_chain(
        request.edits,
        info,
        measured_loudness=measured_loudness,
        target_sample_rate=output.sample_rate,
        target_bit_depth=output.bit_depth or None,
    )

    command = [*ffmpeg.base_args(overwrite=True), "-i", str(request.source)]

    if filters:
        command += ["-af", ",".join(filters)]

    # Audio only: drop any video stream (cover art is re-attached by tags.py,
    # and letting ffmpeg copy an embedded image often breaks the muxer).
    command += ["-vn", "-map", "0:a:0"]

    if request.copy_metadata:
        command += ["-map_metadata", "0"]
    else:
        command += ["-map_metadata", "-1"]

    # -- Encoder --------------------------------------------------------
    profile = output.profile
    if profile.id in ("wav", "aiff"):
        command += ["-c:a", _pcm_encoder_for(profile, output.bit_depth or 24)]
    else:
        command += ["-c:a", output.encoder]

    if output.sample_fmt and profile.id not in ("wav", "aiff"):
        command += ["-sample_fmt", output.sample_fmt]
    if output.bit_depth and profile.id in ("flac", "wavpack"):
        # Tells the encoder the samples are really 24-bit inside an s32 buffer,
        # so it does not waste space coding eight zero bits per sample.
        command += ["-bits_per_raw_sample", str(output.bit_depth)]

    if output.sample_rate:
        command += ["-ar", str(output.sample_rate)]
    if output.channels:
        command += ["-ac", str(output.channels)]

    if output.vbr_quality is not None:
        command += [profile.vbr_flag, str(output.vbr_quality)]
    elif output.bitrate is not None:
        command += ["-b:a", f"{output.bitrate}k"]

    command += list(profile.extra_args)

    if profile.muxer:
        command += ["-f", profile.muxer]

    command += [str(request.destination), *ffmpeg.progress_args()]
    return command


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class ConvertResult:
    """What a completed conversion produced."""

    source: Path
    destination: Path
    output: OutputSpec
    source_info: AudioInfo
    result_info: AudioInfo | None = None

    @property
    def notes(self) -> tuple[QualityNote, ...]:
        return self.output.notes

    @property
    def size_change(self) -> float:
        """Output size as a multiple of the source's."""
        if not self.result_info or not self.source_info.size_bytes:
            return 1.0
        return self.result_info.size_bytes / self.source_info.size_bytes


def unique_destination(path: Path) -> Path:
    """A path that does not exist yet, by appending ' (2)', ' (3)'..."""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    for counter in range(2, 1000):
        candidate = parent / f"{stem} ({counter}){suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"Could not find a free filename near {path}")


#: A "save/trim in place" write is briefly blocked by the exact same class of
#: transient lock as a delete -- Windows Search Indexer or antivirus
#: real-time scanning the destination -- confirmed via debug.log for the
#: delete path. Retrying absorbs it instead of losing the encoded result.
_REPLACE_RETRIES = 5
_REPLACE_RETRY_DELAY_S = 0.5


def replace_atomically(tmp_path: Path, dest_path: Path) -> None:
    """Atomically replace ``dest_path`` with ``tmp_path``, the last step of
    every "encode to a temp file, then swap it in" in-place edit.

    Retries briefly on a transient lock, and clears a stray read-only
    attribute on the destination if that -- not a lock -- turns out to be
    what a PermissionError (WinError 5) is actually about; a read-only flag
    is a one-shot fix, not something a delay would ever resolve on its own.
    """
    import os
    import stat
    import time

    last_exc: OSError | None = None
    for attempt in range(_REPLACE_RETRIES):
        try:
            os.replace(tmp_path, dest_path)
            return
        except PermissionError as exc:
            last_exc = exc
            try:
                mode = dest_path.stat().st_mode
                if not mode & stat.S_IWRITE:
                    dest_path.chmod(mode | stat.S_IWRITE)
            except OSError:
                pass
            if attempt < _REPLACE_RETRIES - 1:
                time.sleep(_REPLACE_RETRY_DELAY_S)
        except OSError as exc:
            last_exc = exc
            if attempt < _REPLACE_RETRIES - 1:
                time.sleep(_REPLACE_RETRY_DELAY_S)
    raise last_exc


def convert(
    request: ConvertRequest,
    *,
    context=None,
    info: AudioInfo | None = None,
    preserve_rate: bool | None = None,
    preserve_depth: bool | None = None,
) -> ConvertResult:
    """Run a conversion, reporting progress through ``context`` if given.

    ``preserve_rate`` and ``preserve_depth`` default to the user's saved
    preferences; pass them explicitly only to override.

    ``context`` is a :class:`~musicstudio.core.jobs.JobContext`; passing None
    runs synchronously with no progress reporting, which is what the tests do.
    """
    info = info or probe.probe(request.source)

    # Fall back to the saved preferences rather than a hardcoded True, so the
    # Preferences panel actually governs conversions.
    if preserve_rate is None or preserve_depth is None:
        from ..config import get_settings

        settings = get_settings()
        if preserve_rate is None:
            preserve_rate = settings.preserve_source_rate
        if preserve_depth is None:
            preserve_depth = settings.preserve_source_depth

    # The edits and the encoder must agree on the final rate and channel count.
    # If the filter chain resamples to 44.1 kHz while '-ar' still says 48 kHz,
    # ffmpeg quietly resamples a second time -- and can hand the encoder a
    # frame size it rejects outright.
    output = resolve_output(
        info,
        request.profile,
        sample_rate=request.sample_rate or request.edits.sample_rate,
        bit_depth=request.bit_depth,
        bitrate=request.bitrate,
        vbr_quality=request.vbr_quality,
        channels=request.edits.output_channels(info.channels or 2),
        preserve_rate=preserve_rate,
        preserve_depth=preserve_depth,
    )

    destination = request.destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not request.overwrite:
        destination = unique_destination(destination)
    if destination.resolve() == request.source.resolve():
        # Encoding onto the input would truncate it before ffmpeg finished
        # reading. Write beside it instead.
        destination = unique_destination(destination)
    request = ConvertRequest(**{**request.__dict__, "destination": destination})

    should_cancel = context.is_cancelled if context is not None else None
    estimated = request.edits.estimated_duration(info.duration)

    # -- Pass 1: loudness measurement, only when normalizing ------------
    measured = None
    if request.edits.needs_loudness_analysis:
        if context is not None:
            context.progress(0.0, "Analysing loudness…")

        def _analysis_progress(p) -> None:
            if context is not None and p.fraction is not None:
                # The measurement pass is roughly a third of the total work.
                context.progress(p.fraction * 0.3, "Analysing loudness…")

        stderr = ffmpeg.run_with_progress(
            edit.build_loudness_analysis_command(request.source, request.edits, info),
            total_seconds=estimated or info.duration,
            on_progress=_analysis_progress,
            should_cancel=should_cancel,
        )
        measured = probe.parse_loudnorm_json(stderr)

    # -- Pass 2: the one and only encode --------------------------------
    base = 0.3 if measured is not None else 0.0
    span = 1.0 - base

    def _encode_progress(p) -> None:
        if context is not None:
            fraction = None if p.fraction is None else base + p.fraction * span
            speed = f" ({p.speed:.0f}x)" if p.speed else ""
            context.progress(fraction, f"Encoding to {request.profile.label}{speed}")

    if context is not None:
        context.progress(base, f"Encoding to {request.profile.label}")

    command = build_command(request, info, output, measured_loudness=measured)
    ffmpeg.run_with_progress(
        command,
        total_seconds=estimated or info.duration,
        on_progress=_encode_progress,
        should_cancel=should_cancel,
    )

    return ConvertResult(
        source=request.source,
        destination=destination,
        output=output,
        source_info=info,
        result_info=probe.try_probe(destination),
    )


def render_preview(
    source: Path,
    edits: EditSpec,
    *,
    start: float = 0.0,
    seconds: float = 20.0,
    context=None,
) -> Path:
    """Render a short excerpt with ``edits`` applied, for listening to.

    The only honest way to preview a +18 dB boost or a loudness normalisation
    is to actually apply it, so this runs the same filter chain the export
    would. Output is uncompressed WAV into the cache directory: fastest to
    encode, and the preview is thrown away rather than kept.

    The excerpt is taken *after* the edits, so trims and cuts are respected --
    the window is applied on top of whatever the spec already does.
    """
    from ..config import TEMP_DIR

    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    # A unique name per render: the preview is usually still loaded in the
    # player (holding the file open) when the next render starts, and Media
    # Foundation's lock makes ffmpeg fail to overwrite it -- it surfaces as
    # exit code -13 (EACCES), not as an obvious "file in use" message.
    destination = unique_destination(
        TEMP_DIR / f"preview_{abs(hash(str(source))) % 10**8}.wav"
    )
    # Unique names would fill the cache with one wav per slider movement, so
    # stale previews are reaped on every render. The one currently playing
    # cannot be deleted -- that failure is fine, the next pass gets it.
    _cleanup_stale_previews(TEMP_DIR, keep=destination)

    info = probe.probe(source)
    output = resolve_output(info, formats.WAV, preserve_rate=True, preserve_depth=True)

    request = ConvertRequest(
        source=source,
        destination=destination,
        profile=formats.WAV,
        edits=edits,
        copy_metadata=False,
        overwrite=True,
    )
    command = build_command(request, info, output)

    # Insert the excerpt window just before the output path. -t after the
    # filters trims the *result*, so it respects the edits rather than fighting
    # them, and keeps the render short no matter how long the track is.
    output_index = command.index(str(destination))
    window = ["-t", f"{max(0.5, seconds):.3f}"]
    if start > 0:
        window = ["-ss", f"{start:.3f}", *window]
    command[output_index:output_index] = window

    ffmpeg.run_with_progress(
        command,
        total_seconds=seconds,
        should_cancel=context.is_cancelled if context is not None else None,
    )
    return destination


def _cleanup_stale_previews(directory: Path, *, keep: Path) -> None:
    """Delete old ``preview_*.wav`` renders from the cache directory.

    Best-effort by design: the preview currently loaded in the player is
    locked and simply survives to be reaped by a later cleanup.
    """
    for candidate in directory.glob("preview_*.wav"):
        if candidate == keep:
            continue
        try:
            candidate.unlink()
        except OSError:
            pass


def suggest_destination(
    source: Path,
    profile: FormatProfile,
    output_dir: Path | None = None,
) -> Path:
    """Where a converted file should go by default."""
    directory = output_dir or source.parent
    return Path(directory) / f"{source.stem}{profile.extension}"


def describe_conversion(info: AudioInfo, output: OutputSpec) -> str:
    """A single line summarising what will change, for confirmation dialogs."""
    source_desc = info.describe()
    bits = f"{output.bit_depth}-bit " if output.bit_depth else ""
    if output.vbr_quality is not None:
        quality = f"VBR q{output.vbr_quality}"
    elif output.bitrate:
        quality = f"{output.bitrate} kbps"
    else:
        quality = "lossless"
    target = (
        f"{output.profile.label} · {output.sample_rate / 1000:g} kHz · "
        f"{bits}{output.channels}ch · {quality}"
    )
    return f"{source_desc}  →  {target}"
