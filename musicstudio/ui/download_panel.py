"""Download audio from a YouTube or other URL."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import get_settings
from ..core import download as download_module
from ..core import formats
from . import theme
from .common import QualityBadge, card, heading, row, section_label, spacer


class DownloadPanel(QWidget):
    """Paste a link, pick a format, get a tagged file."""

    #: Emitted with the paths of files that were downloaded.
    downloaded = Signal(list)

    def __init__(self, job_queue, parent=None) -> None:
        super().__init__(parent)
        self.jobs = job_queue
        self.settings = get_settings()
        self._url_info: download_module.UrlInfo | None = None
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        layout.addWidget(
            heading(
                "Download",
                "Paste a link from YouTube or any of the 1700+ sites yt-dlp supports. "
                "Playlists are downloaded in full.",
            )
        )

        # -- URL --------------------------------------------------------
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://…")
        self.url_input.returnPressed.connect(self._inspect)

        self.inspect_button = QPushButton("Check link")
        self.inspect_button.clicked.connect(self._inspect)

        self.info_label = QLabel("Paste a link to see what it contains.")
        self.info_label.setObjectName("Hint")
        self.info_label.setWordWrap(True)

        self.source_badge = QualityBadge()

        layout.addWidget(
            card(
                section_label("Source"),
                row(self.url_input, self.inspect_button, spacing=8),
                row(self.source_badge, self.info_label, spacer(), spacing=10),
            )
        )

        # -- Output -----------------------------------------------------
        self.keep_radio = QRadioButton("Keep the original stream (best quality, no re-encoding)")
        self.keep_radio.setChecked(self.settings.download_mode == "keep")
        self.keep_radio.toggled.connect(self._update_quality_note)

        self.convert_radio = QRadioButton("Convert to")
        self.convert_radio.setChecked(self.settings.download_mode == "convert")
        self.convert_radio.toggled.connect(self._update_quality_note)

        self.format_combo = QComboBox()
        for profile in formats.ALL_PROFILES:
            self.format_combo.addItem(profile.label, profile.id)
        index = self.format_combo.findData(self.settings.download_format)
        self.format_combo.setCurrentIndex(max(0, index))
        self.format_combo.currentIndexChanged.connect(self._update_quality_note)
        self.format_combo.setEnabled(self.convert_radio.isChecked())
        self.convert_radio.toggled.connect(self.format_combo.setEnabled)

        self.quality_note = QLabel("")
        self.quality_note.setObjectName("Warning")
        self.quality_note.setWordWrap(True)
        self.quality_note.setVisible(False)

        self.output_dir = QLineEdit(self.settings.output_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_output_dir)

        self.thumbnail_check = QCheckBox("Use the video thumbnail as cover art")
        self.thumbnail_check.setChecked(self.settings.download_embed_thumbnail)
        self.artwork_check = QCheckBox("Look up proper album art online instead")
        self.artwork_check.setChecked(False)

        self.playlist_limit = QSpinBox()
        self.playlist_limit.setRange(0, 1000)
        self.playlist_limit.setValue(self.settings.download_playlist_limit)
        self.playlist_limit.setSpecialValueText("All")
        self.playlist_limit.setSuffix(" tracks")

        options = QWidget()
        grid = QGridLayout(options)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(8)
        grid.addWidget(self.keep_radio, 0, 0, 1, 3)
        grid.addWidget(self.convert_radio, 1, 0)
        grid.addWidget(self.format_combo, 1, 1)
        grid.addWidget(QWidget(), 1, 2)
        grid.setColumnStretch(2, 1)

        layout.addWidget(
            card(
                section_label("Output format"),
                options,
                self.quality_note,
                section_label("Save to"),
                row(self.output_dir, browse, spacing=8),
                self.thumbnail_check,
                self.artwork_check,
                row(QLabel("Playlist limit:"), self.playlist_limit, spacer(), spacing=8),
            )
        )

        # -- Action -----------------------------------------------------
        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("Primary")
        self.download_button.setMinimumWidth(140)
        self.download_button.clicked.connect(self._start_download)
        layout.addWidget(row(spacer(), self.download_button))

        # -- Log --------------------------------------------------------
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("Download results appear here.")
        self.log.setMaximumHeight(160)
        layout.addWidget(self.log)
        layout.addStretch(1)

        self._update_quality_note()

    # -- helpers --------------------------------------------------------
    def _selected_profile(self):
        return formats.get_profile(self.format_combo.currentData())

    def _choose_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose where downloads go", self.output_dir.text()
        )
        if directory:
            self.output_dir.setText(directory)

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text)

    # -- inspect --------------------------------------------------------
    def _inspect(self) -> None:
        url = self.url_input.text().strip()
        if not download_module.is_supported_url(url):
            self.info_label.setText("That does not look like a URL.")
            self.info_label.setObjectName("Warning")
            self.info_label.setStyleSheet(f"color: {theme.WARNING};")
            return

        self.inspect_button.setEnabled(False)
        self.info_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.info_label.setText("Checking…")

        def work(context, target):
            return download_module.inspect_url(target)

        job = self.jobs.submit_func("Checking link", work, url, category="download")
        job.signals.finished.connect(self._on_inspected)

    def _on_inspected(self, _job_id: str, state: str, payload) -> None:
        self.inspect_button.setEnabled(True)
        if state != "succeeded":
            self.info_label.setStyleSheet(f"color: {theme.WARNING};")
            self.info_label.setText(f"Could not read that link: {payload}")
            self.source_badge.clear_state()
            return

        info: download_module.UrlInfo = payload
        self._url_info = info
        self.info_label.setStyleSheet(f"color: {theme.TEXT_DIM};")

        if info.is_playlist:
            self.info_label.setText(
                f"Playlist “{info.title}” — {info.entry_count} tracks"
                + (f", {info.duration_label} total" if info.duration else "")
            )
            self.source_badge.clear_state()
        else:
            detail = info.uploader or ""
            self.info_label.setText(
                f"“{info.title}”" + (f" — {detail}" if detail else "")
                + (f" · {info.duration_label}" if info.duration else "")
            )
            codec = (info.best_audio_codec or "unknown").upper()
            bitrate = f"~{info.best_audio_bitrate} kbps" if info.best_audio_bitrate else ""
            self.source_badge.set_lossless(False, f"{codec} {bitrate}".strip())

        self._update_quality_note()

    # -- quality guidance -----------------------------------------------
    def _update_quality_note(self) -> None:
        if not self.convert_radio.isChecked():
            self.quality_note.setVisible(False)
            return
        profile = self._selected_profile()
        info = self._url_info or download_module.UrlInfo(title="")
        note = download_module.quality_note_for(info, profile)
        self.quality_note.setText(note or "")
        self.quality_note.setVisible(bool(note))

    # -- download -------------------------------------------------------
    def _start_download(self) -> None:
        url = self.url_input.text().strip()
        if not download_module.is_supported_url(url):
            self._append_log("That does not look like a URL.")
            return

        request = download_module.DownloadRequest(
            url=url,
            output_dir=Path(self.output_dir.text()),
            mode="convert" if self.convert_radio.isChecked() else "keep",
            profile=self._selected_profile() if self.convert_radio.isChecked() else None,
            embed_thumbnail=self.thumbnail_check.isChecked(),
            playlist_limit=self.playlist_limit.value(),
            fetch_artwork=self.artwork_check.isChecked(),
        )

        self.download_button.setEnabled(False)
        self._append_log(f"→ {url}")

        def work(context, req):
            return download_module.download(req, context=context)

        job = self.jobs.submit_func(f"Downloading {url[:60]}", work, request, category="download")
        job.signals.finished.connect(self._on_downloaded)

    def _on_downloaded(self, _job_id: str, state: str, payload) -> None:
        self.download_button.setEnabled(True)
        if state == "cancelled":
            self._append_log("Cancelled.")
            return
        if state != "succeeded":
            self._append_log(f"Failed: {payload}")
            return

        result: download_module.DownloadResult = payload
        for track in result.tracks:
            detail = f"{track.source_codec.upper()}" if track.source_codec else ""
            if track.converted:
                detail += " → converted"
            self._append_log(f"✓ {track.path.name}  [{detail}]")
            for note in track.notes:
                self._append_log(f"    ⚠ {note}")
        for warning in result.warnings:
            self._append_log(f"⚠ {warning}")

        self.downloaded.emit([t.path for t in result.tracks])
