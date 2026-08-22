"""Batch format conversion, with the quality consequences shown up front."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..config import get_settings
from ..core import convert as convert_module
from ..core import formats, probe
from .common import NoteList, QualityBadge, card, heading, row, section_label, spacer
from .widgets.async_read import AsyncProbeReader

#: Sample rates worth offering. "Source" is first because preserving is default.
SAMPLE_RATES = [("Keep source", 0), ("192 kHz", 192000), ("96 kHz", 96000),
                ("48 kHz", 48000), ("44.1 kHz", 44100), ("32 kHz", 32000), ("22.05 kHz", 22050)]

BIT_DEPTHS = [("Keep source", 0), ("24-bit", 24), ("16-bit", 16)]


class ConvertPanel(QWidget):
    """Pick files, pick a target, see exactly what it costs, convert."""

    converted = Signal(list)

    def __init__(self, job_queue, parent=None) -> None:
        super().__init__(parent)
        self.jobs = job_queue
        self.settings = get_settings()
        self._paths: list[Path] = []
        self._pending = 0
        self._results: list[Path] = []
        #: Remembered across format switches -- VBR is the better default.
        self._vbr_preference = True
        # ffprobe runs off the GUI thread; see AsyncProbeReader.
        self._probe_reader = AsyncProbeReader(self)
        self._probe_reader.ready.connect(self._on_probe_ready)
        self._build()

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(14)

        layout.addWidget(
            heading(
                "Convert",
                "Every conversion decodes once and encodes once, at the highest quality "
                "the target format allows. Source sample rate and bit depth are kept "
                "unless you change them.",
            )
        )

        # -- files ------------------------------------------------------
        self.file_list = QListWidget()
        self.file_list.setMaximumHeight(150)
        self.file_list.currentRowChanged.connect(self._update_preview)

        add_button = QPushButton("Add files…")
        add_button.clicked.connect(self._choose_files)
        clear_button = QPushButton("Clear")
        clear_button.clicked.connect(self.clear_files)

        layout.addWidget(
            card(
                row(section_label("Files"), spacer(), add_button, clear_button, spacing=8),
                self.file_list,
            )
        )

        # -- target -----------------------------------------------------
        self.format_combo = QComboBox()
        for profile in formats.ALL_PROFILES:
            suffix = "  (lossless)" if profile.lossless else ""
            self.format_combo.addItem(f"{profile.label}{suffix}", profile.id)
        self.format_combo.currentIndexChanged.connect(self._on_format_changed)

        self.rate_combo = QComboBox()
        for label, value in SAMPLE_RATES:
            self.rate_combo.addItem(label, value)
        self.rate_combo.currentIndexChanged.connect(self._update_preview)

        self.depth_combo = QComboBox()
        for label, value in BIT_DEPTHS:
            self.depth_combo.addItem(label, value)
        self.depth_combo.currentIndexChanged.connect(self._update_preview)

        self.bitrate_spin = QSpinBox()
        self.bitrate_spin.setRange(32, 512)
        self.bitrate_spin.setSingleStep(32)
        self.bitrate_spin.setValue(256)
        self.bitrate_spin.setSuffix(" kbps")
        self.bitrate_spin.valueChanged.connect(self._update_preview)

        self.vbr_check = QCheckBox("Use variable bitrate (recommended)")
        self.vbr_check.setChecked(True)
        self.vbr_check.toggled.connect(self._on_vbr_toggled)

        target = QWidget()
        grid = QGridLayout(target)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        grid.addWidget(QLabel("Format"), 0, 0)
        grid.addWidget(self.format_combo, 0, 1)
        grid.addWidget(QLabel("Sample rate"), 0, 2)
        grid.addWidget(self.rate_combo, 0, 3)
        grid.addWidget(QLabel("Bit depth"), 1, 0)
        grid.addWidget(self.depth_combo, 1, 1)
        grid.addWidget(QLabel("Bitrate"), 1, 2)
        grid.addWidget(self.bitrate_spin, 1, 3)
        grid.addWidget(self.vbr_check, 2, 0, 1, 4)
        grid.setColumnStretch(4, 1)

        self.format_description = QLabel("")
        self.format_description.setObjectName("Hint")
        self.format_description.setWordWrap(True)

        layout.addWidget(card(section_label("Target"), target, self.format_description))

        # -- preview ----------------------------------------------------
        self.source_badge = QualityBadge()
        self.target_badge = QualityBadge()
        self.preview_label = QLabel("Add files to see what will change.")
        self.preview_label.setObjectName("Hint")
        self.preview_label.setWordWrap(True)
        self.notes = NoteList()
        self.notes.setVisible(False)

        layout.addWidget(
            card(
                section_label("What will happen"),
                row(self.source_badge, QLabel("→"), self.target_badge, spacer(), spacing=10),
                self.preview_label,
                self.notes,
            )
        )

        # -- output & action --------------------------------------------
        self.output_dir = QLineEdit(self.settings.output_dir)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._choose_output_dir)

        self.convert_button = QPushButton("Convert")
        self.convert_button.setObjectName("Primary")
        self.convert_button.setMinimumWidth(140)
        self.convert_button.clicked.connect(self._start)
        self.convert_button.setEnabled(False)

        layout.addWidget(
            card(
                section_label("Save to"),
                row(self.output_dir, browse, spacing=8),
                row(spacer(), self.convert_button),
            )
        )

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(120)
        self.log.setPlaceholderText("Conversion results appear here.")
        layout.addWidget(self.log)
        layout.addStretch(1)

        self._on_format_changed()

    # -- files ----------------------------------------------------------
    def set_files(self, paths: list[Path]) -> None:
        self._paths = [Path(p) for p in paths]
        self.file_list.clear()
        for path in self._paths:
            self.file_list.addItem(path.name)
        if self._paths:
            self.file_list.setCurrentRow(0)
        self.convert_button.setEnabled(bool(self._paths))
        self._update_preview()

    def add_files(self, paths: list[Path]) -> None:
        existing = {p.resolve() for p in self._paths}
        self.set_files(self._paths + [p for p in paths if p.resolve() not in existing])

    def clear_files(self) -> None:
        self.set_files([])

    def _choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Choose files to convert", "", "Audio files (*);;All files (*)"
        )
        if paths:
            self.add_files([Path(p) for p in paths])

    def _choose_output_dir(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose where converted files go", self.output_dir.text()
        )
        if directory:
            self.output_dir.setText(directory)

    # -- target ---------------------------------------------------------
    def _profile(self):
        return formats.get_profile(self.format_combo.currentData())

    def _on_format_changed(self) -> None:
        profile = self._profile()
        self.format_description.setText(profile.description)
        self.depth_combo.setEnabled(profile.lossless)
        self.vbr_check.setEnabled(profile.supports_vbr)

        # Formats without a VBR mode force the box off. Remember what the user
        # actually wanted so switching FLAC -> MP3 restores it, instead of
        # silently leaving them on constant bitrate.
        if profile.supports_vbr:
            self.vbr_check.setChecked(self._vbr_preference)
        else:
            self.vbr_check.setChecked(False)

        self.bitrate_spin.setEnabled(profile.is_lossy and not self.vbr_check.isChecked())
        self._update_preview()

    def _on_vbr_toggled(self, checked: bool) -> None:
        # Only record the preference when the control is live; the programmatic
        # unchecking above must not be mistaken for a user choice.
        if self.vbr_check.isEnabled():
            self._vbr_preference = checked
        self._update_preview()

    def _update_preview(self, *_) -> None:
        profile = self._profile()
        self.bitrate_spin.setEnabled(
            profile.is_lossy and not (self.vbr_check.isChecked() and profile.supports_vbr)
        )

        current = self.file_list.currentRow()
        if not self._paths or current < 0 or current >= len(self._paths):
            self.preview_label.setText("Add files to see what will change.")
            self.source_badge.clear_state()
            self.target_badge.clear_state()
            self.notes.setVisible(False)
            return

        # Probing spawns ffprobe, and this runs on every row change *and* every
        # format or VBR toggle -- inline it froze the page on each one. Results
        # are cached, so changing settings for an already-probed file is free.
        path = self._paths[current]
        probed = self._probe_reader.request(path)
        if probed is False:
            self.preview_label.setText("Reading…")
            return
        self._render_preview(path, probed)

    def _on_probe_ready(self, path: Path, info) -> None:
        current = self.file_list.currentRow()
        if not self._paths or current < 0 or current >= len(self._paths):
            return
        # Ignore a probe that landed after the user moved to another file.
        if Path(self._paths[current]) != Path(path):
            return
        self._render_preview(Path(path), info)

    def _render_preview(self, path: Path, info) -> None:
        profile = self._profile()
        if info is None:
            self.preview_label.setText("That file could not be read.")
            return

        output = convert_module.resolve_output(
            info,
            profile,
            sample_rate=self.rate_combo.currentData() or None,
            bit_depth=self.depth_combo.currentData() or None,
            bitrate=None if (self.vbr_check.isChecked() and profile.supports_vbr)
            else self.bitrate_spin.value(),
            vbr_quality=profile.default_vbr_quality
            if (self.vbr_check.isChecked() and profile.supports_vbr) else None,
        )

        self.source_badge.set_lossless(info.is_lossless, info.describe_technical())
        self.target_badge.set_lossless(profile.lossless, profile.label)
        self.preview_label.setText(convert_module.describe_conversion(info, output))
        self.notes.set_notes(output.notes)

    # -- run ------------------------------------------------------------
    def _start(self) -> None:
        if not self._paths:
            return
        profile = self._profile()
        output_dir = Path(self.output_dir.text())
        use_vbr = self.vbr_check.isChecked() and profile.supports_vbr

        self.convert_button.setEnabled(False)
        self._pending = len(self._paths)
        self._results = []

        for source in self._paths:
            request = convert_module.ConvertRequest(
                source=source,
                destination=convert_module.suggest_destination(source, profile, output_dir),
                profile=profile,
                sample_rate=self.rate_combo.currentData() or None,
                bit_depth=self.depth_combo.currentData() or None,
                bitrate=None if use_vbr else self.bitrate_spin.value(),
                vbr_quality=profile.default_vbr_quality if use_vbr else None,
            )

            def work(context, req):
                result = convert_module.convert(req, context=context)
                # Carry tags and cover art across, since a fresh encode only
                # inherits what ffmpeg's metadata mapping understands.
                from ..core import tags as tags_module

                try:
                    tags_module.copy_tags(req.source, result.destination)
                except tags_module.TagError:
                    pass
                return result

            job = self.jobs.submit_func(
                f"Converting {source.name} → {profile.label}", work, request, category="convert"
            )
            job.signals.finished.connect(self._on_one_finished)

    def _on_one_finished(self, _job_id: str, state: str, payload) -> None:
        self._pending -= 1
        if state == "succeeded":
            result = payload
            self.log.appendPlainText(
                f"✓ {result.destination.name}  ({result.size_change:.2f}× source size)"
            )
            self._results.append(result.destination)
        elif state == "cancelled":
            self.log.appendPlainText("Cancelled.")
        else:
            self.log.appendPlainText(f"✗ {payload}")

        if self._pending <= 0:
            self.convert_button.setEnabled(True)
            if self._results:
                self.converted.emit(self._results)
