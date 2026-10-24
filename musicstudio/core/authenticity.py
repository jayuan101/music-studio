"""Spot "lossless" files that were really made from a lossy source.

A FLAC made by converting an MP3, or ripped from a YouTube stream, is
lossless only in the sense that the container is: the detail the lossy
encoder threw away is gone for good. Lossy encoders leave a fingerprint,
though -- a low-pass filter, so the spectrum falls off a cliff at some
frequency (about 16 kHz for 128 kbps MP3 and AAC, 19-20 kHz for higher
rates) with near-silence above it. Real recordings usually taper gently
instead, or reach 20 kHz and beyond.

The check decodes a minute from the middle of the file, averages its power
spectrum, and looks for the sharpest drop between neighbouring slices of
spectrum. A drop of 25 dB or more within about a kilohertz, below 19.5 kHz,
is called "suspect". It is a heuristic and the UI says so:

* Genuinely lowpassed masters (some old tape, lo-fi, band-limited recordings)
  can be flagged.
* A 320 kbps MP3, and CD masters in general, stop near 20 kHz. That is
  indistinguishable from an ordinary CD lowpass, so a cutoff at 19.5 kHz or
  above is never reported -- better to miss it than to accuse real files.

The thresholds were chosen by running this over a real personal library:
known MP3/AAC files all show cliffs of 43-82 dB between 15.5 and 20.25 kHz,
FLACs with no lossy history show drops of 3-12 dB or sit above 19.5 kHz, and
the gap between 12 and 25 dB holds only a few ambiguous files, which are
left alone on purpose.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import ffmpeg

#: A drop at least this big (dB, between the spectrum just below and just
#: above a frequency) counts as a lowpass cliff.
SUSPECT_DROP_DB = 25.0
#: A cliff at or above this is not reported -- see the module docstring.
SUSPECT_BELOW_HZ = 19_500.0
#: Below this sample rate a lossy lowpass is not distinguishable from the
#: Nyquist limit itself, so no verdict is given.
MIN_SAMPLE_RATE = 44_100
#: How much audio to analyse, taken from the middle of the track.
EXCERPT_SECONDS = 60.0

OK = "ok"
SUSPECT = "suspect"
INCONCLUSIVE = "inconclusive"

_FFT_SIZE = 4096
_CHUNK_FRAMES = 256
_SEARCH_FROM_HZ = 14_000.0
_STEP_HZ = 250.0
_WINDOW_NEAR_HZ = 250.0
_WINDOW_FAR_HZ = 1_500.0


@dataclass(frozen=True)
class Verdict:
    state: str
    #: Where the spectrum falls off, in Hz (250 Hz resolution); None when no
    #: clear cliff was found.
    cutoff_hz: int | None = None
    drop_db: float = 0.0


@dataclass
class CheckResult:
    checked: list[tuple[Path, Verdict]] = field(default_factory=list)
    skipped: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)

    @property
    def suspect(self) -> int:
        return sum(1 for _, v in self.checked if v.state == SUSPECT)


def _average_spectrum(samples: np.ndarray, sample_rate: int):
    """Mean power spectrum in dB, ignoring near-silent frames, or None."""
    n = _FFT_SIZE
    if len(samples) < n * 4:
        return None
    hop = n // 2
    frames = np.lib.stride_tricks.sliding_window_view(samples, n)[::hop]
    rms = np.sqrt((frames.astype(np.float64) ** 2).mean(axis=1))
    loud = rms > max(rms.max() * 0.03, 1e-5)
    if loud.sum() < 8:
        return None
    window = np.hanning(n)
    total = np.zeros(n // 2 + 1)
    count = 0
    indices = np.flatnonzero(loud)
    for start in range(0, len(indices), _CHUNK_FRAMES):
        block = frames[indices[start:start + _CHUNK_FRAMES]] * window
        total += (np.abs(np.fft.rfft(block, axis=1)) ** 2).sum(axis=0)
        count += len(block)
    freqs = np.fft.rfftfreq(n, 1.0 / sample_rate)
    return freqs, 10 * np.log10(total / count + 1e-20)


def analyse_samples(samples: np.ndarray, sample_rate: int) -> Verdict:
    """Verdict for mono float samples. Pure and Qt-free, so it is testable
    without ffmpeg."""
    spectrum = _average_spectrum(np.asarray(samples, dtype=np.float32), sample_rate)
    if spectrum is None:
        return Verdict(INCONCLUSIVE)
    freqs, db = spectrum

    nyquist = sample_rate / 2
    top = min(nyquist - 300.0, 21_800.0)
    best_drop, best_at = 0.0, None
    edge = _SEARCH_FROM_HZ
    while edge <= top - 500.0:
        below = db[(freqs >= edge - _WINDOW_FAR_HZ) & (freqs < edge - _WINDOW_NEAR_HZ)].mean()
        above_mask = (freqs >= edge + _WINDOW_NEAR_HZ) & (
            freqs < min(edge + _WINDOW_FAR_HZ, nyquist - 100.0)
        )
        above = db[above_mask].mean()
        if below - above > best_drop:
            best_drop, best_at = below - above, edge
        edge += _STEP_HZ

    if best_at is not None and best_drop >= SUSPECT_DROP_DB:
        state = SUSPECT if best_at < SUSPECT_BELOW_HZ else OK
        return Verdict(state, int(best_at), float(best_drop))
    return Verdict(OK, None, float(best_drop))


def decode_excerpt(path: Path, duration: float | None, *, timeout: float = 180.0):
    """Decode ~a minute from the middle of ``path`` to mono float samples.

    Returns (samples, sample_rate). The native rate is kept: resampling
    would move or blur the very cutoff being looked for.
    """
    from . import probe

    info = probe.probe(path)
    duration = duration or info.duration
    start = max(0.0, duration / 2 - EXCERPT_SECONDS / 2)
    proc = subprocess.run(
        [
            str(ffmpeg.ffmpeg_path()), "-v", "error",
            "-ss", f"{start:.3f}", "-t", f"{EXCERPT_SECONDS:.3f}",
            "-i", str(path), "-map", "0:a:0", "-ac", "1", "-f", "f32le", "-",
        ],
        capture_output=True,
        timeout=timeout,
        **ffmpeg._no_window_kwargs(),
    )
    if proc.returncode != 0 or not proc.stdout:
        message = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(message[-1] if message else "ffmpeg could not decode the file")
    return np.frombuffer(proc.stdout, dtype="<f4"), info.sample_rate


def check(path: Path, duration: float | None = None) -> Verdict:
    samples, sample_rate = decode_excerpt(Path(path), duration)
    if sample_rate < MIN_SAMPLE_RATE:
        return Verdict(INCONCLUSIVE)
    return analyse_samples(samples, sample_rate)


def check_library(
    paths: list[Path],
    *,
    library,
    context=None,
    skip_existing: bool = True,
) -> CheckResult:
    """Check each lossless track in ``paths`` and store the verdict.

    Lossy files and sample rates under 44.1 kHz are skipped (nothing useful
    to say about them). One file failing never stops the batch.
    """
    result = CheckResult()
    total = len(paths)
    for index, path in enumerate(paths):
        if context is not None:
            context.raise_if_cancelled()
            context.progress(index / total if total else None, f"Checking {Path(path).name}")
        track = library.get(path)
        if track is None or not track.is_lossless or track.sample_rate < MIN_SAMPLE_RATE:
            result.skipped.append(Path(path))
            continue
        if skip_existing and track.authenticity is not None:
            result.skipped.append(Path(path))
            continue
        try:
            verdict = check(track.path, track.duration)
        except Exception as exc:  # noqa: BLE001 -- keep going through the batch
            result.failed.append((Path(path), str(exc)))
            continue
        library.set_authenticity(track.path, verdict.state, verdict.cutoff_hz)
        result.checked.append((track.path, verdict))

    if context is not None:
        context.progress(1.0, f"Checked {len(result.checked)} file(s)")
    return result
