"""Settings persistence and the quality flags it drives."""

from __future__ import annotations

import json

import pytest

from musicstudio import config
from musicstudio.config import Settings
from musicstudio.core import convert, formats

from .test_edit import make_info


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_defaults_favour_preservation():
    """The quality-first defaults are the whole premise of the app."""
    s = Settings()
    assert s.preserve_source_rate
    assert s.preserve_source_depth
    assert s.dither_on_downconvert
    assert not s.overwrite_existing


def test_save_then_load_round_trips_every_field(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    original = Settings(
        output_dir=str(tmp_path / "out"),
        filename_template="{artist}/{album}/{track} {title}",
        preserve_source_rate=False,
        preserve_source_depth=False,
        artwork_min_size=900,
        limiter_ceiling_db=-1.5,
        max_gain_db=42.0,
        download_mode="convert",
        library_paths=[str(tmp_path / "music")],
    )
    original.save()

    loaded = Settings.load()
    assert loaded.to_dict() == original.to_dict()


def test_unknown_keys_in_the_file_are_ignored(tmp_path, monkeypatch):
    """A settings file written by a newer build must not crash an older one."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "_SETTINGS_FILE", path)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    path.write_text(json.dumps({"output_dir": "/tmp/x", "from_the_future": 42}))

    loaded = Settings.load()
    assert loaded.output_dir == "/tmp/x"
    assert not hasattr(loaded, "from_the_future")


def test_corrupt_settings_file_falls_back_to_defaults(tmp_path, monkeypatch):
    """A truncated file must never stop the app from starting."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "_SETTINGS_FILE", path)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)
    path.write_text("{ this is not json")

    loaded = Settings.load()
    assert loaded.preserve_source_rate is True


def test_save_is_atomic(tmp_path, monkeypatch):
    """A crash mid-write must not leave a half-written settings file."""
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "_SETTINGS_FILE", path)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    Settings(output_dir="/first").save()
    Settings(output_dir="/second").save()

    assert json.loads(path.read_text())["output_dir"] == "/second"
    assert not (tmp_path / "settings.json.tmp").exists()


def test_library_paths_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    s = Settings()
    s.library_paths = ["/music/a", "/music/b"]
    s.save()
    assert Settings.load().library_paths == ["/music/a", "/music/b"]


# ---------------------------------------------------------------------------
# The flags actually doing something
# ---------------------------------------------------------------------------


def test_preserving_keeps_hi_res_intact():
    output = convert.resolve_output(
        make_info(sample_rate=96000, bit_depth=24),
        formats.FLAC,
        preserve_rate=True,
        preserve_depth=True,
    )
    assert output.sample_rate == 96000
    assert output.bit_depth == 24


def test_not_preserving_normalises_to_cd_quality():
    """Regression: both branches used to be identical, so the flags did nothing."""
    output = convert.resolve_output(
        make_info(sample_rate=96000, bit_depth=24),
        formats.FLAC,
        preserve_rate=False,
        preserve_depth=False,
    )
    assert output.sample_rate == convert.STANDARD_SAMPLE_RATE
    assert output.bit_depth == convert.STANDARD_BIT_DEPTH


def test_not_preserving_never_upsamples_a_low_res_source():
    """Normalising means bringing things down, never inventing detail."""
    output = convert.resolve_output(
        make_info(sample_rate=22050, bit_depth=16),
        formats.FLAC,
        preserve_rate=False,
        preserve_depth=False,
    )
    assert output.sample_rate == 22050
    assert output.bit_depth == 16


def test_normalising_explains_itself():
    output = convert.resolve_output(
        make_info(sample_rate=96000), formats.FLAC, preserve_rate=False
    )
    assert any(note.title == "Sample rate normalised" for note in output.notes)


def test_explicit_rate_overrides_the_preference():
    output = convert.resolve_output(
        make_info(sample_rate=96000), formats.FLAC, sample_rate=48000, preserve_rate=False
    )
    assert output.sample_rate == 48000
