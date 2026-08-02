"""Detecting and removing non-music intro/outro segments from tracks that
look like they came from a music video (a YouTube rip, a filename carrying
"(Official Video)", etc.).

Two passes: :func:`looks_like_video_source` decides *whether* a track is even
a candidate, so an intentional quiet intro on an ordinary studio recording is
never touched; :func:`analyse` decides *how much* to cut, bounded by
configurable caps. This is deliberately not ``edit.SilenceMode``'s blind
``silenceremove`` -- that has no cap and no source-type gate, and would just
as happily eat the first few seconds of a normal song.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from . import convert as convert_module
from . import ffmpeg, formats
from . import tag_fix
from . import tags as tags_module
from .edit import EditSpec, Region
from .probe import AudioInfo, probe


class AutoTrimState(str, Enum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    APPLIED = "applied"
    SKIPPED = "skipped"


#: Hosts that only ever serve video-first content -- a track carrying one of
#: these as its source_url almost certainly started life as a music video.
_VIDEO_HOSTS = frozenset(
    {
        "youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be",
        "music.youtube.com", "vimeo.com", "www.vimeo.com",
        "dailymotion.com", "www.dailymotion.com",
    }
)


def _host_is_video_source(source_url: str) -> bool:
    if not source_url:
        return False
    try:
        return urlparse(source_url).netloc.lower() in _VIDEO_HOSTS
    except ValueError:
        return False


def looks_like_video_source(*, source_url: str = "", title: str = "", filename: str = "") -> bool:
    """Combined heuristic: was this track probably ripped from a video?

    Used as a gate before ever attempting an automatic trim -- a normal
    studio recording with a long intentional ambient intro must never be
    touched just because it happens to start quiet.
    """
    return (
        _host_is_video_source(source_url)
        or tag_fix.looks_like_video_title(title)
        or tag_fix.looks_like_video_title(filename)
    )


@dataclass(frozen=True)
class AutoTrimSettings:
    threshold_db: float = -50.0
    max_intro_s: float = 12.0
    max_outro_s: float = 12.0
    min_silence_duration_s: float = 0.3
    min_trim_s: float = 1.0


@dataclass(frozen=True)
class SilenceSpan:
    """One run of near-silence, in source seconds. ``end=None`` means the
    silence ran all the way to end of file (ffmpeg never prints a matching
    ``silence_end`` for that case)."""

    start: float
    end: float | None


_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[0-9.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[0-9.]+)")


def parse_silencedetect(stderr: str) -> list[SilenceSpan]:
    """Pair ffmpeg's silencedetect log lines into spans, in order."""
    spans: list[SilenceSpan] = []
    pending_start: float | None = None
    for line in stderr.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match is not None:
            pending_start = float(start_match.group(1))
            continue
        end_match = _SILENCE_END_RE.search(line)
        if end_match is not None and pending_start is not None:
            spans.append(SilenceSpan(start=pending_start, end=float(end_match.group(1))))
            pending_start = None
    if pending_start is not None:
        spans.append(SilenceSpan(start=pending_start, end=None))
    return spans


def detect_silence(path: Path, *, threshold_db: float, min_duration: float) -> list[SilenceSpan]:
    """Run ffmpeg's silencedetect filter as an analysis-only pass and parse
    the spans back out of its stderr log.

    Modeled on probe.measure_clipping()'s "run to -f null -, parse stderr"
    pattern -- nothing is written to disk here.
    """
    stderr = ffmpeg.run_with_progress(
        [
            *ffmpeg.base_args(),
            "-i", str(path),
            "-af", f"silencedetect=noise={threshold_db:g}dB:d={min_duration:g}",
            "-f", "null", "-",
            *ffmpeg.progress_args(),
        ]
    )
    return parse_silencedetect(stderr)


def compute_trim_region(
    spans: list[SilenceSpan],
    duration: float,
    *,
    max_intro_s: float,
    max_outro_s: float,
    min_trim_s: float,
) -> Region | None:
    """Turn detected silence spans into a Region describing what survives.

    Only silence touching t=0 (an intro) or running to end-of-file (an
    outro) counts -- a silent passage in the middle of the song (a
    breakdown, a pause between verses) is left completely alone. The
    trimmed length is capped, never targeted, by max_intro_s/max_outro_s: a
    30-second logo bumper is only ever cut back to the cap, not entirely
    removed blindly. Returns None when there is nothing worth cutting.
    """
    start = 0.0
    for span in spans:
        if span.start > 1e-6:
            break
        span_end = span.end if span.end is not None else duration
        start = min(span_end, max_intro_s)
        break

    end = duration
    for span in reversed(spans):
        span_end = span.end if span.end is not None else duration
        if span_end < duration - 1e-6:
            break
        end = max(span.start, duration - max_outro_s)
        break

    if end <= start:
        return None
    if (start < min_trim_s) and (duration - end < min_trim_s):
        return None

    region_start = start if start >= min_trim_s else 0.0
    region_end = end if (duration - end) >= min_trim_s else None
    if region_start == 0.0 and region_end is None:
        return None
    return Region(start=region_start, end=region_end)


