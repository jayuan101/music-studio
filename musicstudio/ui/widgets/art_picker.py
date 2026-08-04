"""Choosing between cover art candidates.

``artwork.find_all_candidates()`` queries every provider and returns them
ranked. When more than one comes back they are rarely identical -- different
pressings, different resolutions -- so the choice belongs to the user rather
than to whichever provider answered first.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ...core.artwork import ArtworkCandidate
from .. import theme
from ..common import safe_pixmap

THUMBNAIL_SIZE = 170


class _CandidateTile(QWidget):
    """One clickable candidate: image, provider, resolution and match score."""

    def __init__(self, candidate: ArtworkCandidate, on_click, parent=None) -> None:
        super().__init__(parent)
        self.candidate = candidate
        self._on_click = on_click
        self._selected = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(6)

        self.image = QLabel()
        self.image.setFixedSize(THUMBNAIL_SIZE, THUMBNAIL_SIZE)
        self.image.setAlignment(Qt.AlignCenter)
        pixmap = safe_pixmap(candidate.data)
        if not pixmap.isNull():
            self.image.setPixmap(
                pixmap.scaled(
                    THUMBNAIL_SIZE, THUMBNAIL_SIZE,
                    Qt.KeepAspectRatio, Qt.SmoothTransformation,
                )
            )
        else:
            self.image.setText("(unreadable image)")
        layout.addWidget(self.image)

        source = QLabel(candidate.source)
        source.setStyleSheet("font-weight: 600;")
        source.setAlignment(Qt.AlignCenter)
        layout.addWidget(source)

        detail = QLabel(f"{candidate.size_label} · {candidate.score:.0%} match")
        detail.setObjectName("Hint")
        detail.setAlignment(Qt.AlignCenter)
        layout.addWidget(detail)

        if candidate.release_title:
            release = QLabel(
                f"{candidate.release_artist} — {candidate.release_title}"
                if candidate.release_artist
                else candidate.release_title
            )
            release.setObjectName("Hint")
            release.setAlignment(Qt.AlignCenter)
            release.setWordWrap(True)
            release.setMaximumWidth(THUMBNAIL_SIZE + 20)
            layout.addWidget(release)

        layout.addStretch(1)
        self.setCursor(Qt.PointingHandCursor)
        self._apply_style()

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._apply_style()

    def _apply_style(self) -> None:
        border = theme.ACCENT if self._selected else theme.BORDER
        self.setStyleSheet(
            f"_CandidateTile {{ background: {theme.BG_RAISED};"
            f" border: {'2px' if self._selected else '1px'} solid {border};"
            f" border-radius: 8px; }}"
        )

    def mousePressEvent(self, event) -> None:
        self._on_click(self)

    def mouseDoubleClickEvent(self, event) -> None:
        self._on_click(self, accept=True)


class ArtPickerDialog(QDialog):
    """Modal chooser over a list of candidates."""

    def __init__(self, candidates: list[ArtworkCandidate], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Choose cover art")
        self.setMinimumWidth(min(720, 40 + len(candidates) * (THUMBNAIL_SIZE + 40)))

        self._candidates = candidates
        self._tiles: list[_CandidateTile] = []
        self.selected: ArtworkCandidate | None = candidates[0] if candidates else None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        intro = QLabel(
            f"{len(candidates)} result(s). Ranked by how well the release matches "
            f"the track's tags — double-click to accept."
        )
        intro.setObjectName("Hint")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        strip = QWidget()
        strip_layout = QHBoxLayout(strip)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(10)
        for candidate in candidates:
            tile = _CandidateTile(candidate, self._on_tile_clicked)
            self._tiles.append(tile)
            strip_layout.addWidget(tile)
        strip_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(strip)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setMinimumHeight(THUMBNAIL_SIZE + 130)
        layout.addWidget(scroll)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        if self._tiles:
            self._on_tile_clicked(self._tiles[0])

    def _on_tile_clicked(self, tile: _CandidateTile, accept: bool = False) -> None:
        self.selected = tile.candidate
        for other in self._tiles:
            other.set_selected(other is tile)
        if accept:
            self.accept()


def choose_artwork(
    candidates: list[ArtworkCandidate], parent=None
) -> ArtworkCandidate | None:
    """Show the picker, or short-circuit when there is nothing to choose.

    With zero or one candidate a dialog would just be a click to dismiss.
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    dialog = ArtPickerDialog(candidates, parent)
    if dialog.exec() == QDialog.Accepted:
        return dialog.selected
    return None
