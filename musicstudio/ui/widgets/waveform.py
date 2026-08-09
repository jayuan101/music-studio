"""Waveform display with click-and-drag region selection.

Peaks are computed by decoding to low-rate mono PCM through ffmpeg and reducing
it to min/max pairs per pixel column. That keeps the dependency list short (no
numpy, no soundfile) and is fast enough to feel instant on normal tracks.
"""

from __future__ import annotations

import array
import struct
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

from ...core import ffmpeg
from .. import theme

#: Decoding rate for analysis only. 8 kHz is far more resolution than a few
#: hundred pixels of waveform can show, and decodes very quickly.
PEAK_SAMPLE_RATE = 8000


@dataclass
class WaveformData:
    """Min/max peak pairs, normalised to -1..1."""

    minimums: array.array
    maximums: array.array
    duration: float

    def __len__(self) -> int:
        return len(self.minimums)

    @property
    def peak(self) -> float:
        if not len(self.maximums):
            return 0.0
        return max(max(self.maximums, default=0.0), abs(min(self.minimums, default=0.0)))


def compute_peaks(path: str | Path, buckets: int = 2000, *, should_cancel=None) -> WaveformData:
    """Decode ``path`` and reduce it to ``buckets`` min/max pairs.

    Raises :class:`~musicstudio.core.ffmpeg.FFmpegError` if decoding fails.
    """
    path = Path(path)
    command = [
        str(ffmpeg.ffmpeg_path()),
        "-hide_banner", "-loglevel", "error",
        "-i", str(path),
        "-ac", "1",
        "-ar", str(PEAK_SAMPLE_RATE),
        "-map", "0:a:0",
        "-f", "s16le",
        "-",
    ]
    import subprocess

    proc = subprocess.run(
        command,
        capture_output=True,
        **ffmpeg._no_window_kwargs(),  # noqa: SLF001 -- shared console-hiding logic
    )
    if proc.returncode != 0:
        raise ffmpeg.FFmpegError(
            "Could not decode audio for the waveform",
            command=command,
            stderr=proc.stderr.decode("utf-8", "replace"),
            returncode=proc.returncode,
        )

    raw = proc.stdout
    sample_count = len(raw) // 2
    if sample_count == 0:
        return WaveformData(array.array("f"), array.array("f"), 0.0)

    samples = array.array("h")
    samples.frombytes(raw[: sample_count * 2])
    if struct.pack("h", 1) != struct.pack("<h", 1):  # normalise on big-endian hosts
        samples.byteswap()

    buckets = max(1, min(buckets, sample_count))
    per_bucket = sample_count / buckets
    minimums = array.array("f", [0.0]) * buckets
    maximums = array.array("f", [0.0]) * buckets

    for index in range(buckets):
        if should_cancel is not None and should_cancel():
            break
        start = int(index * per_bucket)
        end = max(start + 1, int((index + 1) * per_bucket))
        chunk = samples[start:end]
        if not chunk:
            continue
        minimums[index] = min(chunk) / 32768.0
        maximums[index] = max(chunk) / 32767.0

    return WaveformData(minimums, maximums, sample_count / PEAK_SAMPLE_RATE)


