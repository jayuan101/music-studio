"""PlaybackQueue: Play next / Add to queue, with shuffle and repeat."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtCore import QCoreApplication

from musicstudio.ui.widgets.now_playing import PlaybackQueue, RepeatMode


@pytest.fixture(scope="module")
def qapp():
    return QCoreApplication.instance() or QCoreApplication([])


def songs(*names: str) -> list[Path]:
    return [Path(n) for n in names]


def play_order(queue: PlaybackQueue) -> list[str]:
    """Walk from the current song to the end (repeat off), by name."""
    order = [queue.current.name]
    while queue.next() is not None:
        order.append(queue.current.name)
    return order


@pytest.fixture
def queue(qapp):
    return PlaybackQueue()


def test_insert_next_follows_the_current_song(queue):
    queue.set_tracks(songs("a", "b", "c", "d"), 1)
    queue.insert_next(songs("x", "y"))
    assert play_order(queue) == ["b", "x", "y", "c", "d"]


def test_insert_next_twice_puts_the_latest_first(queue):
    queue.set_tracks(songs("a", "b", "c"), 0)
    queue.insert_next(songs("x"))
    queue.insert_next(songs("y"))
    assert play_order(queue) == ["a", "y", "x", "b", "c"]


def test_append_goes_to_the_end(queue):
    queue.set_tracks(songs("a", "b"), 0)
    queue.append(songs("x", "y"))
    assert play_order(queue) == ["a", "b", "x", "y"]


def test_inserting_never_restarts_the_current_song(queue):
    queue.set_tracks(songs("a", "b"), 0)
    changes: list = []
    queue.current_changed.connect(changes.append)
    announced: list = []
    queue.queue_changed.connect(lambda: announced.append(1))
    queue.insert_next(songs("x"))
    queue.append(songs("y"))
    assert changes == []
    assert len(announced) == 2
    assert queue.current.name == "a"


def test_empty_queue_starts_playing(queue):
    changes: list = []
    queue.current_changed.connect(changes.append)
    queue.insert_next(songs("x", "y"))
    assert [c.name for c in changes] == ["x"]
    queue.clear()
    changes.clear()
    queue.append(songs("p"))
    assert [c.name for c in changes] == ["p"]


def test_nothing_to_add_is_a_no_op(queue):
    queue.set_tracks(songs("a"), 0)
    announced: list = []
    queue.queue_changed.connect(lambda: announced.append(1))
    queue.insert_next([])
    queue.append([])
    assert announced == []


def test_upcoming_count_includes_additions(queue):
    queue.set_tracks(songs("a", "b"), 0)
    assert queue.upcoming_count == 1
    queue.insert_next(songs("x"))
    queue.append(songs("y"))
    assert queue.upcoming_count == 3


def test_play_next_survives_shuffle_on_and_off(queue):
    queue.set_tracks(songs(*"abcdefgh"), 2)
    queue.set_shuffle(True)
    queue.insert_next(songs("x", "y"))
    # With shuffle on, they come straight after the current song...
    assert queue.current.name == "c"
    assert [queue.next().name, queue.next().name] == ["x", "y"]
    # ...and turning shuffle off keeps them beside it, not dumped at the end.
    queue.set_shuffle(False)
    assert queue.current.name == "y"
    assert play_order(queue) == ["y", "d", "e", "f", "g", "h"]


def test_shuffle_off_after_append_keeps_everything(queue):
    queue.set_tracks(songs(*"abcd"), 0)
    queue.set_shuffle(True)
    queue.append(songs("x"))
    queue.set_shuffle(False)
    assert queue.upcoming_count == 4
    names = [queue.current.name]
    while queue.next() is not None:
        names.append(queue.current.name)
    assert sorted(names) == sorted("abcdx")


def test_repeat_all_wraps_through_additions(queue):
    queue.set_tracks(songs("a", "b"), 1)
    queue.repeat = RepeatMode.ALL
    queue.append(songs("x"))
    assert queue.next().name == "x"
    assert queue.next().name == "a"
