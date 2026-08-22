"""Reading a file's metadata without stalling the window.

Two reads happen constantly as the user moves a selection around, and both are
real work that must not sit on the GUI thread:

* **Tags**, which carry the embedded cover art with them -- measured on this
  app's own library at ~2 ms per file on an SSD, and ~84 ms typical / 908 ms
  worst case on slower storage.
* **A probe**, which launches ``ffprobe`` as a *subprocess*. Process creation
  on Windows, against a large file, with a real-time virus scanner watching,
  is unpredictable in a way a plain read is not -- and it is bounded only by
  ``probe.PROBE_TIMEOUT_S``. Inline on the GUI thread, one stalled probe is a
  window that has stopped responding for as long as that timeout lasts.

Five places need one of these -- the Library details panel, the Tags & art
file list, the now-playing bar, the Editor and the Convert page -- so the
pattern lives here once rather than being reimplemented, subtly differently,
five times.

Deliberately *not* routed through :class:`~musicstudio.core.jobs.JobQueue`:
these fire on every click, and the job queue is both a small shared pool that
real work needs and a visible list that never prunes itself. Interactive reads
belong on their own thread where they cannot queue behind a long conversion.

What makes it safe as well as fast:

* Only plain data crosses back to the GUI thread. :class:`~musicstudio.core.tags.TagSet`
  is a dataclass with no Qt in it; the QPixmap is built by the receiving slot,
  because Qt GUI objects must not be constructed on a worker thread.
* A token guards every request, so a slow read for a row the user has already
  moved off is discarded rather than overwriting the current one.
* A short debounce means holding an arrow key down costs one read, not one per
  row travelled through.
* Results are cached against the file's modification time and size, so
  revisiting a track is instant while an edit made elsewhere in the app
  invalidates the entry with nothing to remember to wire up.
"""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal, Slot

from ...core import probe as probe_module
from ...core import tags as tags_module

#: How many reads to remember. Each entry holds that track's embedded cover
#: art, so this is a memory trade: ~64 covers of a few hundred KB is a handful
#: of MB in exchange for instant revisits.
DEFAULT_CACHE_SIZE = 64
#: How long to let the selection settle before reading from disk.
DEFAULT_DELAY_MS = 90


class _ReadSignals(QObject):
    """Signal carrier for :class:`_ReadTask`.

    A separate QObject because QRunnable is not one -- the same split the job
    queue makes, for the same reason.
    """

    #: (token, path, result or None)
    ready = Signal(int, object, object)


class _ReadTask(QRunnable):
    """Run one read function against one file, on a worker thread."""

    def __init__(
        self,
        token: int,
        path: Path,
        signals: _ReadSignals,
        reader: Callable[[Path], Any],
    ) -> None:
        super().__init__()
        self._token = token
        self._path = path
        self._signals = signals
        self._reader = reader

    @Slot()
    def run(self) -> None:
        try:
            result = self._reader(self._path)
        except BaseException:  # noqa: BLE001 -- must never escape a QRunnable override
            # An unreadable file is a blank panel, not a crashed app.
            result = None
        try:
            self._signals.ready.emit(self._token, self._path, result)
        except RuntimeError:
            # The owner went away while this read was in flight. Nobody is
            # listening on a deleted signal source, so this is a no-op rather
            # than a lost result -- the same guard the job queue makes.
            pass


