"""The main window: sidebar navigation over the five panels."""

from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

from .. import APP_NAME, __version__
from ..config import clear_preview_cache, ensure_dirs, get_settings
from ..core import artwork as artwork_module
from ..core import autotrim as autotrim_module
from ..core import ffmpeg
from ..core import tag_fix as tag_fix_module
from ..core import ytmusic as ytmusic_module
from ..core.jobs import JobQueue
from ..db import Library
from . import theme
from .assistant_panel import AssistantPanel
from .convert_panel import ConvertPanel
from .download_panel import DownloadPanel
from .editor_panel import EditorPanel
from .jobs_dock import JobsDock
from .library_view import LibraryPanel
from .settings_panel import SettingsPanel
from .tag_panel import TagPanel
from .widgets.now_playing import NowPlayingBar

#: (label, icon glyph) for each sidebar entry, in order.
PAGES = [
    ("Library", "♪"),
    ("Download", "⤓"),
    ("Convert", "⇄"),
    ("Edit", "∿"),
    ("Tags & art", "◧"),
    ("Assistant", "✦"),
    ("Preferences", "⚙"),
]


def _job_concurrency() -> int:
    """How many jobs may run at once.

    Transcoding is CPU-bound and ffmpeg threads internally, so this is
    deliberately well below the core count -- a job per core thrashes the disk
    and makes the machine unpleasant to use.

    It is equally deliberately above two, which is what this was. Every
    background operation in the app shares this pool, so with only two slots a
    single stuck job -- a download waiting on a dead host, a probe against
    unresponsive storage -- halves the app's capacity, and two stuck jobs stop
    every future action in every panel, permanently. Headroom here is what
    keeps one wedged operation from presenting as the whole app having frozen.
    """
    return max(4, (os.cpu_count() or 4) // 2)


def _app_icon() -> QIcon | None:
    """Locate the icon, whether running from source or from a frozen bundle."""
    import sys

    roots = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        roots.append(Path(meipass) / "assets")
    roots.append(Path(__file__).resolve().parent.parent.parent / "assets")
    for root in roots:
        candidate = root / "icon.ico"
        if candidate.is_file():
            return QIcon(str(candidate))
    return None


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        ensure_dirs()
        # Wipe any preview leftovers a previous session's crash left behind --
        # nothing from a previous session is still running to race against.
        clear_preview_cache()
        self.settings = get_settings()
        self.library = Library()
        self.jobs = JobQueue(max_concurrent=_job_concurrency())

        self.setWindowTitle(f"{APP_NAME} {__version__}")
        icon = _app_icon()
        if icon is not None:
            self.setWindowIcon(icon)
        self.resize(1280, 820)
        self.setMinimumSize(QSize(1000, 680))
        self.setAcceptDrops(True)

        self._build()
        self._connect_panels()
        self._check_ffmpeg()
        self._restore_library()

    # -- construction ---------------------------------------------------
    def _build(self) -> None:
        central = QWidget()
        outer_layout = QVBoxLayout(central)
        outer_layout.setContentsMargins(0, 0, 0, 0)
        outer_layout.setSpacing(0)

        # The sidebar and page stack live in their own row; the playback bar
        # sits below that row so it stays visible no matter which page is
        # showing -- browsing, tagging and listening are not mutually
        # exclusive activities.
        body = QWidget()
        layout = QHBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        outer_layout.addWidget(body, 1)

        # -- sidebar ----------------------------------------------------
        sidebar_container = QWidget()
        sidebar_container.setFixedWidth(190)
        sidebar_layout = QVBoxLayout(sidebar_container)
        sidebar_layout.setContentsMargins(0, 0, 0, 0)
        sidebar_layout.setSpacing(0)

        brand = QLabel(f"  {APP_NAME}")
        brand.setStyleSheet(
            f"font-size: 16px; font-weight: 700; color: {theme.TEXT};"
            f"padding: 18px 12px 14px 12px; background: {theme.BG_DEEP};"
        )
        sidebar_layout.addWidget(brand)

        self.sidebar = QListWidget()
        self.sidebar.setObjectName("Sidebar")
        for label, glyph in PAGES:
            QListWidgetItem(f"  {glyph}   {label}", self.sidebar)
        self.sidebar.setCurrentRow(0)
        self.sidebar.currentRowChanged.connect(self._on_page_changed)
        sidebar_layout.addWidget(self.sidebar, 1)

        version_label = QLabel(f"  v{__version__}")
        version_label.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; font-size: 11px;"
            f"padding: 10px 12px; background: {theme.BG_DEEP};"
        )
        sidebar_layout.addWidget(version_label)
        layout.addWidget(sidebar_container)

        # -- pages ------------------------------------------------------
        self.library_panel = LibraryPanel(self.library, self.jobs)
        self.download_panel = DownloadPanel(self.jobs)
        self.convert_panel = ConvertPanel(self.jobs)
        self.editor_panel = EditorPanel(self.jobs)
        self.tag_panel = TagPanel(self.jobs)
        self.assistant_panel = AssistantPanel(self.jobs, self.library)
        self.settings_panel = SettingsPanel(self.jobs)

        self.stack = QStackedWidget()
        for panel in (
            self.library_panel,
            self.download_panel,
            self.convert_panel,
            self.editor_panel,
            self.tag_panel,
            self.assistant_panel,
            self.settings_panel,
        ):
            self.stack.addWidget(panel)
        layout.addWidget(self.stack, 1)

        # -- persistent playback bar --------------------------------------
        self.now_playing_bar = NowPlayingBar()
        outer_layout.addWidget(self.now_playing_bar)

        self.setCentralWidget(central)

        # -- docks and status -------------------------------------------
        self.jobs_dock = JobsDock(self.jobs, self)
        self.addDockWidget(Qt.RightDockWidgetArea, self.jobs_dock)

        status = QStatusBar()
        self.status_message = QLabel("Ready")
        status.addWidget(self.status_message)
        self.setStatusBar(status)

        self.jobs.queue_changed.connect(self._update_status)

    def _connect_panels(self) -> None:
        """Wire the panels together so actions flow between them."""
        self.library_panel.convert_requested.connect(self._go_convert)
        self.library_panel.edit_requested.connect(self._go_edit)
        self.library_panel.tags_requested.connect(self._go_tags)
        self.library_panel.artwork_requested.connect(self._update_artwork)
        self.library_panel.tags_fix_requested.connect(self._fix_library_tags)
        self.library_panel.ytmusic_format_requested.connect(self._format_for_ytmusic)
        self.library_panel.auto_trim_requested.connect(self._auto_trim_selected)
        self.library_panel.play_requested.connect(self.now_playing_bar.play_queue)
        self.library_panel.selection_changed.connect(self._preload_editor)
        self.download_panel.preview_ready.connect(
            lambda path: self.now_playing_bar.play_queue([path], 0)
        )

        # Two independent playback engines exist -- this bar, for casual
        # listening, and the Editor's own player, for previewing edits --
        # so starting one pauses the other rather than letting both play
        # over each other.
        self.now_playing_bar.playing_changed.connect(self._on_now_playing_started)
        self.editor_panel.player.playing_changed.connect(self._on_editor_playback_started)

        # Anything that produces or changes a file re-indexes it.
        self.download_panel.downloaded.connect(self._reindex)
        self.download_panel.downloaded.connect(self._maybe_autotrim_new_tracks)
        self.convert_panel.converted.connect(self._reindex)
        self.editor_panel.exported.connect(self._reindex)
        self.tag_panel.tags_saved.connect(self._reindex)
        self.assistant_panel.files_changed.connect(self._reindex)

        # A preference change should take effect immediately, not next launch.
        self.settings_panel.settings_changed.connect(self._on_settings_changed)

        # A write to a path currently loaded in either player must release
        # that player's lock on it first -- Windows keeps a file exclusively
        # locked for as long as a QMediaPlayer has it loaded, paused or not.
        self.tag_panel.about_to_write.connect(self._release_file_locks)
        self.editor_panel.about_to_write.connect(self._release_file_locks)
        self.library_panel.about_to_write.connect(self._release_file_locks)

    def _release_file_locks(self, paths: list[Path]) -> None:
        """Stop playback of any of ``paths`` before something writes to it.

        Confirmed via a native crash log: repeated "Access is denied" saves
        traced back to the target file still being loaded in a player --
        Windows keeps a file exclusively locked for as long as a
        QMediaPlayer has it as its source, paused or not. Runs on the main
        thread, synchronously, before the write job is even submitted.
        """
        targets = {Path(p).resolve() for p in paths}
        current = self.now_playing_bar.queue.current
        if current is not None and Path(current).resolve() in targets:
            self.now_playing_bar.player.clear()
        if self.editor_panel.current_path is not None and Path(self.editor_panel.current_path).resolve() in targets:
            self.editor_panel.player.clear()

    # -- navigation -----------------------------------------------------
    def _on_page_changed(self, index: int) -> None:
        self.stack.setCurrentIndex(index)

    def go_to(self, name: str) -> None:
        for index, (label, _) in enumerate(PAGES):
            if label.lower().startswith(name.lower()):
                self.sidebar.setCurrentRow(index)
                return

    def _go_convert(self, paths: list[Path]) -> None:
        self.convert_panel.set_files(paths)
        self.go_to("Convert")

    def _go_edit(self, path: Path) -> None:
        self.editor_panel.load(Path(path))
        self.go_to("Edit")

    def _preload_editor(self, paths: list[Path]) -> None:
        """Keep the Editor loaded with whatever was last selected in the
        Library, so switching to Edit already has something there instead
        of showing "No file loaded" -- without forcing navigation there on
        every click, which is what "Edit tags" auto-navigating used to do
        and was removed for fighting with normal browsing/selecting.

        Deliberately does nothing for zero or multiple selected paths: with
        none selected there is nothing to load, and with several selected
        (building a multi-select for a batch action) it is not obvious
        which one the Editor should show, so whatever it already has stays.
        """
        if len(paths) != 1:
            return
        path = Path(paths[0])
        if path == self.editor_panel.current_path:
            return
        self.editor_panel.load(path)

    def _go_tags(self, paths: list[Path]) -> None:
        self.tag_panel.set_files(paths)
        self.go_to("Tags")

    def _on_now_playing_started(self, playing: bool) -> None:
        if playing and self.editor_panel.player.is_playing:
            self.editor_panel.player.pause()

    def _on_editor_playback_started(self, playing: bool) -> None:
        if playing and self.now_playing_bar.player.is_playing:
            self.now_playing_bar.player.pause()

    def _on_settings_changed(self) -> None:
        """Push new defaults into the panels that display them."""
        settings = self.settings
        self.convert_panel.output_dir.setText(settings.output_dir)
        self.download_panel.output_dir.setText(settings.output_dir)
        # The gain slider's range is a preference, so re-apply it live.
        self.editor_panel.gain_slider.setMaximum(int(settings.max_gain_db * 10))
        self.editor_panel.ceiling_spin.setValue(settings.limiter_ceiling_db)
        self.editor_panel.lufs_spin.setValue(settings.loudnorm_target_lufs)
        # A backend/model/toggle change must take effect on the next command,
        # not require reopening the app.
        self.assistant_panel.refresh_settings()

    # -- actions --------------------------------------------------------
    def _update_artwork(self, paths: list[Path]) -> None:
        if not paths:
            self.status_message.setText("Nothing selected to update")
            return

        def work(context, targets):
            return artwork_module.update_library_artwork(targets, context=context)

        job = self.jobs.submit_func(
            f"Updating artwork for {len(paths)} track(s)", work, paths, category="artwork"
        )
        job.signals.finished.connect(self._on_artwork_finished)

    def _on_artwork_finished(self, _job_id: str, state: str, payload) -> None:
        if state != "succeeded":
            self.status_message.setText(f"Artwork update failed: {payload}")
            return
        updated = [r for r in payload if r.updated]
        self.status_message.setText(
            f"Artwork: updated {len(updated)} of {len(payload)} track(s)"
        )
        self._reindex([r.path for r in updated])

    def _fix_library_tags(self, paths: list[Path]) -> None:
        if not paths:
            self.status_message.setText("Nothing selected to fix")
            return

        def work(context, targets):
            return tag_fix_module.fix_library_tags(targets, context=context)

        job = self.jobs.submit_func(
            f"Fixing metadata for {len(paths)} track(s)", work, paths, category="tags"
        )
        job.signals.finished.connect(self._on_tags_fixed)

    def _on_tags_fixed(self, _job_id: str, state: str, payload) -> None:
        if state != "succeeded":
            self.status_message.setText(f"Fix metadata failed: {payload}")
            return
        updated = [r for r in payload if r.updated]
        self.status_message.setText(
            f"Fix metadata: updated {len(updated)} of {len(payload)} track(s)"
        )
        self._reindex([r.path for r in updated])

    def _format_for_ytmusic(self, paths: list[Path]) -> None:
        if not paths:
            self.status_message.setText("Nothing selected to format")
            return

        confirmed = QMessageBox.question(
            self,
            "YouTube Music format",
            f"Reshape the tags of {len(paths)} track(s) to YouTube Music's conventions?\n\n"
            "This rewrites titles, artists, album artists and genres — unlike "
            "“Fix metadata”, which only fills in blanks.\n\n"
            "The current tags are saved to a snapshot first, so this can be undone.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirmed != QMessageBox.Yes:
            return

        def work(context, targets):
            from ..config import DATA_DIR

            context.progress(0.0, "Saving a snapshot of the current tags…")
            snapshot = ytmusic_module.snapshot_tags(
                [Path(t) for t in targets], Path(DATA_DIR) / "ytmusic-tag-snapshot.json"
            )
            results = ytmusic_module.normalise_library(targets, context=context)
            return snapshot, results

        job = self.jobs.submit_func(
            f"YouTube Music format for {len(paths)} track(s)", work, paths, category="tags"
        )
        job.signals.finished.connect(self._on_ytmusic_formatted)

    def _on_ytmusic_formatted(self, _job_id: str, state: str, payload) -> None:
        if state != "succeeded":
            self.status_message.setText(f"YouTube Music format failed: {payload}")
            return
        snapshot, results = payload
        updated = [r for r in results if r.updated]
        self.status_message.setText(
            f"YouTube Music format: updated {len(updated)} of {len(results)} track(s) "
            f"· previous tags saved to {snapshot.name}"
        )
        self._reindex([r.path for r in updated])

    def _maybe_autotrim_new_tracks(self, paths: list[Path]) -> None:
        """Auto-trim right after a download, when the user has opted in.

        Gated by both toggles rather than just clicking "Auto-trim all…" --
        the settings preference must actually be turned on before this ever
        touches a file the user did not explicitly select.
        """
        settings = get_settings()
        if not (settings.auto_trim_enabled and settings.auto_trim_new_tracks) or not paths:
            return
        trim_settings = autotrim_module.AutoTrimSettings.from_settings(settings)

        def work(context, targets):
            return autotrim_module.autotrim_library(
                targets, library=self.library, settings=trim_settings, context=context
            )

        job = self.jobs.submit_func(
            f"Auto-trimming {len(paths)} new track(s)", work, paths, category="autotrim"
        )
        job.signals.finished.connect(self._on_autotrim_finished)

    def _auto_trim_selected(self, paths: list[Path]) -> None:
        if not paths:
            self.status_message.setText("Nothing selected to auto-trim")
            return
        self._release_file_locks(paths)
        trim_settings = autotrim_module.AutoTrimSettings.from_settings(get_settings())

        def work(context, targets):
            # An explicit selection always re-evaluates, ignoring a prior
            # applied/skipped state and the video-source heuristic gate.
            return autotrim_module.autotrim_library(
                targets, library=self.library, settings=trim_settings, context=context, force=True
            )

        job = self.jobs.submit_func(
            f"Auto-trimming {len(paths)} track(s)", work, paths, category="autotrim"
        )
        job.signals.finished.connect(self._on_autotrim_finished)

    def _on_autotrim_finished(self, _job_id: str, state: str, payload) -> None:
        if state != "succeeded":
            self.status_message.setText(f"Auto-trim failed: {payload}")
            return
        applied = [o for o in payload if o.updated]
        self.status_message.setText(
            f"Auto-trim: trimmed {len(applied)} of {len(payload)} track(s)"
        )
        self._reindex([o.path for o in applied])

    def _reindex(self, paths: list[Path]) -> None:
        """Re-scan files that changed, then refresh the library view."""
        if not paths:
            return

        def work(context, targets):
            from ..db import scan_into_library

            return scan_into_library(self.library, list(targets), context=context, force=True)

        job = self.jobs.submit_func(
            f"Indexing {len(paths)} file(s)", work, paths, category="import"
        )
        job.signals.finished.connect(lambda *_: self.library_panel.refresh())

    def _update_status(self) -> None:
        active = self.jobs.active_count()
        self.status_message.setText(f"{active} job(s) running" if active else "Ready")

    def _restore_library(self) -> None:
        """Re-index remembered folders at startup.

        The database already holds everything, so this is only to pick up
        files added or removed outside the app since it last ran. It is
        skipped when nothing is remembered yet.
        """
        if not self.settings.library_paths:
            return
        self.library_panel.rescan_known_roots()

    # -- environment ----------------------------------------------------
    def _check_ffmpeg(self) -> None:
        """Fail loudly and early if ffmpeg is missing.

        Nearly every feature depends on it, so discovering this at the first
        conversion would be a much worse experience than a message on startup.
        """
        if ffmpeg.is_available():
            return
        QMessageBox.warning(
            self,
            "ffmpeg not found",
            f"{APP_NAME} could not find ffmpeg, which it needs for every audio "
            f"operation.\n\nThe packaged build ships ffmpeg inside the application "
            f"folder. If you are running from source, install ffmpeg and make sure "
            f"it is on your PATH, or place ffmpeg and ffprobe in a 'vendor/ffmpeg' "
            f"folder next to the application.",
        )

    # -- window-level drag and drop -------------------------------------
    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(u.toLocalFile()) for u in event.mimeData().urls() if u.isLocalFile()]
        if paths:
            self.library_panel.import_paths(paths)
            self.go_to("Library")
            event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        self.jobs.cancel_all()
        self.jobs.wait_for_done(3000)
        clear_preview_cache()
        self.library.close()
        super().closeEvent(event)
