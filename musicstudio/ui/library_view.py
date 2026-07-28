"""The library: import files, browse them, and launch actions on a selection."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..db import Library, TrackRow, scan_into_library
from . import theme
from .common import format_size, heading, row, spacer


class TrackTableModel(QAbstractTableModel):
    """Table model over indexed tracks."""

    COLUMNS = ["#", "Title", "Artist", "Album", "Year", "Length", "Quality", "Art"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tracks: list[TrackRow] = []

    def set_tracks(self, tracks: list[TrackRow]) -> None:
        self.beginResetModel()
        self._tracks = tracks
        self.endResetModel()

    def track_at(self, proxy_row: int) -> TrackRow | None:
        if 0 <= proxy_row < len(self._tracks):
            return self._tracks[proxy_row]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._tracks)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(self.COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return self.COLUMNS[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        track = self._tracks[index.row()]
        column = index.column()

        if role == Qt.DisplayRole:
            return [
                str(track.track_number or ""),
                track.display_title,
                track.display_artist,
                track.album or "—",
                (track.date or "")[:4],
                track.duration_label,
                track.quality_label,
                track.artwork_label,
            ][column]

        if role == Qt.ForegroundRole:
            # Lossless files are the ones worth keeping; make them findable
            # at a glance rather than by reading the codec column.
            if column == 6:
                return QColor(theme.LOSSLESS if track.is_lossless else theme.LOSSY)
            if column == 7 and not track.has_artwork:
                return QColor(theme.TEXT_FAINT)
            if column in (0, 4):
                return QColor(theme.TEXT_DIM)

        if role == Qt.TextAlignmentRole and column in (0, 4, 5, 7):
            return int(Qt.AlignCenter)

        if role == Qt.ToolTipRole:
            return str(track.path)

        return None


class LibraryPanel(QWidget):
    """Library browser with import, search and selection-driven actions."""

    #: Emitted with the paths the user selected.
    selection_changed = Signal(list)
    #: Emitted when the user asks to act on the selection.
    convert_requested = Signal(list)
    edit_requested = Signal(object)         # a single path
    tags_requested = Signal(list)
    artwork_requested = Signal(list)

    def __init__(self, library: Library, job_queue, parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.jobs = job_queue
        self._build()
        self.setAcceptDrops(True)
        self.refresh()

    # -- construction ---------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        layout.addWidget(
            heading(
                "Library",
                "Drag a folder anywhere onto this window to import it. "
                "Files are indexed, never moved or modified.",
            )
        )

        # -- toolbar ----------------------------------------------------
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search title, artist, album or genre…")
        self.search_box.textChanged.connect(self._apply_search)
        self.search_box.setClearButtonEnabled(True)

        add_files = QPushButton("Add files…")
        add_files.clicked.connect(self._choose_files)
        add_folder = QPushButton("Add folder…")
        add_folder.clicked.connect(self._choose_folder)

        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        toolbar_layout.addWidget(self.search_box, 1)
        toolbar_layout.addWidget(add_files)
        toolbar_layout.addWidget(add_folder)
        layout.addWidget(toolbar)

        # -- table ------------------------------------------------------
        self.model = TrackTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setShowGrid(False)
        self.table.doubleClicked.connect(self._on_double_click)
        self.table.selectionModel().selectionChanged.connect(self._on_selection_changed)

        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.resizeSection(0, 44)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setSectionResizeMode(3, QHeaderView.Stretch)
        for column, width in ((4, 60), (5, 70), (6, 130), (7, 60)):
            header.setSectionResizeMode(column, QHeaderView.Fixed)
            header.resizeSection(column, width)
        layout.addWidget(self.table, 1)

        # -- action bar -------------------------------------------------
        self.status_label = QLabel("No tracks yet")
        self.status_label.setObjectName("Hint")
        # The stats line is long; give it room rather than letting the
        # action buttons crop it mid-word.
        self.status_label.setMinimumWidth(360)

        self.convert_button = QPushButton("Convert…")
        self.convert_button.clicked.connect(
            lambda: self.convert_requested.emit(self.selected_paths())
        )
        self.edit_button = QPushButton("Edit audio…")
        self.edit_button.clicked.connect(self._emit_edit)
        self.tags_button = QPushButton("Edit tags…")
        self.tags_button.clicked.connect(lambda: self.tags_requested.emit(self.selected_paths()))
        self.artwork_button = QPushButton("Update artwork")
        self.artwork_button.setObjectName("Primary")
        self.artwork_button.clicked.connect(
            lambda: self.artwork_requested.emit(self.selected_paths() or self.all_paths())
        )

        layout.addWidget(
            row(
                self.status_label,
                spacer(),
                self.convert_button,
                self.edit_button,
                self.tags_button,
                self.artwork_button,
            )
        )
        self._update_actions()

    # -- data -----------------------------------------------------------
    def refresh(self) -> None:
        self._apply_search(self.search_box.text() if hasattr(self, "search_box") else "")
        stats = self.library.stats()
        if stats["tracks"]:
            self.status_label.setText(
                f"{stats['tracks']} tracks · {format_size(stats['size'])} · "
                f"{stats['lossless']} lossless · {stats['with_art']} with artwork"
            )
        else:
            self.status_label.setText("No tracks yet — add a folder to get started")

    def _apply_search(self, term: str) -> None:
        self.model.set_tracks(self.library.search(term))
        self._update_actions()

    def selected_paths(self) -> list[Path]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        tracks = [self.model.track_at(r) for r in sorted(rows)]
        return [t.path for t in tracks if t is not None]

    def all_paths(self) -> list[Path]:
        return [t.path for t in (self.model.track_at(r) for r in range(self.model.rowCount())) if t]

    # -- events ---------------------------------------------------------
    def _on_selection_changed(self, *_) -> None:
        self._update_actions()
        self.selection_changed.emit(self.selected_paths())

    def _update_actions(self) -> None:
        selected = len(self.table.selectionModel().selectedRows()) if hasattr(self, "table") else 0
        has_any = self.model.rowCount() > 0
        self.convert_button.setEnabled(selected > 0)
        self.edit_button.setEnabled(selected == 1)
        self.tags_button.setEnabled(selected > 0)
        self.artwork_button.setEnabled(has_any)
        self.artwork_button.setText(
            f"Update artwork ({selected})" if selected else "Update all artwork"
        )

    def _on_double_click(self, index: QModelIndex) -> None:
        track = self.model.track_at(index.row())
        if track is not None:
            self.edit_requested.emit(track.path)

    def _emit_edit(self) -> None:
        paths = self.selected_paths()
        if paths:
            self.edit_requested.emit(paths[0])

    # -- import ---------------------------------------------------------
    def _choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add music files",
            "",
            "Audio files (*.flac *.mp3 *.m4a *.wav *.aiff *.ogg *.opus *.wma *.wv *.ape *.aac);;"
            "All files (*)",
        )
        if paths:
            self.import_paths([Path(p) for p in paths])

    def _choose_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Add a music folder")
        if directory:
            self.import_paths([Path(directory)])

    def import_paths(self, paths: list[Path]) -> None:
        """Index ``paths`` in the background, refreshing when done."""
        if not paths:
            return

        def work(context, targets):
            return scan_into_library(self.library, targets, context=context)

        job = self.jobs.submit_func(
            f"Importing {len(paths)} item(s)", work, paths, category="import"
        )
        job.signals.finished.connect(lambda *_: self.refresh())

    # -- drag and drop --------------------------------------------------
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [
            Path(url.toLocalFile())
            for url in event.mimeData().urls()
            if url.isLocalFile()
        ]
        if paths:
            self.import_paths(paths)
            event.acceptProposedAction()
