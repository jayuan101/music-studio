"""Reviewing and cleaning up duplicate songs.

``Library.find_duplicates()`` groups indexed tracks by normalized (artist,
title) and sorts each group best-quality-first. This dialog shows those
groups and lets the user delete whichever copies are checked -- every copy
but the recommended keeper is pre-checked, since that is the common case
this app can itself create (a song downloaded twice, or converted to a new
format alongside the original rather than in place).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from ..db import DuplicateGroup, Library
from .common import format_size


class DuplicatesDialog(QDialog):
    """Lists duplicate-song groups and deletes whichever copies are checked."""

    def __init__(self, library: Library, groups: list[DuplicateGroup], parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.groups = groups
        #: Filled in as deletions happen, so the caller knows to refresh.
        self.deleted_paths: list[Path] = []

        self.setWindowTitle("Duplicate songs")
        self.resize(760, 560)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)

        self.summary = QLabel()
        self.summary.setObjectName("Hint")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["", "File", "Quality", "Size", "Folder"])
        self.tree.setColumnWidth(0, 28)
        self.tree.setColumnWidth(1, 240)
        self.tree.setColumnWidth(2, 130)
        self.tree.setColumnWidth(3, 90)
        layout.addWidget(self.tree, 1)

        self._populate()

        button_row = QHBoxLayout()
        self.delete_button = QPushButton("Delete checked copies…")
        self.delete_button.setObjectName("Danger")
        self.delete_button.clicked.connect(self._delete_checked)
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        button_row.addStretch(1)
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

    def _delete_checked(self) -> None:
        paths = self._checked_paths()
        if not paths:
            QMessageBox.information(
                self, "Nothing checked", "No copies are checked for deletion."
            )
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

        failed: list[str] = []
        deleted: list[Path] = []
        for path in paths:
            try:
                path.unlink()
            except OSError as exc:
                failed.append(f"{path.name}: {exc}")
            else:
                self.library.remove(path)
                deleted.append(path)

        self.deleted_paths.extend(deleted)
        self.groups = _drop_deleted(self.groups, deleted)
        self._populate()

        if failed:
            QMessageBox.warning(
                self,
                "Some files failed",
                f"Deleted {len(deleted)} file(s); {len(failed)} failed:\n" + "\n".join(failed),
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
