"""File-system operations on library tracks that also need the index kept in
sync.

Both the duplicates dialog and the library view's own "Delete…" action need
exactly the same delete-and-deindex behaviour; this is the one place that
defines what "delete" and "move" mean, so the two callers cannot drift apart.
"""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.platform == "win32":
    # send2trash's default Windows path prefers pywin32's IFileOperation
    # (send2trash.win.modern), falling back to plain SHFileOperationW only if
    # pywin32 is missing. pywin32/COM is notoriously unreliable to freeze
    # correctly with PyInstaller (missing typelib/gen_py data at runtime,
    # even when the import itself succeeds at build time) -- and a COM
    # failure at call time raises something that is not an OSError, which
    # would slip past the except clause below and silently kill the delete
    # action. Going straight to the ctypes-based SHFileOperationW backend
    # sidesteps that whole class of failure; it needs nothing beyond
    # shell32.dll, which every Windows install already has.
    from send2trash.win.legacy import send2trash
else:
    from send2trash import send2trash

from ..db import Library
from . import probe
from . import tags as tags_module
from .convert import unique_destination


@dataclass(frozen=True)
class TrashResult:
    trashed: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)


def send_to_trash(library: Library, paths: list[Path]) -> TrashResult:
    """Move each path to the OS Recycle Bin / Trash and drop its row from
    the index.

    Unlike a hard delete, this can always be undone by the user restoring
    the file from the Recycle Bin -- deliberately the only "delete" this app
    offers, so a mis-click never means an unrecoverable loss.
    """
    trashed: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for path in paths:
        path = Path(path)
        try:
            send2trash(str(path))
        except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the whole delete
            failed.append((path, str(exc)))
        else:
            library.remove(path)
            trashed.append(path)
    return TrashResult(trashed=trashed, failed=failed)


@dataclass(frozen=True)
class MoveResult:
    moved: list[tuple[Path, Path]] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)


def move_files(library: Library, paths: list[Path], destination_dir: Path) -> MoveResult:
    """Move each path into ``destination_dir`` and re-point its index row.

    A name collision at the destination is resolved with convert.py's
    existing " (2)", " (3)"... scheme, so a move never silently overwrites.
    """
    destination_dir = Path(destination_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    failed: list[tuple[Path, str]] = []
    for path in paths:
        path = Path(path)
        target = unique_destination(destination_dir / path.name)
        try:
            shutil.move(str(path), str(target))
        except OSError as exc:
            failed.append((path, str(exc)))
            continue
        library.remove(path)
        info = probe.try_probe(target)
        if info is not None:
            library.upsert(target, info, tags_module.try_read(target))
        moved.append((path, target))
    return MoveResult(moved=moved, failed=failed)
