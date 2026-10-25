"""Durable original-file backups for explicitly undoable operations."""

from contextlib import contextmanager
import hashlib
import json
import shutil
import threading
import time
import uuid
from pathlib import Path

from .. import config

_lock = threading.RLock()


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def _write(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def capture(paths, title: str, *, context=None):
    """Back up before writing; preserve recovery records even on partial failure.

    The caller can update entry['current'] when moving a file. Backups are
    intentionally retained until undo: automatically expiring originals would
    make the promise of recovery misleading.
    """
    with _lock:
        directory = config.DATA_DIR / "undo" / f"{time.time_ns()}-{uuid.uuid4().hex}"
        directory.mkdir(parents=True)
        record = {"title": title, "state": "preparing", "entries": []}
        manifest = directory / "record.json"
        for index, path in enumerate(dict.fromkeys(Path(p).resolve() for p in paths)):
            if context:
                context.raise_if_cancelled()
                context.progress(None, f"Backing up {path.name}")
            backup = directory / f"{index}.original"
            before = digest(path)
            shutil.copy2(path, backup)
            if digest(backup) != before or digest(path) != before:
                raise OSError(f"File changed while backing up: {path}")
            record["entries"].append({"original": str(path), "current": str(path),
                                      "backup": backup.name, "before": before})
        record["state"] = "running"
        _write(manifest, record)
        try:
            yield record["entries"]
        finally:
            for entry in record["entries"]:
                try:
                    entry["after"] = digest(Path(entry["current"]))
                except OSError:
                    entry["after"] = None
            record["state"] = "ready"
            _write(manifest, record)


def latest() -> tuple[Path, dict] | None:
    root = config.DATA_DIR / "undo"
    for path in sorted(root.glob("*/record.json"), reverse=True):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("state") == "ready" and any(
                not e.get("undone") and (e["before"] != e.get("after") or e["current"] != e["original"])
                for e in record["entries"]
            ):
                return path, record
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def undo_latest(library=None, *, expected: Path | None = None) -> tuple[list[Path], list[str]]:
    """Restore only unchanged outputs; keep conflicts and backups for review."""
    with _lock:
        item = latest()
        if item is None:
            return [], []
        manifest, record = item
        if expected is not None and manifest != expected:
            return [], ["A newer operation completed; review Undo last change again"]
        restored, failed = [], []
        for entry in record["entries"]:
            if entry.get("undone"):
                continue
            source, current = Path(entry["original"]), Path(entry["current"])
            if entry["before"] == entry.get("after") and source == current:
                continue
            temporary = source.with_name(f".{source.name}.{uuid.uuid4().hex}.undo")
            try:
                if not entry.get("after") or digest(current) != entry["after"]:
                    raise OSError("File changed after this operation; restore skipped")
                if current != source and source.exists():
                    raise OSError("Original filename is occupied; restore skipped")
                backup = manifest.parent / entry["backup"]
                if digest(backup) != entry["before"]:
                    raise OSError("Backup failed verification")
                if current != source:
                    # A rename/move changes no bytes; move back without overwriting.
                    if entry["before"] != entry["after"]:
                        raise OSError("Moved file was also modified; restore skipped")
                    shutil.move(str(current), str(source))
                    if library:
                        library.relocate(current, source)
                else:
                    shutil.copy2(backup, temporary)
                    temporary.replace(source)
                entry["undone"] = True
                _write(manifest, record)
                restored.append(source)
            except OSError as exc:
                failed.append(f"{source.name}: {exc}")
            finally:
                temporary.unlink(missing_ok=True)
        if not failed:
            record["state"] = "undone"
            _write(manifest, record)
        return restored, failed
