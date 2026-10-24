"""ReplayGain 2.0: measure loudness with ffmpeg and tag files with it.

Players that honour ReplayGain (including this app's own transport) turn
each song up or down so everything plays at a similar loudness. Only tags
are written -- the audio is never touched, so this costs no encode
generation and can be undone by clearing the tags.

Each track is measured with ffmpeg's EBU R128 ``ebur128`` filter, which
gives integrated loudness (LUFS) and true peak. Gain is the distance to the
ReplayGain 2.0 reference of -18 LUFS. Album gain comes from the energy-
weighted (by duration) mean of the album's track loudness rather than a
second decode of the whole album concatenated; the two differ only through
EBU R128's relative gate, by well under 0.1 dB on real albums.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import ffmpeg
from . import tags as tags_module

#: ReplayGain 2.0 reference loudness.
REFERENCE_LUFS = -18.0
#: Quieter than this is silence (ebur128's absolute gate is -70 LUFS); no
#: honest gain exists for it, and +50 dB of "correction" would be dangerous.
SILENCE_LUFS = -70.0

_SUMMARY_RE = re.compile(r"\] Summary:")
_LOUDNESS_RE = re.compile(r"^\s*I:\s*(-?[\d.]+|-inf)\s*LUFS", re.MULTILINE)
_PEAK_RE = re.compile(r"^\s*Peak:\s*(-?[\d.]+|-inf)\s*dBFS", re.MULTILINE)


@dataclass(frozen=True)
class Measurement:
    loudness_lufs: float
    #: Linear true peak, 1.0 = full scale.
    peak: float


@dataclass(frozen=True)
class GainResult:
    path: Path
    track_gain: float
    track_peak: float
    album_gain: float | None = None
    album_peak: float | None = None


@dataclass
class ScanResult:
    updated: list[GainResult] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)


def format_gain(db: float) -> str:
    return f"{db:+.2f} dB"


def format_peak(peak: float) -> str:
    return f"{peak:.6f}"


def parse_gain(text: str) -> float | None:
    """"-6.20 dB" (or "-6.2", "+1.30 dB") -> -6.2; None if blank/garbled."""
    match = re.match(r"\s*([+-]?\d+(?:\.\d+)?)", text or "")
    return float(match.group(1)) if match else None


def parse_peak(text: str) -> float | None:
    try:
        value = float((text or "").strip())
    except ValueError:
        return None
    return value if value >= 0 else None


def playback_gain_db(track, *, mode: str, preamp_db: float = 0.0) -> float:
    """The gain a player should apply to ``track`` (a library row), in dB.

    ``mode`` is "off", "track" or "album"; album mode falls back to the
    track's own gain when the file has no album gain. The result is lowered
    if needed so the track's peak never goes past full scale.
    """
    if mode == "off" or track is None:
        return 0.0
    gain, peak = track.rg_track_gain, track.rg_track_peak
    if mode == "album" and track.rg_album_gain is not None:
        gain, peak = track.rg_album_gain, track.rg_album_peak
    if gain is None:
        return 0.0
    gain += preamp_db
    if peak:
        gain = min(gain, -20 * math.log10(peak))
    return gain


def build_measure_command(path: Path) -> list[str]:
    return [
        *ffmpeg.base_args(),
        "-i", str(path),
        "-map", "0:a:0",
        # framelog=verbose moves the ten-lines-a-second running log below
        # the info level, leaving just the summary on stderr.
        "-af", "ebur128=peak=true:framelog=verbose",
        "-f", "null", "-",
        *ffmpeg.progress_args(),
    ]


def parse_ebur128_summary(stderr: str) -> Measurement:
    """Read the integrated loudness and true peak from ebur128's summary."""
    found = list(_SUMMARY_RE.finditer(stderr))
    if not found:
        raise ValueError("ffmpeg printed no loudness summary")
    summary = stderr[found[-1].end():]
    loudness = _LOUDNESS_RE.search(summary)
    peak = _PEAK_RE.search(summary)
    if loudness is None or peak is None:
        raise ValueError("Could not read the loudness summary")

    def number(text: str) -> float:
        return -math.inf if text == "-inf" else float(text)

    peak_db = number(peak.group(1))
    return Measurement(
        loudness_lufs=number(loudness.group(1)),
        peak=0.0 if peak_db == -math.inf else 10 ** (peak_db / 20),
    )


def measure(path: Path, *, duration: float | None = None, on_progress=None,
            should_cancel=None) -> Measurement:
    stderr = ffmpeg.run_with_progress(
        build_measure_command(Path(path)),
        total_seconds=duration or None,
        on_progress=on_progress,
        should_cancel=should_cancel,
    )
    return parse_ebur128_summary(stderr)


def gain_for(loudness_lufs: float) -> float:
    return REFERENCE_LUFS - loudness_lufs


