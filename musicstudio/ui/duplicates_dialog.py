"""Reviewing and cleaning up duplicate songs.

``Library.find_duplicates()`` groups indexed tracks by normalized (artist,
title) and sorts each group best-quality-first. This dialog shows those
groups and lets the user delete whichever copies are checked -- every copy
but the recommended keeper is pre-checked, since that is the common case
this app can itself create (a song downloaded twice, or converted to a new
format alongside the original rather than in place).

Besides deleting, the user can: pick a different criterion for which copy
counts as the keeper, merge any tags a redundant copy has that the keeper is
missing before deleting it, move checked copies elsewhere instead of
deleting them, and mark a group "ignore" so it stops being reported as a
duplicate at all.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..core import library_ops
from ..core import tags as tags_module
from ..db import DuplicateGroup, Library, sort_key_for_criterion
from .common import confirm_permanent_delete, format_size


class DuplicatesDialog(QDialog):
    """Lists duplicate-song groups and deletes whichever copies are checked."""

    def __init__(self, library: Library, groups: list[DuplicateGroup], parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.groups = groups
        #: Filled in as deletions/moves happen, so the caller knows to refresh.
        self.deleted_paths: list[Path] = []
        self.moved_paths: list[tuple[Path, Path]] = []

        self.setWindowTitle("Duplicate songs")
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.summary = QLabel()
        self.summary.setObjectName("Hint")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        criterion_row = QHBoxLayout()
        criterion_row.addWidget(QLabel("Auto-select best by:"))
        self.criterion_combo = QComboBox()
        self.criterion_combo.addItem("Best quality", "quality")
        self.criterion_combo.addItem("Newest added", "newest")
        self.criterion_combo.addItem("Oldest added", "oldest")
        self.criterion_combo.currentIndexChanged.connect(self._on_criterion_changed)
        criterion_row.addWidget(self.criterion_combo)
        criterion_row.addStretch(1)
        layout.addLayout(criterion_row)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "File", "Quality", "Size", "Folder"])
        self.tree.setColumnWidth(0, 28)
        self.tree.setColumnWidth(1, 240)
        self.tree.setColumnWidth(2, 130)
        self.tree.setColumnWidth(3, 90)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_context_menu)
        layout.addWidget(self.tree, 1)

        self._populate()

        self.merge_metadata_check = QCheckBox(
            "Merge tags from deleted copies into the keeper first"
        )
        self.merge_metadata_check.setToolTip(
            "Fills in any blank field on the copy you keep from the copies you "
            "delete. Never overwrites a value the keeper already has."
        )
        self.merge_metadata_check.setChecked(True)
        layout.addWidget(self.merge_metadata_check)

        button_row = QHBoxLayout()
        self.move_button = QPushButton("Move checked to folder…")
        self.move_button.clicked.connect(self._move_checked)
        self.delete_button = QPushButton("Delete checked copies…")
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete_checked)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        button_row.addStretch(1)
        button_row.addWidget(self.move_button)
        button_row.addWidget(self.delete_button)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

    # -- building ---------------------------------------------------------
    def _populate(self) -> None:
        self.tree.clear()
        for group in self.groups:
            header = QTreeWidgetItem([f"{group.artist} — {group.title}  ({group.count} copies)"])
            header.setFirstColumnSpanned(True)
            header.setFlags(header.flags() & ~Qt.ItemIsSelectable)
            self.tree.addTopLevelItem(header)
            for index, track in enumerate(group.tracks):
                is_keeper = index == 0
                child = QTreeWidgetItem(
                    [
                        "",
                        track.path.name,
                        track.quality_label,
                        format_size(track.size_bytes),
                        str(track.path.parent),
                    ]
                )
                child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                child.setCheckState(0, Qt.Unchecked if is_keeper else Qt.Checked)
                child.setData(0, Qt.UserRole, track.path)
                if is_keeper:
                    child.setToolTip(1, "Recommended keeper — best quality copy in this group")
                header.addChild(child)
            header.setExpanded(True)

        total_tracks = sum(g.count for g in self.groups)
        total_redundant = sum(len(g.redundant_tracks) for g in self.groups)
        total_size = sum(g.redundant_size for g in self.groups)
        self.summary.setText(
            f"{len(self.groups)} duplicate group(s), {total_tracks} file(s) total. "
            f"The best copy in each group is left unchecked; the other "
            f"{total_redundant} copy/copies are checked, freeing about "
            f"{format_size(total_size)} if deleted."
            if self.groups
            else "No duplicates remain."
        )

    def _on_criterion_changed(self) -> None:
        """Re-sort each already-fetched group in place, so the pre-checked
        "keeper" reflects the chosen criterion without re-querying the DB."""
        key = sort_key_for_criterion(self.criterion_combo.currentData())
        for group in self.groups:
            group.tracks.sort(key=key)
        self._populate()

    def _show_context_menu(self, position) -> None:
        item = self.tree.itemAt(position)
        if item is None:
            return
        if item.parent() is None:
            self._show_group_context_menu(item, position)
        else:
            self._show_row_context_menu(item, position)

    def _show_group_context_menu(self, item: QTreeWidgetItem, position) -> None:
        index = self.tree.indexOfTopLevelItem(item)
        if not (0 <= index < len(self.groups)):
            return
        group = self.groups[index]
        menu = QMenu(self)
        menu.addAction(
            "Ignore this group (stop showing it as a duplicate)",
            lambda: self._ignore_group(group),
        )
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _show_row_context_menu(self, item: QTreeWidgetItem, position) -> None:
        """Delete or move exactly this one copy, independent of any checkbox
        state -- the quick path when you know you only want to act on a
        single file rather than managing checkboxes across every group."""
        path = item.data(0, Qt.UserRole)
        if path is None:
            return
        menu = QMenu(self)
        menu.addAction("Delete this copy…", lambda: self._delete_single(path))
        menu.addAction("Move this copy to folder…", lambda: self._move_single(path))
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _ignore_group(self, group: DuplicateGroup) -> None:
        self.library.ignore_duplicate_group(group.artist, group.title)
        self.groups = [g for g in self.groups if g is not group]
        self._populate()

    def _delete_single(self, path: Path) -> None:
        if not confirm_permanent_delete(self, [path]):
            return
        if self.merge_metadata_check.isChecked():
            self._merge_metadata_before_delete([path])

        result = library_ops.delete_files_permanently(self.library, [path])
        self.deleted_paths.extend(result.deleted)
        self.groups = _drop_deleted(self.groups, result.deleted)
        self._populate()
        if result.failed:
            failed_path, err = result.failed[0]
            QMessageBox.warning(self, "Delete failed", f"Could not delete {failed_path.name}: {err}")

    def _move_single(self, path: Path) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Move this copy to")
        if not directory:
            return
        result = library_ops.move_files(self.library, [path], Path(directory))
        self.moved_paths.extend(result.moved)
        self.groups = _drop_deleted(self.groups, [old for old, _new in result.moved])
        self._populate()
        if result.failed:
            failed_path, err = result.failed[0]
            QMessageBox.warning(self, "Move failed", f"Could not move {failed_path.name}: {err}")

    # -- deleting -----------------------------------------------------------
    def _checked_paths(self) -> list[Path]:
        paths = []
        for i in range(self.tree.topLevelItemCount()):
            header = self.tree.topLevelItem(i)
            for j in range(header.childCount()):
                child = header.child(j)
                if child.checkState(0) == Qt.Checked:
                    paths.append(child.data(0, Qt.UserRole))
        return paths

    def _merge_metadata_before_delete(self, paths_to_delete: list[Path]) -> None:
        to_delete = set(paths_to_delete)
        for group in self.groups:
            keeper = group.tracks[0]
            if keeper.path in to_delete:
                continue  # the keeper itself is being deleted -- nothing well-defined to merge into
            donors = [t.path for t in group.tracks[1:] if t.path in to_delete]
            if donors:
                try:
                    tags_module.merge_missing_tags(keeper.path, donors)
                except (tags_module.TagError, OSError):
                    pass  # a merge failure must not block a deletion the user already confirmed

    def _delete_checked(self) -> None:
        paths = self._checked_paths()
        if not paths:
            QMessageBox.information(
                self, "Nothing checked", "No copies are checked for deletion."
            )
            return
        if not confirm_permanent_delete(self, paths):
            return

        if self.merge_metadata_check.isChecked():
            self._merge_metadata_before_delete(paths)

        result = library_ops.delete_files_permanently(self.library, paths)
        self.deleted_paths.extend(result.deleted)
        self.groups = _drop_deleted(self.groups, result.deleted)
        self._populate()

        if result.failed:
            QMessageBox.warning(
                self,
                "Some files failed",
                f"Deleted {len(result.deleted)} file(s); {len(result.failed)} failed:\n"
                + "\n".join(f"{p.name}: {err}" for p, err in result.failed),
            )

    def _move_checked(self) -> None:
        paths = self._checked_paths()
        if not paths:
            QMessageBox.information(self, "Nothing checked", "No copies are checked.")
            return
        directory = QFileDialog.getExistingDirectory(self, "Move duplicate copies to")
        if not directory:
            return

        result = library_ops.move_files(self.library, paths, Path(directory))
        self.moved_paths.extend(result.moved)
        self.groups = _drop_deleted(self.groups, [old for old, _new in result.moved])
        self._populate()

        if result.failed:
            QMessageBox.warning(
                self,
                "Some files failed",
                f"Moved {len(result.moved)} file(s); {len(result.failed)} failed:\n"
                + "\n".join(f"{p.name}: {err}" for p, err in result.failed),
            )


def _drop_deleted(groups: list[DuplicateGroup], deleted: list[Path]) -> list[DuplicateGroup]:
    """Remove deleted tracks from each group, and groups left with one copy."""
    deleted_set = set(deleted)
    remaining = []
    for group in groups:
        survivors = [t for t in group.tracks if t.path not in deleted_set]
        if len(survivors) >= 2:
            remaining.append(DuplicateGroup(artist=group.artist, title=group.title, tracks=survivors))
    return remaining
