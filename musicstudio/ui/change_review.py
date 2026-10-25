"""Review the scope of a write before it is submitted to the worker queue."""

from pathlib import Path
from PySide6.QtWidgets import QMessageBox


def review(parent, title: str, paths, details: str) -> bool:
    box = QMessageBox(parent)
    box.setWindowTitle(title)
    box.setText(f"{title}: {len(paths)} file(s)")
    box.setInformativeText(details + "\n\nOriginal files will be backed up for Undo last change in Library. Backups use additional disk space.")
    box.setDetailedText("\n".join(str(Path(p)) for p in paths))
    box.setStandardButtons(QMessageBox.Apply | QMessageBox.Cancel)
    box.setDefaultButton(QMessageBox.Cancel)
    return box.exec() == QMessageBox.Apply
