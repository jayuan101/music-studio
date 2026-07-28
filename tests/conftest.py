"""Shared fixtures.

Tests that need real audio generate it with ffmpeg rather than committing
binary files, so the suite stays small and the sources are reproducible.
"""

from __future__ import annotations

import subprocess
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from musicstudio.core import ffmpeg  # noqa: E402

requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg.is_available(), reason="ffmpeg is not installed"
)


def make_tone(
    path: Path,
    *,
    duration: float = 3.0,
    sample_rate: int = 48000,
    frequency: int = 440,
    channels: int = 2,
    codec: str = "flac",
    extra: list[str] | None = None,
) -> Path:
    """Render a test tone to ``path`` with ffmpeg."""
    path.parent.mkdir(parents=True, exist_ok=True)
    layout = "stereo" if channels == 2 else "mono"
    command = [
        str(ffmpeg.ffmpeg_path()),
        "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi",
        "-i", f"sine=frequency={frequency}:duration={duration}:sample_rate={sample_rate}",
        "-af", f"aformat=channel_layouts={layout},volume=-12dB",
        "-c:a", codec,
        *(extra or []),
        str(path),
    ]
    subprocess.run(command, check=True, capture_output=True)
    return path


def make_png(width: int = 240, height: int = 240) -> bytes:
    """A valid PNG carrying a gradient, so artwork tests need no image library.

    The gradient matters: a flat colour compresses down to a couple of hundred
    bytes, which the artwork code correctly rejects as a placeholder image.
    Test fixtures have to be realistically sized to exercise the real path.
    """

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    rows = []
    for y in range(height):
        pixels = bytearray()
        for x in range(width):
            pixels += bytes(((x * 7) % 256, (y * 5) % 256, ((x + y) * 3) % 256))
        rows.append(b"\x00" + bytes(pixels))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def cover_png() -> bytes:
    """Cover art large enough to pass the minimum-size check (>1 KB)."""
    data = make_png()
    assert len(data) > 1024, "fixture image must exceed the placeholder threshold"
    return data


@pytest.fixture
def tone_flac(tmp_path: Path) -> Path:
    """A 24-bit / 48 kHz stereo FLAC -- the reference source for most tests."""
    return make_tone(
        tmp_path / "tone.flac",
        codec="flac",
        extra=["-sample_fmt", "s32", "-bits_per_raw_sample", "24"],
    )


@pytest.fixture
def tone_mp3(tmp_path: Path) -> Path:
    """A lossy source, for the quality-warning paths."""
    return make_tone(tmp_path / "tone.mp3", codec="libmp3lame", extra=["-b:a", "192k"])


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Keep tests away from the user's real config, cache and library."""
    from musicstudio import config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(config, "ARTWORK_CACHE_DIR", tmp_path / "cache" / "artwork")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "data" / "library.db")
    config.reset_settings_cache()
    yield
    config.reset_settings_cache()
