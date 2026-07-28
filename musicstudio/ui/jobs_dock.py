"""The background job queue, shown as a dock so progress is always visible."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.jobs import Job, JobQueue, JobState
from . import theme
from .common import spacer

_STATE_COLOUR = {
    JobState.PENDING: theme.TEXT_FAINT,
    JobState.RUNNING: theme.ACCENT,
    JobState.SUCCEEDED: theme.LOSSLESS,
    JobState.FAILED: theme.DANGER,
    JobState.CANCELLED: theme.TEXT_FAINT,
}

_STATE_TEXT = {
    JobState.PENDING: "Waiting",
    JobState.RUNNING: "Running",
    JobState.SUCCEEDED: "Done",
    JobState.FAILED: "Failed",
    JobState.CANCELLED: "Cancelled",
}


class JobRow(QWidget):
    """One job: title, progress, message, and a cancel button while running."""

    def __init__(self, job: Job, queue: JobQueue, parent=None) -> None:
        super().__init__(parent)
        self.job = job
        self.queue = queue

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)

        self.title_label = QLabel(job.title)
        self.title_label.setStyleSheet("font-weight: 600;")
        self.title_label.setWordWrap(True)

        self.state_label = QLabel(_STATE_TEXT[job.state])
        self.state_label.setStyleSheet(f"color: {_STATE_COLOUR[job.state]}; font-size: 11px;")

        self.cancel_button = QPushButton("✕")
        self.cancel_button.setFixedSize(22, 22)
        self.cancel_button.setToolTip("Cancel this job")
        self.cancel_button.clicked.connect(lambda: self.queue.cancel(self.job.id))

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        header.addWidget(self.title_label, 1)
        header.addWidget(self.state_label)
        header.addWidget(self.cancel_button)
        layout.addLayout(header)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        self.message_label = QLabel("")
        self.message_label.setObjectName("Hint")
        self.message_label.setWordWrap(True)
        layout.addWidget(self.message_label)

        self.setStyleSheet(
            f"JobRow {{ background: {theme.BG_RAISED}; border: 1px solid {theme.BORDER};"
            f" border-radius: 6px; }}"
        )
        self.refresh()

    def refresh(self) -> None:
        job = self.job
        self.state_label.setText(_STATE_TEXT[job.state])
        self.state_label.setStyleSheet(f"color: {_STATE_COLOUR[job.state]}; font-size: 11px;")
        self.cancel_button.setVisible(not job.state.is_finished)

        if job.state.is_finished:
            self.progress.setRange(0, 100)
            self.progress.setValue(100 if job.state is JobState.SUCCEEDED else 0)
        elif job.fraction is None:
            # Indeterminate: a busy bar is honest when we cannot know the total.
            self.progress.setRange(0, 0)
        else:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(job.fraction * 100))

        self.message_label.setText(job.message)
        self.message_label.setVisible(bool(job.message))


class JobsDock(QDockWidget):
    """Dockable list of every job, running and finished."""

    def __init__(self, queue: JobQueue, parent=None) -> None:
        super().__init__("Activity", parent)
        self.queue = queue
        self._rows: dict[str, JobRow] = {}

        self.setAllowedAreas(Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        self.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetClosable)

        container = QWidget()
        outer = QVBoxLayout(container)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(8)

        self.summary_label = QLabel("No activity")
        self.summary_label.setObjectName("Hint")
        clear_button = QPushButton("Clear finished")
        clear_button.clicked.connect(self._clear_finished)
        cancel_all = QPushButton("Cancel all")
        cancel_all.setObjectName("Danger")
        cancel_all.clicked.connect(self.queue.cancel_all)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.summary_label)
        header.addWidget(spacer(), 1)
        header.addWidget(cancel_all)
        header.addWidget(clear_button)
        outer.addLayout(header)

        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(6)
        self.list_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(self.list_widget)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll, 1)

        self.setWidget(container)
        self.setMinimumWidth(300)

        queue.job_added.connect(self._on_job_added)
        queue.job_progress.connect(self._on_progress)
        queue.job_finished.connect(self._on_finished)
        queue.queue_changed.connect(self._update_summary)

    def _on_job_added(self, job_id: str) -> None:
        job = self.queue.job(job_id)
        if job is None or job_id in self._rows:
            return
        widget = JobRow(job, self.queue)
        self._rows[job_id] = widget
        # Insert above the trailing stretch so newest appears at the top.
        self.list_layout.insertWidget(0, widget)
        self._update_summary()

    def _on_progress(self, job_id: str, _fraction, _message: str) -> None:
        widget = self._rows.get(job_id)
        if widget is not None:
            widget.refresh()

    def _on_finished(self, job_id: str, _state: str, _payload) -> None:
        widget = self._rows.get(job_id)
        if widget is not None:
            widget.refresh()
        self._update_summary()

    def _clear_finished(self) -> None:
        self.queue.clear_finished()
        for job_id in [i for i, w in self._rows.items() if w.job.state.is_finished]:
            widget = self._rows.pop(job_id)
            self.list_layout.removeWidget(widget)
            widget.deleteLater()
        self._update_summary()

    def _update_summary(self) -> None:
        active = self.queue.active_count()
        total = len(self._rows)
        if active:
            self.summary_label.setText(f"{active} running · {total} total")
        elif total:
            self.summary_label.setText(f"{total} finished")
        else:
            self.summary_label.setText("No activity")
