"""Application entry point."""

from __future__ import annotations

import gc
import os
import sys

# Qt6 supports pluggable QtMultimedia backends; on Windows both "ffmpeg" and
# the native "windows" (Media Foundation) backends get bundled, and Qt's own
# default selection has picked ffmpeg here -- confirmed via the startup log
# ("Using Qt multimedia with FFmpeg version...") and via ffmpegmediaplugin.dll
# showing up in a crash dump's thread stack. Media Foundation is the OS's own,
# far more battle-tested decoder (it's what Windows Media Player etc. use) and
# is all playback of an already-converted library file needs -- this app's own
# ffmpeg binary handles every encode/decode that actually requires it,
# entirely separately from QtMultimedia. Must be set before the first
# QMediaPlayer is constructed, so as early as possible.
os.environ.setdefault("QT_MEDIA_BACKEND", "windows")

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication

from . import APP_NAME, APP_ORG, __version__
from .config import ensure_dirs
from .core import crash_log
from .ui import theme


def _disable_ime_attachment() -> None:
    """Stop Windows' Text Services Framework from attaching to this process.

    Two independent crash dumps (exception 0xc0000409 / FAST_FAIL_FATAL_APP_EXIT,
    always at the same ucrtbase.dll offset) both showed msctf.dll and imm32.dll
    on the crashing thread's stack, and the second one crashed in the instant
    right after a modal QMessageBox closed and focus returned -- exactly when
    TSF re-attaches to whatever gets focus next. Ruling out QtMultimedia (the
    second crash had none of its DLLs on the stack at all) left this as the
    remaining common factor across both.

    Chromium hit the same crash class years ago and worked around it the same
    way: calling ImmDisableIME(0) before any window exists disables IME/TSF
    attachment for the whole process. The only real cost is that Windows' IME
    (Chinese/Japanese/Korean input) stops working in this app -- ordinary
    English/Latin-script typing, which is all this app's own text fields need,
    is unaffected.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.imm32.ImmDisableIME(0)
    except OSError:
        pass


def _tame_garbage_collector(app: QApplication) -> None:
    """Stop Python's cyclic GC from running at an arbitrary, unsafe moment.

    Every access-violation crash the native capture has recorded (waveform
    peak computation, a Library search, a JobQueue signal connection --
    three unrelated code paths) shows the same last captured state:
    "Garbage-collecting". Python's automatic collector can run at
    essentially any bytecode boundary, on any thread, including in the
    middle of constructing or tearing down a PySide6-wrapped Qt object --
    a known hazard class for PySide6/PyQt apps, since shiboken's C++/Python
    reference bookkeeping is not safe to interrupt like that.

    Disabling automatic collection and running it manually instead, on a
    timer tied to the main thread's own event loop, moves every collection
    to a moment Qt itself considers safe (between processed events) rather
    than an arbitrary one on an arbitrary thread. Reference cycles this app
    does create -- JobQueue's own signal-connection lambdas are exactly
    this shape -- still get reclaimed, just on a predictable schedule
    instead of at random.
    """
    gc.disable()
    timer = QTimer(app)
    timer.setInterval(5 * 60 * 1000)  # frequent enough to bound memory
    timer.timeout.connect(gc.collect)  # growth, rare enough to cost nothing
    timer.start()
    app._gc_timer = timer  # keep a Python-side reference alongside Qt's own


def create_app(argv: list[str] | None = None) -> QApplication:
    """Build the QApplication with our identity and theme applied."""
    app = QApplication.instance() or QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName(APP_ORG)
    app.setApplicationVersion(__version__)
    # Fusion renders our stylesheet consistently across Windows versions;
    # the native style overrides several of the colours we set.
    app.setStyle("Fusion")
    theme.apply_theme(app)
    return app


def main(argv: list[str] | None = None) -> int:
    ensure_dirs()
    # First, before anything else could abort: a windowed build has no real
    # stderr, so every native fatal-error message has been vanishing.
    crash_log.install_native_capture()
    crash_log.install()
    crash_log.install_qt_message_handler()
    _disable_ime_attachment()
    app = create_app(argv)
    _tame_garbage_collector(app)

    from .ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    # Files passed on the command line (or via "Open with") get imported.
    arguments = (argv if argv is not None else sys.argv)[1:]
    if arguments:
        from pathlib import Path

        window.library_panel.import_paths([Path(a) for a in arguments])

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
