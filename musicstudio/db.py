"""SQLite index of the music library.

Scanning a folder of thousands of files with ffprobe takes minutes; reading
them back from here takes milliseconds. The database is a cache, never the
source of truth -- the files on disk always win, and a row whose file has
changed on disk is re-read rather than trusted.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

from .config import DB_PATH
from .core import probe
from .core import tags as tags_module
from .core.formats import IMPORTABLE_EXTENSIONS

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tracks (
    id              INTEGER PRIMARY KEY,
    path            TEXT NOT NULL UNIQUE,
    filename        TEXT NOT NULL,
    -- File identity, so we can tell when a file changed under us.
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    modified_time   REAL    NOT NULL DEFAULT 0,

    title           TEXT NOT NULL DEFAULT '',
    artist          TEXT NOT NULL DEFAULT '',
    album           TEXT NOT NULL DEFAULT '',
    albumartist     TEXT NOT NULL DEFAULT '',
    date            TEXT NOT NULL DEFAULT '',
    genre           TEXT NOT NULL DEFAULT '',
    track_number    INTEGER,
    disc_number     INTEGER,

    codec           TEXT NOT NULL DEFAULT '',
    duration        REAL NOT NULL DEFAULT 0,
    sample_rate     INTEGER NOT NULL DEFAULT 0,
    bit_depth       INTEGER NOT NULL DEFAULT 0,
    channels        INTEGER NOT NULL DEFAULT 0,
    bitrate         INTEGER NOT NULL DEFAULT 0,
    is_lossless     INTEGER NOT NULL DEFAULT 0,

    has_artwork     INTEGER NOT NULL DEFAULT 0,
    artwork_width   INTEGER NOT NULL DEFAULT 0,
    added_at        REAL NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_tracks_album  ON tracks(albumartist, album);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


@dataclass
class TrackRow:
    """One row of the library table, shaped for display."""

    id: int
    path: Path
    title: str
    artist: str
    album: str
    albumartist: str
    date: str
    genre: str
    track_number: int | None
    disc_number: int | None
    codec: str
    duration: float
    sample_rate: int
    bit_depth: int
    channels: int
    bitrate: int
    is_lossless: bool
    has_artwork: bool
    artwork_width: int

    @property
    def display_title(self) -> str:
        return self.title or self.path.stem

    @property
    def display_artist(self) -> str:
        return self.artist or self.albumartist or "—"

    @property
    def duration_label(self) -> str:
        if not self.duration:
            return "—"
        minutes, seconds = divmod(int(self.duration), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"

    @property
    def quality_label(self) -> str:
        parts = [self.codec.upper() or "?"]
        if self.sample_rate:
            parts.append(f"{self.sample_rate / 1000:g}k")
        if self.bit_depth:
            parts.append(f"{self.bit_depth}b")
        elif self.bitrate:
            parts.append(f"{round(self.bitrate / 1000)}k")
        return " ".join(parts)

    @property
    def artwork_label(self) -> str:
        if not self.has_artwork:
            return "—"
        return f"{self.artwork_width}px" if self.artwork_width else "yes"


class Library:
    """Thread-safe SQLite wrapper for the track index."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )

    # -- connection -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """One connection per thread; sqlite objects are not thread-safe."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30)
            conn.row_factory = sqlite3.Row
            # WAL lets the UI read while a background scan writes.
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # -- writing --------------------------------------------------------
    def upsert(self, path: Path, info: probe.AudioInfo, tags: tags_module.TagSet) -> int:
        path = Path(path).resolve()
        try:
            stat = path.stat()
            size, modified = stat.st_size, stat.st_mtime
        except OSError:
            size, modified = 0, 0.0

        artwork = tags.artwork
        conn = self._connect()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO tracks (
                    path, filename, size_bytes, modified_time,
                    title, artist, album, albumartist, date, genre,
                    track_number, disc_number,
                    codec, duration, sample_rate, bit_depth, channels, bitrate, is_lossless,
                    has_artwork, artwork_width
                ) VALUES (?,?,?,?, ?,?,?,?,?,?, ?,?, ?,?,?,?,?,?,?, ?,?)
                ON CONFLICT(path) DO UPDATE SET
                    filename=excluded.filename, size_bytes=excluded.size_bytes,
                    modified_time=excluded.modified_time,
                    title=excluded.title, artist=excluded.artist, album=excluded.album,
                    albumartist=excluded.albumartist, date=excluded.date, genre=excluded.genre,
                    track_number=excluded.track_number, disc_number=excluded.disc_number,
                    codec=excluded.codec, duration=excluded.duration,
                    sample_rate=excluded.sample_rate, bit_depth=excluded.bit_depth,
                    channels=excluded.channels, bitrate=excluded.bitrate,
                    is_lossless=excluded.is_lossless,
                    has_artwork=excluded.has_artwork, artwork_width=excluded.artwork_width
                """,
                (
                    str(path), path.name, size, modified,
                    tags.title, tags.artist, tags.album, tags.albumartist, tags.date, tags.genre,
                    tags.track_number, tags.disc_number,
                    info.codec, info.duration, info.sample_rate, info.bit_depth,
                    info.channels, info.bitrate, int(info.is_lossless),
                    int(tags.has_artwork()), artwork.width if artwork else 0,
                ),
            )
            return cursor.lastrowid or 0

    def remove(self, path: Path) -> None:
        conn = self._connect()
        with conn:
            conn.execute("DELETE FROM tracks WHERE path = ?", (str(Path(path).resolve()),))

    def clear(self) -> None:
        conn = self._connect()
        with conn:
            conn.execute("DELETE FROM tracks")

    def prune_missing(self) -> int:
        """Drop rows whose files are gone. Returns how many were removed."""
        removed = 0
        for row in self.all_tracks():
            if not row.path.exists():
                self.remove(row.path)
                removed += 1
        return removed

    # -- reading --------------------------------------------------------
    def _row_to_track(self, row: sqlite3.Row) -> TrackRow:
        return TrackRow(
            id=row["id"],
            path=Path(row["path"]),
            title=row["title"],
            artist=row["artist"],
            album=row["album"],
            albumartist=row["albumartist"],
            date=row["date"],
            genre=row["genre"],
            track_number=row["track_number"],
            disc_number=row["disc_number"],
            codec=row["codec"],
            duration=row["duration"],
            sample_rate=row["sample_rate"],
            bit_depth=row["bit_depth"],
            channels=row["channels"],
            bitrate=row["bitrate"],
            is_lossless=bool(row["is_lossless"]),
            has_artwork=bool(row["has_artwork"]),
            artwork_width=row["artwork_width"],
        )

    def all_tracks(self) -> list[TrackRow]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT * FROM tracks ORDER BY albumartist, album, disc_number, track_number, title"
        ).fetchall()
        return [self._row_to_track(r) for r in rows]

    def search(self, term: str) -> list[TrackRow]:
        if not term.strip():
            return self.all_tracks()
        pattern = f"%{term.strip()}%"
        conn = self._connect()
        rows = conn.execute(
            """
            SELECT * FROM tracks
            WHERE title LIKE ? OR artist LIKE ? OR album LIKE ?
               OR albumartist LIKE ? OR genre LIKE ? OR filename LIKE ?
            ORDER BY albumartist, album, disc_number, track_number, title
            """,
            (pattern,) * 6,
        ).fetchall()
        return [self._row_to_track(r) for r in rows]

    def get(self, path: Path) -> TrackRow | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT * FROM tracks WHERE path = ?", (str(Path(path).resolve()),)
        ).fetchone()
        return self._row_to_track(row) if row else None

    def count(self) -> int:
        return self._connect().execute("SELECT COUNT(*) FROM tracks").fetchone()[0]

    def needs_rescan(self, path: Path) -> bool:
        """Whether the file changed since we indexed it."""
        path = Path(path).resolve()
        conn = self._connect()
        row = conn.execute(
            "SELECT size_bytes, modified_time FROM tracks WHERE path = ?", (str(path),)
        ).fetchone()
        if row is None:
            return True
        try:
            stat = path.stat()
        except OSError:
            return True
        # A 1-second tolerance here meant an edit made in the same second as
        # the last scan was never picked up. Rescanning spuriously is cheap;
        # serving stale tags is not, so bias towards re-reading.
        return (
            stat.st_size != row["size_bytes"]
            or abs(stat.st_mtime - row["modified_time"]) > 0.001
        )

    def stats(self) -> dict:
        conn = self._connect()
        row = conn.execute(
            """
            SELECT COUNT(*) AS tracks,
                   COALESCE(SUM(duration), 0) AS duration,
                   COALESCE(SUM(size_bytes), 0) AS size,
                   COALESCE(SUM(is_lossless), 0) AS lossless,
                   COALESCE(SUM(has_artwork), 0) AS with_art
            FROM tracks
            """
        ).fetchone()
        return {
            "tracks": row["tracks"],
            "duration": row["duration"],
            "size": row["size"],
            "lossless": row["lossless"],
            "with_art": row["with_art"],
        }


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


def find_audio_files(roots: list[Path], *, recursive: bool = True) -> list[Path]:
    """Collect importable audio files under ``roots``.

    Accepts a mix of files and directories, which is what a drag-and-drop
    from Explorer actually delivers.
    """
    found: list[Path] = []
    seen: set[Path] = set()

    for root in roots:
        root = Path(root)
        if root.is_file():
            if root.suffix.lower() in IMPORTABLE_EXTENSIONS:
                resolved = root.resolve()
                if resolved not in seen:
                    seen.add(resolved)
                    found.append(resolved)
            continue
        if not root.is_dir():
            continue
        iterator = root.rglob("*") if recursive else root.glob("*")
        for path in iterator:
            if not path.is_file() or path.suffix.lower() not in IMPORTABLE_EXTENSIONS:
                continue
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                found.append(resolved)

    return sorted(found)


def scan_into_library(
    library: Library,
    paths: list[Path],
    *,
    context=None,
    force: bool = False,
) -> tuple[int, int]:
    """Index ``paths`` into ``library``. Returns ``(imported, skipped)``.

    A file that cannot be probed is skipped rather than raising -- libraries
    always contain a few corrupt or zero-byte files, and one must not stop
    the import.
    """
    files = find_audio_files(paths)
    imported = skipped = 0
    total = len(files)

    for index, path in enumerate(files):
        if context is not None:
            context.raise_if_cancelled()
            context.progress(index / total if total else None, f"Scanning {path.name}")

        if not force and not library.needs_rescan(path):
            skipped += 1
            continue

        info = probe.try_probe(path)
        if info is None:
            skipped += 1
            continue
        library.upsert(path, info, tags_module.try_read(path))
        imported += 1

    if context is not None:
        context.progress(1.0, f"Indexed {imported} file(s)")
    return imported, skipped
