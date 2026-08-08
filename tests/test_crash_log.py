"""Last-resort crash logging.

crash_log.py exists because past crashes only ever showed Windows Error
Reporting's exception code and a module offset -- never a cause. These
tests prove two things: the log paths honour a monkeypatched DATA_DIR
(the bug that let the test suite's own noise leak into the real user's
log), and install_native_capture() actually captures a fatal signal's
traceback, which is the whole point of it existing.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

from musicstudio.core import crash_log


def test_debug_writes_to_the_monkeypatched_data_dir(tmp_path, monkeypatch):
    from musicstudio import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")

    crash_log.debug("hello")

    log_path = tmp_path / "data" / "debug.log"
    assert log_path.is_file()
    assert "hello" in log_path.read_text(encoding="utf-8")


def test_crash_log_path_and_debug_log_path_track_data_dir(tmp_path, monkeypatch):
    from musicstudio import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")

    assert crash_log.crash_log_path() == tmp_path / "data" / "crash.log"
    assert crash_log.debug_log_path() == tmp_path / "data" / "debug.log"
    assert crash_log.native_log_path() == tmp_path / "data" / "native.log"


def test_debug_never_raises_when_the_path_is_unwritable(tmp_path, monkeypatch):
    from musicstudio import config

    # A file where a directory is expected -- mkdir() must fail with OSError.
    blocker = tmp_path / "data"
    blocker.write_text("not a directory")
    monkeypatch.setattr(config, "DATA_DIR", blocker)

    crash_log.debug("this must not raise")  # no assertion needed -- just must not throw


def test_install_native_capture_records_a_fatal_signal_traceback(tmp_path):
    """End-to-end, in a real child process: a fatal signal after
    install_native_capture() must leave the exact cause on disk.

    Has to run out-of-process -- the whole point of faulthandler here is
    that it fires right before the process actually dies.
    """
    data_dir = tmp_path / "data"
    script = textwrap.dedent(f"""
        import sys
        from pathlib import Path
        sys.path.insert(0, {str(__import__("pathlib").Path(__file__).resolve().parent.parent)!r})
        from musicstudio import config
        config.DATA_DIR = Path({str(data_dir)!r})
        from musicstudio.core import crash_log
        crash_log.install_native_capture()
        import faulthandler
        faulthandler._sigabrt()
    """)
    subprocess.run([sys.executable, "-c", script], timeout=15)

    log = (data_dir / "native.log").read_text(encoding="utf-8", errors="replace")
    assert "=== app start" in log
    assert "Fatal Python error" in log
