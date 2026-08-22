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
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStyle, QVBoxLayout, QWidget

from .. import theme
from ..common import safe_pixmap
from .async_read import AsyncTagReader
from .player import Player

THUMBNAIL_SIZE = 42


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

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
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

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        self.setObjectName("NowPlayingBar")
        self.setStyleSheet(
            f"#NowPlayingBar {{ background: {theme.BG_DEEP}; border-top: 1px solid {theme.BORDER}; }}"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
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
        self.queue.set_tracks(tracks, start_index)

    def _on_current_changed(self, path: object) -> None:
        track = Path(path) if path else None
        if track is None:
            self.player.clear()
            self.title_label.setText("Nothing playing")
            self.artist_label.setText("")
            self._clear_art()
            self._update_controls()
            return

        self.player.load(track)
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

    def _apply_track_info(self, path: Path, info) -> None:
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
