"""Shared delete/move operations used by the duplicates dialog and library view."""

from __future__ import annotations

from musicstudio.core import library_ops
from musicstudio.core import tags as T
from musicstudio.db import Library, scan_into_library

from .conftest import make_tone, requires_ffmpeg

pytestmark = requires_ffmpeg


def test_delete_files_permanently_removes_file_and_row(tmp_path):
    library = Library(tmp_path / "test.db")
    track = make_tone(tmp_path / "a.flac", duration=1.0)
    scan_into_library(library, [track])
    assert library.get(track) is not None

    result = library_ops.delete_files_permanently(library, [track])

    assert result.deleted == [track]
    assert not result.failed
    assert not track.exists()
    assert library.get(track) is None


def test_delete_files_permanently_reports_failures_without_raising(tmp_path):
    library = Library(tmp_path / "test.db")
    missing = tmp_path / "does_not_exist.flac"

    result = library_ops.delete_files_permanently(library, [missing])

    assert result.deleted == []
    assert len(result.failed) == 1
    assert result.failed[0][0] == missing


def test_move_files_relocates_and_reindexes(tmp_path):
    library = Library(tmp_path / "test.db")
    track = make_tone(tmp_path / "source" / "a.flac", duration=1.0)
    T.write(track, T.TagSet(title="Moved Song", artist="Band"))
    scan_into_library(library, [track])

    destination_dir = tmp_path / "destination"
    result = library_ops.move_files(library, [track], destination_dir)

    assert len(result.moved) == 1
    old_path, new_path = result.moved[0]
    assert old_path == track
    assert new_path.parent == destination_dir
    assert new_path.exists()
    assert not old_path.exists()
    assert library.get(old_path) is None
    row = library.get(new_path)
    assert row is not None
    assert row.title == "Moved Song"


def test_move_files_avoids_a_name_collision(tmp_path):
    library = Library(tmp_path / "test.db")
    track = make_tone(tmp_path / "source" / "a.flac", duration=1.0)
    scan_into_library(library, [track])

    destination_dir = tmp_path / "destination"
    destination_dir.mkdir()
    (destination_dir / "a.flac").write_bytes(b"already here")

    result = library_ops.move_files(library, [track], destination_dir)

    assert len(result.moved) == 1
    _old, new_path = result.moved[0]
    assert new_path.name == "a (2).flac"


def test_move_files_reports_failures_without_raising(tmp_path):
    library = Library(tmp_path / "test.db")
    missing = tmp_path / "does_not_exist.flac"

    result = library_ops.move_files(library, [missing], tmp_path / "destination")

    assert result.moved == []
    assert len(result.failed) == 1
