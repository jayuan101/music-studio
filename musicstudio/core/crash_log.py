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

from ..config import DATA_DIR

CRASH_LOG_PATH = DATA_DIR / "crash.log"
DEBUG_LOG_PATH = DATA_DIR / "debug.log"


def debug(message: str) -> None:
    """Append a timestamped line to debug.log.

    Separate from crash.log -- this is for temporary, targeted instrumentation
    of a specific code path under active investigation, not for the crash
    hook's job. Never raises; a failure to log must never break the caller.
    """
    try:
        DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DEBUG_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()}  {message}\n")
    except OSError:
        pass


def _write(header: str, exc_type, exc_value, exc_tb) -> None:
    try:
        CRASH_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(CRASH_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n=== {header} at {datetime.now(timezone.utc).isoformat()} ===\n")
            traceback.print_exception(exc_type, exc_value, exc_tb, file=f)
    except OSError:
        pass  # a failure to log must never itself crash the crash handler


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
