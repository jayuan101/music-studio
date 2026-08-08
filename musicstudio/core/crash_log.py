"""Last-resort logging for uncaught exceptions.

Nothing in this app logs anything today, which makes a crash reported by the
user completely undiagnosable after the fact -- Windows Error Reporting only
ever records a native exception code and "faulting module: unknown", never
the actual Python traceback. Installing this at startup means the *next*
crash leaves a real trace in the app's data directory.
"""

from __future__ import annotations

import sys
import threading
import traceback
from datetime import datetime, timezone


def crash_log_path():
    """Resolved fresh on every call, not cached at import time.

    A cached module-level path baked in ``config.DATA_DIR`` at import time
    is what let the pytest suite's own crash/debug activity leak into the
    real user's log directory -- the test fixture that monkeypatches
    ``config.DATA_DIR`` runs long after this module was first imported, so
    a constant computed once would keep pointing at the real location
    regardless. Resolving here instead means the monkeypatch is honoured.
    """
    from .. import config

    return config.DATA_DIR / "crash.log"


def debug_log_path():
    from .. import config

    return config.DATA_DIR / "debug.log"


def native_log_path():
    from .. import config

    return config.DATA_DIR / "native.log"


def debug(message: str) -> None:
    """Append a timestamped line to debug.log.

    Separate from crash.log -- this is for temporary, targeted instrumentation
    of a specific code path under active investigation, not for the crash
    hook's job. Never raises; a failure to log must never break the caller.
    """
    try:
        path = debug_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()}  {message}\n")
    except OSError:
        pass


def _write(header: str, exc_type, exc_value, exc_tb) -> None:
    try:
        path = crash_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n=== {header} at {datetime.now(timezone.utc).isoformat()} ===\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
    except OSError:
        pass  # a failure to log must never itself crash the crash handler


def install_native_capture() -> None:
    """Give C/C++ code and Python's own fatal-error path somewhere to write.

    This app is built windowed (``console=False``), so a frozen build has no
    real stderr -- fd 2 goes nowhere and ``sys.stderr`` is ``None``. Every
    native abort() (a Python fatal error, a C++ ``terminate()``, Qt's own
    ``qFatal()`` print) writes its dying message to stderr immediately before
    the process dies, and every one of those messages has been vanishing as
    a result -- which is why past crashes (exception 0xc0000409, a plain
    ``abort()``) only ever showed an opaque code and a module offset.
    Redirecting fd 2 to a real file, giving Python a real ``sys.stderr``,
    and enabling ``faulthandler`` (which dumps every thread's Python
    traceback on a fatal signal) together mean the *next* crash leaves an
    actual named cause on disk.

    Must run before anything else that might abort, so this is the first
    call in ``app.main()`` -- and it must never itself raise, since
    instrumentation breaking startup would be strictly worse than no
    instrumentation.
    """
    try:
        import faulthandler
        import os

        from .. import __version__

        path = native_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        f = open(path, "a", buffering=1, encoding="utf-8", errors="replace")
        f.write(f"\n=== app start (v{__version__}) at {datetime.now(timezone.utc).isoformat()} ===\n")
        f.flush()

        sys.stderr = f
        try:
            os.dup2(f.fileno(), 2)
        except (OSError, ValueError):
            # No real fd 2 to replace (e.g. running under a debugger/console
            # that already owns it) -- the sys.stderr redirect above still
            # covers Python-level writers either way.
            pass

        faulthandler.enable(file=f, all_threads=True)
    except OSError:
        pass


def install() -> None:
    """Route uncaught exceptions on the main thread and any Python
    ``threading.Thread`` into ``crash.log`` instead of vanishing."""
    default_hook = sys.excepthook

    def _excepthook(exc_type, exc_value, exc_tb) -> None:
        _write("Unhandled exception (main thread)", exc_type, exc_value, exc_tb)
        default_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _excepthook

    def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
        _write(
            f"Unhandled exception (thread {args.thread.name!r})",
            args.exc_type, args.exc_value, args.exc_traceback,
        )

    threading.excepthook = _thread_excepthook


def install_qt_message_handler() -> None:
    """Log every Qt-level warning/critical/fatal message to debug.log.

    Windows Error Reporting has shown this app crashing with exception code
    0xc0000409 faulting inside ucrtbase.dll, additional info 7
    (FAST_FAIL_FATAL_APP_EXIT) -- the exact signature of a plain C
    ``abort()`` call. Qt calls ``abort()`` itself via ``qFatal()`` on certain
    internal assertion failures (the most common being a GUI operation
    performed from the wrong thread), always printing a message immediately
    before doing so. That message never reaches sys.excepthook -- the
    process dies below the Python interpreter, not inside it -- so this is
    the only way to see what Qt thought was fatal enough to abort over.
    """
    from PySide6.QtCore import QtMsgType, qInstallMessageHandler

    level_names = {
        QtMsgType.QtDebugMsg: "debug",
        QtMsgType.QtInfoMsg: "info",
        QtMsgType.QtWarningMsg: "warning",
        QtMsgType.QtCriticalMsg: "critical",
        QtMsgType.QtFatalMsg: "FATAL",
    }

    def _handler(msg_type, context, message) -> None:
        level = level_names.get(msg_type, str(msg_type))
        location = f" ({context.file}:{context.line})" if getattr(context, "file", None) else ""
        debug(f"qt {level}: {message}{location}")

    qInstallMessageHandler(_handler)
