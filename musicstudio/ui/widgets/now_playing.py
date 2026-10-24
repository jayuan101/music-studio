"""The persistent playback bar: a queue on top of the shared Player engine.

Visible on every page, the way a real music player's transport is -- you can
keep browsing the Library, editing tags, or converting files while whatever
you queued keeps playing. This is a second, independent playback engine from
the one inside the Editor (which renders and previews *edits*, not a casual
listen), so :class:`MainWindow` pauses whichever one is not the one you just
pressed play on, rather than letting two files play over each other.
"""

from __future__ import annotations

import random
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QVBoxLayout,
    QWidget,
)

from ...config import get_settings
from ...core import lyrics as lyrics_module
from ...core import replaygain
from .. import theme
from ..common import safe_pixmap
from .async_read import AsyncTagReader
from .lyrics_fetch import LyricsFetcher
from .player import Player

THUMBNAIL_SIZE = 42


class _ElidedLabel(QLabel):
    """A one-line label that shortens long text with "..." instead of demanding
    the width -- a long lyric line must not widen the whole window.

    Use :meth:`set_full_text`; the shortened text is what QLabel itself holds,
    so the stylesheet's colour and font apply as usual.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setAlignment(Qt.AlignCenter)

    def minimumSizeHint(self):  # noqa: N802 (Qt API)
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self._elide()

    @property
    def full_text(self) -> str:
        return self._full_text

    def _elide(self) -> None:
        self.setText(
            self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, max(0, self.width() - 4))
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 (Qt API)
        super().resizeEvent(event)
        self._elide()


class RepeatMode(str, Enum):
    OFF = "off"
    ALL = "all"
    ONE = "one"


class PlaybackQueue(QObject):
    """An ordered list of tracks plus where playback is in it.

    Shuffle reorders everything *after* the current track rather than the
    whole list, so turning it on mid-album does not replay something you
    just heard; turning it back off restores the original order without
    losing your place.
    """

    #: The track that should be playing now, or None when the queue is empty
    #: or has run off the end.
    current_changed = Signal(object)
    queue_changed = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tracks: list[Path] = []
        self._order: list[int] = []      # indices into _tracks, in play order
        self._position: int = -1         # index into _order
        self._shuffle = False
        self.repeat = RepeatMode.OFF

    # -- building the queue ----------------------------------------------
    def set_tracks(self, tracks: list[Path], start_index: int = 0) -> None:
        """Replace the queue, starting playback at ``start_index``.

        The clicked track always plays first regardless of shuffle -- only
        the rest of the list is randomised -- so "play this song" never
        feels like it ignored what you clicked.
        """
        self._tracks = list(tracks)
        indices = list(range(len(self._tracks)))
        if not indices:
            self._order, self._position = [], -1
        elif self._shuffle:
            chosen = indices.pop(start_index) if 0 <= start_index < len(indices) else indices.pop(0)
            random.shuffle(indices)
            indices.insert(0, chosen)
            self._order, self._position = indices, 0
        else:
            self._order = indices
            self._position = start_index if 0 <= start_index < len(indices) else 0
        self.current_changed.emit(self.current)
        self.queue_changed.emit()

    def insert_next(self, tracks: list[Path]) -> None:
        """Play ``tracks`` right after the current one, keeping the rest.

        They go into ``_tracks`` just after the current track's own slot and
        into ``_order`` just after the current position, so they follow it
        with shuffle on *and* stay beside it if shuffle is turned off later
        (which rebuilds ``_order`` from ``_tracks``). Only queue_changed is
        emitted: current_changed would reload and restart the song.
        """
        tracks = list(tracks)
        if not tracks:
            return
        if self.is_empty:
            self.set_tracks(tracks, 0)
            return
        count = len(tracks)
        at = self._order[self._position] + 1
        self._tracks[at:at] = tracks
        self._order = [i + count if i >= at else i for i in self._order]
        self._order[self._position + 1:self._position + 1] = range(at, at + count)
        self.queue_changed.emit()

    def append(self, tracks: list[Path]) -> None:
        """Add ``tracks`` to the end of the queue."""
        tracks = list(tracks)
        if not tracks:
            return
        if self.is_empty:
            self.set_tracks(tracks, 0)
            return
        first = len(self._tracks)
        self._tracks.extend(tracks)
        self._order.extend(range(first, first + len(tracks)))
        self.queue_changed.emit()

    def clear(self) -> None:
        self._tracks, self._order, self._position = [], [], -1
        self.current_changed.emit(None)
        self.queue_changed.emit()

    def set_shuffle(self, on: bool) -> None:
        if on == self._shuffle:
            return
        self._shuffle = on
        current_track = self._order[self._position] if 0 <= self._position < len(self._order) else None
        indices = list(range(len(self._tracks)))
        if on:
            if current_track is not None and current_track in indices:
                indices.remove(current_track)
                random.shuffle(indices)
                indices.insert(0, current_track)
            else:
                random.shuffle(indices)
        # else: falling back to _tracks' own order, which is the un-shuffled state.
        self._order = indices
        self._position = self._order.index(current_track) if current_track in self._order else 0
        self.queue_changed.emit()

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @property
    def is_empty(self) -> bool:
        return not self._order

    @property
    def current(self) -> Path | None:
        if 0 <= self._position < len(self._order):
            return self._tracks[self._order[self._position]]
        return None

    @property
    def upcoming_count(self) -> int:
        """How many tracks play after this one, accounting for repeat."""
        if not self._order:
            return 0
        if self.repeat != RepeatMode.OFF:
            return len(self._order) - 1
        return len(self._order) - 1 - self._position

    # -- moving through it ------------------------------------------------
    def next(self) -> Path | None:
        if not self._order:
            return None
        if self.repeat == RepeatMode.ONE:
            pass  # stay put -- caller re-plays the same track
        elif self._position < len(self._order) - 1:
            self._position += 1
        elif self.repeat == RepeatMode.ALL:
            self._position = 0
        else:
            return None  # ran off the end with repeat off
        self.current_changed.emit(self.current)
        return self.current

    def previous(self) -> Path | None:
        if not self._order:
            return None
        if self._position > 0:
            self._position -= 1
        elif self.repeat == RepeatMode.ALL:
            self._position = len(self._order) - 1
        # else: already at the first track -- restart it rather than do nothing.
        self.current_changed.emit(self.current)
        return self.current


class NowPlayingBar(QWidget):
    """Track info, transport and queue controls, fixed at the window's bottom."""

    #: Mirrors the underlying Player's, so MainWindow can pause the Editor's
    #: own player when this one starts (and vice versa) without either page
    #: needing to know the other exists.
    playing_changed = Signal(bool)

    def __init__(self, library=None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        #: Where ReplayGain values come from; None plays everything unadjusted.
        self.library = library
        #: The queue ran out and playback stopped; see _resume_if_finished.
        self._finished = False
        #: Synced lyrics for the current song, and which line is showing.
        self._lyrics: lyrics_module.SyncedLyrics | None = None
        self._lyric_index = -2
        self._lyrics_token = 0
        self._current_info = None
        self._current_path: Path | None = None
        self._lyrics_fetcher = LyricsFetcher(self)
        self._lyrics_fetcher.ready.connect(self._on_lyrics_ready)
        self.queue = PlaybackQueue(self)
        self.player = Player()
        # Track info is read off the GUI thread; see AsyncTagReader. No debounce
        # here -- unlike arrow-keying through a table, a track change is a
        # deliberate act and its details should appear as soon as they can.
        self._tag_reader = AsyncTagReader(self, delay_ms=0)
        self._tag_reader.ready.connect(self._on_track_info_ready)
        self._build()

        self.queue.current_changed.connect(self._on_current_changed)
        self.queue.queue_changed.connect(self._update_controls)
        self.player.play_requested.connect(self.player.play)
        self.player.playing_changed.connect(self.playing_changed)
        self.player.finished.connect(self._on_finished)
        self.player.position_changed.connect(self._on_position)

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        self.setObjectName("NowPlayingBar")
        self.setStyleSheet(
            f"#NowPlayingBar {{ background: {theme.BG_DEEP}; border-top: 1px solid {theme.BORDER}; }}"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 8, 14, 10)
        outer.setSpacing(4)

        # One lyric line above the transport, spanning the full width. Hidden
        # (and so taking no room) unless the current song has synced lyrics.
        self.lyric_label = _ElidedLabel()
        self.lyric_label.setStyleSheet(f"color: {theme.TEXT}; font-size: 13px;")
        self.lyric_label.setVisible(False)
        outer.addWidget(self.lyric_label)

        row = QWidget()
        outer.addWidget(row)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)

        self.art_label = QLabel()
        self.art_label.setFixedSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        self.art_label.setAlignment(Qt.AlignCenter)
        self._clear_art()

        info = QWidget()
        info_layout = QVBoxLayout(info)
        info_layout.setContentsMargins(0, 0, 0, 0)
        info_layout.setSpacing(1)
        self.title_label = QLabel("Nothing playing")
        self.title_label.setStyleSheet(f"color: {theme.TEXT}; font-weight: 600;")
        self.artist_label = QLabel("")
        self.artist_label.setObjectName("Hint")
        info_layout.addWidget(self.title_label)
        info_layout.addWidget(self.artist_label)
        info.setMinimumWidth(160)
        info.setMaximumWidth(220)

        style = self.style()
        self.prev_button = QPushButton()
        self.prev_button.setIcon(style.standardIcon(QStyle.SP_MediaSkipBackward))
        self.prev_button.setFixedWidth(36)
        self.prev_button.setToolTip("Previous")
        self.prev_button.clicked.connect(self.queue.previous)

        self.next_button = QPushButton()
        self.next_button.setIcon(style.standardIcon(QStyle.SP_MediaSkipForward))
        self.next_button.setFixedWidth(36)
        self.next_button.setToolTip("Next")
        self.next_button.clicked.connect(self.queue.next)

        self.shuffle_button = QPushButton("Shuffle")
        self.shuffle_button.setCheckable(True)
        self.shuffle_button.setToolTip("Shuffle the rest of the queue")
        self.shuffle_button.toggled.connect(self.queue.set_shuffle)

        self.repeat_button = QPushButton("Repeat: Off")
        self.repeat_button.setToolTip("Cycle repeat: off → queue → one track")
        self.repeat_button.clicked.connect(self._cycle_repeat)

        self.queue_label = QLabel("")
        self.queue_label.setObjectName("Hint")
        self.queue_label.setMinimumWidth(90)

        layout.addWidget(self.art_label)
        layout.addWidget(info)
        layout.addWidget(self.prev_button)
        layout.addWidget(self.player, 1)
        layout.addWidget(self.next_button)
        layout.addWidget(self.shuffle_button)
        layout.addWidget(self.repeat_button)
        layout.addWidget(self.queue_label)

        self._update_controls()

    # -- playing ------------------------------------------------------------
    def play_queue(self, tracks: list[Path], start_index: int = 0) -> None:
        """Queue ``tracks`` (e.g. everything currently visible in the
        Library) and start playing from ``start_index``."""
        self._finished = False
        self.queue.set_tracks(tracks, start_index)

    def queue_next(self, tracks: list[Path]) -> None:
        """Play ``tracks`` after the current song."""
        self.queue.insert_next(tracks)
        self._resume_if_finished()

    def queue_append(self, tracks: list[Path]) -> None:
        """Add ``tracks`` to the end of the queue."""
        self.queue.append(tracks)
        self._resume_if_finished()

    def _resume_if_finished(self) -> None:
        """If the queue had already played out, start on what was just added.

        The last song ending leaves the queue parked on it with the player
        stopped, so nothing would otherwise pick up a song queued afterwards.
        """
        if self._finished:
            self._finished = False
            self.queue.next()

    def _on_current_changed(self, path: object) -> None:
        track = Path(path) if path else None
        self._set_lyrics(None)
        self._lyrics_fetcher.cancel()
        self._current_info = None
        if track is None:
            self.player.clear()
            self.title_label.setText("Nothing playing")
            self.artist_label.setText("")
            self._clear_art()
            self._update_controls()
            return

        self.player.load(track)
        # Before play(), so a loud track never starts loud for a moment.
        self.apply_replaygain()
        self.player.play()

        # Playback starts immediately; the title, artist and cover art follow
        # when the read lands. Reading them inline here used to stall the whole
        # window on every track change -- including the automatic advance at the
        # end of a song, so simply listening through an album stuttered the UI.
        result = self._tag_reader.request(track)
        if result is not False:
            self._apply_track_info(track, result)
        else:
            self.title_label.setText(track.stem)
            self.artist_label.setText("")
            self._clear_art()
        self._update_controls()

    def apply_replaygain(self) -> None:
        """Set the player's gain for the current track from the library.

        A database lookup, not a file read, so it is safe on the GUI thread.
        Also called when the ReplayGain preference changes mid-song.
        """
        track = self.queue.current
        gain = 0.0
        if track is not None and self.library is not None:
            settings = get_settings()
            gain = replaygain.playback_gain_db(
                self.library.get(Path(track)),
                mode=settings.replaygain_mode,
                preamp_db=settings.replaygain_preamp_db,
            )
        self.player.set_gain_db(gain)

    # -- lyrics -------------------------------------------------------------
    def _set_lyrics(self, synced: lyrics_module.SyncedLyrics | None) -> None:
        self._lyrics = synced
        self._lyric_index = -2
        self.lyric_label.set_full_text("")
        self.lyric_label.setVisible(synced is not None)

    def _load_lyrics(self, path: Path, info) -> None:
        """Pick lyrics for the current song: embedded synced lyrics first,
        then (only if the user opted in) LRCLIB via the cache. Never writes
        to the file -- Windows has it locked while it plays."""
        self._current_info, self._current_path = info, path
        if info is None:
            return
        embedded = lyrics_module.SyncedLyrics.from_text(info.lyrics or "")
        if embedded is not None:
            self._set_lyrics(embedded)
            return
        artist = info.artist or info.albumartist
        if not (get_settings().lyrics_online_enabled and artist and info.title):
            return
        row = self.library.get(path) if self.library is not None else None
        duration = row.duration if row is not None else 0.0
        self._lyrics_token = self._lyrics_fetcher.request(artist, info.title, info.album, duration)

    def refresh_lyrics(self) -> None:
        """Re-evaluate lyrics for the current song (the preference changed)."""
        if self._lyrics is None and self._current_info is not None:
            self._load_lyrics(self._current_path, self._current_info)
        elif self._lyrics is not None and not get_settings().lyrics_online_enabled:
            # Only lyrics that came from the network are switched off with it.
            if not lyrics_module.is_synced(getattr(self._current_info, "lyrics", "") or ""):
                self._set_lyrics(None)

    def _on_lyrics_ready(self, token: int, result) -> None:
        if token != self._lyrics_token or result is None or not result.synced:
            return
        synced = lyrics_module.SyncedLyrics.from_text(result.synced)
        if synced is not None:
            self._set_lyrics(synced)

    def _on_position(self, seconds: float) -> None:
        if self._lyrics is None:
            return
        index = self._lyrics.index_at(seconds)
        if index != self._lyric_index:
            self._lyric_index = index
            self.lyric_label.set_full_text(self._lyrics.lines[index][1] if index >= 0 else "")

    def _apply_track_info(self, path: Path, info) -> None:
        self._load_lyrics(path, info)
        if info is None:
            self.title_label.setText(path.stem)
            self.artist_label.setText("")
            self._clear_art()
            return
        self.title_label.setText(info.display_title)
        self.artist_label.setText(info.display_artist)
        if info.has_artwork():
            pixmap = safe_pixmap(info.artwork.data)
            if not pixmap.isNull():
                self.art_label.setPixmap(
                    pixmap.scaled(
                        THUMBNAIL_SIZE, THUMBNAIL_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                )
            else:
                self._clear_art()
        else:
            self._clear_art()

    def _on_track_info_ready(self, path: Path, info) -> None:
        # Ignore a read that landed after the user skipped on again.
        if self.queue.current and Path(self.queue.current) == Path(path):
            self._apply_track_info(Path(path), info)

    def _on_finished(self) -> None:
        if self.queue.repeat == RepeatMode.ONE:
            # Same track again: current_changed does not fire for an
            # unchanged position, so re-trigger the load explicitly.
            self._on_current_changed(self.queue.current)
            return
        if self.queue.next() is None:
            self._finished = True
            self.player.stop()

    def _cycle_repeat(self) -> None:
        order = [RepeatMode.OFF, RepeatMode.ALL, RepeatMode.ONE]
        self.queue.repeat = order[(order.index(self.queue.repeat) + 1) % len(order)]
        labels = {RepeatMode.OFF: "Repeat: Off", RepeatMode.ALL: "Repeat: Queue", RepeatMode.ONE: "Repeat: One"}
        self.repeat_button.setText(labels[self.queue.repeat])
        self.repeat_button.setChecked(self.queue.repeat != RepeatMode.OFF)

    def _clear_art(self) -> None:
        self.art_label.setPixmap(QPixmap())
        self.art_label.setStyleSheet(
            f"background: {theme.BG_RAISED}; border: 1px solid {theme.BORDER}; border-radius: 6px;"
        )

    def _update_controls(self) -> None:
        has_queue = not self.queue.is_empty
        self.prev_button.setEnabled(has_queue)
        self.next_button.setEnabled(has_queue)
        self.shuffle_button.setEnabled(has_queue)
        self.repeat_button.setEnabled(has_queue)
        remaining = self.queue.upcoming_count
        self.queue_label.setText(f"{remaining} more in queue" if remaining else "")
