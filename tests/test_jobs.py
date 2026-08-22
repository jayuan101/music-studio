"""The job queue's retention and concurrency guarantees.

These are not tests of what a job *does* -- they are tests of the two
properties that, when they were missing, made the whole app appear to freeze:
that finished jobs do not accumulate for the life of the session, and that the
pool has enough room that one wedged job does not stop everything else.
"""

from __future__ import annotations

import pytest
from PySide6.QtCore import QCoreApplication

from musicstudio.core.jobs import MAX_FINISHED_JOBS, JobQueue


@pytest.fixture(scope="module")
def qt_app() -> QCoreApplication:
    """A Qt event loop for the queued signals the job system delivers on.

    Module-scoped and reused rather than recreated, because a QCoreApplication
    is a process-wide singleton -- and no QApplication, since nothing here
    needs a GUI.
    """
    return QCoreApplication.instance() or QCoreApplication([])


def _drain(queue: JobQueue, app: QCoreApplication) -> None:
    """Let every submitted job finish and its signals be delivered."""
    queue.wait_for_done(10_000)
    # Job completion reaches the queue through queued signals, so the event
    # loop has to turn before the retention trim has actually run.
    for _ in range(5):
        app.processEvents()


def test_finished_jobs_do_not_accumulate_without_bound(qt_app):
    """Retention is capped.

    Until this was, ``clear_finished()`` was the only thing that ever removed
    a job, and it only ran when someone clicked "Clear finished" -- so every
    job held its whole result for the session, inside a reference cycle the
    app no longer collects (the cyclic GC is disabled outright).
    """
    queue = JobQueue(max_concurrent=4)
    for index in range(MAX_FINISHED_JOBS + 25):
        queue.submit_func(f"job {index}", lambda _ctx, i=index: i)
    _drain(queue, qt_app)

    assert len(queue.jobs()) <= MAX_FINISHED_JOBS


def test_the_newest_finished_jobs_are_the_ones_kept(qt_app):
    """Trimming drops the oldest, so the dock still shows recent history."""
    queue = JobQueue(max_concurrent=4)
    total = MAX_FINISHED_JOBS + 10
    for index in range(total):
        queue.submit_func(f"job {index}", lambda _ctx, i=index: i)
    _drain(queue, qt_app)

    kept = {job.title for job in queue.jobs()}
    assert f"job {total - 1}" in kept
    assert "job 0" not in kept


def test_a_running_job_is_never_trimmed(qt_app):
    """Only finished jobs are eligible; a live one must not vanish."""
    import threading

    release = threading.Event()
    queue = JobQueue(max_concurrent=4)
    running = queue.submit_func("long one", lambda _ctx: release.wait(10))
    for index in range(MAX_FINISHED_JOBS + 10):
        queue.submit_func(f"job {index}", lambda _ctx, i=index: i)
    for _ in range(5):
        qt_app.processEvents()

    assert queue.job(running.id) is not None
    release.set()
    _drain(queue, qt_app)


def test_pool_has_room_for_more_than_one_stuck_job(qt_app):
    """Concurrency headroom is the difference between a stall and a deadlock.

    With only two slots, two jobs blocking meant every later action in every
    panel queued forever -- which is what a user experiences as the app having
    frozen permanently rather than being briefly busy.
    """
    from musicstudio.ui.main_window import _job_concurrency

    assert _job_concurrency() >= 4
