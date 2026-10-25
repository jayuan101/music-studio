"""Recovery features tested with disposable files and deterministic failures."""

import json
from pathlib import Path
from unittest.mock import Mock
import zipfile

import pytest
from PySide6.QtCore import QCoreApplication

from musicstudio import config
from musicstudio.core import history, session, relink, update_install
from musicstudio.core.jobs import Job, JobQueue, JobState
from musicstudio.ui.widgets.now_playing import PlaybackQueue, RepeatMode
from musicstudio.db import Library, scan_into_library


def test_undo_partial_batch_survives_restart(tmp_path):
    a, b = tmp_path / "one.flac", tmp_path / "two.flac"
    a.write_bytes(b"original one")
    b.write_bytes(b"original two")
    with pytest.raises(RuntimeError):
        with history.capture([a, b], "Batch"):
            a.write_bytes(b"changed")
            raise RuntimeError("second file failed")
    # No in-memory operation state is required to find and undo the batch.
    restored, failed = history.undo_latest()
    assert restored == [a]
    assert not failed
    assert a.read_bytes() == b"original one"
    assert b.read_bytes() == b"original two"
    assert history.latest() is None


def test_undo_refuses_external_modification(tmp_path):
    path = tmp_path / "song"
    path.write_bytes(b"before")
    with history.capture([path], "Edit"):
        path.write_bytes(b"after")
    path.write_bytes(b"external edit")
    restored, failed = history.undo_latest()
    assert not restored and failed
    assert path.read_bytes() == b"external edit"
    assert history.latest() is not None


def test_backup_failure_prevents_mutation(tmp_path, monkeypatch):
    path = tmp_path / "song"
    path.write_bytes(b"before")
    monkeypatch.setattr(history.shutil, "copy2", Mock(side_effect=OSError("disk full")))
    entered = False
    with pytest.raises(OSError, match="disk full"):
        with history.capture([path], "Edit"):
            entered = True
    assert not entered
    assert path.read_bytes() == b"before"


def test_undo_does_not_switch_to_a_newer_operation(tmp_path):
    path = tmp_path / "song"
    path.write_bytes(b"before")
    with history.capture([path], "First"):
        path.write_bytes(b"first")
    expected, _ = history.latest()
    with history.capture([path], "Second"):
        path.write_bytes(b"second")
    restored, failed = history.undo_latest(expected=expected)
    assert not restored and failed
    assert path.read_bytes() == b"second"


def test_undo_move_preserves_playlist(tone_flac, tmp_path):
    from musicstudio.core.library_ops import move_files
    library = Library(tmp_path / "library.db")
    scan_into_library(library, [tone_flac])
    playlist = library.create_playlist("Mix")
    library.add_to_playlist(playlist, [tone_flac])
    result = move_files(library, [tone_flac], tmp_path / "moved")
    assert len(result.moved) == 1
    restored, failed = history.undo_latest(library)
    assert not failed and restored == [tone_flac]
    assert Path(library.playlist_tracks(playlist)[0].path) == tone_flac


def test_session_atomic_failure_keeps_previous(tmp_path, monkeypatch):
    session.save({"old": True})
    real_replace = Path.replace
    def fail(path, target):
        if path.name == "playback-session.tmp":
            raise OSError("locked")
        return real_replace(path, target)
    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError):
        session.save({"new": True})
    assert session.load() == {"old": True}


def test_corrupt_session_is_ignored():
    config.DATA_DIR.mkdir(parents=True)
    (config.DATA_DIR / "playback-session.json").write_text("{broken")
    assert session.load() == {}


def test_queue_restore_preserves_duplicates_order_and_modes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.touch()
    b.touch()
    queue = PlaybackQueue()
    state = {"tracks": [str(a), str(b), str(a)], "order": [2, 0, 1],
             "position": 1, "repeat": "all", "shuffle": True}
    assert queue.restore(state)
    assert queue.snapshot() == state
    assert queue.current == a
    assert queue.next() == b
    assert queue.next() == a


