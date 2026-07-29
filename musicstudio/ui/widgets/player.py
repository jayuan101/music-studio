"""Audio playback and the transport bar.

Two things matter here beyond "make sound come out":

* **Play just the selection.** Qt has no native concept of a play range, so the
  end point is enforced by watching position updates and stopping at it.
* **Preview the edits, not the file.** Hearing a +18 dB boost means hearing it
  *applied*. The transport can play a short rendered excerpt with the current
  effect stack baked in, so what you hear is what you would export.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Qt, Signal
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QStyle,
    QWidget,
)

from .. import theme
from ..common import format_duration


class Player(QWidget):
    """Transport bar: play/pause, stop, scrubber, time readout, volume."""

    #: Playback position in seconds, emitted continuously while playing.
    position_changed = Signal(float)
    #: True when playback starts, False when it stops or pauses.
    playing_changed = Signal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._player = QMediaPlayer(self)
        self._audio = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._audio.setVolume(0.8)

        #: Stop automatically at this position (seconds) when playing a region.
        self._stop_at: float | None = None
        #: True while we are moving the slider ourselves, so the valueChanged
        #: handler does not treat it as the user scrubbing and seek back.
        self._updating_slider = False
        #: Offset applied to reported positions, so a rendered excerpt still
        #: reports its true position on the original timeline.
        self._offset = 0.0

        self._build()

        self._player.positionChanged.connect(self._on_position)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)
        self._player.errorOccurred.connect(self._on_error)

    # -- construction ---------------------------------------------------
    def _build(self) -> None:
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Qt's built-in media icons rather than unicode glyphs: the box and
        # speaker characters fall back to whatever the system font has, which
        # renders as a stray vertical bar on a lot of setups.
        style = self.style()
        self._play_icon = style.standardIcon(QStyle.SP_MediaPlay)
        self._pause_icon = style.standardIcon(QStyle.SP_MediaPause)

        self.play_button = QPushButton()
        self.play_button.setIcon(self._play_icon)
        self.play_button.setFixedWidth(40)
        self.play_button.setToolTip("Play / pause  (Space)")
        self.play_button.clicked.connect(self.toggle)

        self.stop_button = QPushButton()
        self.stop_button.setIcon(style.standardIcon(QStyle.SP_MediaStop))
        self.stop_button.setFixedWidth(36)
        self.stop_button.setToolTip("Stop")
        self.stop_button.clicked.connect(self.stop)

        self.scrubber = QSlider(Qt.Horizontal)
        self.scrubber.setRange(0, 0)
        self.scrubber.sliderMoved.connect(self._on_scrubbed)

        self.time_label = QLabel("0:00 / 0:00")
        self.time_label.setObjectName("Hint")
        self.time_label.setMinimumWidth(96)
        self.time_label.setAlignment(Qt.AlignCenter)

        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(80)
        self.volume_slider.setFixedWidth(90)
        self.volume_slider.setToolTip("Playback volume")
        self.volume_slider.valueChanged.connect(
            lambda value: self._audio.setVolume(value / 100.0)
        )

        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")

        layout.addWidget(self.play_button)
        layout.addWidget(self.stop_button)
        layout.addWidget(self.scrubber, 1)
        layout.addWidget(self.time_label)
        volume_icon = QLabel()
        volume_icon.setPixmap(
            style.standardIcon(QStyle.SP_MediaVolume).pixmap(16, 16)
        )
        layout.addWidget(volume_icon)
        layout.addWidget(self.volume_slider)

        self.setEnabled(False)

    # -- loading --------------------------------------------------------
    def load(self, path: str | Path, *, offset: float = 0.0) -> None:
        """Load a file, ready to play.

        ``offset`` shifts reported positions, so a rendered excerpt starting at
        30 s into the original still reports 30 s rather than 0.
        """
        self._stop_at = None
        self._offset = offset
        self._player.setSource(QUrl.fromLocalFile(str(Path(path).resolve())))
        self.setEnabled(True)

    def clear(self) -> None:
        self._player.stop()
        self._player.setSource(QUrl())
        self.setEnabled(False)
        self.scrubber.setRange(0, 0)
        self.time_label.setText("0:00 / 0:00")

    # -- transport ------------------------------------------------------
    def play(self, *, start: float | None = None, stop: float | None = None) -> None:
        """Play, optionally only the span from ``start`` to ``stop`` seconds."""
        if start is not None:
            self._player.setPosition(int(max(0.0, start - self._offset) * 1000))
        self._stop_at = stop
        self._player.play()

    def pause(self) -> None:
        self._player.pause()

    def stop(self) -> None:
        self._stop_at = None
        self._player.stop()

    def toggle(self) -> None:
        if self.is_playing:
            self.pause()
        else:
            self._player.play()

    def seek(self, seconds: float) -> None:
        """Jump to a position. Cancels any region limit.

        Seeking somewhere is an instruction to play from *there*; leaving a
        stale region end armed would stop playback the moment you land past it.
        """
        self._stop_at = None
        self._player.setPosition(int(max(0.0, seconds - self._offset) * 1000))

    @property
    def is_playing(self) -> bool:
        return self._player.playbackState() == QMediaPlayer.PlayingState

    @property
    def position(self) -> float:
        return self._player.position() / 1000.0 + self._offset

    @property
    def duration(self) -> float:
        return self._player.duration() / 1000.0

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    # -- signals --------------------------------------------------------
    def _on_position(self, milliseconds: int) -> None:
        seconds = milliseconds / 1000.0 + self._offset

        # Qt has no play-range concept, so enforce the region end here.
        if self._stop_at is not None and seconds >= self._stop_at:
            # Capture the end before stopping: stop() clears _stop_at, and
            # emitting the cleared value pushes None into a Signal(float).
            end = self._stop_at
            self.stop()
            self.position_changed.emit(end)
            return

        if not self.scrubber.isSliderDown():
            self._updating_slider = True
            self.scrubber.setValue(milliseconds)
            self._updating_slider = False

        self._update_time_label(milliseconds)
        self.position_changed.emit(seconds)

    def _on_duration(self, milliseconds: int) -> None:
        self.scrubber.setRange(0, max(0, milliseconds))
        self._update_time_label(self._player.position())

    def _on_state(self, state) -> None:
        playing = state == QMediaPlayer.PlayingState
        self.play_button.setIcon(self._pause_icon if playing else self._play_icon)
        self.playing_changed.emit(playing)

    def _on_error(self, error, message: str = "") -> None:
        if error == QMediaPlayer.NoError:
            return
        # A missing codec is the usual cause on a fresh Windows install, and a
        # silent failure would look like the app is simply broken.
        self.status_label.setText(
            message or "Could not play this file — the system may lack a codec for it."
        )
        self.status_label.setStyleSheet(f"color: {theme.WARNING};")

    def _on_scrubbed(self, milliseconds: int) -> None:
        if self._updating_slider:
            return
        self._stop_at = None  # scrubbing cancels a region-limited playback
        self._player.setPosition(milliseconds)

    def _update_time_label(self, milliseconds: int) -> None:
        position = milliseconds / 1000.0 + self._offset
        total = self._player.duration() / 1000.0 + self._offset
        self.time_label.setText(
            f"{format_duration(position)} / {format_duration(total)}"
        )


class PreviewController:
    """Renders short excerpts with the current edits applied, then plays them.

    Rendering is debounced: dragging the gain slider would otherwise queue a
    render per pixel of travel.
    """

    #: How much audio to render for a preview.
    PREVIEW_SECONDS = 20.0
    #: Wait this long after the last change before rendering.
    DEBOUNCE_MS = 400

    def __init__(self, player: Player, job_queue, parent: QWidget) -> None:
        self.player = player
        self.jobs = job_queue
        self._parent = parent
        self._source: Path | None = None
        self._spec_provider = None
        self._start = 0.0
        self._pending_job = None

        self._timer = QTimer(parent)
        self._timer.setSingleShot(True)
        self._timer.setInterval(self.DEBOUNCE_MS)
        self._timer.timeout.connect(self._render)

    def configure(self, source: Path | None, spec_provider) -> None:
        """Set the file to preview and a callable returning the current EditSpec."""
        self._source = source
        self._spec_provider = spec_provider

    def request(self, start: float = 0.0) -> None:
        """Ask for a preview from ``start``, coalescing rapid requests."""
        self._start = start
        self._timer.start()

    def cancel(self) -> None:
        self._timer.stop()
        if self._pending_job is not None:
            self._pending_job.cancel()
            self._pending_job = None

    def _render(self) -> None:
        if self._source is None or self._spec_provider is None:
            return
        spec = self._spec_provider()
        source, start = self._source, self._start

        self.player.set_status("Rendering preview…")

        def work(context, path, edit_spec, begin):
            from ...core import convert as convert_module

            return convert_module.render_preview(
                path, edit_spec, start=begin, seconds=PreviewController.PREVIEW_SECONDS,
                context=context,
            )

        self._pending_job = self.jobs.submit_func(
            "Rendering preview", work, source, spec, start, category="preview"
        )
        self._pending_job.signals.finished.connect(self._on_rendered)

    def _on_rendered(self, _job_id: str, state: str, payload) -> None:
        self._pending_job = None
        if state != "succeeded":
            if state == "failed":
                self.player.set_status(f"Preview failed: {payload}")
            return
        self.player.set_status("Previewing with effects applied")
        self.player.load(payload, offset=self._start)
        self.player.play()
