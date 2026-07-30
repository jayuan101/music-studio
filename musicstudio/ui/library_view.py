"""The library: import files, browse them, and launch actions on a selection."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QFileSystemWatcher, QModelIndex, QTimer, Qt, Signal
from PySide6.QtGui import QColor, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..config import get_settings
from ..core import tags as tags_module
from ..db import Library, TrackRow, scan_into_library
from . import theme
from .common import card, format_size, heading, row, section_label, spacer
from .tag_panel import ArtworkView

#: Smaller than the Tags & art page's own artwork box -- this is a
#: secondary, at-a-glance reference, not the primary editing view.
LIBRARY_ART_PREVIEW_SIZE = 140


class _LibraryArtworkPreview(ArtworkView):
    """Read-only artwork preview for the Library page.

    Same rendering as the Tags & art page's box, sized down, with the
    drop-an-image affordance turned off since dropping here would not
    actually do anything -- editing artwork stays on the Tags & art page.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(LIBRARY_ART_PREVIEW_SIZE, LIBRARY_ART_PREVIEW_SIZE)
        self.setAcceptDrops(False)

    def clear_art(self) -> None:
        self.setPixmap(QPixmap())
        self.setText("No cover art")
        self.setStyleSheet(
            f"border: 1px dashed {theme.BORDER}; border-radius: 8px; "
            f"color: {theme.TEXT_FAINT}; background: {theme.BG_DEEP};"
        )


