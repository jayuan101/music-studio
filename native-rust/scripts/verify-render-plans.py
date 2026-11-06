"""Run the exact RENDER_PLAN commands printed by the Rust tests through real FFmpeg.

Usage: verify-render-plans.py LOG [--ffmpeg PATH]

The audio is generated here, never user media. Each plan's own argument list is
used unchanged apart from substituting the SOURCE/OUTPUT placeholders, so this
tests what the app would actually run rather than a separate copy of the contract.
"""
import argparse
import array
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
CODECS = {"libmp3lame": "mp3", "libopus": "opus", "libvorbis": "vorbis"}
LOSSLESS = {"flac", "alac", "wavpack", "pcm_s16le", "pcm_s24le", "pcm_s32le", "pcm_f32le",
            "pcm_s16be", "pcm_s24be", "pcm_s32be", "pcm_f32be"}
SOURCES = {
    "SOURCE.wav": ("s24", "pcm_s24le", 48000, 1.0),
    "SOURCE16.wav": ("s16", "pcm_s16le", 44100, 1.0),
    "SOURCEF.wav": ("flt", "pcm_f32le", 48000, 1.0),
}


def run(*args, timeout=60):
    return subprocess.run([str(a) for a in args], capture_output=True, timeout=timeout)


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def plans_from(log):
    found = []
    for line in Path(log).read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.search(r"RENDER_PLAN (\{.*\})", line)
        if match:
            found.append(json.loads(match.group(1)))
    if len(found) < 11:
        sys.exit(f"Expected at least 11 RENDER_PLAN lines in {log}; found {len(found)}.")
    return found


def make_sources(ffmpeg, folder):
    made = {}
    for name, (_, codec, rate, seconds) in SOURCES.items():
        path = folder / f"{name[:-4]} & unchanged.wav"
        # Different content per channel, so a swap or accidental mono output shows up.
        expression = f"0.9*sin(2*PI*440*t)|0.8*sin(2*PI*660*t):s={rate}:d={seconds}"
        result = run(ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                     "-i", f"aevalsrc={expression}", "-c:a", codec, path)
        assert result.returncode == 0, result.stderr
        made[name] = path
    return made


def pcm(ffmpeg, path, rate=None, channels=None, fmt="s32le"):
    command = [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-i", path]
    if rate:
        command += ["-ar", rate]
    if channels:
        command += ["-ac", channels]
    result = run(*command, "-f", fmt, "-")
    assert result.returncode == 0, result.stderr
    return result.stdout


def peak_db(ffmpeg, path):
    result = run(ffmpeg, "-nostdin", "-hide_banner", "-i", path, "-af", "volumedetect", "-f", "null", "-")
    match = re.search(r"max_volume: (-?[\d.]+|-inf) dB", result.stderr.decode(errors="replace"))
    assert match, result.stderr
    return float(match.group(1)) if match.group(1) != "-inf" else float("-inf")


def check_edit_effects(ffmpeg, plan, output):
    arguments = plan["args"]
    filters = arguments[arguments.index("-af") + 1] if "-af" in arguments else ""
    if "alimiter" in filters:
        peak = peak_db(ffmpeg, output)
        assert peak <= -0.2, f"limiter ceiling exceeded: {peak} dB"
    if "afade=t=in" in filters:
        samples = array.array("f", pcm(ffmpeg, output, channels=1, fmt="f32le"))
        rate = int(plan["rate"])
        window = rate // 200
        loud = lambda part: max(abs(value) for value in part)
        assert loud(samples[:window]) < loud(samples[rate // 10:rate // 10 + window]) * 0.6, "fade-in not audible"
        assert loud(samples[-window:]) < loud(samples[rate // 10:rate // 10 + window]) * 0.6, "fade-out not audible"


def check(ffmpeg, ffprobe, plan, sources, folder, index):
    source_name = plan["args"][plan["args"].index("-i") + 1]
    source = sources[source_name]
    placeholder = plan["args"][-1]
    assert placeholder.startswith("OUTPUT."), placeholder
    output = folder / f"{index:02d}-{re.sub(r'[^A-Za-z0-9]+', '_', plan['name'])}.{plan['extension']}"
    arguments = [str(source) if a == source_name else str(output) if a == placeholder else a for a in plan["args"]]
    before = digest(source)
    result = run(ffmpeg, *arguments)
    stderr = result.stderr.decode(errors="replace").strip()
    # media.rs treats any FFmpeg stderr as failure, so a warning here would fail in the app too.
    assert result.returncode == 0 and not stderr, f"exit {result.returncode}: {stderr}"
    assert output.is_file() and output.stat().st_size > 0, "no output written"
    assert digest(source) == before, "source changed"

    probe = run(ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", output)
    assert probe.returncode == 0, probe.stderr
    info = json.loads(probe.stdout)
    stream = info["streams"][0]
    requested = plan["args"][plan["args"].index("-c:a") + 1]
    codec = CODECS.get(requested, requested)
    assert stream["codec_name"] == codec, f"codec {stream['codec_name']} != {codec}"
    assert int(stream["sample_rate"]) == plan["rate"], f"rate {stream['sample_rate']}"
    assert int(stream["channels"]) == plan["channels"], f"channels {stream['channels']}"
    duration = float(info["format"].get("duration", stream.get("duration", 0)))
    tolerance = 0.03 if codec in LOSSLESS else 0.12  # Lossy encoders add a priming delay.
    assert abs(duration - plan["duration"]) <= tolerance, f"duration {duration} vs {plan['duration']}"

    notes = [f"{codec} {stream['sample_rate']} Hz {stream['channels']} ch {duration:.3f}s"]
    if "-af" not in plan["args"] and codec in LOSSLESS:
        assert pcm(ffmpeg, output) == pcm(ffmpeg, source), "decoded samples differ from the source"
        notes.append("sample-identical")
    else:
        assert peak_db(ffmpeg, output) > -60, "output is silent"
    check_edit_effects(ffmpeg, plan, output)

    existing = digest(output)
    run(ffmpeg, *arguments)
    assert digest(output) == existing, "-n did not protect the existing output"
    assert digest(source) == before, "source changed on the second run"
    return "; ".join(notes)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    parser.add_argument("--ffmpeg", default=str(ROOT / "tools/ffmpeg/ffmpeg.exe"))
    options = parser.parse_args()
    ffmpeg = Path(options.ffmpeg)
    ffprobe = ffmpeg.with_name("ffprobe.exe")
    version = run(ffmpeg, "-version").stdout.decode(errors="replace").splitlines()[0]
    print(version)
    plans = plans_from(options.log)
    base = ROOT / ".tmp" / "render-plans"
    base.mkdir(parents=True, exist_ok=True)
    failures = 0
    with tempfile.TemporaryDirectory(prefix="plans-", dir=base) as temporary:
        folder = Path(temporary)
        sources = make_sources(ffmpeg, folder)
        for index, plan in enumerate(plans):
            try:
                print(f"PASS {plan['name']}: {check(ffmpeg, ffprobe, plan, sources, folder, index)}")
            except AssertionError as error:
                failures += 1
                print(f"FAIL {plan['name']}: {error}\n     args: {plan['args']}")
    print(f"{len(plans) - failures}/{len(plans)} render plans passed against {ffmpeg}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
