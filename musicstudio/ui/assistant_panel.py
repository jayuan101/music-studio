"""Personal AI: a chat panel that can drive everything else in the app.

``core/assistant.py`` is Qt-free by design, so the pieces that need Qt --
bridging its synchronous confirmation callback across threads, and the
transcript UI itself -- live here instead.
"""

from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import (
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ..config import get_settings
from ..core import assistant as assistant_module
from ..core import secrets
from . import theme
from .common import card, heading, row, section_label, spacer


class ConfirmationGate(QObject):
    """Bridges a worker-thread confirmation request onto the Qt main thread.

    ``Assistant.send()`` runs inside a background Job and must pause for a
    real decision from the user. ``ask()`` is called from that thread: it
    emits ``requested`` (Qt auto-queues delivery to the main thread, the same
    cross-thread pattern ``JobSignals`` already uses), then blocks on a
    ``threading.Event`` until ``resolve()`` -- called from the main-thread
    slot connected to ``requested``, after Confirm/Cancel is clicked --
    supplies the answer.
    """

    requested = Signal(object)  # ActionPreview

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._event = threading.Event()
        self._decision = False

    def ask(self, preview: assistant_module.ActionPreview) -> bool:
        self._event.clear()
        self.requested.emit(preview)
        self._event.wait()
        return self._decision

    def resolve(self, decision: bool) -> None:
        self._decision = decision
        self._event.set()


class AssistantPanel(QWidget):
    """Chat with the assistant; mutating actions pause for confirmation."""

    #: Emitted with files the assistant changed on disk, so the app can
    #: re-index them -- mirrors the other panels' "produced files" signals.
    files_changed = Signal(list)

    #: Internal relays from the background job thread to this (main-thread)
    #: widget. Cross-thread emit is safe the same way JobSignals is.
    _text_delta = Signal(str)
    _narration_text = Signal(str)

    def __init__(self, job_queue, library, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.jobs = job_queue
        self.library = library
        self.settings = get_settings()
        self._assistant: assistant_module.Assistant | None = None
        self._busy = False
        self._streaming = False
        self._streamed_any_text = False

        self._gate = ConfirmationGate(self)
        self._gate.requested.connect(self._on_confirmation_requested)
        self._text_delta.connect(self._on_text_delta)
        self._narration_text.connect(self._on_narration)

        self._build()
        self.refresh_settings()

    # -- construction -----------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)
        layout.addWidget(
            heading(
                "Assistant",
                "Ask in plain language: convert, edit, tag, download, or search your "
                "library. Anything that changes a file is shown to you before it happens.",
            )
        )

        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setPlaceholderText(
            "Ask me to convert, edit, tag, download, or search your library."
        )
        layout.addWidget(self.transcript, 1)

        self.pending_summary = QLabel("")
        self.pending_summary.setWordWrap(True)

        self.pending_details = QPlainTextEdit()
        self.pending_details.setReadOnly(True)
        self.pending_details.setMaximumHeight(100)

        self.pending_notes = QWidget()
        self._notes_layout = QVBoxLayout(self.pending_notes)
        self._notes_layout.setContentsMargins(0, 0, 0, 0)
        self._notes_layout.setSpacing(4)

        self.confirm_button = QPushButton("Confirm")
        self.confirm_button.setObjectName("Primary")
        self.confirm_button.clicked.connect(lambda: self._resolve_confirmation(True))
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(lambda: self._resolve_confirmation(False))

        self.pending_card = card(
            section_label("Confirm this action"),
            self.pending_summary,
            self.pending_details,
            self.pending_notes,
            row(spacer(), self.cancel_button, self.confirm_button),
        )
        self.pending_card.setVisible(False)
        layout.addWidget(self.pending_card)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a command…")
        self.input.returnPressed.connect(self._on_send_clicked)

        self.send_button = QPushButton("Send")
        self.send_button.setObjectName("Primary")
        self.send_button.clicked.connect(self._on_send_clicked)
        layout.addWidget(row(self.input, self.send_button, spacing=8))

        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # -- backend selection --------------------------------------------------
    def refresh_settings(self) -> None:
        """Rebuild the assistant from current Settings. Call after any
        Preferences change so a new host/model/toggle takes effect at once."""
        self.settings = get_settings()
        backend = self._make_backend()
        ctx = assistant_module.AssistantContext(library=self.library, settings=self.settings)
        self._assistant = (
            assistant_module.Assistant(backend, ctx, confirm=self._gate.ask) if backend else None
        )

        ready = self._assistant is not None
        self.input.setEnabled(ready and not self._busy)
        self.send_button.setEnabled(ready and not self._busy)
        if not ready:
            self._set_status(
                "Set up a local Ollama model (or enable Claude) in Preferences to use the assistant."
            )
        else:
            self._set_status("")

    def _make_backend(self):
        if self.settings.ai_use_claude:
            api_key = secrets.get_claude_api_key(self.settings)
            if not api_key:
                return None
            return assistant_module.ClaudeBackend(api_key, self.settings.ai_claude_model)
        if not self.settings.ai_ollama_model:
            return None
        return assistant_module.OllamaBackend(self.settings.ai_ollama_host, self.settings.ai_ollama_model)

    # -- sending ------------------------------------------------------------
    def _on_send_clicked(self) -> None:
        text = self.input.text().strip()
        if not text or self._assistant is None or self._busy:
            return

        self.input.clear()
        self._append_line(f"You: {text}")
        self._busy = True
        self._streamed_any_text = False
        self.input.setEnabled(False)
        self.send_button.setEnabled(False)
        self._set_status("Thinking…")

        active_assistant = self._assistant

        def work(context, message):
            reply = active_assistant.send(
                message, context,
                on_text=self._text_delta.emit,
                on_narration=self._narration_text.emit,
            )
            return reply, list(active_assistant.last_changed_paths)

        job = self.jobs.submit_func("Assistant", work, text, category="assistant")
        job.signals.finished.connect(self._on_finished)

    def _on_finished(self, _job_id: str, state: str, payload) -> None:
        self._end_streaming()
        self._busy = False
        self.input.setEnabled(self._assistant is not None)
        self.send_button.setEnabled(self._assistant is not None)
        self._set_status("")

        if state == "cancelled":
            self._append_line("(cancelled)")
            return
        if state != "succeeded":
            self._append_line(f"Error: {payload}")
            return

        text, changed_paths = payload
        if not self._streamed_any_text:
            self._append_line(f"Assistant: {text}")
        if changed_paths:
            self.files_changed.emit([Path(p) for p in changed_paths])

    # -- streaming text -------------------------------------------------
    def _append_line(self, text: str) -> None:
        self.transcript.appendPlainText(text)

    def _on_text_delta(self, delta: str) -> None:
        if not delta:
            return
        if not self._streaming:
            self._append_line("")
            self.transcript.insertPlainText("Assistant: ")
            self._streaming = True
        self._streamed_any_text = True
        cursor = self.transcript.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.transcript.setTextCursor(cursor)
        self.transcript.insertPlainText(delta)

    def _on_narration(self, summary: str) -> None:
        self._end_streaming()
        self._append_line(f"  → {summary}")

    def _end_streaming(self) -> None:
        if self._streaming:
            self.transcript.appendPlainText("")
            self._streaming = False

    # -- confirmation -----------------------------------------------------
    def _on_confirmation_requested(self, preview: assistant_module.ActionPreview) -> None:
        self.pending_summary.setText(preview.summary)
        self.pending_details.setPlainText(preview.details)
        self.pending_details.setVisible(bool(preview.details))
        self._render_notes(preview.notes)
        self.pending_card.setVisible(True)

    def _render_notes(self, notes) -> None:
        while self._notes_layout.count():
            item = self._notes_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for note in notes:
            label = QLabel(f"⚠  {note}")
            label.setObjectName("Warning")
            label.setWordWrap(True)
            self._notes_layout.addWidget(label)

    def _resolve_confirmation(self, decision: bool) -> None:
        self.pending_card.setVisible(False)
        self._gate.resolve(decision)

    # -- status -----------------------------------------------------------
    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
