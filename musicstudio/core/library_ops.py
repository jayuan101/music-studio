"""File-system operations on library tracks that also need the index kept in
sync.

Both the duplicates dialog and the library view's own "Delete…" action need
exactly the same delete-and-deindex behaviour; this is the one place that
defines what "delete" and "move" mean, so the two callers cannot drift apart.
"""

from __future__ import annotations

import shutil
import sys
import time
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
from . import crash_log
from . import organise
from . import probe
from . import tags as tags_module
from .convert import unique_destination


@dataclass(frozen=True)
class TrashResult:
    trashed: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)


#: A file that was just added or modified (a fresh download, a just-completed
#: conversion) is briefly held open by Windows Search Indexer or antivirus
#: real-time scanning -- confirmed via debug.log as the actual cause of a
#: "file in use by another process" (WinError 32) delete failure this
#: session. That lock is transient, typically gone within a second or two,
#: so a short retry absorbs it instead of surfacing a failure for something
#: that would succeed if tried again a moment later.
_TRASH_RETRIES = 5
_TRASH_RETRY_DELAY_S = 0.5


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
        last_exc: Exception | None = None
        for attempt in range(_TRASH_RETRIES):
            try:
                send2trash(str(path))
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 -- one bad file must not abort the whole delete
                last_exc = exc
                # TEMPORARY: remove once the retry is confirmed to actually
                # absorb the transient lock in practice, not just in theory.
                crash_log.debug(f"send_to_trash: attempt {attempt + 1} failed for {path.name}: {exc!r}")
                if attempt < _TRASH_RETRIES - 1:
                    time.sleep(_TRASH_RETRY_DELAY_S)
        if last_exc is not None:
            failed.append((path, str(last_exc)))
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


@dataclass(frozen=True)
class RenameResult:
    renamed: list[tuple[Path, Path]] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)


def rename_to_tags(
    library: Library, paths: list[Path], *, pattern: str = "{artist} - {title}"
) -> RenameResult:
    """Rename each file, in its current folder, to match its own tags.

    Exists for the gap between "the library shows the right title/artist"
    and "the file on disk is still called whatever the downloader named
    it" -- tags.py and organise.py already agree on how a name is built and
    parsed, this just applies that to a file already sitting in the
    library rather than a fresh download. Only the filename changes, never
    the folder, so this never turns into an unrequested reorganisation.
    """
    renamed: list[tuple[Path, Path]] = []
    unchanged: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for path in paths:
        path = Path(path)
        try:
            tags = tags_module.try_read(path)
            rendered = organise.render_template(pattern, tags)
        except (tags_module.TagError, organise.TemplateError, OSError) as exc:
            failed.append((path, str(exc)))
            continue
        new_name = organise.sanitise_component(rendered) + path.suffix
        if new_name == path.name:
            unchanged.append(path)
            continue
        target = unique_destination(path.with_name(new_name))
        try:
            path.rename(target)
        except OSError as exc:
            failed.append((path, str(exc)))
            continue
        library.remove(path)
        info = probe.try_probe(target)
        if info is not None:
            library.upsert(target, info, tags_module.try_read(target))
        renamed.append((path, target))
    return RenameResult(renamed=renamed, unchanged=unchanged, failed=failed)
