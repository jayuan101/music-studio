"""Find missing-track candidates without deleting or moving any audio."""

from collections import defaultdict
from pathlib import Path

from . import probe, tags


def find_candidates(library, folder: Path, context=None):
    missing = [row for row in library.all_tracks() if not Path(row.path).exists()]
    names = {Path(row.path).name.casefold() for row in missing}
    indexed = {str(Path(row.path).resolve()).casefold() for row in library.all_tracks()}
    candidates = defaultdict(list)
    for path in Path(folder).rglob("*"):
        if context:
            context.raise_if_cancelled()
        if path.name.casefold() in names and path.is_file() and str(path.resolve()).casefold() not in indexed:
            candidates[path.name.casefold()].append(path)
    proposals, unresolved = [], []
    used = set()
    for row in missing:
        matches = []
        for path in candidates[Path(row.path).name.casefold()]:
            if path.stat().st_size != row.size_bytes:
                continue
            info = probe.try_probe(path)
            if info and abs(info.duration - row.duration) <= 0.5 and info.codec == row.codec:
                matches.append(path)
        if len(matches) == 1 and matches[0] not in used:
            proposals.append((Path(row.path), matches[0]))
            used.add(matches[0])
        else:
            unresolved.append(Path(row.path))
    return proposals, unresolved


def apply(library, proposals, context=None):
    repaired, failed = [], []
    for old, new in proposals:
        if context:
            context.raise_if_cancelled()
        try:
            if old.exists() or not new.is_file() or library.get(new) is not None:
                raise OSError("Paths changed since the scan; rescan before relinking")
            info = probe.try_probe(new)
            if info is None:
                raise OSError("Replacement is not readable audio")
            if not library.relocate(old, new):
                raise OSError("Library entry no longer exists")
            library.upsert(new, info, tags.try_read(new))
            repaired.append(new)
        except OSError as exc:
            failed.append(f"{old.name}: {exc}")
    return repaired, failed
