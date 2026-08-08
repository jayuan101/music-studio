"""Local, offline voice-activity detection for auto-trim's "also catch a
spoken intro/outro, not just silence" mode.

Uses Silero VAD (MIT-licensed, bundled as ``assets/silero_vad.onnx``) via
``onnxruntime`` -- no network calls, no cloud API, no GPU needed. The model's
input/output tensor names and shapes below were confirmed against the actual
bundled file (``onnxruntime.InferenceSession(...).get_inputs()``), not
guessed from memory, since Silero's onnx export has changed shape across
versions.

Deliberately optional everywhere: a missing ``onnxruntime`` install, a
missing model file, or any inference failure all degrade to "found nothing"
rather than raising, so this module can only ever make auto-trim more
capable, never less reliable, for callers that do not use it.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import ffmpeg
from .autotrim import SilenceSpan

MODEL_FILENAME = "silero_vad.onnx"

#: Silero VAD is trained and calibrated specifically for 512-sample chunks
#: at 16 kHz (32 ms each) -- other chunk sizes are not officially supported
#: even though the graph itself does not enforce it.
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 512

#: How close two detected non-music spans need to be to count as one
#: continuous run of junk (e.g. silence, then talking, then a beat before
#: the music starts) rather than two separate, disjoint events.
_MERGE_GAP_TOLERANCE_S = 1.0

_session = None  # type: ignore[var-annotated]
_session_load_failed = False


def _model_path() -> Path | None:
    """Locate the bundled model, whether running from source or frozen.

    Mirrors ``musicstudio.ui.main_window._app_icon()``'s existing lookup for
    ``assets/icon.ico``.
    """
    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "assets")
    roots.append(Path(__file__).resolve().parent.parent.parent / "assets")
    for root in roots:
        candidate = root / MODEL_FILENAME
        if candidate.is_file():
            return candidate
    return None


def is_available() -> bool:
    """Whether speech detection can actually run right now."""
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return False
    return _model_path() is not None


def _get_session():
    """Lazily load and cache the ONNX session. ``None`` if unavailable."""
    global _session, _session_load_failed
    if _session is not None:
        return _session
    if _session_load_failed:
        return None
    try:
        import onnxruntime

        model_path = _model_path()
        if model_path is None:
            _session_load_failed = True
            return None
        _session = onnxruntime.InferenceSession(
            str(model_path), providers=["CPUExecutionProvider"]
        )
    except Exception:
        _session_load_failed = True
        return None
    return _session


def _decode_window_pcm(path: Path, *, duration_s: float, from_end: bool):
    """Decode just the leading or trailing ``duration_s`` seconds of ``path``
    to mono 16 kHz float32 PCM, as a 1-D numpy array.

    Reads raw stdout bytes directly (not ``ffmpeg.run()``, which is
    text-mode) -- one short-lived ffmpeg process, nothing written to disk.
    """
    import numpy as np

    args = [str(ffmpeg.ffmpeg_path()), "-hide_banner", "-loglevel", "error"]
    if from_end:
        args += ["-sseof", f"-{duration_s:g}"]
    args += ["-i", str(path)]
    if not from_end:
        args += ["-t", f"{duration_s:g}"]
    args += ["-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "f32le", "-"]

    proc = subprocess.run(args, capture_output=True, **ffmpeg._no_window_kwargs())
    if proc.returncode != 0:
        raise ffmpeg.FFmpegError(
            "ffmpeg exited with code %d" % proc.returncode,
            command=args,
            stderr=proc.stderr.decode("utf-8", "replace"),
            returncode=proc.returncode,
        )
    return np.frombuffer(proc.stdout, dtype="<f4")


def speech_spans_in_window(
    path: Path,
    *,
    duration_s: float,
    track_duration: float,
    from_end: bool = False,
    threshold: float = 0.5,
) -> list[SilenceSpan]:
    """Spans of detected speech within the leading or trailing window, in
    absolute file-time seconds -- the same shape :func:`detect_silence`
    produces, so the two can simply be merged before being handed to
    :func:`compute_trim_region`.

    Never raises: any failure (no onnxruntime, no model, a decode error)
    returns an empty list.
    """
    if duration_s <= 0 or track_duration <= 0:
        return []

    try:
        session = _get_session()
        if session is None:
            return []

        import numpy as np

        window_s = min(duration_s, track_duration)
        samples = _decode_window_pcm(path, duration_s=window_s, from_end=from_end)
        num_chunks = samples.size // CHUNK_SAMPLES
        if num_chunks == 0:
            return []

        window_start = max(0.0, track_duration - window_s) if from_end else 0.0
        chunk_duration = CHUNK_SAMPLES / SAMPLE_RATE

        state = np.zeros((2, 1, 128), dtype=np.float32)
        sr = np.array(SAMPLE_RATE, dtype=np.int64)
        flags: list[bool] = []
        for i in range(num_chunks):
            chunk = samples[i * CHUNK_SAMPLES : (i + 1) * CHUNK_SAMPLES].reshape(1, -1)
            prob, state = session.run(
                ["output", "stateN"], {"input": chunk, "state": state, "sr": sr}
            )
            flags.append(float(prob[0, 0]) >= threshold)

        return _coalesce_chunks(
            flags,
            chunk_duration=chunk_duration,
            window_start=window_start,
            track_duration=track_duration,
            from_end=from_end,
        )
    except Exception:
        return []


def _coalesce_chunks(
    flags: list[bool],
    *,
    chunk_duration: float,
    window_start: float,
    track_duration: float,
    from_end: bool,
) -> list[SilenceSpan]:
    """Turn a per-chunk speech/not-speech flag list into spans.

    The trailing window's last chunk is snapped to ``track_duration`` exactly
    rather than the chunk-boundary math, since an under-32ms tail of
    un-analysed samples (the remainder after floor-dividing into whole
    chunks) would otherwise leave the span a hair short of end-of-file --
    which matters, because ``compute_trim_region`` only counts a trailing
    span that reaches all the way to the end.
    """
    spans: list[SilenceSpan] = []
    run_start: float | None = None
    last_index = len(flags) - 1
    for i, flag in enumerate(flags):
        t = window_start + i * chunk_duration
        if flag and run_start is None:
            run_start = t
        elif not flag and run_start is not None:
            spans.append(SilenceSpan(start=run_start, end=t))
            run_start = None
    if run_start is not None:
        end = track_duration if from_end else window_start + (last_index + 1) * chunk_duration
        spans.append(SilenceSpan(start=run_start, end=end))
    return spans


def merge_spans(
    spans: list[SilenceSpan], *, gap_tolerance_s: float = _MERGE_GAP_TOLERANCE_S
) -> list[SilenceSpan]:
    """Coalesce overlapping or near-adjacent spans into one.

    ``compute_trim_region`` only ever looks at a single leading span and a
    single trailing span. Silence detection and speech detection producing
    two separate, slightly-gapped spans for what is really one continuous
    run of non-music (silence, then talking, then a beat of quiet before the
    music starts) must be combined into one span first, or only the first
    piece would ever actually get trimmed.
    """
    if not spans:
        return []
    ordered = sorted(spans, key=lambda s: s.start)
    merged = [ordered[0]]
    for span in ordered[1:]:
        last = merged[-1]
        last_end = last.end if last.end is not None else float("inf")
        if span.start <= last_end + gap_tolerance_s:
            if last.end is None or span.end is None:
                merged[-1] = SilenceSpan(start=last.start, end=None)
            else:
                merged[-1] = SilenceSpan(start=last.start, end=max(last_end, span.end))
        else:
            merged.append(span)
    return merged
