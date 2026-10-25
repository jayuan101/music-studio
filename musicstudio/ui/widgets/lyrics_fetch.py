"""Look a song's lyrics up off the GUI thread.

Same shape as the Library's preview reader: a one-thread pool so skipping
through songs queues at most one lookup behind the current one, a token so a
result for a song the user has already skipped is dropped, and only plain
data crossing back to the main thread.
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from ...core import lyrics as lyrics_module


class _Signals(QObject):
    #: (token, LyricsResult | None) -- None for "no lyrics" or a failed lookup.
    ready = Signal(int, object)


class _Task(QRunnable):
    def __init__(self, token: int, query: tuple, signals: _Signals) -> None:
        super().__init__()
        self._token = token
        self._query = query
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            result = lyrics_module.lookup_cached(*self._query)
        except BaseException:  # noqa: BLE001 -- must never escape a QRunnable override
            # Offline or a service error: show nothing now, and (because
            # lookup_cached caches nothing on failure) try again next time.
            result = None
        try:
            self._signals.ready.emit(self._token, result)
        except RuntimeError:
            pass  # the bar went away while this was in flight


class LyricsFetcher(QObject):
    """``request(artist, title, album, duration)`` -> ``ready(token, result)``."""

    ready = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._signals = _Signals(self)
        self._signals.ready.connect(self._on_ready)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._token = 0

    def request(self, artist: str, title: str, album: str, duration: float) -> int:
        """Start a lookup; returns the token its result will carry."""
        self.cancel()
        self._pool.start(_Task(self._token, (artist, title, album, duration), self._signals))
        return self._token

    def cancel(self) -> None:
        """Drop queued lookups and discard the result of any running lookup."""
        self._token += 1
        self._pool.clear()

    @Slot(int, object)
    def _on_ready(self, token: int, result) -> None:
        if token == self._token:
            self.ready.emit(token, result)
