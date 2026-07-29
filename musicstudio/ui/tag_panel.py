"""Metadata editor with cover art lookup."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import get_settings
from ..core import artwork as artwork_module
from ..core import tags as tags_module
from . import theme
from .widgets.art_picker import choose_artwork
from .common import card, heading, row, section_label, spacer

ART_PREVIEW_SIZE = 190


class ArtworkView(QLabel):
    """Cover art preview that accepts a dropped or pasted image."""

    image_dropped = Signal(bytes)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(ART_PREVIEW_SIZE, ART_PREVIEW_SIZE)
        self.setAlignment(Qt.AlignCenter)
        self.setAcceptDrops(True)
        self.clear_art()

    def clear_art(self) -> None:
        self.setPixmap(QPixmap())
        self.setText("No cover art\n\nDrop an image here")
        self.setStyleSheet(
            f"border: 1px dashed {theme.BORDER}; border-radius: 8px; "
            f"color: {theme.TEXT_FAINT}; background: {theme.BG_DEEP};"
        )

    def set_art(self, data: bytes) -> None:
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data):
            self.clear_art()
            return
        self.setPixmap(
            pixmap.scaled(
                ART_PREVIEW_SIZE, ART_PREVIEW_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
            )
        )
        self.setText("")
        self.setStyleSheet(
            f"border: 1px solid {theme.BORDER}; border-radius: 8px; background: {theme.BG_DEEP};"
        )

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls() or event.mimeData().hasImage():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        for url in event.mimeData().urls():
            if url.isLocalFile():
                try:
                    self.image_dropped.emit(Path(url.toLocalFile()).read_bytes())
                except OSError:
                    continue
                event.acceptProposedAction()
                return


class TagPanel(QWidget):
    """Edit metadata for one file, or apply shared fields across many."""

    tags_saved = Signal(list)

    def __init__(self, job_queue, parent=None) -> None:
        super().__init__(parent)
        self.jobs = job_queue
        self.settings = get_settings()
        self._paths: list[Path] = []
        self._tags = tags_module.TagSet()
        self._artwork: tags_module.Artwork | None = None
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        layout.addWidget(
            heading(
                "Tags & artwork",
                "Edit metadata for one file, or select several and apply shared fields "
                "to all of them at once.",
            )
        )

        # -- file selector ----------------------------------------------
        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(110)
        self.file_list.currentRowChanged.connect(self._load_current)
        add_button = QPushButton("Add files…")
        add_button.clicked.connect(self._choose_files)

        layout.addWidget(
            card(row(section_label("Files"), spacer(), add_button), self.file_list)
        )

        # -- fields ------------------------------------------------------
        self.fields: dict[str, QWidget] = {}
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        for key, label in [
            ("title", "Title"),
            ("artist", "Artist"),
            ("album", "Album"),
            ("albumartist", "Album artist"),
            ("date", "Year"),
            ("genre", "Genre"),
            ("composer", "Composer"),
        ]:
            edit = QLineEdit()
            self.fields[key] = edit
            form.addRow(label, edit)

        for key, label, maximum in [
            ("track_number", "Track no.", 999),
            ("track_total", "of", 999),
            ("disc_number", "Disc no.", 99),
            ("bpm", "BPM", 400),
        ]:
            spin = QSpinBox()
            spin.setRange(0, maximum)
            spin.setSpecialValueText("—")
            self.fields[key] = spin
            form.addRow(label, spin)

        comment = QPlainTextEdit()
        comment.setMaximumHeight(60)
        self.fields["comment"] = comment
        form.addRow("Comment", comment)

        # -- artwork column ---------------------------------------------
        self.art_view = ArtworkView()
        self.art_view.image_dropped.connect(self._set_artwork_bytes)

        self.art_label = QLabel("—")
        self.art_label.setObjectName("Hint")
        self.art_label.setAlignment(Qt.AlignCenter)

        self.fetch_art_button = QPushButton("Find cover art")
        self.fetch_art_button.clicked.connect(self._fetch_artwork)
        choose_art = QPushButton("Choose image…")
        choose_art.clicked.connect(self._choose_artwork)
        remove_art = QPushButton("Remove")
        remove_art.setObjectName("Danger")
        remove_art.clicked.connect(self._remove_artwork)

        art_column = QWidget()
        art_layout = QVBoxLayout(art_column)
        art_layout.setContentsMargins(0, 0, 0, 0)
        art_layout.setSpacing(8)
        art_layout.addWidget(section_label("Cover art"))
        art_layout.addWidget(self.art_view)
        art_layout.addWidget(self.art_label)
        art_layout.addWidget(self.fetch_art_button)
        art_layout.addWidget(choose_art)
        art_layout.addWidget(remove_art)
        art_layout.addStretch(1)

        layout.addWidget(card(row(form_widget, art_column, spacing=20, stretch_last=False)), 1)

        # -- actions -----------------------------------------------------
        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")

        self.save_button = QPushButton("Save tags")
        self.save_button.setObjectName("Primary")
        self.save_button.clicked.connect(self._save)
        self.save_button.setEnabled(False)

        self.save_all_button = QPushButton("Apply to all selected")
        self.save_all_button.clicked.connect(self._save_all)
        self.save_all_button.setEnabled(False)

        layout.addWidget(
            row(self.status_label, spacer(), self.save_all_button, self.save_button, spacing=8)
        )

    # -- files ----------------------------------------------------------
    def set_files(self, paths: list[Path]) -> None:
        self._paths = [Path(p) for p in paths]
        self.file_list.clear()
        for path in self._paths:
            self.file_list.addItem(path.name)
        if self._paths:
            self.file_list.setCurrentRow(0)
        else:
            self._clear_form()
        self.save_button.setEnabled(bool(self._paths))
        self.save_all_button.setEnabled(len(self._paths) > 1)

    def _choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose files to tag", "", "Audio files (*);;All files (*)"
        )
        if paths:
            self.set_files(self._paths + [Path(p) for p in paths])

    def _current_path(self) -> Path | None:
        index = self.file_list.currentRow()
        if 0 <= index < len(self._paths):
            return self._paths[index]
        return None

    # -- form -----------------------------------------------------------
    def _clear_form(self) -> None:
        for widget in self.fields.values():
            if isinstance(widget, QLineEdit):
                widget.clear()
            elif isinstance(widget, QSpinBox):
                widget.setValue(0)
            elif isinstance(widget, QPlainTextEdit):
                widget.clear()
        self._artwork = None
        self.art_view.clear_art()
        self.art_label.setText("—")

    def _load_current(self, *_) -> None:
        path = self._current_path()
        if path is None:
            self._clear_form()
            return

        self._tags = tags_module.try_read(path)
        for key, widget in self.fields.items():
            value = getattr(self._tags, key, None)
            if isinstance(widget, QLineEdit):
                widget.setText(str(value or ""))
            elif isinstance(widget, QSpinBox):
                widget.setValue(int(value) if value else 0)
            elif isinstance(widget, QPlainTextEdit):
                widget.setPlainText(str(value or ""))

        self._artwork = self._tags.artwork
        if self._artwork and self._artwork.data:
            self.art_view.set_art(self._artwork.data)
            self.art_label.setText(f"{self._artwork.size_label} · {self._artwork.mime}")
        else:
            self.art_view.clear_art()
            self.art_label.setText("None embedded")

        self.status_label.setText(str(path))

    def _collect(self) -> tags_module.TagSet:
        """Read the form back into a TagSet, keeping fields we do not show."""
        collected = tags_module.TagSet(**self._tags.to_dict())
        for key, widget in self.fields.items():
            if isinstance(widget, QLineEdit):
                setattr(collected, key, widget.text().strip())
            elif isinstance(widget, QSpinBox):
                setattr(collected, key, widget.value() or None)
            elif isinstance(widget, QPlainTextEdit):
                setattr(collected, key, widget.toPlainText().strip())
        return collected

    # -- artwork --------------------------------------------------------
    def _set_artwork_bytes(self, data: bytes) -> None:
        self._artwork = tags_module.Artwork.from_bytes(data)
        self.art_view.set_art(data)
        self.art_label.setText(f"{self._artwork.size_label} · {self._artwork.mime}")

    def _choose_artwork(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a cover image", "", "Images (*.jpg *.jpeg *.png *.webp)"
        )
        if path:
            try:
                self._set_artwork_bytes(Path(path).read_bytes())
            except OSError as exc:
                self.status_label.setText(f"Could not read that image: {exc}")

    def _remove_artwork(self) -> None:
        self._artwork = tags_module.Artwork(b"")
        self.art_view.clear_art()
        self.art_label.setText("Will be removed on save")

    def _fetch_artwork(self) -> None:
        tags = self._collect()
        artist = tags.effective_albumartist
        if not artist and not tags.album and not tags.title:
            self.status_label.setText("Fill in an artist or album first, then search.")
            return

        self.fetch_art_button.setEnabled(False)
        self.art_label.setText("Searching…")

        def work(context, a, b, t):
            context.progress(None, f"Searching for {b or t}")
            # Ask every provider so the user can compare, rather than silently
            # taking whichever answered first.
            return artwork_module.find_all_candidates(a, b, title=t)

        job = self.jobs.submit_func(
            "Cover art lookup", work, artist, tags.album, tags.title, category="artwork"
        )
        job.signals.finished.connect(self._on_artwork_found)

    def _on_artwork_found(self, _job_id: str, state: str, payload) -> None:
        self.fetch_art_button.setEnabled(True)
        if state != "succeeded":
            self.art_label.setText(f"Lookup failed: {payload}")
            return

        candidates = payload or []
        if not candidates:
            self.art_label.setText("No cover art found online")
            return

        chosen = choose_artwork(candidates, self)
        if chosen is None:
            self.art_label.setText("Kept the existing artwork")
            return
        self._set_artwork_bytes(chosen.data)
        self.art_label.setText(f"{chosen.size_label} from {chosen.source}")

    # -- saving ---------------------------------------------------------
    def _save(self) -> None:
        path = self._current_path()
        if path is None:
            return
        self._write_to([path])

    def _save_all(self) -> None:
        """Apply the fields that make sense across a whole selection.

        Track numbers and titles are per-track, so applying them to every file
        would be destructive; album-level fields are the ones that broadcast.
        """
        if not self._paths:
            return
        self._write_to(self._paths, shared_only=True)

    def _write_to(self, paths: list[Path], *, shared_only: bool = False) -> None:
        edited = self._collect()
        artwork = self._artwork

        def work(context, targets):
            written = []
            for index, target in enumerate(targets):
                context.raise_if_cancelled()
                context.progress(index / len(targets), f"Tagging {Path(target).name}")
                if shared_only:
                    existing = tags_module.try_read(target)
                    for field_name in (
                        "album", "albumartist", "artist", "date", "genre", "composer", "comment"
                    ):
                        setattr(existing, field_name, getattr(edited, field_name))
                    payload = existing
                else:
                    payload = edited
                tags_module.write(target, payload, artwork=artwork)
                written.append(Path(target))
            context.progress(1.0, f"Tagged {len(written)} file(s)")
            return written

        self.save_button.setEnabled(False)
        self.save_all_button.setEnabled(False)
        job = self.jobs.submit_func(f"Saving tags ({len(paths)})", work, paths, category="tags")
        job.signals.finished.connect(self._on_saved)

    def _on_saved(self, _job_id: str, state: str, payload) -> None:
        self.save_button.setEnabled(bool(self._paths))
        self.save_all_button.setEnabled(len(self._paths) > 1)
        if state == "succeeded":
            self.status_label.setText(f"Saved {len(payload)} file(s)")
            self.tags_saved.emit(payload)
            self._load_current()
        else:
            self.status_label.setText(f"Could not save: {payload}")
