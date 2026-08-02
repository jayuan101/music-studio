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
from .core import convert as convert_module
from .core import ffmpeg
from .core import probe
from .core import tags as tags_module
from .core.convert import ConvertRequest, unique_destination
from .core.formats import FLAC, IMPORTABLE_EXTENSIONS, NORMALIZE_ON_IMPORT_EXTENSIONS

SCHEMA_VERSION = 2

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

CREATE TABLE IF NOT EXISTS ignored_duplicate_groups (
    artist_key TEXT NOT NULL,
    title_key  TEXT NOT NULL,
    ignored_at REAL NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (artist_key, title_key)
);
"""

#: Statements to run when upgrading from schema version (key - 1) to key.
#: Applied in order inside _migrate(); a rerun tolerates "duplicate column"
#: since ALTER TABLE ADD COLUMN has no IF NOT EXISTS form in SQLite.
_MIGRATIONS: dict[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE tracks ADD COLUMN source_url TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE tracks ADD COLUMN auto_trim_state TEXT NOT NULL DEFAULT 'not_applicable'",
        "CREATE INDEX IF NOT EXISTS idx_tracks_autotrim ON tracks(auto_trim_state)",
    ),
}


def _migrate(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    current = int(row["value"]) if row else 0
    for version in range(current + 1, SCHEMA_VERSION + 1):
        for statement in _MIGRATIONS.get(version, ()):
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if "duplicate column" not in str(exc).lower():
                    raise
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )


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
    size_bytes: int = 0
    added_at: float = 0.0
    source_url: str = ""
    auto_trim_state: str = "not_applicable"

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


#: Columns query_tracks() is allowed to sort by. SQL cannot parameterize a
#: column name, so this allowlist is what stands between a tool-supplied
#: sort field and a syntax error (or worse) -- never interpolate order_by
#: without checking it against this set first.
_SORTABLE_COLUMNS = frozenset(
    {
        "title", "artist", "album", "albumartist", "date", "genre",
        "duration", "sample_rate", "bit_depth", "bitrate", "size_bytes",
        "added_at", "path",
    }
)


@dataclass
class TrackFilter:
    """A structured library query.

    ``Library.search()`` is one ``LIKE`` string match, which cannot answer the
    kind of question a natural-language assistant gets asked -- "which tracks
    are lossy", "under 192kbps", "missing artwork", "everything under this
    folder". Every field here is optional; unset fields are not filtered on.
    All values are bound as SQL parameters, never interpolated -- a tool
    argument from a model is untrusted input in exactly the sense any other
    user input is.
    """

    is_lossless: bool | None = None
    codec: str | None = None
    min_bitrate: int | None = None
    max_bitrate: int | None = None
    min_sample_rate: int | None = None
    has_artwork: bool | None = None
    artist_contains: str | None = None
    album_contains: str | None = None
    genre_contains: str | None = None
    title_contains: str | None = None
    #: Restrict to files under this path (as a prefix match on the resolved,
    #: absolute path string).
    path_prefix: str | None = None
    #: One of _SORTABLE_COLUMNS, optionally prefixed with "-" for descending.
    order_by: str = "albumartist"
    limit: int | None = None

    def to_sql(self) -> tuple[str, list]:
        """Build a parameterized WHERE clause and ORDER BY, plus its bindings."""
        clauses: list[str] = []
        params: list = []

        if self.is_lossless is not None:
            clauses.append("is_lossless = ?")
            params.append(int(self.is_lossless))
        if self.codec:
            clauses.append("codec = ?")
            params.append(self.codec.lower())
        if self.min_bitrate is not None:
            clauses.append("bitrate >= ?")
            params.append(self.min_bitrate)
        if self.max_bitrate is not None:
            clauses.append("bitrate <= ?")
            params.append(self.max_bitrate)
        if self.min_sample_rate is not None:
            clauses.append("sample_rate >= ?")
            params.append(self.min_sample_rate)
        if self.has_artwork is not None:
            clauses.append("has_artwork = ?")
            params.append(int(self.has_artwork))
        for field, column in (
            ("artist_contains", "artist"),
            ("album_contains", "album"),
            ("genre_contains", "genre"),
            ("title_contains", "title"),
        ):
            value = getattr(self, field)
            if value:
                clauses.append(f"{column} LIKE ?")
                params.append(f"%{value}%")
        if self.path_prefix:
            clauses.append("path LIKE ?")
            params.append(f"{self.path_prefix}%")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        order_field = self.order_by or "albumartist"
        descending = order_field.startswith("-")
        column = order_field[1:] if descending else order_field
        if column not in _SORTABLE_COLUMNS:
            raise ValueError(
                f"Cannot sort by {column!r}. Available: {', '.join(sorted(_SORTABLE_COLUMNS))}"
            )
        order = f"ORDER BY {column} {'DESC' if descending else 'ASC'}"

        limit = f"LIMIT {int(self.limit)}" if self.limit else ""

        return f"{where} {order} {limit}".strip(), params


def _normalized_key(text: str) -> str:
    """Casefold and collapse whitespace, so "The Weeknd" and "the  weeknd "
    are recognised as the same artist/title when grouping duplicates."""
    return " ".join(text.split()).casefold()


def _quality_sort_key(t: "TrackRow") -> tuple:
    return (
        not t.is_lossless,
        not t.has_artwork,
        -(t.bit_depth or 0),
        -(t.sample_rate or 0),
        -(t.bitrate or 0),
        -(t.size_bytes or 0),
    )


def _newest_sort_key(t: "TrackRow") -> tuple:
    return (-(t.added_at or 0),)


def _oldest_sort_key(t: "TrackRow") -> tuple:
    return (t.added_at or 0,)


_KEEP_CRITERIA = {
    "quality": _quality_sort_key,
    "newest": _newest_sort_key,
    "oldest": _oldest_sort_key,
}


def sort_key_for_criterion(criterion: str):
    """The sort key find_duplicates() uses to pick each group's keeper for
    ``criterion`` ("quality" / "newest" / "oldest"), exposed so the duplicates
    dialog can re-sort an already-fetched group in place when the user
    changes the auto-select dropdown, without re-querying the database."""
    return _KEEP_CRITERIA.get(criterion, _quality_sort_key)


@dataclass
class DuplicateGroup:
    """Two or more indexed tracks that appear to be the same song.

    ``tracks`` is sorted best-quality-first (lossless, then bit depth, sample
    rate, bitrate, file size) so callers can default to recommending every
    entry after the first for deletion.
    """

    artist: str
    title: str
    tracks: list[TrackRow]

    @property
    def count(self) -> int:
        return len(self.tracks)

    @property
    def redundant_tracks(self) -> list[TrackRow]:
        """Every copy except the recommended keeper."""
        return self.tracks[1:]

    @property
    def redundant_size(self) -> int:
        """Disk space the redundant copies take up."""
        return sum(t.size_bytes for t in self.redundant_tracks)


class Library:
    """Thread-safe SQLite wrapper for the track index."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            _migrate(conn)

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
                    has_artwork, artwork_width, source_url
                ) VALUES (?,?,?,?, ?,?,?,?,?,?, ?,?, ?,?,?,?,?,?,?, ?,?,?)
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
                    has_artwork=excluded.has_artwork, artwork_width=excluded.artwork_width,
                    source_url=excluded.source_url
                """,
                (
                    str(path), path.name, size, modified,
                    tags.title, tags.artist, tags.album, tags.albumartist, tags.date, tags.genre,
                    tags.track_number, tags.disc_number,
                    info.codec, info.duration, info.sample_rate, info.bit_depth,
                    info.channels, info.bitrate, int(info.is_lossless),
                    int(tags.has_artwork()), artwork.width if artwork else 0, tags.source_url,
                ),
            )
            return cursor.lastrowid or 0

    def set_auto_trim_state(self, path: Path, state: str) -> None:
        """Persist auto-trim's verdict for one track without touching anything else.

        Deliberately not part of upsert()'s ON CONFLICT clause -- a rescan
        must never reset a track's trim state back to 'not_applicable'.
        """
        conn = self._connect()
        with conn:
            conn.execute(
                "UPDATE tracks SET auto_trim_state = ? WHERE path = ?",
                (state, str(Path(path).resolve())),
            )

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
            size_bytes=row["size_bytes"],
            added_at=row["added_at"],
            source_url=row["source_url"],
            auto_trim_state=row["auto_trim_state"],
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

    def query_tracks(self, filters: TrackFilter) -> list[TrackRow]:
        """Structured query for callers that need more than a text search --
        the assistant's ``search_library`` tool is the reason this exists."""
        where_order_limit, params = filters.to_sql()
        conn = self._connect()
        rows = conn.execute(f"SELECT * FROM tracks {where_order_limit}", params).fetchall()
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

    def find_duplicates(
        self, *, keep_criterion: str = "quality", include_ignored: bool = False
    ) -> list[DuplicateGroup]:
        """Group indexed tracks that appear to be the same song.

        Grouped by normalized (artist, title) since that is what this app can
        itself create duplicates of -- a song downloaded twice, or converted
        to a new format alongside the original rather than in place. Tracks
        with a blank artist or title are skipped rather than grouped under
        an empty key, which would otherwise lump every untagged file
        together as one giant false "duplicate". This will not catch two
        files with the same audio under different tags -- that would need
        decoding and comparing the audio itself, which this does not attempt.

        ``keep_criterion`` picks which copy sorts first (the recommended
        keeper) within each group -- see sort_key_for_criterion(). Groups the
        user has previously marked "ignore" are left out unless
        ``include_ignored`` is set.
        """
        ignored = self.ignored_duplicate_groups() if not include_ignored else set()
        sort_key = sort_key_for_criterion(keep_criterion)

        groups: dict[tuple[str, str], list[TrackRow]] = {}
        for track in self.all_tracks():
            artist_key = _normalized_key(track.artist or track.albumartist)
            title_key = _normalized_key(track.title)
            if not artist_key or not title_key:
                continue
            if (artist_key, title_key) in ignored:
                continue
            groups.setdefault((artist_key, title_key), []).append(track)

        result: list[DuplicateGroup] = []
        for tracks in groups.values():
            if len(tracks) < 2:
                continue
            tracks.sort(key=sort_key)
            result.append(
                DuplicateGroup(artist=tracks[0].artist, title=tracks[0].title, tracks=tracks)
            )

        result.sort(key=lambda g: (_normalized_key(g.artist), _normalized_key(g.title)))
        return result

    def ignore_duplicate_group(self, artist: str, title: str) -> None:
        """Mark a (artist, title) duplicate group so find_duplicates() stops
        surfacing it -- e.g. two genuinely different live/studio takes that
        happen to share a title."""
        conn = self._connect()
        with conn:
            conn.execute(
                "INSERT OR IGNORE INTO ignored_duplicate_groups(artist_key, title_key) VALUES (?, ?)",
                (_normalized_key(artist), _normalized_key(title)),
            )

    def ignored_duplicate_groups(self) -> set[tuple[str, str]]:
        conn = self._connect()
        rows = conn.execute(
            "SELECT artist_key, title_key FROM ignored_duplicate_groups"
        ).fetchall()
        return {(r["artist_key"], r["title_key"]) for r in rows}

    def autotrim_candidates(self) -> list[TrackRow]:
        """Indexed tracks that look video-sourced and have not already been
        trimmed or explicitly skipped -- the working set for a bulk
        "Auto-trim all" pass."""
        from .core.autotrim import looks_like_video_source

        return [
            row
            for row in self.all_tracks()
            if row.auto_trim_state not in ("applied", "skipped")
            and looks_like_video_source(
                source_url=row.source_url, title=row.title, filename=row.path.name
            )
        ]


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


