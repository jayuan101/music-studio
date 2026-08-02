"""Background job queue.

Every ffmpeg run and every network call goes through here so the UI thread
never blocks. Jobs report progress, can be cancelled individually, and survive
failure without taking the batch down with them.

The queue is deliberately Qt-aware but the :class:`Job` protocol is not, so the
core engine stays testable without a QApplication.
"""

from __future__ import annotations

import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot

from .ffmpeg import CancelledError


class JobState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_finished(self) -> bool:
        return self in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED)


@dataclass
class JobContext:
    """Handed to the job function so it can report progress and check for cancel.

    A job that ignores ``cancelled`` still gets terminated at the ffmpeg level,
    because the ffmpeg runner is given ``is_cancelled`` as its cancel check.
    """

    _cancelled: bool = False
    _report: Callable[[float | None, str], None] = lambda fraction, message: None

    def is_cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def raise_if_cancelled(self) -> None:
        if self._cancelled:
            raise CancelledError("Operation cancelled")

    def progress(self, fraction: float | None = None, message: str = "") -> None:
        """Report completion in 0..1 (or None when indeterminate)."""
        self._report(fraction, message)


class JobSignals(QObject):
    """Signals a job emits. Separate QObject because QRunnable is not one."""

    progress = Signal(str, object, str)      # job_id, fraction|None, message
    finished = Signal(str, str, object)      # job_id, state, result|exception
    state_changed = Signal(str, str)         # job_id, state


@dataclass
class Job(QRunnable):
    """A unit of background work.

    ``func`` is called with a :class:`JobContext` as its first argument, plus
    whatever extra args were supplied.
    """

    title: str
    func: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    #: Free-form tag so the UI can group or filter jobs ("convert", "artwork"...).
    category: str = "general"

    def __post_init__(self) -> None:
        QRunnable.__init__(self)
        self.id: str = uuid.uuid4().hex[:12]
        self.state: JobState = JobState.PENDING
        self.result: Any = None
        self.error: BaseException | None = None
        self.message: str = ""
        self.fraction: float | None = None
        self.signals = JobSignals()
        self.context = JobContext()
        self.context._report = self._on_progress
        self.setAutoDelete(False)  # the queue keeps jobs around for display

    # -- progress plumbing ---------------------------------------------
    def _safe_emit(self, signal, *args) -> None:
        """Emit, tolerating a signal whose underlying QObject is already
        gone.

        ``self.signals`` is a plain QObject with no Qt parent, so nothing
        guarantees its C++ side outlives this Job if the last Python
        reference to the Job is dropped while a worker thread is still
        running it (QRunnable, unlike QObject, has no parent-child
        ownership to keep it alive). Letting that show up as an exception
        escaping a Python override of a C++ virtual (QRunnable.run()) is
        unsafe -- PySide6 cannot always propagate it cleanly, and it has been
        observed taking the whole process down instead of just this job.
        Nobody is listening on a deleted signal source anyway, so this is a
        safe no-op, not a swallowed real failure.
        """
        try:
            signal.emit(*args)
        except RuntimeError:
            pass

    def _on_progress(self, fraction: float | None, message: str) -> None:
        self.fraction = fraction
        if message:
            self.message = message
        self._safe_emit(self.signals.progress, self.id, fraction, message)

    def _set_state(self, state: JobState) -> None:
        self.state = state
        self._safe_emit(self.signals.state_changed, self.id, state.value)

    def cancel(self) -> None:
        """Request cancellation. A pending job stops immediately; a running one
        stops at its next checkpoint or when ffmpeg is terminated."""
        self.context.cancel()
        if self.state is JobState.PENDING:
            self._set_state(JobState.CANCELLED)
            self._safe_emit(self.signals.finished, self.id, JobState.CANCELLED.value, None)

    # -- execution ------------------------------------------------------
    @Slot()
    def run(self) -> None:
        try:
            self._run()
        except BaseException:  # noqa: BLE001 -- must never escape a QRunnable override
            traceback.print_exc()

    def _run(self) -> None:
        if self.state is JobState.CANCELLED:
            return
        self._set_state(JobState.RUNNING)
        try:
            self.result = self.func(self.context, *self.args, **self.kwargs)
        except CancelledError:
            self._set_state(JobState.CANCELLED)
            self._safe_emit(self.signals.finished, self.id, JobState.CANCELLED.value, None)
            return
        except BaseException as exc:  # noqa: BLE001 -- one bad job must not kill the queue
            self.error = exc
            self.message = str(exc) or exc.__class__.__name__
            traceback.print_exc()
            self._set_state(JobState.FAILED)
            self._safe_emit(self.signals.finished, self.id, JobState.FAILED.value, exc)
            return
        self.fraction = 1.0
        self._set_state(JobState.SUCCEEDED)
        self._safe_emit(self.signals.finished, self.id, JobState.SUCCEEDED.value, self.result)


class JobQueue(QObject):
    """Owns the thread pool and the list of jobs shown in the queue dock."""

    job_added = Signal(str)
    job_progress = Signal(str, object, str)
    job_finished = Signal(str, str, object)
    queue_changed = Signal()

    def __init__(self, max_concurrent: int = 2, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        # Transcoding is CPU-bound and ffmpeg already threads internally, so a
        # small pool beats one-job-per-core: it keeps the machine responsive and
        # avoids thrashing the disk with parallel writes.
        self._pool.setMaxThreadCount(max(1, max_concurrent))
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []

    # -- submission -----------------------------------------------------
    def submit(self, job: Job) -> Job:
        self._jobs[job.id] = job
        self._order.append(job.id)
        job.signals.progress.connect(self._relay_progress)
        job.signals.finished.connect(self._relay_finished)
        job.signals.state_changed.connect(lambda *_: self.queue_changed.emit())
        self.job_added.emit(job.id)
        self.queue_changed.emit()
        self._pool.start(job)
        return job

    def submit_func(
        self,
        title: str,
        func: Callable[..., Any],
        *args: Any,
        category: str = "general",
        **kwargs: Any,
    ) -> Job:
        return self.submit(Job(title=title, func=func, args=args, kwargs=kwargs, category=category))

    # -- relays ---------------------------------------------------------
    @Slot(str, object, str)
    def _relay_progress(self, job_id: str, fraction: object, message: str) -> None:
        self.job_progress.emit(job_id, fraction, message)

    @Slot(str, str, object)
    def _relay_finished(self, job_id: str, state: str, payload: object) -> None:
        self.job_finished.emit(job_id, state, payload)
        self.queue_changed.emit()

    # -- inspection -----------------------------------------------------
    def job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def jobs(self) -> list[Job]:
        return [self._jobs[i] for i in self._order if i in self._jobs]

    def active_count(self) -> int:
        return sum(1 for j in self._jobs.values() if not j.state.is_finished)

    # -- control --------------------------------------------------------
    def cancel(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if job is not None:
            job.cancel()

    def cancel_all(self) -> None:
        for job in list(self._jobs.values()):
            if not job.state.is_finished:
                job.cancel()

    def clear_finished(self) -> None:
        for job_id in [i for i, j in self._jobs.items() if j.state.is_finished]:
            self._jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        self.queue_changed.emit()

    def wait_for_done(self, timeout_ms: int = -1) -> bool:
        """Block until the pool drains. Used on shutdown and by tests."""
        return self._pool.waitForDone(timeout_ms)
