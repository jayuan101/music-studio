"""Preferences.

Every control here is bound to a field on :class:`~musicstudio.config.Settings`
and saved the moment it changes, so nothing is lost on exit.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import get_settings
from ..core import artwork as artwork_module
from ..core import assistant as assistant_module
from ..core import organise
from ..core import secrets
from . import theme
from .common import card, heading, row, section_label, spacer


class SettingsPanel(QWidget):
    """Edit and persist the application settings."""

    #: Emitted after any change is saved, so panels can pick up new defaults.
    settings_changed = Signal()

    def __init__(self, job_queue, parent=None) -> None:
        super().__init__(parent)
        self.jobs = job_queue
        self.settings = get_settings()
        #: Suppresses saving while the form is being populated.
        self._loading = True
        self._build()
        self._load()
        self._loading = False

    # -- construction ---------------------------------------------------
    def _build(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 20, 24, 20)
        outer.setSpacing(14)
        outer.addWidget(
            heading(
                "Preferences",
                "Changes are saved as you make them. These are the defaults every "
                "panel starts from.",
            )
        )

        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(14)

        layout.addWidget(self._build_output_card())
        layout.addWidget(self._build_quality_card())
        layout.addWidget(self._build_artwork_card())
        layout.addWidget(self._build_editor_card())
        layout.addWidget(self._build_download_card())
        layout.addWidget(self._build_ai_card())
        layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(page)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        outer.addWidget(scroll, 1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Hint")
        outer.addWidget(self.status_label)

    # -- cards ----------------------------------------------------------
    def _build_output_card(self) -> QWidget:
        self.output_dir = QLineEdit()
        self.output_dir.editingFinished.connect(self._save)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_output_dir)

        self.filename_template = QLineEdit()
        self.filename_template.textChanged.connect(self._on_template_changed)

        self.template_preview = QLabel("")
        self.template_preview.setObjectName("Hint")
        self.template_preview.setWordWrap(True)

        template_help = QLabel(organise.TEMPLATE_HELP)
        template_help.setObjectName("Hint")
        template_help.setWordWrap(True)

        self.overwrite_existing = QCheckBox(
            "Overwrite files that already exist (otherwise a number is appended)"
        )
        self.overwrite_existing.toggled.connect(self._save)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)
        form_layout.addRow("Output folder", row(self.output_dir, browse, spacing=8))
        form_layout.addRow("Filename template", self.filename_template)

        return card(
            section_label("Output"),
            form,
            template_help,
            self.template_preview,
            self.overwrite_existing,
        )

    def _build_quality_card(self) -> QWidget:
        self.preserve_rate = QCheckBox("Keep the source sample rate")
        self.preserve_depth = QCheckBox("Keep the source bit depth")
        self.dither = QCheckBox("Apply dither when reducing bit depth")
        for box in (self.preserve_rate, self.preserve_depth, self.dither):
            box.toggled.connect(self._save)

        self.preserve_hint = QLabel("")
        self.preserve_hint.setObjectName("Hint")
        self.preserve_hint.setWordWrap(True)

        self.warn_lossy_lossless = QCheckBox(
            "Warn when converting compressed audio to a lossless format"
        )
        self.warn_lossy_lossy = QCheckBox("Warn when re-encoding compressed audio")
        for box in (self.warn_lossy_lossless, self.warn_lossy_lossy):
            box.toggled.connect(self._save)

        return card(
            section_label("Quality"),
            self.preserve_rate,
            self.preserve_depth,
            self.preserve_hint,
            self.dither,
            self.warn_lossy_lossless,
            self.warn_lossy_lossy,
        )

    def _build_artwork_card(self) -> QWidget:
        self.artwork_enabled = QCheckBox("Look up cover art online")
        self.artwork_musicbrainz = QCheckBox("Use MusicBrainz / Cover Art Archive")
        self.artwork_itunes = QCheckBox("Use iTunes as a fallback")
        for box in (self.artwork_enabled, self.artwork_musicbrainz, self.artwork_itunes):
            box.toggled.connect(self._save)

        self.artwork_min = QSpinBox()
        self.artwork_min.setRange(0, 4000)
        self.artwork_min.setSingleStep(100)
        self.artwork_min.setSuffix(" px")
        self.artwork_min.setToolTip(
            "'Update all artwork' replaces embedded images smaller than this"
        )
        self.artwork_preferred = QSpinBox()
        self.artwork_preferred.setRange(100, 3000)
        self.artwork_preferred.setSingleStep(100)
        self.artwork_preferred.setSuffix(" px")
        for spin in (self.artwork_min, self.artwork_preferred):
            spin.valueChanged.connect(self._save)

        self.user_agent = QLineEdit()
        self.user_agent.setToolTip(
            "MusicBrainz requires clients to identify themselves contactably"
        )
        self.user_agent.editingFinished.connect(self._save)

        clear_cache = QPushButton("Clear artwork cache")
        clear_cache.setToolTip("Forget cached images and 'not found' results")
        clear_cache.clicked.connect(self._clear_artwork_cache)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)
        form_layout.addRow("Replace art smaller than", self.artwork_min)
        form_layout.addRow("Request size", self.artwork_preferred)
        form_layout.addRow("MusicBrainz user agent", self.user_agent)

        return card(
            section_label("Cover art"),
            self.artwork_enabled,
            self.artwork_musicbrainz,
            self.artwork_itunes,
            form,
            row(spacer(), clear_cache),
        )

    def _build_editor_card(self) -> QWidget:
        self.limiter_ceiling = QDoubleSpinBox()
        self.limiter_ceiling.setRange(-6.0, 0.0)
        self.limiter_ceiling.setSingleStep(0.1)
        self.limiter_ceiling.setSuffix(" dBTP")

        self.max_gain = QDoubleSpinBox()
        self.max_gain.setRange(6.0, 60.0)
        self.max_gain.setSingleStep(3.0)
        self.max_gain.setSuffix(" dB")
        self.max_gain.setToolTip("How far the editor's gain slider can be pushed")

        self.loudnorm_target = QDoubleSpinBox()
        self.loudnorm_target.setRange(-30.0, -5.0)
        self.loudnorm_target.setSingleStep(0.5)
        self.loudnorm_target.setSuffix(" LUFS")

        for spin in (self.limiter_ceiling, self.max_gain, self.loudnorm_target):
            spin.valueChanged.connect(self._save)

        self.gain_hint = QLabel("")
        self.gain_hint.setObjectName("Hint")
        self.gain_hint.setWordWrap(True)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)
        form_layout.addRow("Limiter ceiling", self.limiter_ceiling)
        form_layout.addRow("Maximum boost", self.max_gain)
        form_layout.addRow("Normalise to", self.loudnorm_target)

        return card(section_label("Editor"), form, self.gain_hint)

    def _build_download_card(self) -> QWidget:
        self.download_mode = QComboBox()
        self.download_mode.addItem("Keep the original stream (best quality)", "keep")
        self.download_mode.addItem("Convert to a chosen format", "convert")
        self.download_mode.currentIndexChanged.connect(self._save)

        self.download_thumbnail = QCheckBox("Use the video thumbnail as cover art")
        self.download_thumbnail.toggled.connect(self._save)

        self.playlist_limit = QSpinBox()
        self.playlist_limit.setRange(0, 1000)
        self.playlist_limit.setSpecialValueText("All")
        self.playlist_limit.setSuffix(" tracks")
        self.playlist_limit.valueChanged.connect(self._save)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)
        form_layout.addRow("Default mode", self.download_mode)
        form_layout.addRow("Playlist limit", self.playlist_limit)

        return card(section_label("Downloads"), form, self.download_thumbnail)

    def _build_ai_card(self) -> QWidget:
        self.ai_ollama_host = QLineEdit()
        self.ai_ollama_host.setPlaceholderText("http://localhost:11434")
        self.ai_ollama_host.editingFinished.connect(self._save)

        self.ai_ollama_model = QComboBox()
        self.ai_ollama_model.setEditable(True)
        self.ai_ollama_model.editTextChanged.connect(self._save)

        refresh_models = QPushButton("Refresh models")
        refresh_models.clicked.connect(self._refresh_ollama_models)

        self.ai_ollama_status = QLabel(
            "Ollama runs on your own machine -- nothing is sent anywhere for this path."
        )
        self.ai_ollama_status.setObjectName("Hint")
        self.ai_ollama_status.setWordWrap(True)

        self.ai_use_claude = QCheckBox("Use Claude for harder commands")
        self.ai_use_claude.toggled.connect(self._on_ai_use_claude_toggled)

        self.ai_claude_model = QComboBox()
        self.ai_claude_model.addItem("Claude Sonnet 5 (recommended, lower cost)", "claude-sonnet-5")
        self.ai_claude_model.addItem("Claude Opus 5 (more capable, higher cost)", "claude-opus-5")
        self.ai_claude_model.currentIndexChanged.connect(self._save)

        self.ai_claude_key = QLineEdit()
        self.ai_claude_key.setEchoMode(QLineEdit.Password)
        self.ai_claude_key.setPlaceholderText("sk-ant-…")
        self.ai_claude_key.editingFinished.connect(self._save_claude_key)

        test_connection = QPushButton("Test connection")
        test_connection.clicked.connect(self._test_claude_connection)

        self.ai_claude_status = QLabel("")
        self.ai_claude_status.setObjectName("Hint")
        self.ai_claude_status.setWordWrap(True)

        self.ai_key_storage_note = QLabel("")
        self.ai_key_storage_note.setObjectName("Hint")
        self.ai_key_storage_note.setWordWrap(True)

        billing_note = QLabel(
            "Claude requests leave this machine over the network and are billed to your "
            "own Anthropic account. The local model never sends anything anywhere."
        )
        billing_note.setObjectName("Hint")
        billing_note.setWordWrap(True)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)
        form_layout.addRow("Ollama host", row(self.ai_ollama_host, refresh_models, spacing=8))
        form_layout.addRow("Model", self.ai_ollama_model)

        claude_form = QWidget()
        claude_layout = QFormLayout(claude_form)
        claude_layout.setContentsMargins(0, 0, 0, 0)
        claude_layout.setSpacing(8)
        claude_layout.addRow("Claude model", self.ai_claude_model)
        claude_layout.addRow("API key", row(self.ai_claude_key, test_connection, spacing=8))

        return card(
            section_label("Personal AI"),
            form,
            self.ai_ollama_status,
            self.ai_use_claude,
            claude_form,
            self.ai_claude_status,
            self.ai_key_storage_note,
            billing_note,
        )

    # -- load / save ----------------------------------------------------
    def _load(self) -> None:
        s = self.settings
        self.output_dir.setText(s.output_dir)
        self.filename_template.setText(s.filename_template)
        self.overwrite_existing.setChecked(s.overwrite_existing)

        self.preserve_rate.setChecked(s.preserve_source_rate)
        self.preserve_depth.setChecked(s.preserve_source_depth)
        self.dither.setChecked(s.dither_on_downconvert)
        self.warn_lossy_lossless.setChecked(s.warn_on_lossy_to_lossless)
        self.warn_lossy_lossy.setChecked(s.warn_on_lossy_to_lossy)

        self.artwork_enabled.setChecked(s.artwork_enabled)
        self.artwork_musicbrainz.setChecked(s.artwork_use_musicbrainz)
        self.artwork_itunes.setChecked(s.artwork_use_itunes)
        self.artwork_min.setValue(s.artwork_min_size)
        self.artwork_preferred.setValue(s.artwork_preferred_size)
        self.user_agent.setText(s.musicbrainz_user_agent)

        self.limiter_ceiling.setValue(s.limiter_ceiling_db)
        self.max_gain.setValue(s.max_gain_db)
        self.loudnorm_target.setValue(s.loudnorm_target_lufs)

        index = self.download_mode.findData(s.download_mode)
        self.download_mode.setCurrentIndex(max(0, index))
        self.download_thumbnail.setChecked(s.download_embed_thumbnail)
        self.playlist_limit.setValue(s.download_playlist_limit)

        self.ai_ollama_host.setText(s.ai_ollama_host)
        if s.ai_ollama_model:
            self.ai_ollama_model.addItem(s.ai_ollama_model)
            self.ai_ollama_model.setCurrentText(s.ai_ollama_model)
        self.ai_use_claude.setChecked(s.ai_use_claude)
        index = self.ai_claude_model.findData(s.ai_claude_model)
        self.ai_claude_model.setCurrentIndex(max(0, index))
        self.ai_claude_key.setText(secrets.get_claude_api_key(s))
        self.ai_claude_model.setEnabled(s.ai_use_claude)
        self.ai_claude_key.setEnabled(s.ai_use_claude)

        self._update_hints()
        self._update_key_storage_note()

    def _save(self, *_) -> None:
        if self._loading:
            return
        s = self.settings
        s.output_dir = self.output_dir.text().strip() or s.output_dir
        s.filename_template = self.filename_template.text()
        s.overwrite_existing = self.overwrite_existing.isChecked()

        s.preserve_source_rate = self.preserve_rate.isChecked()
        s.preserve_source_depth = self.preserve_depth.isChecked()
        s.dither_on_downconvert = self.dither.isChecked()
        s.warn_on_lossy_to_lossless = self.warn_lossy_lossless.isChecked()
        s.warn_on_lossy_to_lossy = self.warn_lossy_lossy.isChecked()

        s.artwork_enabled = self.artwork_enabled.isChecked()
        s.artwork_use_musicbrainz = self.artwork_musicbrainz.isChecked()
        s.artwork_use_itunes = self.artwork_itunes.isChecked()
        s.artwork_min_size = self.artwork_min.value()
        s.artwork_preferred_size = self.artwork_preferred.value()
        s.musicbrainz_user_agent = self.user_agent.text().strip() or s.musicbrainz_user_agent

        s.limiter_ceiling_db = self.limiter_ceiling.value()
        s.max_gain_db = self.max_gain.value()
        s.loudnorm_target_lufs = self.loudnorm_target.value()

        s.download_mode = self.download_mode.currentData()
        s.download_embed_thumbnail = self.download_thumbnail.isChecked()
        s.download_playlist_limit = self.playlist_limit.value()

        s.ai_ollama_host = self.ai_ollama_host.text().strip() or s.ai_ollama_host
        s.ai_ollama_model = self.ai_ollama_model.currentText().strip()
        s.ai_use_claude = self.ai_use_claude.isChecked()
        s.ai_claude_model = self.ai_claude_model.currentData()

        try:
            s.save()
        except OSError as exc:
            self.status_label.setText(f"Could not save preferences: {exc}")
            self.status_label.setStyleSheet(f"color: {theme.WARNING};")
            return

        self.status_label.setText("Saved")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        self._update_hints()
        self.settings_changed.emit()

    def _update_hints(self) -> None:
        self.template_preview.setText(
            "Example:  " + organise.preview_template(self.filename_template.text())
        )
        normalising = not (self.preserve_rate.isChecked() and self.preserve_depth.isChecked())
        self.preserve_hint.setText(
            "Turning these off normalises everything down to CD quality "
            "(44.1 kHz / 16-bit). Nothing is ever upsampled."
            if normalising
            else "Conversions keep whatever the source has, which is what you want "
            "for archiving."
        )
        self.gain_hint.setText(
            f"The editor's slider will reach {self.max_gain.value():+.0f} dB "
            f"(about {10 ** (self.max_gain.value() / 20) * 100:,.0f}% volume), "
            f"limited at {self.limiter_ceiling.value():+.1f} dBTP unless you pick raw gain."
        )

    # -- actions --------------------------------------------------------
    def _on_template_changed(self, *_) -> None:
        self._update_hints()
        self._save()

    def _choose_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose the default output folder", self.output_dir.text()
        )
        if directory:
            self.output_dir.setText(directory)
            self._save()

    def _clear_artwork_cache(self) -> None:
        removed = artwork_module.clear_cache()
        self.status_label.setText(f"Cleared {removed} cached artwork entr{'y' if removed == 1 else 'ies'}")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_FAINT};")

    # -- Personal AI ------------------------------------------------------
    def _on_ai_use_claude_toggled(self, checked: bool) -> None:
        self.ai_claude_model.setEnabled(checked)
        self.ai_claude_key.setEnabled(checked)
        self._save()

    def _refresh_ollama_models(self) -> None:
        host = self.ai_ollama_host.text().strip() or "http://localhost:11434"
        self.ai_ollama_status.setText("Checking…")
        self.ai_ollama_status.setStyleSheet(f"color: {theme.TEXT_DIM};")

        def work(context):
            return assistant_module.OllamaBackend(host, "").list_models()

        job = self.jobs.submit_func("Checking Ollama models", work, category="assistant")
        job.signals.finished.connect(self._on_ollama_models)

    def _on_ollama_models(self, _job_id: str, state: str, payload) -> None:
        if state != "succeeded":
            self.ai_ollama_status.setText(f"Could not reach Ollama: {payload}")
            self.ai_ollama_status.setStyleSheet(f"color: {theme.WARNING};")
            return

        models = payload
        current = self.ai_ollama_model.currentText()
        self.ai_ollama_model.clear()
        self.ai_ollama_model.addItems(models)
        index = self.ai_ollama_model.findText(current)
        if index >= 0:
            self.ai_ollama_model.setCurrentIndex(index)
        elif current:
            self.ai_ollama_model.setEditText(current)

        if models:
            self.ai_ollama_status.setText(f"Found {len(models)} model(s).")
        else:
            self.ai_ollama_status.setText(
                "Reachable, but no models are pulled yet -- try `ollama pull llama3.1`."
            )
        self.ai_ollama_status.setStyleSheet(f"color: {theme.TEXT_FAINT};")

    def _save_claude_key(self) -> None:
        if self._loading:
            return
        stored_in_keyring = secrets.set_claude_api_key(self.settings, self.ai_claude_key.text().strip())
        self._update_key_storage_note(stored_in_keyring)
        self.status_label.setText("Saved")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        self.settings_changed.emit()

    def _update_key_storage_note(self, stored_in_keyring: bool | None = None) -> None:
        if stored_in_keyring is None:
            stored_in_keyring = secrets.keyring_available()
        self.ai_key_storage_note.setText(
            "The key is stored in your OS credential store."
            if stored_in_keyring
            else "⚠ No OS credential store is available -- the key is saved in plain text "
            "in settings.json."
        )
        self.ai_key_storage_note.setStyleSheet(
            f"color: {theme.TEXT_FAINT if stored_in_keyring else theme.WARNING};"
        )

    def _test_claude_connection(self) -> None:
        api_key = secrets.get_claude_api_key(self.settings)
        if not api_key:
            self.ai_claude_status.setText("Enter an API key first.")
            self.ai_claude_status.setStyleSheet(f"color: {theme.WARNING};")
            return

        model = self.ai_claude_model.currentData()
        self.ai_claude_status.setText("Checking…")
        self.ai_claude_status.setStyleSheet(f"color: {theme.TEXT_DIM};")

        def work(context):
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)
            client.messages.create(model=model, max_tokens=1, messages=[{"role": "user", "content": "hi"}])
            return True

        job = self.jobs.submit_func("Testing Claude connection", work, category="assistant")
        job.signals.finished.connect(self._on_claude_tested)

    def _on_claude_tested(self, _job_id: str, state: str, payload) -> None:
        if state == "succeeded":
            self.ai_claude_status.setText("Connected.")
            self.ai_claude_status.setStyleSheet(f"color: {theme.LOSSLESS};")
        else:
            self.ai_claude_status.setText(f"Could not connect: {payload}")
            self.ai_claude_status.setStyleSheet(f"color: {theme.WARNING};")
