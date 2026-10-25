"""Exercise actual widgets in a separate, isolated QApplication process."""

import os
import subprocess
import sys
import textwrap
from pathlib import Path
import pytest


@pytest.mark.parametrize("restore_playback", [False, True])
def test_window_opens_all_pages_and_closes(tmp_path, tone_flac, restore_playback):
    script = textwrap.dedent('''
        import sys
        from pathlib import Path
        from musicstudio import config

        root = Path(sys.argv[1])
        config.CONFIG_DIR = root / "config"
        config.DATA_DIR = root / "data"
        config.CACHE_DIR = root / "cache"
        config.ARTWORK_CACHE_DIR = root / "cache" / "artwork"
        config.TEMP_DIR = root / "cache" / "tmp"
        config.PREVIEW_CACHE_DIR = config.TEMP_DIR / "download_preview"
        config.DEFAULT_OUTPUT_DIR = root / "output"
        config.DB_PATH = root / "data" / "library.db"
        config._SETTINGS_FILE = root / "config" / "settings.json"
        config.reset_settings_cache()
        from musicstudio.core import session
        restore = sys.argv[2] == "True"
        song = sys.argv[3]
        if restore:
            session.save({"tracks": [song, song], "order": [1, 0], "position": 0,
                          "shuffle": True, "repeat": "all", "seconds": 1.0})

        from PySide6.QtWidgets import QApplication
        from PySide6.QtTest import QTest
        from musicstudio.ui.main_window import MainWindow, PAGES

        errors = []
        def report_error(*args):
            errors.append(str(args[1]))
            sys.__excepthook__(*args)
        sys.excepthook = report_error
        app = QApplication([])
        window = MainWindow()
        window.show()
        if restore:
            QTest.qWait(500)
            assert not window.now_playing_bar.player.is_playing
            assert window.now_playing_bar.queue.current == Path(song)
            assert window.now_playing_bar.queue.snapshot()["order"] == [1, 0]
            assert abs(window.now_playing_bar.player.position - 1.0) < 0.3
        for label, _ in PAGES:
            window.go_to(label)
            app.processEvents()
        assert window.isVisible()
        assert window.close()
        app.processEvents()
        assert not errors, errors
        assert config._SETTINGS_FILE.exists()
        print("All pages opened and window closed")
    ''')
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path), str(restore_playback), str(tone_flac)],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "All pages opened" in result.stdout