class AsyncFileReader(QObject):
    """Runs ``reader(path)`` off the GUI thread, for the current request only.

    Connect to :attr:`ready` and call :meth:`request`. Only the most recent
    request is ever delivered; earlier ones are dropped rather than arriving
    late and overwriting the panel with a stale file's details.

    Parent it to the widget that uses it, so its thread pool is torn down with
    that widget -- ``~QThreadPool()`` waits for running tasks, which is what
    keeps a read from outliving the object it would deliver to.

    ``reader`` must return plain data and must not touch Qt: it runs on a
    worker thread, where constructing a QPixmap (or any QObject) is exactly
    the shiboken hazard this app has chased native crashes through.
    """

    #: (path, result or None) -- emitted on the GUI thread for the live request.
    ready = Signal(object, object)

    def __init__(
        self,
        reader: Callable[[Path], Any],
        parent: QObject | None = None,
        *,
        cache_size: int = DEFAULT_CACHE_SIZE,
        delay_ms: int = DEFAULT_DELAY_MS,
    ) -> None:
        super().__init__(parent)
        self._reader = reader
        self._signals = _ReadSignals(self)
        self._signals.ready.connect(self._on_ready)
        self._pool = QThreadPool(self)
        # One thread: scrolling should queue at most one read behind the
        # current one, not start a stampede against the same disk.
        self._pool.setMaxThreadCount(1)
        self._cache_size = cache_size
        self._cache: OrderedDict[tuple, Any] = OrderedDict()
        self._token = 0
        self._pending: Path | None = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._start)

    # -- requesting ------------------------------------------------------
    def request(self, path: Path) -> Any:
        """Ask for ``path``'s details.

        Returns the result immediately when it is already cached, in which
        case :attr:`ready` does *not* fire and the caller can render at once.
        Returns ``False`` when a read has been scheduled and the result will
        arrive on :attr:`ready`.
        """
        self.cancel()
        cached = self._cached(path)
        if cached is not _MISSING:
            return cached
        self._pending = path
        self._timer.start()
        return False

    def cancel(self) -> None:
        """Drop any pending or in-flight request.

        Bumping the token lets a running read finish and be discarded, which
        is cheaper and safer than trying to interrupt it.
        """
        self._timer.stop()
        self._token += 1
        self._pending = None

    def invalidate(self, path: Path) -> None:
        """Forget any cached read for ``path``.

        The mtime/size key already covers edits made through this app; this is
        for callers that want to force a re-read regardless.
        """
        key = self._key(path)
        if key is not None:
            self._cache.pop(key, None)

    # -- internals -------------------------------------------------------
    def _start(self) -> None:
        path = self._pending
        if path is None:
            return
        self._pool.start(_ReadTask(self._token, path, self._signals, self._reader))

    @Slot(int, object, object)
    def _on_ready(self, token: int, path: Path, result: Any) -> None:
        if token != self._token:
            return  # the selection moved on while this read was in flight
        self._store(path, result)
        self.ready.emit(path, result)

    def _key(self, path: Path) -> tuple | None:
        """Identify a file by path *and* content stamp.

        Keying on mtime and size as well as the path means editing a track's
        tags anywhere in the app invalidates its cached read for free.
        """
        try:
            info = path.stat()
        except OSError:
            return None
        return (str(path), info.st_mtime_ns, info.st_size)

    def _cached(self, path: Path):
        key = self._key(path)
        if key is None or key not in self._cache:
            return _MISSING
        self._cache.move_to_end(key)
        return self._cache[key]

    def _store(self, path: Path, result: Any) -> None:
        key = self._key(path)
        if key is None:
            return
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)


class _Missing:
    """Distinguishes "not cached" from a cached ``None`` (an unreadable file)."""


_MISSING = _Missing()


class AsyncTagReader(AsyncFileReader):
    """Reads a file's tags (and embedded cover art) off the GUI thread."""

    def __init__(self, parent: QObject | None = None, **kwargs) -> None:
        super().__init__(tags_module.try_read, parent, **kwargs)


class AsyncProbeReader(AsyncFileReader):
    """Runs ffprobe off the GUI thread.

    A smaller cache than the tag reader's: an ``AudioInfo`` is a handful of
    numbers rather than a cover image, but there is also less to gain from
    holding many, since it is the *subprocess launch* being avoided.
    """

    def __init__(self, parent: QObject | None = None, **kwargs) -> None:
        kwargs.setdefault("cache_size", 128)
        super().__init__(probe_module.try_probe, parent, **kwargs)
