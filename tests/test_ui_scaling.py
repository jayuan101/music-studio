"""Interface size picked from the screen (musicstudio/ui/scaling.py)."""

import os

from musicstudio.ui import scaling


def test_1080p_at_100_percent_windows_scaling_gets_bigger():
    # The case that prompted this: a 1920x1080 laptop at 100% looked tiny.
    assert scaling.auto_factor(1080) == 1.2


def test_small_or_unknown_screens_never_shrink():
    assert scaling.auto_factor(768) == 1.0
    assert scaling.auto_factor(None) == 1.0


def test_large_screens_are_capped():
    assert scaling.auto_factor(2160) == scaling.AUTO_MAX


def test_manual_choice_overrides_auto():
    assert scaling.resolve("150", logical_height=1080) == 1.5
    assert scaling.resolve("125%", logical_height=1080) == 1.25


def test_manual_choice_is_clamped_and_garbage_falls_back_to_auto():
    assert scaling.resolve("500", logical_height=1080) == scaling.MANUAL_MAX
    assert scaling.resolve("banana", logical_height=1080) == 1.2
    assert scaling.resolve("", logical_height=1080) == 1.2


def test_apply_respects_an_existing_environment_override(monkeypatch):
    monkeypatch.setenv("QT_SCALE_FACTOR", "1.75")
    assert scaling.apply("100") == 1.75
    assert os.environ["QT_SCALE_FACTOR"] == "1.75"


def test_apply_sets_the_factor(monkeypatch):
    monkeypatch.delenv("QT_SCALE_FACTOR", raising=False)
    assert scaling.apply("125") == 1.25
    assert os.environ["QT_SCALE_FACTOR"] == "1.25"