class WaveformView(QWidget):
    """Draws a waveform and lets the user drag out a time region."""

    #: Emitted with (start_seconds, end_seconds) whenever the selection changes.
    selection_changed = Signal(float, float)
    #: Emitted when the user clicks a position without dragging.
    position_clicked = Signal(float)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._data: WaveformData | None = None
        self._selection: tuple[float, float] | None = None
        self._dragging = False
        self._drag_origin = 0.0
        self._cursor_time: float | None = None
        #: Current playback position, drawn as a bright vertical line.
        self._playhead: float | None = None
        #: Regions marked for removal, drawn struck through.
        self._cuts: list[tuple[float, float]] = []
        self._message = "No file loaded"
        #: Cached waveform pen, rebuilt only when the widget's height
        #: changes -- see _waveform_pen().
        self._waveform_pen_cache: QPen | None = None
        self._waveform_pen_cache_height: int | None = None

        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setCursor(Qt.IBeamCursor)

    # -- state ----------------------------------------------------------
    def set_data(self, data: WaveformData | None, message: str = "") -> None:
        self._data = data
        self._selection = None
        self._cuts = []
        self._message = message or ("No file loaded" if data is None else "")
        self.update()

    def set_message(self, message: str) -> None:
        self._message = message
        self.update()

    def set_cuts(self, cuts: list[tuple[float, float]]) -> None:
        self._cuts = list(cuts)
        self.update()

    def set_playhead(self, seconds: float | None) -> None:
        """Move the playback marker. Called on every position update."""
        self._playhead = seconds
        # Repaint only the two narrow columns the playhead moved between,
        # rather than the whole waveform 20 times a second.
        self.update()

    def selection(self) -> tuple[float, float] | None:
        return self._selection

    def set_selection(self, start: float, end: float) -> None:
        if self._data is None:
            return
        start, end = sorted((max(0.0, start), min(self.duration, end)))
        self._selection = (start, end) if end > start else None
        self.update()
        if self._selection:
            self.selection_changed.emit(*self._selection)

    def clear_selection(self) -> None:
        self._selection = None
        self.update()

    @property
    def duration(self) -> float:
        return self._data.duration if self._data else 0.0

    # -- coordinate mapping ---------------------------------------------
    def _time_at(self, x: float) -> float:
        if self.width() <= 0 or self.duration <= 0:
            return 0.0
        return max(0.0, min(self.duration, (x / self.width()) * self.duration))

    def _x_at(self, time: float) -> float:
        if self.duration <= 0:
            return 0.0
        return (time / self.duration) * self.width()

    # -- interaction ----------------------------------------------------
    def mousePressEvent(self, event) -> None:
        if self._data is None or event.button() != Qt.LeftButton:
            return
        self._dragging = True
        self._drag_origin = self._time_at(event.position().x())
        self._selection = None
        self.update()

    def mouseMoveEvent(self, event) -> None:
        self._cursor_time = self._time_at(event.position().x())
        if self._dragging and self._data is not None:
            current = self._time_at(event.position().x())
            start, end = sorted((self._drag_origin, current))
            self._selection = (start, end)
        self.update()

    def mouseReleaseEvent(self, event) -> None:
        if not self._dragging:
            return
        self._dragging = False
        released = self._time_at(event.position().x())
        # A drag under a few pixels is a click, not a selection.
        if abs(self._x_at(released) - self._x_at(self._drag_origin)) < 3:
            self._selection = None
            self.position_clicked.emit(released)
        elif self._selection:
            self.selection_changed.emit(*self._selection)
        self.update()

    def leaveEvent(self, event) -> None:
        self._cursor_time = None
        self.update()

    # -- painting -------------------------------------------------------
    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, False)
        rect = self.rect()

        painter.fillRect(rect, QColor(theme.BG_DEEP))
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawRect(rect.adjusted(0, 0, -1, -1))

        if self._data is None or not len(self._data):
            painter.setPen(QColor(theme.TEXT_FAINT))
            painter.drawText(rect, Qt.AlignCenter, self._message)
            return

        mid_y = rect.height() / 2.0

        # Centre line.
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawLine(0, int(mid_y), rect.width(), int(mid_y))

        self._paint_selection(painter, rect)
        self._paint_waveform(painter, rect, mid_y)
        self._paint_cuts(painter, rect)
        self._paint_cursor(painter, rect)
        self._paint_playhead(painter, rect)
        self._paint_ruler(painter, rect)
        self._paint_peak_label(painter, rect)

    def _paint_playhead(self, painter: QPainter, rect) -> None:
        if self._playhead is None or self.duration <= 0:
            return
        x = self._x_at(self._playhead)
        painter.setPen(QPen(QColor("#ffffff"), 2))
        painter.drawLine(QPointF(x, 0), QPointF(x, rect.height() - 14))

    def _paint_peak_label(self, painter: QPainter, rect) -> None:
        """State the real peak, since the drawing is scaled to fill the height.

        Without this the display would imply every file is near full scale.
        """
        data = self._data
        if data is None or not len(data):
            return
        import math

        peak = data.peak
        peak_db = 20 * math.log10(peak) if peak > 0 else float("-inf")
        text = f"peak {peak_db:+.1f} dBFS" if peak > 0 else "silent"

        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        painter.setPen(QColor(theme.TEXT_FAINT))
        painter.drawText(rect.adjusted(0, 4, -8, 0), Qt.AlignTop | Qt.AlignRight, text)

    def _paint_waveform(self, painter: QPainter, rect, mid_y: float) -> None:
        data = self._data
        assert data is not None
        buckets = len(data)
        width = rect.width()
        half = mid_y - 14  # leave room for the time ruler along the bottom

        # Scale the drawing to the file's own peak. Without this, a quiet
        # recording renders as a flat line you cannot select regions on --
        # which is exactly the file you most need to see in order to fix it.
        # The floor stops near-silence from being amplified into noise.
        peak = max(data.peak, 0.02)
        scale = 0.94 / peak

        waveform_pen = self._waveform_pen(rect.height())
        # Reused for every selected pixel column too -- constructing a fresh
        # QPen per column (potentially hundreds of times per single repaint,
        # and many repaints per second while dragging a selection) was
        # implicated in a rare access-violation crash: heavy allocation
        # churn during paintEvent colliding with a GC pass inside
        # PySide6/shiboken. Building both pens once removes the churn
        # rather than trying to out-guess the exact race.
        selection_pen = QPen(QColor(theme.WAVEFORM_SEL), 1)
        painter.setPen(waveform_pen)

        selection = self._selection
        for x in range(width):
            index = int(x / max(1, width) * buckets)
            if index >= buckets:
                break
            low = max(-1.0, data.minimums[index] * scale)
            high = min(1.0, data.maximums[index] * scale)
            y_top = mid_y - high * half
            y_bottom = mid_y - low * half

            if selection is not None and selection[0] <= self._time_at(x) <= selection[1]:
                painter.setPen(selection_pen)
            else:
                painter.setPen(waveform_pen)
            painter.drawLine(QPointF(x, y_top), QPointF(x, max(y_bottom, y_top + 1)))

    def _waveform_pen(self, height: int) -> QPen:
        """The gradient waveform pen, rebuilt only when ``height`` changes."""
        if self._waveform_pen_cache is None or self._waveform_pen_cache_height != height:
            gradient = QLinearGradient(0, 0, 0, height)
            gradient.setColorAt(0.0, QColor(theme.WAVEFORM).lighter(120))
            gradient.setColorAt(0.5, QColor(theme.WAVEFORM))
            gradient.setColorAt(1.0, QColor(theme.WAVEFORM).darker(130))
            self._waveform_pen_cache = QPen(QBrush(gradient), 1)
            self._waveform_pen_cache_height = height
        return self._waveform_pen_cache

    def _paint_selection(self, painter: QPainter, rect) -> None:
        if self._selection is None:
            return
        start_x = self._x_at(self._selection[0])
        end_x = self._x_at(self._selection[1])
        band = QRectF(start_x, 0, end_x - start_x, rect.height())
        painter.fillRect(band, QColor(78, 201, 168, 28))
        painter.setPen(QPen(QColor(theme.WAVEFORM_SEL), 1))
        painter.drawLine(QPointF(start_x, 0), QPointF(start_x, rect.height()))
        painter.drawLine(QPointF(end_x, 0), QPointF(end_x, rect.height()))

    def _paint_cuts(self, painter: QPainter, rect) -> None:
        for start, end in self._cuts:
            start_x, end_x = self._x_at(start), self._x_at(end)
            painter.fillRect(
                QRectF(start_x, 0, end_x - start_x, rect.height()), QColor(224, 108, 117, 45)
            )
            painter.setPen(QPen(QColor(theme.DANGER), 1, Qt.DashLine))
            painter.drawRect(QRectF(start_x, 1, end_x - start_x, rect.height() - 2))

    def _paint_cursor(self, painter: QPainter, rect) -> None:
        if self._cursor_time is None:
            return
        x = self._x_at(self._cursor_time)
        painter.setPen(QPen(QColor(theme.TEXT_DIM), 1, Qt.DotLine))
        painter.drawLine(QPointF(x, 0), QPointF(x, rect.height()))

    def _paint_ruler(self, painter: QPainter, rect) -> None:
        """Time labels along the bottom, spaced to a round interval."""
        duration = self.duration
        if duration <= 0:
            return
        for interval in (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 1800):
            if duration / interval <= 12:
                break

        painter.setPen(QColor(theme.TEXT_FAINT))
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)

        marker = 0.0
        while marker <= duration:
            x = self._x_at(marker)
            painter.drawLine(QPointF(x, rect.height() - 12), QPointF(x, rect.height() - 8))
            minutes, seconds = divmod(int(marker), 60)
            painter.drawText(QPointF(x + 3, rect.height() - 2), f"{minutes}:{seconds:02d}")
            marker += interval