def test_queue_restore_skips_missing_file(tmp_path):
    present = tmp_path / "present"
    present.touch()
    queue = PlaybackQueue()
    assert queue.restore({"tracks": [str(tmp_path / "missing"), str(present)],
                          "order": [0, 1], "position": 0})
    assert queue.current == present
    assert queue.repeat == RepeatMode.OFF


@pytest.mark.parametrize("state", [{}, {"tracks": None},
    {"tracks": ["x"], "order": [4], "position": 0},
    {"tracks": ["x"], "order": [False], "position": 0}])
def test_invalid_queue_state_is_ignored(state):
    assert not PlaybackQueue().restore(state)


def test_relink_keeps_playlist(tone_flac, tmp_path):
    library = Library(tmp_path / "library.db")
    scan_into_library(library, [tone_flac])
    playlist = library.create_playlist("Mix")
    library.add_to_playlist(playlist, [tone_flac])
    folder = tmp_path / "new-location"
    folder.mkdir()
    new = folder / tone_flac.name
    tone_flac.rename(new)
    proposals, unresolved = relink.find_candidates(library, folder)
    assert proposals == [(tone_flac, new)] and not unresolved
    assert relink.apply(library, proposals) == ([new], [])
    assert Path(library.playlist_tracks(playlist)[0].path) == new


def test_relink_ambiguous_matches_are_not_selected(tone_flac, tmp_path):
    import shutil
    library = Library(tmp_path / "library.db")
    scan_into_library(library, [tone_flac])
    folder = tmp_path / "new"
    for sub in ("one", "two"):
        dest = folder / sub / tone_flac.name
        dest.parent.mkdir(parents=True)
        shutil.copy2(tone_flac, dest)
    tone_flac.unlink()
    proposals, unresolved = relink.find_candidates(library, folder)
    assert not proposals and unresolved == [tone_flac]


def test_retry_is_explicit_and_only_once():
    app = QCoreApplication.instance() or QCoreApplication([])
    queue = JobQueue()
    job = Job("failed", lambda ctx: None)
    job.state = JobState.FAILED
    job.retry_action = Mock()
    queue._jobs[job.id] = job
    queue._order.append(job.id)
    queue.retry(job.id)
    queue.retry(job.id)
    job.retry_action.assert_called_once()


def test_batch_retry_excludes_successes():
    from types import SimpleNamespace
    from musicstudio.ui.main_window import MainWindow
    queue = JobQueue()
    job = Job("batch", lambda ctx: None)
    job.state = JobState.SUCCEEDED
    queue._jobs[job.id] = job
    action = Mock()
    results = [SimpleNamespace(path=Path("good"), updated=True, reason="Done"),
               SimpleNamespace(path=Path("failed"), updated=False, reason="Failed: offline"),
               SimpleNamespace(path=Path("skip"), updated=False, reason="Nothing missing")]
    MainWindow._offer_batch_retry(SimpleNamespace(jobs=queue), job.id, "succeeded", results, action)
    queue.retry(job.id)
    action.assert_called_once_with([Path("failed")])


@pytest.mark.parametrize("invalid", ["../escape", "C:/escape", "MusicStudio.exe:stream", "folder./x"])
def test_update_rejects_unsafe_archives(tmp_path, invalid):
    path = tmp_path / "update.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("MusicStudio.exe", b"MZstub")
        archive.writestr("_internal/runtime.dll", b"runtime")
        archive.writestr(invalid, b"bad")
    with pytest.raises(ValueError):
        update_install.extract_package(path, tmp_path / "extracted")
    assert not (tmp_path / "extracted").exists()


@pytest.mark.parametrize("entries", [{}, {"MusicStudio.exe": b"MZ"},
    {"MusicStudio.exe": b"not exe", "_internal/runtime.dll": b"runtime"}])
def test_update_rejects_incomplete_package(tmp_path, entries):
    path = tmp_path / "update.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    with pytest.raises(ValueError):
        update_install.extract_package(path, tmp_path / "extracted")


def test_update_accepts_package_layout(tmp_path):
    path = tmp_path / "update.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("MusicStudio.exe", b"MZstub")
        archive.writestr("_internal/runtime.dll", b"runtime")
    folder = update_install.extract_package(path, tmp_path / "extracted")
    assert (folder / "MusicStudio.exe").read_bytes() == b"MZstub"
