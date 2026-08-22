"""The audio editor: waveform, region selection and a stack of effects.

Nothing is applied until you export. The original file is never overwritten
unless you explicitly choose to.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import get_settings
from ..core import convert as convert_module
from ..core import crash_log
from ..core import edit as edit_module
from ..core import formats, probe
from ..core.edit import ChannelMode, EditSpec, EqBand, GainMode, Region, SilenceMode
from . import theme
from .common import QualityBadge, card, format_duration, heading, row, section_label, spacer
from .widgets.async_read import AsyncProbeReader
from .widgets.player import Player, PreviewController
from .widgets.waveform import WaveformView, compute_peaks


class EditorPanel(QWidget):
    """Waveform editor with a non-destructive effect stack."""

    exported = Signal(list)
    #: Emitted right before a write is submitted, so MainWindow can release
    #: a player's lock on any of these paths first (Windows keeps a file
    #: exclusively locked for as long as a QMediaPlayer has it loaded).
    about_to_write = Signal(list)

    def __init__(self, job_queue, parent=None) -> None:
        super().__init__(parent)
        self.jobs = job_queue
        self.settings = get_settings()
        self._path: Path | None = None
        self._info: probe.AudioInfo | None = None
        self._cuts: list[Region] = []
        # ffprobe runs off the GUI thread; see AsyncProbeReader.
        self._probe_reader = AsyncProbeReader(self)
        self._probe_reader.ready.connect(self._on_probe_ready)
        self._build()

    # -- construction ---------------------------------------------------
    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        layout.addWidget(
            heading(
                "Edit",
                "Drag across the waveform to select a region. Effects are previewed as "
                "settings and applied once, on export — the original file is untouched.",
            )
        )

        # -- file bar ---------------------------------------------------
        self.file_label = QLabel("No file loaded")
        self.file_label.setObjectName("SubHeading")
        self.source_badge = QualityBadge()
        open_button = QPushButton("Open file…")
        open_button.clicked.connect(self._choose_file)
        layout.addWidget(row(self.file_label, spacer(), self.source_badge, open_button, spacing=10))

        # -- waveform ---------------------------------------------------
        self.waveform = WaveformView()
        self.waveform.selection_changed.connect(self._on_selection)
        self.waveform.position_clicked.connect(self._on_waveform_clicked)
        layout.addWidget(self.waveform, 1)

        # -- transport --------------------------------------------------
        self.player = Player()
        self.player.position_changed.connect(self.waveform.set_playhead)
        self.player.play_requested.connect(self._on_play_requested)

        self.preview = PreviewController(self.player, self.jobs, self)

        self.play_selection_button = QPushButton("Play selection")
        self.play_selection_button.setToolTip("Play only the highlighted region")
        self.play_selection_button.clicked.connect(self._play_selection)

        self.preview_button = QPushButton("Preview with effects")
        self.preview_button.setToolTip(
            "Render a short excerpt with the current effects applied and play it, "
            "so you hear exactly what would be exported"
        )
        self.preview_button.clicked.connect(self._preview_effects)

        layout.addWidget(
            row(self.player, self.play_selection_button, self.preview_button, spacing=8)
        )
        layout.addWidget(self.player.status_label)

        self.selection_label = QLabel("No selection")
        self.selection_label.setObjectName("Hint")

        self.trim_button = QPushButton("Keep selection")
        self.trim_button.setToolTip("Discard everything outside the selected region")
        self.trim_button.clicked.connect(self._keep_selection)
        self.cut_button = QPushButton("Cut selection")
        self.cut_button.setToolTip("Remove the selected region and close the gap")
        self.cut_button.clicked.connect(self._cut_selection)
        self.reset_button = QPushButton("Reset edits")
        self.reset_button.clicked.connect(self.reset_edits)

        layout.addWidget(
            row(
                self.selection_label,
                spacer(),
                self.trim_button,
                self.cut_button,
                self.reset_button,
                spacing=8,
            )
        )

        # -- effect tabs ------------------------------------------------
        tabs = QTabWidget()
        tabs.addTab(self._build_level_tab(), "Volume")
        tabs.addTab(self._build_speed_tab(), "Speed && pitch")  # && renders one literal &
        tabs.addTab(self._build_eq_tab(), "Equaliser")
        tabs.addTab(self._build_cleanup_tab(), "Cleanup")
        tabs.setMaximumHeight(300)
        layout.addWidget(tabs)

        # -- export -----------------------------------------------------
        self.export_format = QComboBox()
        for profile in formats.ALL_PROFILES:
            self.export_format.addItem(profile.label, profile.id)

        self.export_button = QPushButton("Export")
        self.export_button.setObjectName("Primary")
        self.export_button.setMinimumWidth(130)
        self.export_button.clicked.connect(self._export)
        self.export_button.setEnabled(False)

        self.save_button = QPushButton("Save")
        self.save_button.setToolTip(
            "Apply these edits to the library file in place, keeping its current "
            "format and location -- no new file, no Export dialog."
        )
        self.save_button.clicked.connect(self._save_in_place)
        self.save_button.setEnabled(False)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("Hint")
        self.summary_label.setWordWrap(True)

        layout.addWidget(
            row(
                self.summary_label,
                spacer(),
                self.save_button,
                QLabel("Export as"),
                self.export_format,
                self.export_button,
                spacing=8,
            )
        )

    # -- tab: level -----------------------------------------------------
    def _build_level_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        # Gain slider runs well past 0 dB. Stored in tenths of a dB so the
        # integer slider can still offer 0.1 dB precision.
        self.gain_slider = QSlider(Qt.Horizontal)
        max_gain = int(self.settings.max_gain_db * 10)
        self.gain_slider.setRange(-600, max_gain)
        self.gain_slider.setValue(0)
        self.gain_slider.valueChanged.connect(self._on_gain_changed)

        self.gain_label = QLabel("0.0 dB  (100%)")
        self.gain_label.setMinimumWidth(150)

        reset_gain = QPushButton("Reset")
        reset_gain.clicked.connect(lambda: self.gain_slider.setValue(0))

        self.gain_mode = QComboBox()
        self.gain_mode.addItem("Boost, then limit peaks (recommended)", GainMode.LIMIT.value)
        self.gain_mode.addItem("Compress, then boost (loudest)", GainMode.COMPRESS.value)
        self.gain_mode.addItem("Raw gain — allow clipping", GainMode.RAW.value)
        self.gain_mode.currentIndexChanged.connect(self._update_gain_warning)
        self.gain_mode.currentIndexChanged.connect(self._update_summary)

        self.ceiling_spin = QDoubleSpinBox()
        self.ceiling_spin.setRange(-6.0, 0.0)
        self.ceiling_spin.setSingleStep(0.1)
        self.ceiling_spin.setValue(self.settings.limiter_ceiling_db)
        self.ceiling_spin.setSuffix(" dBTP ceiling")
        self.ceiling_spin.valueChanged.connect(self._update_summary)

        self.gain_warning = QLabel("")
        self.gain_warning.setWordWrap(True)
        self.gain_warning.setObjectName("Hint")

        self.analyse_button = QPushButton("Measure clipping")
        self.analyse_button.setToolTip(
            "Decode the file at this gain and count exactly how many samples clip"
        )
        self.analyse_button.clicked.connect(self._measure_clipping)

        layout.addWidget(section_label("Gain"))
        layout.addWidget(row(self.gain_slider, self.gain_label, reset_gain, spacing=10))
        layout.addWidget(row(self.gain_mode, self.ceiling_spin, self.analyse_button, spacing=8))
        self.gain_warning.setMinimumHeight(34)
        layout.addWidget(self.gain_warning)

        # -- normalization ----------------------------------------------
        self.normalize_check = QCheckBox("Normalise loudness to")
        self.normalize_check.toggled.connect(self._update_summary)
        self.lufs_spin = QDoubleSpinBox()
        self.lufs_spin.setRange(-30.0, -5.0)
        self.lufs_spin.setSingleStep(0.5)
        self.lufs_spin.setValue(self.settings.loudnorm_target_lufs)
        self.lufs_spin.setSuffix(" LUFS")

        self.dynaudnorm_check = QCheckBox(
            "Make quiet parts loud (dynamic normalisation — good for speech and old recordings)"
        )
        self.dynaudnorm_check.toggled.connect(self._update_summary)

        self.fade_in = QDoubleSpinBox()
        self.fade_in.setRange(0, 60)
        self.fade_in.setSingleStep(0.5)
        self.fade_in.setSuffix(" s fade in")
        self.fade_out = QDoubleSpinBox()
        self.fade_out.setRange(0, 60)
        self.fade_out.setSingleStep(0.5)
        self.fade_out.setSuffix(" s fade out")

        layout.addWidget(section_label("Loudness"))
        layout.addWidget(row(self.normalize_check, self.lufs_spin, spacer(), spacing=8))
        layout.addWidget(self.dynaudnorm_check)
        layout.addWidget(row(self.fade_in, self.fade_out, spacer(), spacing=8))
        layout.addStretch(1)

        self._on_gain_changed(0)
        return _scrollable(page)

    # -- tab: speed & pitch ---------------------------------------------
    def _build_speed_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.tempo_spin = QDoubleSpinBox()
        self.tempo_spin.setRange(0.25, 4.0)
        self.tempo_spin.setSingleStep(0.05)
        self.tempo_spin.setValue(1.0)
        self.tempo_spin.setSuffix("× speed")
        self.tempo_spin.valueChanged.connect(self._update_summary)

        self.pitch_spin = QDoubleSpinBox()
        self.pitch_spin.setRange(-24.0, 24.0)
        self.pitch_spin.setSingleStep(1.0)
        self.pitch_spin.setValue(0.0)
        self.pitch_spin.setSuffix(" semitones")
        self.pitch_spin.valueChanged.connect(self._update_summary)

        hint = QLabel(
            "Speed changes keep the pitch, and pitch changes keep the speed — "
            "they are independent."
        )
        hint.setObjectName("Hint")
        hint.setWordWrap(True)

        layout.addWidget(section_label("Tempo and pitch"))
        layout.addWidget(row(self.tempo_spin, self.pitch_spin, spacer(), spacing=8))
        layout.addWidget(hint)
        layout.addStretch(1)
        return _scrollable(page)

    # -- tab: EQ ---------------------------------------------------------
    def _build_eq_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 10, 14, 14)
        layout.setSpacing(8)

        bands = QWidget()
        grid = QGridLayout(bands)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(4)

        self.eq_sliders: list[tuple[int, QSlider, QLabel]] = []
        for column, frequency in enumerate(edit_module.DEFAULT_EQ_FREQUENCIES):
            slider = QSlider(Qt.Vertical)
            slider.setRange(-120, 120)  # ±12 dB in tenths
            slider.setValue(0)
            slider.setMinimumHeight(110)
            value_label = QLabel("0")
            value_label.setAlignment(Qt.AlignCenter)
            value_label.setObjectName("Hint")

            frequency_label = QLabel(
                f"{frequency // 1000}k" if frequency >= 1000 else str(frequency)
            )
            frequency_label.setAlignment(Qt.AlignCenter)
            frequency_label.setObjectName("Hint")

            slider.valueChanged.connect(
                lambda value, lbl=value_label: (
                    lbl.setText(f"{value / 10:+.1f}" if value else "0"),
                    self._update_summary(),
                )
            )

            grid.addWidget(value_label, 0, column)
            grid.addWidget(slider, 1, column, Qt.AlignHCenter)
            grid.addWidget(frequency_label, 2, column)
            self.eq_sliders.append((frequency, slider, value_label))

        reset_eq = QPushButton("Flatten")
        reset_eq.clicked.connect(
            lambda: [slider.setValue(0) for _, slider, _ in self.eq_sliders]
        )

        layout.addWidget(row(section_label("10-band equaliser (dB)"), spacer(), reset_eq))
        layout.addWidget(bands)
        layout.addStretch(1)
        return _scrollable(page)

    # -- tab: cleanup ----------------------------------------------------
    def _build_cleanup_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.silence_combo = QComboBox()
        self.silence_combo.addItem("Leave silence alone", SilenceMode.NONE.value)
        self.silence_combo.addItem("Trim silence from the start", SilenceMode.LEADING.value)
        self.silence_combo.addItem("Trim silence from the end", SilenceMode.TRAILING.value)
        self.silence_combo.addItem("Trim silence from both ends", SilenceMode.BOTH.value)
        self.silence_combo.currentIndexChanged.connect(self._update_summary)

        self.silence_threshold = QDoubleSpinBox()
        self.silence_threshold.setRange(-90.0, -20.0)
        self.silence_threshold.setValue(-50.0)
        self.silence_threshold.setSuffix(" dB threshold")

        self.channel_combo = QComboBox()
        self.channel_combo.addItem("Keep channels as they are", ChannelMode.KEEP.value)
        self.channel_combo.addItem("Convert to mono", ChannelMode.MONO.value)
        self.channel_combo.addItem("Convert to stereo", ChannelMode.STEREO.value)
        self.channel_combo.addItem("Swap left and right", ChannelMode.SWAP.value)
        self.channel_combo.currentIndexChanged.connect(self._update_summary)

        self.rate_combo = QComboBox()
        self.rate_combo.addItem("Keep sample rate", 0)
        for rate in (192000, 96000, 48000, 44100, 32000, 22050):
            self.rate_combo.addItem(f"{rate / 1000:g} kHz", rate)
        self.rate_combo.currentIndexChanged.connect(self._update_summary)

        layout.addWidget(section_label("Silence"))
        layout.addWidget(row(self.silence_combo, self.silence_threshold, spacer(), spacing=8))
        layout.addWidget(section_label("Channels and rate"))
        layout.addWidget(row(self.channel_combo, self.rate_combo, spacer(), spacing=8))
        layout.addStretch(1)
        return _scrollable(page)

    # -- loading --------------------------------------------------------
    @property
    def current_path(self) -> Path | None:
        """The file currently loaded in the editor, if any."""
        return self._path

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Open an audio file", "", "Audio files (*);;All files (*)"
        )
        if path:
            self.load(Path(path))

    def load(self, path: Path) -> None:
        """Load a file and render its waveform in the background."""
        self._path = Path(path)
        self.reset_edits()
        self.file_label.setText(self._path.name)

        # Probing spawns ffprobe. Inline here -- and this runs on every Library
        # row click, via MainWindow._preload_editor -- a slow or stalled process
        # froze the whole window for as long as its timeout allowed.
        self._info = None
        self.export_button.setEnabled(False)
        self.save_button.setEnabled(False)
        probed = self._probe_reader.request(self._path)
        if probed is not False:
            self._apply_probe(self._path, probed)

        # Any in-flight preview belongs to the previous file.
        self.preview.cancel()
        self.player.load(self._path)
        self.player.set_status("")
        self.preview.configure(self._path, self.build_spec)

        self.waveform.set_data(None, "Rendering waveform…")

        def work(context, target):
            context.progress(None, "Reading audio")
            return compute_peaks(target, buckets=2400, should_cancel=context.is_cancelled)

        job = self.jobs.submit_func(
            f"Waveform for {self._path.name}", work, self._path, category="waveform"
        )
        job.signals.finished.connect(self._on_waveform_ready)

    def _on_probe_ready(self, path: Path, info) -> None:
        # Ignore a probe that landed after the user selected a different file.
        if self._path is None or Path(path) != Path(self._path):
            return
        self._apply_probe(Path(path), info)

    def _apply_probe(self, path: Path, info) -> None:
        self._info = info
        if info is not None:
            self.source_badge.set_lossless(info.is_lossless, info.describe_technical())
            index = self.export_format.findData(
                (formats.profile_for_extension(path.suffix) or formats.FLAC).id
            )
            self.export_format.setCurrentIndex(max(0, index))
        self.export_button.setEnabled(info is not None)
        self.save_button.setEnabled(info is not None)
        self._update_summary()

    def _on_waveform_ready(self, _job_id: str, state: str, payload) -> None:
        if state != "succeeded":
            self.waveform.set_data(None, f"Could not render waveform: {payload}")
            return
        self.waveform.set_data(payload)
        self._update_summary()

    # -- playback -------------------------------------------------------
    def _on_waveform_clicked(self, seconds: float) -> None:
        """A click on the waveform seeks there, as in any audio editor."""
        self.player.seek(seconds)
        self.waveform.set_playhead(seconds)

    def _play_selection(self) -> None:
        selection = self.waveform.selection()
        if selection is None:
            self.player.set_status("Drag across the waveform to select a region first")
            return
        if self.build_spec().is_empty:
            self.player.play(start=selection[0], stop=selection[1])
            return
        # The rendered preview is a fixed-length excerpt, so it cannot honor
        # a selection end -- the same limitation "Preview with effects" has.
        self.preview.configure(self._path, self.build_spec)
        self.preview.request(selection[0])

    def _on_play_requested(self) -> None:
        """Route Play/spacebar through the effect chain, unless there is none.

        The transport otherwise plays the raw source, so a gain increase (or
        any other edit) would silently have no audible effect until the user
        found the separate "Preview with effects" button.
        """
        if self._path is None or self.build_spec().is_empty:
            self.player.play()
            return
        self.preview.configure(self._path, self.build_spec)
        self.preview.request(self.player.position)

    def _preview_effects(self) -> None:
        """Render and play a short excerpt with the effect stack applied."""
        if self._path is None:
            return
        spec = self.build_spec()
        if spec.is_empty:
            # Nothing to apply, so play the original rather than spending a
            # render on producing an identical copy.
            self.player.load(self._path)
            self.player.play()
            self.player.set_status("No effects applied — playing the original")
            return
        # Preview from the selection if there is one, else from the playhead.
        selection = self.waveform.selection()
        start = selection[0] if selection else 0.0
        self.preview.configure(self._path, self.build_spec)
        self.preview.request(start)

    def _refresh_preview_if_playing(self) -> None:
        """Keep an in-progress preview in sync with the controls being edited.

        Without this, dragging the gain slider while a preview is already
        playing would only take effect the next time Play is pressed.
        """
        if not self._is_fully_built() or self._path is None or not self.player.is_playing:
            return
        if self.build_spec().is_empty:
            return
        self.preview.configure(self._path, self.build_spec)
        self.preview.request(self.player.position)

    def keyPressEvent(self, event) -> None:
        # Space is the universal play/pause in audio tools.
        if event.key() == Qt.Key_Space and self.player.isEnabled():
            self.player.toggle()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- selection ------------------------------------------------------
    def _on_selection(self, start: float, end: float) -> None:
        self.selection_label.setText(
            f"Selected {format_duration(start)} – {format_duration(end)} "
            f"({end - start:.2f} s)"
        )
        self._update_summary()

    def _keep_selection(self) -> None:
        selection = self.waveform.selection()
        if selection is None:
            self.selection_label.setText("Drag across the waveform to select a region first")
            return
        self._trim = Region(selection[0], selection[1])
        self.selection_label.setText(
            f"Keeping {format_duration(selection[0])} – {format_duration(selection[1])}"
        )
        self._update_summary()

    def _cut_selection(self) -> None:
        selection = self.waveform.selection()
        if selection is None:
            self.selection_label.setText("Drag across the waveform to select a region first")
            return
        self._cuts.append(Region(selection[0], selection[1]))
        self.waveform.set_cuts([(c.start, c.end or 0.0) for c in self._cuts])
        self.waveform.clear_selection()
        self.selection_label.setText(f"{len(self._cuts)} region(s) marked for removal")
        self._update_summary()

    def reset_edits(self) -> None:
        self._trim: Region | None = None
        self._cuts = []
        self.waveform.set_cuts([])
        self.waveform.clear_selection()
        self.selection_label.setText("No selection")
        if hasattr(self, "gain_slider"):
            self.gain_slider.setValue(0)
            self.normalize_check.setChecked(False)
            self.dynaudnorm_check.setChecked(False)
            self.fade_in.setValue(0)
            self.fade_out.setValue(0)
            self.tempo_spin.setValue(1.0)
            self.pitch_spin.setValue(0.0)
            self.silence_combo.setCurrentIndex(0)
            self.channel_combo.setCurrentIndex(0)
            self.rate_combo.setCurrentIndex(0)
            for _, slider, _ in self.eq_sliders:
                slider.setValue(0)
        self._update_summary()

    # -- gain -----------------------------------------------------------
    def _on_gain_changed(self, raw: int) -> None:
        gain_db = raw / 10.0
        percent = edit_module.db_to_linear(gain_db) * 100
        self.gain_label.setText(f"{gain_db:+.1f} dB  ({percent:.0f}%)")
        self._update_gain_warning()
        self._update_summary()

    def _update_gain_warning(self) -> None:
        gain_db = self.gain_slider.value() / 10.0
        mode = self.gain_mode.currentData()
        self.ceiling_spin.setEnabled(mode != GainMode.RAW.value)

        if gain_db <= 0:
            self.gain_warning.setText("")
            self.gain_warning.setStyleSheet(f"color: {theme.TEXT_DIM};")
            return

        if mode == GainMode.RAW.value:
            self.gain_warning.setText(
                f"Raw gain of {gain_db:+.1f} dB. Anything that goes past full scale will "
                f"be clipped flat, which sounds like distortion. Measure it to see how much."
            )
            self.gain_warning.setStyleSheet(f"color: {theme.WARNING};")
        elif mode == GainMode.COMPRESS.value:
            self.gain_warning.setText(
                f"Compressing, then boosting {gain_db:+.1f} dB and limiting at "
                f"{self.ceiling_spin.value():+.1f} dBTP. Loudest option; reduces dynamic range."
            )
            self.gain_warning.setStyleSheet(f"color: {theme.TEXT_DIM};")
        else:
            self.gain_warning.setText(
                f"Boosting {gain_db:+.1f} dB with a lookahead limiter at "
                f"{self.ceiling_spin.value():+.1f} dBTP, so it gets loud without clipping. "
                f"If the source is already loud, extra gain here may stop making an audible "
                f"difference — use Measure clipping to check exactly how close you are to the "
                f"ceiling."
            )
            self.gain_warning.setStyleSheet(f"color: {theme.LOSSLESS};")

    def _measure_clipping(self) -> None:
        """Measure the signal as it would actually be exported.

        Measuring a bare gain would be misleading: the limiter, the compressor,
        loudness normalisation and even the EQ all change where the peaks land.
        A +48 dB boost clips badly in raw mode and not at all with the limiter
        engaged, so the number has to come from the real chain.
        """
        if self._path is None or self._info is None:
            return
        self.analyse_button.setEnabled(False)
        filters = edit_module.build_filter_chain(self.build_spec(), self._info)

        def work(context, target, chain):
            context.progress(None, "Analysing levels")
            return probe.measure_clipping(target, filters=chain)

        job = self.jobs.submit_func(
            "Measuring clipping", work, self._path, filters, category="analysis"
        )
        job.signals.finished.connect(self._on_clipping_measured)

    def _on_clipping_measured(self, _job_id: str, state: str, payload) -> None:
        self.analyse_button.setEnabled(True)
        if state != "succeeded":
            return
        report: probe.ClippingReport = payload
        self.gain_warning.setText(report.describe())
        self.gain_warning.setStyleSheet(
            f"color: {theme.DANGER if report.clips else theme.LOSSLESS};"
        )

    # -- spec -----------------------------------------------------------
    def _is_fully_built(self) -> bool:
        """Whether every control the spec reads from exists yet."""
        return all(
            hasattr(self, name)
            for name in (
                "summary_label", "gain_slider", "gain_mode", "ceiling_spin",
                "normalize_check", "lufs_spin", "dynaudnorm_check",
                "fade_in", "fade_out", "tempo_spin", "pitch_spin",
                "eq_sliders", "silence_combo", "silence_threshold",
                "channel_combo", "rate_combo",
            )
        )

    def build_spec(self) -> EditSpec:
        """Collect every control into an :class:`EditSpec`."""
        bands = [
            EqBand(frequency, slider.value() / 10.0)
            for frequency, slider, _ in self.eq_sliders
            if slider.value() != 0
        ]
        return EditSpec(
            trim=getattr(self, "_trim", None),
            cuts=list(self._cuts),
            gain_db=self.gain_slider.value() / 10.0,
            gain_mode=GainMode(self.gain_mode.currentData()),
            limiter_ceiling_db=self.ceiling_spin.value(),
            normalize=self.normalize_check.isChecked(),
            normalize_target_lufs=self.lufs_spin.value(),
            dynamic_normalize=self.dynaudnorm_check.isChecked(),
            fade_in=self.fade_in.value(),
            fade_out=self.fade_out.value(),
            tempo=self.tempo_spin.value(),
            pitch_semitones=self.pitch_spin.value(),
            eq_bands=bands,
            trim_silence=SilenceMode(self.silence_combo.currentData()),
            silence_threshold_db=self.silence_threshold.value(),
            channel_mode=ChannelMode(self.channel_combo.currentData()),
            sample_rate=self.rate_combo.currentData() or None,
        )

    def _update_summary(self, *_) -> None:
        # Tabs are built in sequence and each one refreshes on construction, so
        # this can fire before the later widgets exist. Wait until the whole
        # panel is assembled rather than ordering the tabs by coincidence.
        if not self._is_fully_built():
            return
        if self._info is None:
            self.summary_label.setText("")
            return
        spec = self.build_spec()
        if spec.is_empty:
            self.summary_label.setText("No edits — export would just change the format.")
            return

        parts: list[str] = []
        if spec.trim is not None:
            parts.append("trimmed")
        if spec.cuts:
            parts.append(f"{len(spec.cuts)} cut(s)")
        if abs(spec.gain_db) > 0.05:
            parts.append(f"{spec.gain_db:+.1f} dB")
        if spec.normalize:
            parts.append(f"normalised to {spec.normalize_target_lufs:g} LUFS")
        if spec.dynamic_normalize:
            parts.append("dynamic normalise")
        if abs(spec.tempo - 1.0) > 0.001:
            parts.append(f"{spec.tempo:g}× speed")
        if abs(spec.pitch_semitones) > 0.001:
            parts.append(f"{spec.pitch_semitones:+g} semitones")
        if spec.eq_bands:
            parts.append(f"EQ ({len(spec.eq_bands)} bands)")
        if spec.trim_silence is not SilenceMode.NONE:
            parts.append("silence trimmed")
        if spec.channel_mode is not ChannelMode.KEEP:
            parts.append(spec.channel_mode.value)
        if spec.sample_rate:
            parts.append(f"{spec.sample_rate / 1000:g} kHz")

        duration = spec.estimated_duration(self._info.duration)
        self.summary_label.setText(
            f"{' · '.join(parts)}  →  {format_duration(duration)}"
        )
        self._refresh_preview_if_playing()

    # -- export ---------------------------------------------------------
    def _export(self) -> None:
        if self._path is None or self._info is None:
            return
        profile = formats.get_profile(self.export_format.currentData())
        suggested = self._path.with_name(f"{self._path.stem} (edited){profile.extension}")

        destination, _ = QFileDialog.getSaveFileName(
            self, "Export edited audio", str(suggested), f"{profile.label} (*{profile.extension})"
        )
        if not destination:
            return

        request = convert_module.ConvertRequest(
            source=self._path,
            destination=Path(destination),
            profile=profile,
            edits=self.build_spec(),
            overwrite=True,
        )
        self.export_button.setEnabled(False)

        def work(context, req):
            result = convert_module.convert(context=context, request=req)
            from ..core import tags as tags_module

            try:
                tags_module.copy_tags(req.source, result.destination)
            except tags_module.TagError:
                pass
            return result

        job = self.jobs.submit_func(
            f"Exporting {Path(destination).name}", work, request, category="export"
        )
        job.signals.finished.connect(self._on_exported)

    def _on_exported(self, _job_id: str, state: str, payload) -> None:
        self.export_button.setEnabled(True)
        if state == "succeeded":
            self.summary_label.setText(f"Exported {payload.destination.name}")
            self.exported.emit([payload.destination])
        elif state == "failed":
            self.summary_label.setText(f"Export failed: {payload}")

    def _save_in_place(self) -> None:
        """Apply the current edits to the library file itself.

        ffmpeg cannot read and overwrite the same file at once, so `convert()`
        deliberately refuses a destination that resolves to its own source
        (see core/convert.py). The safe way to still end up with the edits
        applied at the *same path* is: encode to a temp file beside it, carry
        the original tags over, then atomically replace the original --
        nothing at the real path changes until the very last step succeeds.
        """
        if self._path is None or self._info is None:
            return
        if self.build_spec().is_empty:
            self.summary_label.setText("No edits to save.")
            return

        profile = formats.profile_for_extension(self._path.suffix)
        if profile is None:
            self.summary_label.setText(
                f"Don't know how to re-encode {self._path.suffix} files in place."
            )
            return

        confirmed = QMessageBox.question(
            self,
            "Save in place",
            f"Apply these edits to \"{self._path.name}\" and overwrite it?\n\n"
            "This replaces the file in your library. The original audio is not kept.",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if confirmed != QMessageBox.Yes:
            return

        source = self._path
        spec = self.build_spec()
        self.save_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.about_to_write.emit([source])

        def work(context, source, spec, profile):
            from ..core import tags as tags_module

            crash_log.debug(f"save_in_place: start for {source.name}")
            original_tags = tags_module.try_read(source)
            tmp_destination = source.with_name(f".{source.stem}.tmp{profile.extension}")
            request = convert_module.ConvertRequest(
                source=source,
                destination=tmp_destination,
                profile=profile,
                edits=spec,
                overwrite=True,
            )
            try:
                result = convert_module.convert(context=context, request=request)
                try:
                    tags_module.write(
                        result.destination, original_tags, artwork=original_tags.artwork
                    )
                except tags_module.TagError:
                    pass
                convert_module.replace_atomically(result.destination, source)
                crash_log.debug(f"save_in_place: succeeded for {source.name}")
            except BaseException as exc:
                crash_log.debug(f"save_in_place: failed for {source.name}: {exc!r}")
                tmp_destination.unlink(missing_ok=True)
                raise
            return source

        job = self.jobs.submit_func(
            f"Saving {source.name}", work, source, spec, profile, category="export"
        )
        job.signals.finished.connect(self._on_saved_in_place)

    def _on_saved_in_place(self, _job_id: str, state: str, payload) -> None:
        self.save_button.setEnabled(self._info is not None)
        self.export_button.setEnabled(self._info is not None)
        if state == "succeeded":
            self.summary_label.setText(f"Saved {payload.name}")
            self.exported.emit([payload])
            # The file on disk changed under the currently loaded path --
            # reload it so the waveform/player reflect what's actually there.
            self.load(payload)
        elif state == "failed":
            self.summary_label.setText(f"Save failed: {payload}")


def _scrollable(widget: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.NoFrame)
    return area