def _normalize_to_flac(path: Path, info: probe.AudioInfo) -> Path:
    """Transcode a WebM/WebA source to FLAC and remove the original.

    Called for every file found in NORMALIZE_ON_IMPORT_EXTENSIONS so the
    library only ever holds proper music file extensions -- a raw video-site
    container kept from a download otherwise sits there indefinitely,
    indistinguishable from any other "not really music" file. Raises on
    failure (a bad/incomplete webm, or ffmpeg rejecting it), leaving the
    original file on disk untouched.

    WebM downloads from a video site carry no tags mutagen can read at all
    (mutagen does not recognise the container), so a straight copy_tags()
    would always be a no-op -- instead this fills in title/artist from the
    filename and, when still missing, album/year/genre from an online
    lookup, the same as the "Fix metadata" button does. A network failure
    here must never fail the conversion itself: worst case the track is
    imported with only what the filename gave it, same as any other
    unrecognised-tag file.
    """
    from .core import tag_fix as tag_fix_module

    destination = unique_destination(path.with_suffix(".flac"))
    request = ConvertRequest(source=path, destination=destination, profile=FLAC, overwrite=True)
    convert_module.convert(request, info=info)

    # The original is only ever deleted once the FLAC has been independently
    # verified to hold the same audio -- a conversion that silently produced
    # a truncated or empty file must never cost the user their only copy.
    result_info = probe.try_probe(destination)
    if result_info is None or abs(result_info.duration - info.duration) > 1.0:
        destination.unlink(missing_ok=True)
        raise ValueError(f"Converted duration did not match source for {path.name}")

    try:
        fixed = tag_fix_module.fix_file_tags(destination)
        tags_module.write(destination, fixed)
    except Exception:  # noqa: BLE001 -- a metadata guess failing must not lose the audio
        pass
    path.unlink()
    return destination


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
    the import. A file in NORMALIZE_ON_IMPORT_EXTENSIONS (currently just
    WebM/WebA) is transcoded to FLAC and the original deleted before being
    indexed, rather than indexed under its original extension.
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

        if path.suffix.lower() in NORMALIZE_ON_IMPORT_EXTENSIONS:
            try:
                path = _normalize_to_flac(path, info)
                info = probe.try_probe(path)
            except (OSError, ValueError, ffmpeg.FFmpegError, ffmpeg.FFmpegNotFound):
                skipped += 1
                continue
            if info is None:
                skipped += 1
                continue

        library.upsert(path, info, tags_module.try_read(path))
        imported += 1

    if context is not None:
        context.progress(1.0, f"Indexed {imported} file(s)")
    return imported, skipped
