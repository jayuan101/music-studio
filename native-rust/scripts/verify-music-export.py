"""Validate the FFmpeg export contract using generated silence, never user media.

This checks the existing FFmpeg tool and planned argument contract. Windows CI
separately tests the Rust argument builder; this is not an interactive app test.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import wave

ffmpeg = Path(r"D:\Projects\MusicStudio-Rust\tools\ffmpeg\ffmpeg.exe")
ffprobe = ffmpeg.with_name("ffprobe.exe")
base = Path(__file__).resolve().parents[1] / ".tmp" / "music-export-tests"
base.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(prefix="export-", dir=base) as temporary:
    directory = Path(temporary)
    assert directory.resolve().is_relative_to(base.resolve())
    source = directory / "source & unchanged.wav"
    destination = directory / "trimmed.flac"
    with wave.open(str(source), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(44100)
        output.writeframes(bytes(44100 * 2 * 2 * 2))
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    args = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
            "-i", str(source), "-ss", "0.250000", "-t", "0.500000", "-map", "0:a:0",
            "-vn", "-map_metadata", "0", "-c:a", "flac", "-compression_level", "8", str(destination)]
    first = subprocess.run(args, capture_output=True, timeout=20)
    assert first.returncode == 0, first.stderr
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    probe = subprocess.run([str(ffprobe), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(destination)],
                           capture_output=True, text=True, timeout=20, check=True)
    info = json.loads(probe.stdout)
    assert info["streams"][0]["sample_rate"] == "44100"
    assert info["streams"][0]["channels"] == 2
    assert abs(float(info["format"]["duration"]) - 0.5) < 0.01
    destination.write_bytes(b"Pre-existing export must not be replaced")
    exported = hashlib.sha256(destination.read_bytes()).hexdigest()
    second = subprocess.run(args, capture_output=True, timeout=20)
    assert hashlib.sha256(destination.read_bytes()).hexdigest() == exported, second.stderr
    print(f"FFmpeg existing-file exit code: {second.returncode}; {second.stderr.decode(errors='replace').strip()}")
print("PASS: synthetic trim/export; original hash, sample rate and channels preserved; overwrite refused.")
