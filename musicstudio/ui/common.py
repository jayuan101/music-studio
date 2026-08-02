"""Small shared UI pieces used across the panels."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from . import theme
from ..core import crash_log


def confirm_delete(parent: QWidget | None, paths: list[Path]) -> bool:
    """The one delete confirmation, shared by every delete entry point --
    the duplicates dialog and the library view's own Delete action both
    call this instead of building their own near-identical dialog."""
    crash_log.debug(f"confirm_delete: called with {len(paths)} path(s)")
    names = "\n".join(Path(p).name for p in paths[:10])
    if len(paths) > 10:
        names += f"\n… and {len(paths) - 10} more"
    reply = QMessageBox.question(
        parent,
        "Delete from library",
        f"Remove {len(paths)} file(s) from the library and send them to the "
        "Recycle Bin?\n\nYou can restore them from the Recycle Bin if you "
        "change your mind.\n\n" + names,
        QMessageBox.Yes | QMessageBox.Cancel,
        QMessageBox.Cancel,
    )
    crash_log.debug(f"confirm_delete: dialog closed, reply={reply!r}")
    return reply == QMessageBox.Yes


def heading(text: str, subtitle: str = "") -> QWidget:
    """A page title with optional explanatory subtitle."""
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(2)

    title = QLabel(text)
    title.setObjectName("Heading")
    layout.addWidget(title)

    if subtitle:
        sub = QLabel(subtitle)
        sub.setObjectName("SubHeading")
        sub.setWordWrap(True)
        layout.addWidget(sub)
    return container


def section_label(text: str) -> QLabel:
    label = QLabel(text.upper())
    label.setObjectName("SectionLabel")
    return label


def card(*children: QWidget, spacing: int = 12, margins: int = 16) -> QFrame:
    """A bordered panel grouping related controls."""
    frame = QFrame()
    frame.setObjectName("Card")
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(margins, margins, margins, margins)
    layout.setSpacing(spacing)
    for child in children:
        layout.addWidget(child)
    return frame


def divider() -> QFrame:
    line = QFrame()
    line.setObjectName("Divider")
    line.setFrameShape(QFrame.HLine)
    line.setFixedHeight(1)
    return line


def row(*widgets: QWidget, spacing: int = 8, stretch_last: bool = False) -> QWidget:
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(spacing)
    for index, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if (stretch_last and index == len(widgets) - 1) else 0)
    return container


def spacer() -> QWidget:
    widget = QWidget()
    widget.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
    return widget


class QualityBadge(QLabel):
    """Shows whether audio is lossless or lossy, colour-coded.

    Quality is the whole point of the app, so it gets a persistent visual
    marker rather than being buried in a details pane.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.clear_state()

    def clear_state(self) -> None:
        self.setText("—")
        self._style(theme.TEXT_FAINT)

    def set_lossless(self, lossless: bool, detail: str = "") -> None:
        if lossless:
            self.setText(f"LOSSLESS{'  ·  ' + detail if detail else ''}")
            self._style(theme.LOSSLESS)
        else:
            self.setText(f"LOSSY{'  ·  ' + detail if detail else ''}")
            self._style(theme.LOSSY)

    def _style(self, colour: str) -> None:
        self.setStyleSheet(
            f"color: {colour}; border: 1px solid {colour}; border-radius: 4px;"
            f"padding: 3px 9px; font-size: 11px; font-weight: 600;"
        )


class NoteList(QWidget):
    """Displays the quality notes a conversion produced."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(6)

    def clear(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def set_notes(self, notes) -> None:
        self.clear()
        for note in notes:
            severity = getattr(note, "severity", None)
            is_warning = getattr(severity, "value", str(severity)) == "warning"
            label = QLabel(f"{'⚠' if is_warning else 'ℹ'}  {note.title} — {note.detail}")
            label.setObjectName("Warning" if is_warning else "Hint")
            label.setWordWrap(True)
            self._layout.addWidget(label)
        self.setVisible(self._layout.count() > 0)


def format_duration(seconds: float) -> str:
    if not seconds:
        return "0:00"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def format_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.0f} {unit}" if unit == "B" else f"{num_bytes:.1f} {unit}"
        num_bytes /= 1024
    return f"{num_bytes:.1f} TB"
