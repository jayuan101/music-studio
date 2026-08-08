"""Local voice-activity detection used by auto-trim's opt-in "also catch a
spoken intro/outro" mode.

Two tiers, same split as test_autotrim.py: deterministic logic (span
coalescing, offset math) tested with no model involved at all, then a real
inference smoke test skipped when onnxruntime/the model are unavailable.
True speech-vs-music *accuracy* on real recordings cannot be validated here
and is not claimed by these tests -- see the plan's Verification section.
"""

from __future__ import annotations

import subprocess

import pytest

from musicstudio.core import ffmpeg, speech
from musicstudio.core.autotrim import SilenceSpan

from .conftest import requires_ffmpeg

requires_speech = pytest.mark.skipif(
    not speech.is_available(), reason="onnxruntime or the bundled model is not available"
)


# ---------------------------------------------------------------------------
# merge_spans -- pure logic, no model needed
# ---------------------------------------------------------------------------


def test_merge_spans_bridges_a_small_gap():
    merged = speech.merge_spans([SilenceSpan(0.0, 0.1), SilenceSpan(0.15, 5.0)])
    assert merged == [SilenceSpan(0.0, 5.0)]


def test_merge_spans_leaves_far_apart_spans_separate():
    merged = speech.merge_spans([SilenceSpan(0.0, 1.0), SilenceSpan(50.0, 55.0)])
    assert merged == [SilenceSpan(0.0, 1.0), SilenceSpan(50.0, 55.0)]


def test_merge_spans_combines_open_ended_trailing_spans():
    merged = speech.merge_spans([SilenceSpan(90.0, None), SilenceSpan(85.0, 90.5)])
    assert merged == [SilenceSpan(85.0, None)]


def test_merge_spans_handles_empty_input():
    assert speech.merge_spans([]) == []


def test_merge_spans_sorts_before_merging():
    merged = speech.merge_spans([SilenceSpan(10.0, 11.0), SilenceSpan(0.0, 1.0)])
    assert merged == [SilenceSpan(0.0, 1.0), SilenceSpan(10.0, 11.0)]


# ---------------------------------------------------------------------------
# _coalesce_chunks -- pure logic, no model needed
# ---------------------------------------------------------------------------


def test_coalesce_chunks_intro_window_starts_at_zero():
    spans = speech._coalesce_chunks(
        [True, True, False, False],
        chunk_duration=0.5,
        window_start=0.0,
        track_duration=100.0,
        from_end=False,
    )
    assert spans == [SilenceSpan(0.0, 1.0)]


def test_coalesce_chunks_outro_window_snaps_final_span_to_track_duration():
    """The last chunk's true end is a few ms before track_duration (floor
    division drops a partial trailing chunk) -- it must be snapped to the
    exact duration, or compute_trim_region's "must reach end-of-file" check
    would reject an otherwise valid outro trim by a hair."""
    spans = speech._coalesce_chunks(
        [False, True, True],
        chunk_duration=0.5,
        window_start=98.0,
        track_duration=99.49,
        from_end=True,
    )
    assert spans == [SilenceSpan(98.5, 99.49)]


def test_coalesce_chunks_no_speech_returns_nothing():
    assert speech._coalesce_chunks(
        [False, False], chunk_duration=0.5, window_start=0.0, track_duration=10.0, from_end=False
    ) == []


# ---------------------------------------------------------------------------
# Real inference smoke test
# ---------------------------------------------------------------------------


@requires_ffmpeg
@requires_speech
def test_pure_silence_yields_no_speech_spans(tmp_path):
    path = tmp_path / "silence.flac"
    subprocess.run(
        [
            str(ffmpeg.ffmpeg_path()), "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=3.0",
            "-c:a", "flac", str(path),
        ],
        check=True, capture_output=True,
    )
    spans = speech.speech_spans_in_window(path, duration_s=12.0, track_duration=3.0)
    assert spans == []


@requires_speech
def test_is_available_reports_true_when_model_and_runtime_are_present():
    assert speech.is_available() is True