def analyse(
    path: Path, info: AudioInfo | None = None, *, settings: AutoTrimSettings | None = None
) -> Region | None:
    settings = settings or AutoTrimSettings()
    info = info or probe(path)
    spans = detect_silence(
        path, threshold_db=settings.threshold_db, min_duration=settings.min_silence_duration_s
    )
    return compute_trim_region(
        spans,
        info.duration,
        max_intro_s=settings.max_intro_s,
        max_outro_s=settings.max_outro_s,
        min_trim_s=settings.min_trim_s,
    )


@dataclass(frozen=True)
class AutoTrimOutcome:
    path: Path
    state: str
    updated: bool = False
    trimmed_start_s: float = 0.0
    trimmed_end_s: float = 0.0
    reason: str = ""


def autotrim_track(
    path: str | Path,
    *,
    library=None,
    settings: AutoTrimSettings | None = None,
    force: bool = False,
    context=None,
) -> AutoTrimOutcome:
    """Decide, detect and (if warranted) apply a trim to one file, in place.

    Reads tags fresh from disk for its decision rather than requiring a
    Library row, so this works equally for a track that has not been indexed
    yet (straight off a download) and one pulled from a bulk library scan.
    When ``library`` is given, the verdict is persisted so a later pass never
    re-attempts an already-applied or user-skipped track -- unless
    ``force`` is set, which is what an explicit user action should pass.
    """
    path = Path(path)
    settings = settings or AutoTrimSettings()

    def _finish(state: str, **kwargs) -> AutoTrimOutcome:
        if library is not None:
            library.set_auto_trim_state(path, state)
        return AutoTrimOutcome(path=path, state=state, **kwargs)

    if not force and library is not None:
        row = library.get(path)
        if row is not None and row.auto_trim_state in ("applied", "skipped"):
            return AutoTrimOutcome(path=path, state=row.auto_trim_state, reason="already processed")

    tags = tags_module.try_read(path)
    if not force and not looks_like_video_source(
        source_url=tags.source_url, title=tags.title, filename=path.name
    ):
        return _finish(AutoTrimState.NOT_APPLICABLE, reason="does not look video-sourced")

    try:
        info = probe(path)
        region = analyse(path, info, settings=settings)
    except (OSError, ValueError, ffmpeg.FFmpegError, ffmpeg.FFmpegNotFound) as exc:
        return AutoTrimOutcome(path=path, state="pending", reason=f"analysis failed: {exc}")

    if region is None:
        return _finish(AutoTrimState.SKIPPED, reason="no leading/trailing silence found")

    profile = formats.profile_for_extension(path.suffix)
    if profile is None:
        return AutoTrimOutcome(
            path=path, state="pending", reason=f"don't know how to re-encode {path.suffix} in place"
        )

    tmp_destination = path.with_name(f".{path.stem}.autotrim.tmp{profile.extension}")
    request = convert_module.ConvertRequest(
        source=path,
        destination=tmp_destination,
        profile=profile,
        edits=EditSpec(trim=region),
        overwrite=True,
    )
    try:
        result = convert_module.convert(context=context, request=request, info=info)
        try:
            tags_module.write(result.destination, tags, artwork=tags.artwork)
        except tags_module.TagError:
            pass
        os.replace(result.destination, path)
    except BaseException:
        tmp_destination.unlink(missing_ok=True)
        raise

    trimmed_start = region.start
    trimmed_end = info.duration - region.end if region.end is not None else 0.0
    return _finish(
        AutoTrimState.APPLIED,
        updated=True,
        trimmed_start_s=trimmed_start,
        trimmed_end_s=trimmed_end,
        reason="trimmed",
    )


def autotrim_library(
    paths: list[Path],
    *,
    library=None,
    settings: AutoTrimSettings | None = None,
    force: bool = False,
    context=None,
) -> list[AutoTrimOutcome]:
    """Bulk pass over ``paths``, same calling shape as
    tag_fix.fix_library_tags()/artwork.update_library_artwork() -- one file
    failing never stops the batch."""
    results: list[AutoTrimOutcome] = []
    total = len(paths)

    for index, path in enumerate(paths):
        if context is not None:
            context.raise_if_cancelled()
            context.progress(index / total if total else None, f"Auto-trimming {Path(path).name}")
        try:
            results.append(
                autotrim_track(path, library=library, settings=settings, force=force, context=context)
            )
        except ffmpeg.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- keep going through the batch
            results.append(AutoTrimOutcome(path=Path(path), state="pending", reason=f"failed: {exc}"))

    if context is not None:
        applied = sum(1 for r in results if r.updated)
        context.progress(1.0, f"Auto-trimmed {applied} of {total}")
    return results