def album_loudness(measurements: list[tuple[Measurement, float]]) -> float:
    """Energy mean of (measurement, duration) pairs, weighted by duration."""
    usable = [(m, d) for m, d in measurements if m.loudness_lufs > SILENCE_LUFS and d > 0]
    if not usable:
        return -math.inf
    total = sum(d for _, d in usable)
    energy = sum(d * 10 ** (m.loudness_lufs / 10) for m, d in usable) / total
    return 10 * math.log10(energy)


def group_for_album_gain(tracks) -> list[list]:
    """Group library rows into albums (album artist, else artist, + album).

    Rows with no album tag each form a group of one and get no album gain.
    Order within an album follows disc and track number.
    """
    albums: dict[tuple[str, str], list] = {}
    singles: list[list] = []
    for track in tracks:
        if not (track.album or "").strip():
            singles.append([track])
            continue
        artist = (track.albumartist or track.artist or "").strip().casefold()
        albums.setdefault((artist, track.album.strip().casefold()), []).append(track)
    groups = [
        sorted(g, key=lambda t: (t.disc_number or 0, t.track_number or 0, str(t.path)))
        for g in albums.values()
    ]
    return groups + singles


def scan_library(
    paths: list[Path],
    *,
    library,
    context=None,
    skip_existing: bool = True,
) -> ScanResult:
    """Measure ``paths`` and write ReplayGain tags, then re-index each file.

    Album gain uses the whole album as the library knows it -- every indexed
    track sharing the album -- not just the selected ones, since an "album
    gain" measured over half an album would be wrong for the other half.
    One file failing never stops the batch.
    """
    from . import probe

    if not ffmpeg.has_filter("ebur128"):
        raise RuntimeError("This ffmpeg build has no ebur128 loudness filter")

    selected = {Path(p).resolve() for p in paths}
    rows = [library.get(p) for p in selected]
    rows = [r for r in rows if r is not None]
    album_keys = {
        ((r.albumartist or r.artist or "").strip().casefold(), r.album.strip().casefold())
        for r in rows if (r.album or "").strip()
    }
    # Pull in the rest of each touched album.
    extra = [
        t for t in library.all_tracks()
        if (t.album or "").strip()
        and ((t.albumartist or t.artist or "").strip().casefold(),
             t.album.strip().casefold()) in album_keys
        and t.path not in selected
    ]
    groups = group_for_album_gain(rows + extra)

    result = ScanResult()
    total_seconds = sum(t.duration or 0 for g in groups for t in g) or None
    done_seconds = 0.0

    for group in groups:
        is_album = bool((group[0].album or "").strip())
        if skip_existing and all(
            t.rg_track_gain is not None and (not is_album or t.rg_album_gain is not None)
            for t in group
        ):
            result.skipped.extend(t.path for t in group if t.path in selected)
            done_seconds += sum(t.duration or 0 for t in group)
            continue

        measured: list[tuple[object, Measurement]] = []
        for track in group:
            if context is not None:
                context.raise_if_cancelled()
                base = done_seconds

                def on_progress(p, base=base, name=track.path.name):
                    if total_seconds:
                        context.progress(min(1.0, (base + p.seconds_done) / total_seconds),
                                         f"Measuring {name}")
                context.progress(base / total_seconds if total_seconds else None,
                                 f"Measuring {track.path.name}")
            else:
                on_progress = None
            try:
                m = measure(
                    track.path,
                    duration=track.duration,
                    on_progress=on_progress,
                    should_cancel=context.is_cancelled if context is not None else None,
                )
                measured.append((track, m))
            except ffmpeg.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 -- keep going through the batch
                result.failed.append((track.path, str(exc)))
            done_seconds += track.duration or 0

        album_lufs = album_loudness([(m, t.duration) for t, m in measured]) if is_album else None
        album_peak = max((m.peak for _, m in measured), default=0.0)

        for track, m in measured:
            if m.loudness_lufs <= SILENCE_LUFS:
                result.failed.append((track.path, "silent: no gain to give it"))
                continue
            gains = GainResult(
                path=track.path,
                track_gain=gain_for(m.loudness_lufs),
                track_peak=m.peak,
            )
            fields = {
                "replaygain_track_gain": format_gain(gains.track_gain),
                "replaygain_track_peak": format_peak(gains.track_peak),
            }
            if album_lufs is not None and album_lufs > SILENCE_LUFS:
                gains = GainResult(
                    path=track.path,
                    track_gain=gains.track_gain,
                    track_peak=gains.track_peak,
                    album_gain=gain_for(album_lufs),
                    album_peak=album_peak,
                )
                fields["replaygain_album_gain"] = format_gain(gains.album_gain)
                fields["replaygain_album_peak"] = format_peak(album_peak)
            try:
                tags_module.write_fields(track.path, **fields)
            except Exception as exc:  # noqa: BLE001
                result.failed.append((track.path, str(exc)))
                continue
            info = probe.try_probe(track.path)
            if info is not None:
                library.upsert(track.path, info, tags_module.try_read(track.path))
            result.updated.append(gains)

    if context is not None:
        context.progress(1.0, f"ReplayGain: tagged {len(result.updated)} file(s)")
    return result
