"""The "is this lossless file really lossless?" check, and Library health presets."""

from __future__ import annotations

import numpy as np
import pytest

from musicstudio.core import authenticity as auth
from musicstudio.core import health_presets
from musicstudio.core import tags as T
from musicstudio.core.probe import AudioInfo
from musicstudio.db import Library, scan_into_library

from .conftest import make_noise, requires_ffmpeg, transcode_through_mp3

SR = 44100


def _noise(seconds: float = 20.0, *, lowpass_hz: float | None = None, seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(int(SR * seconds)).astype(np.float32)
    if lowpass_hz:
        spectrum = np.fft.rfft(x)
        spectrum[np.fft.rfftfreq(len(x), 1 / SR) > lowpass_hz] = 0
        x = np.fft.irfft(spectrum, len(x)).astype(np.float32)
    return x * 0.2


def test_full_band_noise_is_ok():
    verdict = auth.analyse_samples(_noise(), SR)
    assert verdict.state == auth.OK and verdict.cutoff_hz is None


@pytest.mark.parametrize("cutoff", [16000, 17000, 18500])
def test_a_lowpass_below_19_5k_is_suspect(cutoff):
    verdict = auth.analyse_samples(_noise(lowpass_hz=cutoff), SR)
    assert verdict.state == auth.SUSPECT
    assert abs(verdict.cutoff_hz - cutoff) <= 750
    assert verdict.drop_db >= auth.SUSPECT_DROP_DB


def test_a_cd_style_lowpass_near_20k_is_not_reported():
    verdict = auth.analyse_samples(_noise(lowpass_hz=20300), SR)
    assert verdict.state == auth.OK


def test_a_gentle_rolloff_is_not_a_cliff():
    # A one-pole low-pass: -6 dB/octave, like a dull recording, no cliff.
    x = _noise()
    y = np.empty_like(x)
    acc = 0.0
    for i, v in enumerate(x):
        acc += 0.35 * (v - acc)
        y[i] = acc
    assert auth.analyse_samples(y, SR).state == auth.OK


def test_silence_and_short_clips_are_inconclusive():
    assert auth.analyse_samples(np.zeros(SR * 10, dtype=np.float32), SR).state == auth.INCONCLUSIVE
    assert auth.analyse_samples(_noise(0.2), SR).state == auth.INCONCLUSIVE


# -- real files ---------------------------------------------------------------

@requires_ffmpeg
def test_check_tells_a_transcode_from_the_real_thing(tmp_path):
    real = make_noise(tmp_path / "real.flac")
    fake = transcode_through_mp3(real, tmp_path / "fake.flac", "128k")
    assert auth.check(real).state == auth.OK
    verdict = auth.check(fake)
    assert verdict.state == auth.SUSPECT
    assert 15000 <= verdict.cutoff_hz <= 17500


@requires_ffmpeg
def test_check_library_stores_verdicts_and_skips_what_it_should(tmp_path):
    folder = tmp_path / "music"
    real = make_noise(folder / "real.flac")
    T.write(real, T.TagSet(title="real"))
    fake = transcode_through_mp3(real, folder / "fake.flac")
    T.write(fake, T.TagSet(title="fake"))
    lossy = make_noise(folder / "lossy.mp3", codec="libmp3lame", extra=["-b:a", "128k"])
    library = Library(tmp_path / "t.db")
    scan_into_library(library, [folder])

    result = auth.check_library([real, fake, lossy], library=library)
    assert sorted((p.name, v.state) for p, v in result.checked) == [
        ("fake.flac", auth.SUSPECT), ("real.flac", auth.OK)]
    assert [p.name for p in result.skipped] == ["lossy.mp3"]
    assert result.suspect == 1
    assert library.get(fake).authenticity == auth.SUSPECT
    assert library.get(fake).authenticity_cutoff_hz
    assert library.get(lossy).authenticity is None

    # Already checked: nothing to do the second time.
    again = auth.check_library([real, fake], library=library)
    assert not again.checked and len(again.skipped) == 2


@requires_ffmpeg
def test_a_verdict_survives_tag_edits_but_not_a_change_to_the_audio(tmp_path):
    folder = tmp_path / "music"
    track = make_noise(folder / "a.flac")
    library = Library(tmp_path / "t.db")
    scan_into_library(library, [folder])
    library.set_authenticity(track, auth.SUSPECT, 16000)

    T.write(track, T.TagSet(title="renamed"))
    scan_into_library(library, [folder], force=True)
    row = library.get(track)
    assert row.title == "renamed" and row.authenticity == auth.SUSPECT

    make_noise(track, duration=20.0)  # different length: different audio
    scan_into_library(library, [folder], force=True)
    assert library.get(track).authenticity is None


# -- health presets -----------------------------------------------------------

def _add(library, folder, name, *, lossless=True, **tags):
    path = folder / name
    path.write_bytes(b"x")
    codec = "flac" if lossless else "mp3"
    info = AudioInfo(path, codec, codec, "", 100.0, 44100, 2, "", 16 if lossless else 0,
                     900_000, 1, {})
    library.upsert(path, info, T.TagSet(**tags))
    return path.resolve()


def _names(library, preset_id):
    return sorted(t.path.name for t in library.playlist_tracks(preset_id))


def test_health_presets(tmp_path):
    folder = tmp_path / "m"
    folder.mkdir()
    library = Library(tmp_path / "t.db")
    full = _add(library, folder, "full.flac", title="t", album="A", date="2020",
                replaygain_track_gain="-3.00 dB")
    _add(library, folder, "bare.flac", title="t")
    _add(library, folder, "lossy.mp3", lossless=False, title="t", album="A", date="2020")
    library.set_authenticity(full, auth.SUSPECT, 16000)

    ids = {name: pid for pid, name, _ in health_presets.PRESETS}
    assert [p.name for p in library.health_presets()] == [n for _, n, _ in health_presets.PRESETS]
    assert all(p.id < 0 and p.is_smart for p in library.health_presets())
    assert _names(library, ids["Missing album"]) == ["bare.flac"]
    assert _names(library, ids["Missing year"]) == ["bare.flac"]
    assert _names(library, ids["No ReplayGain"]) == ["bare.flac", "lossy.mp3"]
    assert _names(library, ids["Lossy files"]) == ["lossy.mp3"]
    assert _names(library, ids["Looks transcoded"]) == ["full.flac"]
    assert _names(library, ids["Lossless, not checked yet"]) == ["bare.flac"]
    assert _names(library, ids["Missing artwork"]) == ["bare.flac", "full.flac", "lossy.mp3"]
    assert library.count_rules(dict(health_presets.PRESETS[3][2])) == 2
    # Presets are never stored with the user's own playlists.
    assert library.playlists() == []
    assert library.get_playlist(-99) is None
