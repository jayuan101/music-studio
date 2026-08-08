"""Download audio from a YouTube or other URL, or search for a song.

One box handles both: paste a link and it's auto-checked as soon as you
stop typing; type a few words and it searches YouTube/SoundCloud instead.
Either way, picking a result *previews* it first -- fetched to a scratch
cache and played immediately -- so you can hear it before deciding whether
to keep it. Nothing lands in your library until you click Keep.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import PREVIEW_CACHE_DIR, get_settings
from ..core import download as download_module
from ..core import formats
from ..core.download import _THUMBNAIL_EXTENSIONS
from . import theme
from .common import QualityBadge, card, heading, reveal_in_file_manager, row, section_label, spacer

#: How long to wait after the user stops typing a pasted link before it's
#: auto-checked. Long enough that a link pasted a character at a time (or
#: replaced by a second paste) doesn't fire a check per keystroke; short
#: enough that it still feels immediate.
_INSPECT_DEBOUNCE_MS = 450


class DownloadPanel(QWidget):
    """Paste a link or search, preview it, then keep it if you like it."""

    #: Emitted with the paths of files that were actually saved into the
    #: library folder -- a kept preview, or a direct URL/playlist download.
    #: Never emitted for a preview that hasn't been kept.
    downloaded = Signal(list)
    #: Emitted with one track that just started playing from the preview
    #: cache, so the Now Playing bar can pick it up.
    preview_ready = Signal(Path)

    def __init__(self, job_queue, parent=None) -> None:
        super().__init__(parent)
        self.jobs = job_queue
        self.settings = get_settings()
        self._mode = "idle"  # "idle" | "search" | "url"
        self._url_info: download_module.UrlInfo | None = None
        self._preview_track: download_module.DownloadedTrack | None = None
        self._last_saved_paths: list[Path] = []
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        layout.addWidget(
            heading(
                "Download",
                "Paste a link from YouTube or any of the 1700+ sites yt-dlp supports, "
                "or type to search. Preview a result to hear it, then Keep it if you like it.",
            )
        )

        # -- Find music (merged search + URL) --------------------------
        self.smart_input = QLineEdit()
        self.smart_input.setPlaceholderText(
            "Paste a link, or search YouTube and SoundCloud for a song, artist or album…"
        )
        self.smart_input.textChanged.connect(self._on_text_changed)
        self.smart_input.returnPressed.connect(self._activate)

        self.smart_button = QPushButton("Search")
        self.smart_button.clicked.connect(self._activate)

        self.status_label = QLabel("Paste a link, or type to search.")
        self.status_label.setObjectName("Hint")
        self.status_label.setWordWrap(True)

        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(_INSPECT_DEBOUNCE_MS)
        self._debounce_timer.timeout.connect(self._inspect)

        self.results_list = QListWidget()
        self.results_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results_list.setMaximumHeight(190)
        self.results_list.setVisible(False)
        self.results_list.itemDoubleClicked.connect(self._preview_result)
        self.results_list.itemSelectionChanged.connect(self._update_action_state)

        self.source_badge = QualityBadge()
        self.source_row = row(self.source_badge, spacer(), spacing=10)
        self.source_row.setVisible(False)

        self.preview_button = QPushButton("Preview")
        self.preview_button.setToolTip(
            "Fetch this to a scratch cache and play it right away, so you can hear it "
            "before deciding whether to keep it. Nothing is saved to your library yet."
        )
        self.preview_button.clicked.connect(self._preview_clicked)
        self.preview_button.setEnabled(False)

        self.keep_button = QPushButton("Keep")
        self.keep_button.setObjectName("Primary")
        self.keep_button.setToolTip(
            "Save the track that's currently previewing into your library, using the "
            "format and destination settings below."
        )
        self.keep_button.clicked.connect(self._keep_preview)
        self.keep_button.setVisible(False)

        layout.addWidget(
            card(
                section_label("Find music"),
                row(self.smart_input, self.smart_button, spacing=8),
                self.status_label,
                self.results_list,
                self.source_row,
                row(spacer(), self.preview_button, self.keep_button, spacing=8),
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
                section_label("What happens when you save"),
                options,
                self.quality_note,
                section_label("Destination"),
                row(self.output_dir, browse, spacing=8),
                self.thumbnail_check,
                self.artwork_check,
                row(QLabel("Playlist limit:"), self.playlist_limit, spacer(), spacing=8),
            )
        )

        # -- Action -----------------------------------------------------
        self.show_in_folder_button = QPushButton("Show in folder")
        self.show_in_folder_button.clicked.connect(self._show_in_folder)
        self.show_in_folder_button.setVisible(False)

        self.download_button = QPushButton("Download")
        self.download_button.setObjectName("Primary")
        self.download_button.setMinimumWidth(140)
        self.download_button.setToolTip(
            "Download a pasted link straight to your library without previewing it "
            "first -- the whole playlist, if it is one."
        )
        self.download_button.clicked.connect(self._start_download)
        layout.addWidget(row(self.show_in_folder_button, spacer(), self.download_button))

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

    # -- merged input: mode switching ------------------------------------
    def _on_text_changed(self, text: str) -> None:
        text = text.strip()
        self._debounce_timer.stop()

        if not text:
            self._mode = "idle"
            self._url_info = None
            self.source_badge.clear_state()
            self.results_list.clear()
            self.status_label.setText("Paste a link, or type to search.")
            self.smart_button.setText("Search")
            self._update_action_state()
            return

        if download_module.is_supported_url(text):
            self._mode = "url"
            self.smart_button.setText("Check link")
            self.results_list.clear()
            self._url_info = None
            self.status_label.setText("Checking as soon as you stop typing…")
            self._debounce_timer.start()
        else:
            self._mode = "search"
            self.smart_button.setText("Search")
            self._url_info = None
            self.source_badge.clear_state()
        self._update_action_state()

    def _activate(self) -> None:
        """Enter, or the button: skip the debounce and act immediately."""
        self._debounce_timer.stop()
        text = self.smart_input.text().strip()
        if not text:
            return
        if download_module.is_supported_url(text):
            self._inspect()
        else:
            self._search()

    def _update_action_state(self) -> None:
        is_search = self._mode == "search"
        is_url = self._mode == "url"
        self.results_list.setVisible(is_search and self.results_list.count() > 0)
        self.source_row.setVisible(is_url and self._url_info is not None)
        can_preview_search = is_search and bool(self.results_list.selectedItems())
        can_preview_url = (
            is_url and self._url_info is not None and not self._url_info.is_playlist
        )
        self.preview_button.setEnabled(can_preview_search or can_preview_url)

    # -- search -----------------------------------------------------------
    def _search(self) -> None:
        query = self.smart_input.text().strip()
        if not query:
            return

        self.smart_button.setEnabled(False)
        self.status_label.setText("Searching…")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.results_list.clear()
        self.results_list.setVisible(False)

        def work(context, q):
            return download_module.search(q)

        job = self.jobs.submit_func(f"Searching “{query}”", work, query, category="download")
        job.signals.finished.connect(self._on_searched)

    def _on_searched(self, _job_id: str, state: str, payload) -> None:
        self.smart_button.setEnabled(True)
        if state != "succeeded":
            self.status_label.setText(f"Search failed: {payload}")
            self.status_label.setStyleSheet(f"color: {theme.WARNING};")
            return

        results: list[download_module.SearchResult] = payload
        if not results:
            self.status_label.setText("No results.")
            self.status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
            return

        self.status_label.setText(f"{len(results)} result(s) — double-click one to preview it")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        for result in results:
            detail = " · ".join(
                part for part in (result.source, result.uploader, result.duration_label) if part
            )
            item = QListWidgetItem(f"{result.title}" + (f"  —  {detail}" if detail else ""))
            item.setData(Qt.UserRole, result)
            self.results_list.addItem(item)
        self.results_list.setVisible(True)
        self._update_action_state()

    # -- inspect (URL mode) ----------------------------------------------
    def _inspect(self) -> None:
        url = self.smart_input.text().strip()
        if not download_module.is_supported_url(url):
            return

        self.smart_button.setEnabled(False)
        self.status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.status_label.setText("Checking…")

        def work(context, target):
            return download_module.inspect_url(target)

        job = self.jobs.submit_func("Checking link", work, url, category="download")
        job.signals.finished.connect(self._on_inspected)

    def _on_inspected(self, _job_id: str, state: str, payload) -> None:
        self.smart_button.setEnabled(True)
        if state != "succeeded":
            self.status_label.setStyleSheet(f"color: {theme.WARNING};")
            self.status_label.setText(f"Could not read that link: {payload}")
            self.source_badge.clear_state()
            self._url_info = None
            self._update_action_state()
            return

        info: download_module.UrlInfo = payload
        self._url_info = info
        self.status_label.setStyleSheet(f"color: {theme.TEXT_DIM};")

        if info.is_playlist:
            self.status_label.setText(
                f"Playlist “{info.title}” — {info.entry_count} tracks"
                + (f", {info.duration_label} total" if info.duration else "")
                + " — use Download to fetch the whole playlist (playlists can't be previewed)."
            )
            self.source_badge.clear_state()
        else:
            detail = info.uploader or ""
            self.status_label.setText(
                f"“{info.title}”" + (f" — {detail}" if detail else "")
                + (f" · {info.duration_label}" if info.duration else "")
            )
            codec = (info.best_audio_codec or "unknown").upper()
            bitrate = f"~{info.best_audio_bitrate} kbps" if info.best_audio_bitrate else ""
            self.source_badge.set_lossless(False, f"{codec} {bitrate}".strip())

        self._update_quality_note()
        self._update_action_state()

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

    # -- preview ----------------------------------------------------------
    def _preview_clicked(self) -> None:
        if self._mode == "search":
            items = self.results_list.selectedItems()
            if items:
                self._preview_result(items[0])
        elif self._mode == "url" and self._url_info is not None and not self._url_info.is_playlist:
            self._start_preview(self.smart_input.text().strip(), self._url_info.title)

    def _preview_result(self, item: QListWidgetItem) -> None:
        result: download_module.SearchResult = item.data(Qt.UserRole)
        if result is None:
            return
        self._start_preview(result.url, result.title)

    def _start_preview(self, url: str, title: str) -> None:
        self._discard_current_preview()

        self.preview_button.setEnabled(False)
        self.status_label.setText(f"Fetching “{title}” to preview…")
        self._append_log(f"→ Preview: {title}")

        request = download_module.DownloadRequest(
            url=url,
            output_dir=PREVIEW_CACHE_DIR,
            mode="keep",
            embed_thumbnail=True,
            fetch_artwork=False,
        )

        def work(context, req):
            return download_module.download(req, context=context)

        job = self.jobs.submit_func(f"Fetching preview: {title[:50]}", work, request, category="download")
        job.signals.finished.connect(self._on_preview_downloaded)

    def _discard_current_preview(self) -> None:
        """Best-effort delete of the previous preview's temp file.

        Only one preview is ever kept around at a time -- starting a new
        one discards the last, so there is nothing to bound with an age or
        size sweep. A file still open for playback simply gets skipped and
        is cleaned up by the next app startup's cache wipe.
        """
        track = self._preview_track
        self._preview_track = None
        self.keep_button.setVisible(False)
        if track is None:
            return
        try:
            track.path.unlink(missing_ok=True)
        except OSError:
            pass
        for suffix in _THUMBNAIL_EXTENSIONS:
            try:
                track.path.with_suffix(suffix).unlink(missing_ok=True)
            except OSError:
                pass

    def _on_preview_downloaded(self, _job_id: str, state: str, payload) -> None:
        self._update_action_state()
        if state != "succeeded":
            self.status_label.setText(f"Preview failed: {payload}")
            self._append_log(f"Preview failed: {payload}")
            return

        result: download_module.DownloadResult = payload
        for warning in result.warnings:
            self._append_log(f"⚠ {warning}")
        if not result.tracks:
            self.status_label.setText("Nothing came back from that preview.")
            return

        track = result.tracks[0]
        self._preview_track = track
        self.keep_button.setVisible(True)
        self.status_label.setText(
            f"Playing “{track.path.stem}” — click Keep to save it, or preview something else."
        )
        self.preview_ready.emit(track.path)

    # -- keep ---------------------------------------------------------
    def _keep_preview(self) -> None:
        track = self._preview_track
        if track is None:
            return

        request = download_module.DownloadRequest(
            url=track.url,
            output_dir=Path(self.output_dir.text()),
            mode="convert" if self.convert_radio.isChecked() else "keep",
            profile=self._selected_profile() if self.convert_radio.isChecked() else None,
            embed_thumbnail=self.thumbnail_check.isChecked(),
            fetch_artwork=self.artwork_check.isChecked(),
            source=track.path,
            source_entry=track.raw_entry,
        )

        self.keep_button.setEnabled(False)
        self._append_log(f"→ Keep: {track.title}")

        def work(context, req):
            return download_module.download(req, context=context)

        job = self.jobs.submit_func(f"Saving {track.title[:50]}", work, request, category="download")
        job.signals.finished.connect(self._on_kept)

    def _on_kept(self, _job_id: str, state: str, payload) -> None:
        self.keep_button.setEnabled(True)
        if state != "succeeded":
            self._append_log(f"Keep failed: {payload}")
            self.status_label.setText(f"Could not save that: {payload}")
            return

        result: download_module.DownloadResult = payload
        for track in result.tracks:
            self._append_log(f"✓ {track.path.name}")
        for warning in result.warnings:
            self._append_log(f"⚠ {warning}")

        self.downloaded.emit([t.path for t in result.tracks])
        self._present_saved_paths([t.path for t in result.tracks])
        # The preview file has already been copied from, not moved -- it is
        # left for the normal discard-on-next-preview / startup cleanup.
        self._preview_track = None
        self.keep_button.setVisible(False)

    # -- direct download (URL mode, skips preview -- e.g. playlists) ------
    def _start_download(self) -> None:
        url = self.smart_input.text().strip()
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
        self._present_saved_paths([t.path for t in result.tracks])

    # -- "where did it save?" --------------------------------------------
    def _present_saved_paths(self, paths: list[Path]) -> None:
        self._last_saved_paths = paths
        if not paths:
            self.show_in_folder_button.setVisible(False)
            return
        if len(paths) == 1:
            self.status_label.setText(f"Saved “{paths[0].stem}”.")
        else:
            self.status_label.setText(f"Saved {len(paths)} tracks.")
        self.show_in_folder_button.setVisible(True)

    def _show_in_folder(self) -> None:
        if not self._last_saved_paths:
            return
        target = (
            self._last_saved_paths[0]
            if len(self._last_saved_paths) == 1
            else Path(self.output_dir.text())
        )
        reveal_in_file_manager(target)
