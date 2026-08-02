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
