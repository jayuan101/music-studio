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

from .. import __version__
from ..config import get_settings
from ..core import artwork as artwork_module
from ..core import assistant as assistant_module
from ..core import organise
from ..core import secrets
from ..core import spotify as spotify_module
from ..core import updater as updater_module
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
        layout.addWidget(self._build_autotrim_card())
        layout.addWidget(self._build_ai_card())
        layout.addWidget(self._build_update_card())
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
        self.artwork_youtube = QCheckBox("Use a YouTube video thumbnail as a last resort")
        self.artwork_youtube.setToolTip(
            "No credentials needed, but it's a video frame, not real album art -- "
            "quality and accuracy vary. Tried only after everything else finds nothing."
        )
        for box in (self.artwork_enabled, self.artwork_musicbrainz, self.artwork_itunes, self.artwork_youtube):
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
            self._build_spotify_section(),
            self.artwork_youtube,
            form,
            row(spacer(), clear_cache),
        )

    def _build_spotify_section(self) -> QWidget:
        """Spotify's own sub-section: best cover art and catalogue of any
        provider here, but the only one needing credentials."""
        self.spotify_enabled = QCheckBox("Use Spotify (best match quality, needs a free API app)")
        self.spotify_enabled.toggled.connect(self._on_spotify_enabled_toggled)

        self.spotify_client_id = QLineEdit()
        self.spotify_client_id.setPlaceholderText("Client ID")
        self.spotify_client_id.editingFinished.connect(self._save)

        self.spotify_client_secret = QLineEdit()
        self.spotify_client_secret.setEchoMode(QLineEdit.Password)
        self.spotify_client_secret.setPlaceholderText("Client Secret")
        self.spotify_client_secret.editingFinished.connect(self._save_spotify_secret)

        test_spotify = QPushButton("Test connection")
        test_spotify.clicked.connect(self._test_spotify_connection)

        self.spotify_status = QLabel(
            "Get both free at developer.spotify.com/dashboard -- create an app, "
            "no special access needed, just Client Credentials."
        )
        self.spotify_status.setObjectName("Hint")
        self.spotify_status.setWordWrap(True)

        self.spotify_key_storage_note = QLabel("")
        self.spotify_key_storage_note.setObjectName("Hint")
        self.spotify_key_storage_note.setWordWrap(True)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(24, 4, 0, 4)
        form_layout.setSpacing(8)
        form_layout.addRow("Client ID", self.spotify_client_id)
        form_layout.addRow("Client Secret", row(self.spotify_client_secret, test_spotify, spacing=8))

        section = QWidget()
        section_layout = QVBoxLayout(section)
        section_layout.setContentsMargins(0, 0, 0, 0)
        section_layout.setSpacing(6)
        section_layout.addWidget(self.spotify_enabled)
        section_layout.addWidget(form)
        section_layout.addWidget(self.spotify_status)
        section_layout.addWidget(self.spotify_key_storage_note)
        return section

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

        self.ytmusic_downloads = QCheckBox("Tag downloads in YouTube Music format")
        self.ytmusic_downloads.setToolTip(
            "Clean “(Official Video)”-style noise out of the title, move guests into "
            "it as “(feat. X)”, and always fill album artist — the field YouTube "
            "Music groups a library by."
        )
        self.ytmusic_downloads.toggled.connect(self._save)

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

        return card(
            section_label("Downloads"), form, self.download_thumbnail, self.ytmusic_downloads
        )

    def _build_autotrim_card(self) -> QWidget:
        self.autotrim_enabled = QCheckBox("Enable auto-trim")
        self.autotrim_enabled.setToolTip(
            "Detect and remove leading/trailing silence or logo bumpers from tracks "
            "that look like they came from a music video. This rewrites the audio "
            "file in place."
        )
        self.autotrim_enabled.toggled.connect(self._on_autotrim_enabled_toggled)

        self.autotrim_new_tracks = QCheckBox("Run automatically after each download")
        self.autotrim_new_tracks.toggled.connect(self._save)

        self.autotrim_detect_speech = QCheckBox("Also try to detect spoken intros/outros (experimental)")
        self.autotrim_detect_speech.setToolTip(
            "Uses local voice detection to also catch a spoken intro or outro that "
            "isn't silent, like someone talking or a jingle before the song starts. "
            "It cannot always tell singing from speech, so a vocal-led intro can "
            "occasionally get trimmed too -- still bounded by the caps below, never "
            "more. Try it on a few tracks before leaving it on for everything."
        )
        self.autotrim_detect_speech.toggled.connect(self._save)

        self.autotrim_threshold = QDoubleSpinBox()
        self.autotrim_threshold.setRange(-90.0, -20.0)
        self.autotrim_threshold.setSuffix(" dB")
        self.autotrim_threshold.valueChanged.connect(self._save)

        self.autotrim_max_intro = QDoubleSpinBox()
        self.autotrim_max_intro.setRange(0.0, 60.0)
        self.autotrim_max_intro.setSuffix(" s")
        self.autotrim_max_intro.valueChanged.connect(self._save)

        self.autotrim_max_outro = QDoubleSpinBox()
        self.autotrim_max_outro.setRange(0.0, 60.0)
        self.autotrim_max_outro.setSuffix(" s")
        self.autotrim_max_outro.valueChanged.connect(self._save)

        form = QWidget()
        form_layout = QFormLayout(form)
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(8)
        form_layout.addRow("Silence threshold", self.autotrim_threshold)
        form_layout.addRow("Max intro to cut", self.autotrim_max_intro)
        form_layout.addRow("Max outro to cut", self.autotrim_max_outro)

        return card(
            section_label("Auto-trim"),
            self.autotrim_enabled,
            self.autotrim_new_tracks,
            self.autotrim_detect_speech,
            form,
        )

    def _on_autotrim_enabled_toggled(self, checked: bool) -> None:
        self.autotrim_new_tracks.setEnabled(checked)
        self._save()

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

    def _build_update_card(self) -> QWidget:
        self.version_label = QLabel(f"Installed version: {__version__}")
        self.version_label.setObjectName("Hint")

        self.check_update_button = QPushButton("Check for updates")
        self.check_update_button.clicked.connect(self._check_for_update)

        self.update_now_button = QPushButton("Update now")
        self.update_now_button.setObjectName("Primary")
        self.update_now_button.clicked.connect(self._start_update)
        self.update_now_button.setVisible(False)

        self.update_status = QLabel(
            "" if updater_module.is_frozen()
            else "Running from source -- checking works, but installing an update "
            "only applies to the packaged app."
        )
        self.update_status.setObjectName("Hint")
        self.update_status.setWordWrap(True)

        self._pending_update: updater_module.UpdateInfo | None = None

        return card(
            section_label("Updates"),
            row(self.version_label, spacer(), self.check_update_button, self.update_now_button),
            self.update_status,
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
        self.artwork_youtube.setChecked(s.artwork_use_youtube_thumbnail)
        self.artwork_min.setValue(s.artwork_min_size)
        self.artwork_preferred.setValue(s.artwork_preferred_size)
        self.user_agent.setText(s.musicbrainz_user_agent)

        self.spotify_enabled.setChecked(s.spotify_enabled)
        self.spotify_client_id.setText(s.spotify_client_id)
        self.spotify_client_secret.setText(secrets.get_spotify_client_secret(s))
        self.spotify_client_id.setEnabled(s.spotify_enabled)
        self.spotify_client_secret.setEnabled(s.spotify_enabled)

        self.limiter_ceiling.setValue(s.limiter_ceiling_db)
        self.max_gain.setValue(s.max_gain_db)
        self.loudnorm_target.setValue(s.loudnorm_target_lufs)

        index = self.download_mode.findData(s.download_mode)
        self.download_mode.setCurrentIndex(max(0, index))
        self.download_thumbnail.setChecked(s.download_embed_thumbnail)
        self.ytmusic_downloads.setChecked(s.ytmusic_format_downloads)
        self.playlist_limit.setValue(s.download_playlist_limit)

        self.autotrim_enabled.setChecked(s.auto_trim_enabled)
        self.autotrim_new_tracks.setChecked(s.auto_trim_new_tracks)
        self.autotrim_new_tracks.setEnabled(s.auto_trim_enabled)
        self.autotrim_detect_speech.setChecked(s.auto_trim_detect_speech)
        self.autotrim_threshold.setValue(s.auto_trim_silence_threshold_db)
        self.autotrim_max_intro.setValue(s.auto_trim_max_intro_s)
        self.autotrim_max_outro.setValue(s.auto_trim_max_outro_s)

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
        self._update_spotify_key_storage_note()

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
        s.artwork_use_youtube_thumbnail = self.artwork_youtube.isChecked()
        s.artwork_min_size = self.artwork_min.value()
        s.artwork_preferred_size = self.artwork_preferred.value()
        s.musicbrainz_user_agent = self.user_agent.text().strip() or s.musicbrainz_user_agent

        s.spotify_enabled = self.spotify_enabled.isChecked()
        s.spotify_client_id = self.spotify_client_id.text().strip()

        s.limiter_ceiling_db = self.limiter_ceiling.value()
        s.max_gain_db = self.max_gain.value()
        s.loudnorm_target_lufs = self.loudnorm_target.value()

        s.download_mode = self.download_mode.currentData()
        s.download_embed_thumbnail = self.download_thumbnail.isChecked()
        s.ytmusic_format_downloads = self.ytmusic_downloads.isChecked()
        s.download_playlist_limit = self.playlist_limit.value()

        s.auto_trim_enabled = self.autotrim_enabled.isChecked()
        s.auto_trim_new_tracks = self.autotrim_new_tracks.isChecked()
        s.auto_trim_detect_speech = self.autotrim_detect_speech.isChecked()
        s.auto_trim_silence_threshold_db = self.autotrim_threshold.value()
        s.auto_trim_max_intro_s = self.autotrim_max_intro.value()
        s.auto_trim_max_outro_s = self.autotrim_max_outro.value()

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

    # -- Spotify ------------------------------------------------------------
    def _on_spotify_enabled_toggled(self, checked: bool) -> None:
        self.spotify_client_id.setEnabled(checked)
        self.spotify_client_secret.setEnabled(checked)
        self._save()

    def _save_spotify_secret(self) -> None:
        if self._loading:
            return
        stored_in_keyring = secrets.set_spotify_client_secret(
            self.settings, self.spotify_client_secret.text().strip()
        )
        spotify_module.clear_token_cache()
        self._update_spotify_key_storage_note(stored_in_keyring)
        self.status_label.setText("Saved")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_FAINT};")
        self.settings_changed.emit()

    def _update_spotify_key_storage_note(self, stored_in_keyring: bool | None = None) -> None:
        if stored_in_keyring is None:
            stored_in_keyring = secrets.keyring_available()
        self.spotify_key_storage_note.setText(
            "The secret is stored in your OS credential store."
            if stored_in_keyring
            else "⚠ No OS credential store is available -- the secret is saved in plain "
            "text in settings.json."
        )
        self.spotify_key_storage_note.setStyleSheet(
            f"color: {theme.TEXT_FAINT if stored_in_keyring else theme.WARNING};"
        )

    def _test_spotify_connection(self) -> None:
        self._save()  # the Client ID field only saves on editingFinished/blur
        if not spotify_module.is_configured(self.settings):
            self.spotify_status.setText(
                "Enter both a Client ID and Client Secret first, and check the box above."
            )
            self.spotify_status.setStyleSheet(f"color: {theme.WARNING};")
            return

        self.spotify_status.setText("Checking…")
        self.spotify_status.setStyleSheet(f"color: {theme.TEXT_DIM};")

        def work(context):
            spotify_module.clear_token_cache()
            token = spotify_module._get_token(self.settings)
            if token is None:
                raise RuntimeError("Could not authenticate -- check the Client ID and Secret")
            return True

        job = self.jobs.submit_func("Testing Spotify connection", work, category="artwork")
        job.signals.finished.connect(self._on_spotify_tested)

    def _on_spotify_tested(self, _job_id: str, state: str, payload) -> None:
        if state == "succeeded":
            self.spotify_status.setText("Connected.")
            self.spotify_status.setStyleSheet(f"color: {theme.LOSSLESS};")
        else:
            self.spotify_status.setText(f"Could not connect: {payload}")
            self.spotify_status.setStyleSheet(f"color: {theme.WARNING};")

    # -- updates ----------------------------------------------------------
    def _check_for_update(self) -> None:
        self.check_update_button.setEnabled(False)
        self.update_now_button.setVisible(False)
        self._pending_update = None
        self.update_status.setText("Checking for updates…")
        self.update_status.setStyleSheet(f"color: {theme.TEXT_DIM};")

        def work(context):
            return updater_module.check_for_update()

        job = self.jobs.submit_func("Checking for updates", work, category="general")
        job.signals.finished.connect(self._on_update_checked)

    def _on_update_checked(self, _job_id: str, state: str, payload) -> None:
        self.check_update_button.setEnabled(True)
        if state != "succeeded":
            self.update_status.setText(f"Could not check for updates: {payload}")
            self.update_status.setStyleSheet(f"color: {theme.WARNING};")
            return

        info = payload
        if info is None:
            self.update_status.setText(f"You're up to date ({__version__}).")
            self.update_status.setStyleSheet(f"color: {theme.TEXT_FAINT};")
            return

        self._pending_update = info
        self.update_status.setText(
            f"Version {info.version} is available.{' ' + info.notes if info.notes else ''}"
        )
        self.update_status.setStyleSheet(f"color: {theme.LOSSLESS};")
        self.update_now_button.setVisible(updater_module.is_frozen())

    def _start_update(self) -> None:
        info = self._pending_update
        if info is None:
            return
        self.update_now_button.setEnabled(False)
        self.check_update_button.setEnabled(False)
        self.update_status.setText("Downloading update…")
        self.update_status.setStyleSheet(f"color: {theme.TEXT_DIM};")

        def work(context):
            zip_path = updater_module.download_update(info, context=context)
            updater_module.apply_update(zip_path)
            return True

        job = self.jobs.submit_func(f"Downloading update {info.version}", work, category="general")
        job.signals.progress.connect(self._on_update_progress)
        job.signals.finished.connect(self._on_update_applied)

    def _on_update_progress(self, _job_id: str, fraction: object, message: str) -> None:
        if message:
            self.update_status.setText(message)

    def _on_update_applied(self, _job_id: str, state: str, payload) -> None:
        if state != "succeeded":
            self.update_now_button.setEnabled(True)
            self.check_update_button.setEnabled(True)
            self.update_status.setText(f"Update failed: {payload}")
            self.update_status.setStyleSheet(f"color: {theme.WARNING};")
            return

        self.update_status.setText("Update downloaded -- restarting to finish installing…")
        self.update_status.setStyleSheet(f"color: {theme.LOSSLESS};")

        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication

        # A helper process is already waiting for this one to exit -- give
        # the status text a moment to actually paint before quitting.
        QTimer.singleShot(800, QApplication.instance().quit)