class TrackTableModel(QAbstractTableModel):
    """Table model over indexed tracks."""

    COLUMNS = [
        "#", "Title", "Artist", "Album", "Album Artist", "Year", "Length",
        "Quality", "Art", "Genre",
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._tracks: list[TrackRow] = []

    def set_tracks(self, tracks: list[TrackRow]) -> None:
        self.beginResetModel()
        self._tracks = tracks
        self.endResetModel()

    #: One sort key per column, in COLUMNS order. Text columns sort
    #: case-insensitively so "abba" and "ABBA" land next to each other.
    _SORT_KEYS = [
        lambda t: t.track_number or 0,
        lambda t: (t.display_title or "").lower(),
        lambda t: (t.display_artist or "").lower(),
        lambda t: (t.album or "").lower(),
        lambda t: (t.albumartist or "").lower(),
        lambda t: t.date or "",
        lambda t: t.duration,
        lambda t: (t.is_lossless, t.bitrate),
        lambda t: t.has_artwork,
        lambda t: (t.genre or "").lower(),
    ]

    def sort(self, column: int, order: Qt.SortOrder = Qt.AscendingOrder) -> None:
        if not 0 <= column < len(self._SORT_KEYS):
            return
        self.layoutAboutToBeChanged.emit()
        self._tracks.sort(key=self._SORT_KEYS[column], reverse=order == Qt.DescendingOrder)
        self.layoutChanged.emit()

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
                track.albumartist or "—",
                (track.date or "")[:4],
                track.duration_label,
                track.quality_label,
                track.artwork_label,
                track.genre or "—",
            ][column]

        if role == Qt.ForegroundRole:
            # Lossless files are the ones worth keeping; make them findable
            # at a glance rather than by reading the codec column.
            if column == 7:
                return QColor(theme.LOSSLESS if track.is_lossless else theme.LOSSY)
            if column == 8 and not track.has_artwork:
                return QColor(theme.TEXT_FAINT)
            if column in (0, 5):
                return QColor(theme.TEXT_DIM)

        if role == Qt.TextAlignmentRole and column in (0, 5, 6, 8):
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
    tags_fix_requested = Signal(list)
    ytmusic_format_requested = Signal(list)
    #: Double-click: play this track, queuing every track currently visible
    #: (respecting the active search/sort) -- paths, then the clicked row's
    #: index within that list.
    play_requested = Signal(list, int)

    def __init__(self, library: Library, job_queue, parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.jobs = job_queue
        self._build()
        self.setAcceptDrops(True)

        # Files copied into a remembered folder from outside the app (e.g.
        # Explorer) should appear without the user having to click Rescan.
        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_watched_dir_changed)
        self._rescan_timer = QTimer(self)
        self._rescan_timer.setSingleShot(True)
        # Debounced: copying a whole album fires many change events in a
        # burst, and each one restarts this timer instead of triggering its
        # own rescan.
        self._rescan_timer.setInterval(1500)
        self._rescan_timer.timeout.connect(self.rescan_known_roots)
        self._sync_watched_roots()

        self.refresh()

    # -- construction ---------------------------------------------------
    def _build(self) -> None:
        outer_layout = QVBoxLayout(self)
        outer_layout.setContentsMargins(24, 20, 24, 20)
        outer_layout.setSpacing(14)

        outer_layout.addWidget(
            heading(
                "Library",
                "Drag a folder anywhere onto this window to import it. "
                "Files are indexed, never moved or modified.",
            )
        )

        # Everything below lives in the left column; the artwork preview is
        # a fixed-width panel alongside it, updated as the selection changes.
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(16)
        outer_layout.addLayout(body, 1)

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        body.addLayout(layout, 1)
        body.addWidget(self._build_artwork_preview())

        # -- toolbar ----------------------------------------------------
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("Search title, artist, album or genre…")
        self.search_box.textChanged.connect(self._apply_search)
        self.search_box.setClearButtonEnabled(True)

        add_files = QPushButton("Add files…")
        add_files.clicked.connect(self._choose_files)
        add_folder = QPushButton("Add folder…")
        add_folder.clicked.connect(self._choose_folder)
        rescan = QPushButton("Rescan")
        rescan.setToolTip("Re-index remembered folders and drop tracks whose files are gone")
        rescan.clicked.connect(self.rescan_known_roots)
        remove_missing = QPushButton("Remove missing")
        remove_missing.setToolTip("Drop library entries whose files no longer exist")
        remove_missing.clicked.connect(self.remove_missing)

        toolbar = QWidget()
        toolbar_layout = QHBoxLayout(toolbar)
        toolbar_layout.setContentsMargins(0, 0, 0, 0)
        toolbar_layout.setSpacing(8)
        toolbar_layout.addWidget(self.search_box, 1)
        toolbar_layout.addWidget(add_files)
        toolbar_layout.addWidget(add_folder)
        toolbar_layout.addWidget(rescan)
        toolbar_layout.addWidget(remove_missing)
        layout.addWidget(toolbar)

        # -- table ------------------------------------------------------
        self.model = TrackTableModel(self)
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(4, Qt.AscendingOrder)  # Album Artist, matching the library's own order
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
        header.setSectionResizeMode(4, QHeaderView.Stretch)
        for column, width in ((5, 60), (6, 70), (7, 130), (8, 60), (9, 110)):
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
        self.fix_tags_button = QPushButton("Fix metadata")
        self.fix_tags_button.setToolTip(
            "Fill in missing Title/Artist/Album/Year/Genre from the filename and an "
            "online lookup. Never overwrites a field that already has a value."
        )
        self.fix_tags_button.clicked.connect(
            lambda: self.tags_fix_requested.emit(self.selected_paths() or self.all_paths())
        )
        self.ytmusic_button = QPushButton("YouTube Music format")
        self.ytmusic_button.setToolTip(
            "Reshape tags the way YouTube Music expects: fill album artist (what it "
            "groups albums by), strip “(Official Video)”-style noise from titles, "
            "move guests into the title as “(feat. X)”, and fold duplicate genre "
            "spellings together. Overwrites existing values — the current tags are "
            "saved first so it can be undone."
        )
        self.ytmusic_button.clicked.connect(
            lambda: self.ytmusic_format_requested.emit(self.selected_paths() or self.all_paths())
        )
        self.delete_button = QPushButton("Delete…")
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete_selected)

        layout.addWidget(
            row(
                self.status_label,
                spacer(),
                self.convert_button,
                self.edit_button,
                self.tags_button,
                self.artwork_button,
                self.fix_tags_button,
                self.ytmusic_button,
                self.delete_button,
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
        # A model reset does not re-sort itself, so a search keystroke or a
        # rescan would otherwise silently drop the user's chosen sort column.
        header = self.table.horizontalHeader()
        self.table.sortByColumn(header.sortIndicatorSection(), header.sortIndicatorOrder())
        self._update_actions()

    def selected_paths(self) -> list[Path]:
        rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        tracks = [self.model.track_at(r) for r in sorted(rows)]
        return [t.path for t in tracks if t is not None]

    def all_paths(self) -> list[Path]:
        return [t.path for t in (self.model.track_at(r) for r in range(self.model.rowCount())) if t]

    # -- artwork preview --------------------------------------------------
    def _build_artwork_preview(self) -> QWidget:
        self.preview_art = _LibraryArtworkPreview()
        self.preview_label = QLabel("Select a track to preview its artwork")
        self.preview_label.setObjectName("Hint")
        self.preview_label.setWordWrap(True)
        self.preview_label.setAlignment(Qt.AlignCenter)

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(8)
        content_layout.addWidget(section_label("Artwork"))
        content_layout.addWidget(self.preview_art, 0, Qt.AlignHCenter)
        content_layout.addWidget(self.preview_label)
        content_layout.addStretch(1)

        panel = card(content)
        panel.setFixedWidth(LIBRARY_ART_PREVIEW_SIZE + 48)
        return panel

    def _refresh_artwork_preview(self, paths: list[Path]) -> None:
        if not paths:
            self.preview_art.clear_art()
            self.preview_label.setText("Select a track to preview its artwork")
            return
        if len(paths) > 1:
            self.preview_art.clear_art()
            self.preview_label.setText(f"{len(paths)} tracks selected")
            return

        tags = tags_module.try_read(paths[0])
        if tags.has_artwork():
            self.preview_art.set_art(tags.artwork.data)
            self.preview_label.setText(Path(paths[0]).name)
        else:
            self.preview_art.clear_art()
            self.preview_label.setText(f"No artwork\n\n{Path(paths[0]).name}")

    # -- events ---------------------------------------------------------
    def _on_selection_changed(self, *_) -> None:
        self._update_actions()
        paths = self.selected_paths()
        self.selection_changed.emit(paths)
        self._refresh_artwork_preview(paths)

    def _update_actions(self) -> None:
        selected = len(self.table.selectionModel().selectedRows()) if hasattr(self, "table") else 0
        has_any = self.model.rowCount() > 0
        self.convert_button.setEnabled(selected > 0)
        self.edit_button.setEnabled(selected == 1)
        self.tags_button.setEnabled(selected > 0)
        self.artwork_button.setEnabled(has_any)
        self.fix_tags_button.setEnabled(has_any)
        self.ytmusic_button.setEnabled(has_any)
        self.delete_button.setEnabled(selected > 0)
        self.artwork_button.setText(
            f"Update artwork ({selected})" if selected else "Update all artwork"
        )
        self.fix_tags_button.setText(
            f"Fix metadata ({selected})" if selected else "Fix all metadata"
        )
        self.ytmusic_button.setText(
            f"YouTube Music format ({selected})" if selected else "YouTube Music format"
        )

    def _on_double_click(self, index: QModelIndex) -> None:
        track = self.model.track_at(index.row())
        if track is not None:
            self.play_requested.emit(self.all_paths(), index.row())

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
            "Audio files (*.flac *.mp3 *.m4a *.wav *.aiff *.ogg *.opus *.wma *.wv *.ape *.aac *.mp4);;"
            "All files (*)",
        )
        if paths:
            self.import_paths([Path(p) for p in paths])

    def _choose_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Add a music folder")
        if directory:
            self.import_paths([Path(directory)])

    def import_paths(self, paths: list[Path], *, remember: bool = True) -> None:
        """Index ``paths`` in the background, refreshing when done."""
        if not paths:
            return
        if remember:
            self._remember_roots(paths)

        def work(context, targets):
            return scan_into_library(self.library, targets, context=context)

        job = self.jobs.submit_func(
            f"Importing {len(paths)} item(s)", work, paths, category="import"
        )
        job.signals.finished.connect(lambda *_: self.refresh())

    def _remember_roots(self, paths: list[Path]) -> None:
        """Record imported folders so the library survives a restart.

        Only directories are remembered. Keeping every individual file would
        turn the settings file into a second, worse copy of the database.
        """
        settings = get_settings()
        known = set(settings.library_paths)
        for path in paths:
            resolved = Path(path).resolve()
            root = resolved if resolved.is_dir() else resolved.parent
            known.add(str(root))
        if set(settings.library_paths) != known:
            settings.library_paths = sorted(known)
            try:
                settings.save()
            except OSError:
                # Failing to remember a folder is not worth interrupting an
                # import that otherwise succeeded.
                pass
        self._sync_watched_roots()

    def _sync_watched_roots(self) -> None:
        """Watch every remembered folder, and its subfolders, for changes.

        QFileSystemWatcher only reports changes in directories it was
        explicitly given, so a plain top-level watch would miss files
        dropped into an album subfolder. Re-walking on every sync also
        picks up subfolders created since the last sync.
        """
        existing = self._watcher.directories()
        if existing:
            self._watcher.removePaths(existing)

        directories: set[str] = set()
        for root in get_settings().library_paths:
            root_path = Path(root)
            if not root_path.is_dir():
                continue
            directories.add(str(root_path))
            for sub in root_path.rglob("*"):
                if sub.is_dir():
                    directories.add(str(sub))

        if directories:
            self._watcher.addPaths(sorted(directories))

    def _on_watched_dir_changed(self, _path: str) -> None:
        self._rescan_timer.start()

    def rescan_known_roots(self) -> None:
        """Re-index every remembered folder, picking up outside changes."""
        roots = [Path(p) for p in get_settings().library_paths if Path(p).is_dir()]
        if not roots:
            self.status_label.setText("No folders remembered yet — add one to get started")
            return

        def work(context, targets):
            imported, skipped = scan_into_library(self.library, targets, context=context)
            removed = self.library.prune_missing()
            return imported, skipped, removed

        job = self.jobs.submit_func(
            f"Rescanning {len(roots)} folder(s)", work, roots, category="import"
        )
        job.signals.finished.connect(self._on_rescanned)

    def _on_rescanned(self, _job_id: str, state: str, payload) -> None:
        self.refresh()
        self._sync_watched_roots()
        if state == "succeeded" and payload:
            imported, _skipped, removed = payload
            parts = []
            if imported:
                parts.append(f"{imported} added or updated")
            if removed:
                parts.append(f"{removed} missing removed")
            self.status_label.setText(
                "Rescan: " + (", ".join(parts) if parts else "nothing changed")
            )

    def _delete_selected(self) -> None:
        """Permanently delete the selected files from disk, not just the index."""
        paths = self.selected_paths()
        if not paths:
            return

        names = "\n".join(p.name for p in paths[:10])
        if len(paths) > 10:
            names += f"\n… and {len(paths) - 10} more"
        reply = QMessageBox.warning(
            self,
            "Delete from disk",
            f"Permanently delete {len(paths)} file(s)? This cannot be undone.\n\n{names}",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if reply != QMessageBox.Yes:
            return

        failed = []
        deleted = 0
        for path in paths:
            try:
                path.unlink()
            except OSError as exc:
                failed.append(f"{path.name}: {exc}")
            else:
                self.library.remove(path)
                deleted += 1

        self.refresh()
        if failed:
            self.status_label.setText(
                f"Deleted {deleted} file(s); {len(failed)} failed: {'; '.join(failed)}"
            )
        else:
            self.status_label.setText(f"Deleted {deleted} file(s)")

    def remove_missing(self) -> None:
        """Drop rows whose files are no longer on disk."""
        removed = self.library.prune_missing()
        self.refresh()
        self.status_label.setText(
            f"Removed {removed} track(s) whose files are gone"
            if removed
            else "Every indexed file is still on disk"
        )

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
