"""Create or edit a smart playlist: a name plus rules the library is matched
against every time the playlist is shown (see core/playlist_rules.py)."""

from __future__ import annotations

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core import playlist_rules as rules_module
from ..core.playlist_rules import BOOLEAN, FIELDS, NUMBER, OPERATORS, TEXT, TIME
from ..db import Library

_ORDER_CHOICES = [
    ("Album artist, album, track", "albumartist"),
    ("Artist", "artist"),
    ("Title", "title"),
    ("Year", "date"),
    ("Genre", "genre"),
    ("Length", "duration"),
    ("Bitrate", "bitrate"),
    ("Newest added first", "-added_at"),
    ("Oldest added first", "added_at"),
    ("Random", "random"),
]


def _number_box(unit: str) -> QDoubleSpinBox:
    box = QDoubleSpinBox()
    box.setRange(-1_000_000, 10_000_000)
    box.setDecimals(0)
    if unit:
        box.setSuffix(f" {unit}")
    return box


class _RuleRow(QWidget):
    """One "field / test / value" line. Rebuilds its value editor whenever
    the field changes, since text, numbers, yes/no and dates each need a
    different one."""

    def __init__(self, on_change, on_remove, rule: dict | None = None) -> None:
        super().__init__()
        self._on_change = on_change
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.field_box = QComboBox()
        for field in FIELDS.values():
            self.field_box.addItem(field.label, field.key)
        self.op_box = QComboBox()
        self.value_holder = QHBoxLayout()
        self.value_holder.setContentsMargins(0, 0, 0, 0)
        remove = QPushButton("✕")
        remove.setToolTip("Remove this rule")
        remove.setFixedWidth(32)
        remove.clicked.connect(lambda: on_remove(self))

        layout.addWidget(self.field_box)
        layout.addWidget(self.op_box)
        layout.addLayout(self.value_holder, 1)
        layout.addWidget(remove)

        self._value_widgets: list[QWidget] = []
        self.field_box.currentIndexChanged.connect(self._on_field_changed)
        self.op_box.currentIndexChanged.connect(self._on_op_changed)

        if rule and rule.get("field") in FIELDS:
            self.field_box.setCurrentIndex(self.field_box.findData(rule["field"]))
        self._on_field_changed()
        if rule:
            index = self.op_box.findData(rule.get("op"))
            if index >= 0:
                self.op_box.setCurrentIndex(index)
            self._set_value(rule.get("value"))

    @property
    def field(self):
        return FIELDS[self.field_box.currentData()]

    def _on_field_changed(self) -> None:
        self.op_box.blockSignals(True)
        self.op_box.clear()
        for key, label in OPERATORS[self.field.kind].items():
            self.op_box.addItem(label, key)
        self.op_box.blockSignals(False)
        self._rebuild_value()

    def _on_op_changed(self) -> None:
        self._rebuild_value()

    def _rebuild_value(self) -> None:
        for widget in self._value_widgets:
            widget.deleteLater()
        self._value_widgets = []
        field, op = self.field, self.op_box.currentData()

        if field.kind == TEXT:
            edit = QLineEdit()
            edit.textChanged.connect(self._on_change)
            self._value_widgets = [edit]
        elif field.kind == NUMBER:
            count = 2 if op == "between" else 1
            for _ in range(count):
                box = _number_box(field.unit)
                box.valueChanged.connect(self._on_change)
                self._value_widgets.append(box)
        elif field.kind == TIME:
            box = QSpinBox()
            box.setRange(1, 36500)
            box.setValue(30)
            box.setSuffix(" days")
            box.valueChanged.connect(self._on_change)
            self._value_widgets = [box]
        # BOOLEAN needs no value: the operator says yes or no.

        for widget in self._value_widgets:
            self.value_holder.addWidget(widget, 1)
        self._on_change()

    def _set_value(self, value) -> None:
        widgets = self._value_widgets
        if not widgets or value is None:
            return
        if isinstance(widgets[0], QLineEdit):
            widgets[0].setText(str(value))
        elif isinstance(value, (list, tuple)):
            for widget, part in zip(widgets, value):
                widget.setValue(float(part))
        else:
            widgets[0].setValue(float(value))

    def rule(self) -> dict:
        field, op = self.field, self.op_box.currentData()
        rule = {"field": field.key, "op": op}
        widgets = self._value_widgets
        if field.kind == TEXT:
            rule["value"] = widgets[0].text()
        elif field.kind == NUMBER:
            values = [w.value() for w in widgets]
            rule["value"] = values if op == "between" else values[0]
        elif field.kind == TIME:
            rule["value"] = widgets[0].value()
        return rule


class SmartPlaylistDialog(QDialog):
    """Name, match all/any, rule rows, order, limit, and a live match count."""

    def __init__(self, library: Library, *, name: str = "", rules: dict | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self.library = library
        self.setWindowTitle("Smart playlist")
        self.setMinimumWidth(620)
        rules = rules or {
            "match": "all",
            "rules": [{"field": "added_at", "op": "in_last_days", "value": 30}],
            "order_by": "-added_at",
        }

        self.name_edit = QLineEdit(name or "New smart playlist")
        self.match_box = QComboBox()
        self.match_box.addItem("all of these rules", "all")
        self.match_box.addItem("any of these rules", "any")
        self.match_box.setCurrentIndex(self.match_box.findData(rules.get("match", "all")))
        self.match_box.currentIndexChanged.connect(self._schedule_count)

        self.rows_layout = QVBoxLayout()
        self.rows_layout.setSpacing(6)
        self._rows: list[_RuleRow] = []
        add_rule = QPushButton("Add rule")
        add_rule.clicked.connect(lambda: self._add_row())

        self.order_box = QComboBox()
        for label, key in _ORDER_CHOICES:
            self.order_box.addItem(label, key)
        index = self.order_box.findData(rules.get("order_by") or "albumartist")
        self.order_box.setCurrentIndex(max(0, index))
        self.order_box.currentIndexChanged.connect(self._schedule_count)

        self.limit_box = QSpinBox()
        self.limit_box.setRange(0, 100_000)
        self.limit_box.setSpecialValueText("No limit")
        self.limit_box.setValue(int(rules.get("limit") or 0))
        self.limit_box.valueChanged.connect(self._schedule_count)

        self.count_label = QLabel("")
        self.count_label.setObjectName("Hint")

        form = QFormLayout()
        form.addRow("Name:", self.name_edit)
        form.addRow("Match:", self.match_box)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        self.save_button = buttons.button(QDialogButtonBox.Save)

        bottom = QFormLayout()
        bottom.addRow("Order:", self.order_box)
        bottom.addRow("Limit to:", self.limit_box)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addLayout(self.rows_layout)
        layout.addWidget(add_rule)
        layout.addLayout(bottom)
        layout.addWidget(self.count_label)
        layout.addWidget(buttons)

        # Counting runs a query; debounce so typing in a text rule doesn't run
        # one per keystroke.
        self._count_timer = QTimer(self)
        self._count_timer.setSingleShot(True)
        self._count_timer.setInterval(250)
        self._count_timer.timeout.connect(self._update_count)

        for rule in rules.get("rules") or []:
            self._add_row(rule)
        self._update_count()

    def _add_row(self, rule: dict | None = None) -> None:
        row = _RuleRow(self._schedule_count, self._remove_row, rule)
        self._rows.append(row)
        self.rows_layout.addWidget(row)
        self._schedule_count()

    def _remove_row(self, row: _RuleRow) -> None:
        self._rows.remove(row)
        row.deleteLater()
        self._schedule_count()

    def _schedule_count(self, *_) -> None:
        if hasattr(self, "_count_timer"):
            self._count_timer.start()

    def rules(self) -> dict:
        rules = {
            "match": self.match_box.currentData(),
            "rules": [row.rule() for row in self._rows],
            "order_by": self.order_box.currentData(),
        }
        if self.limit_box.value():
            rules["limit"] = self.limit_box.value()
        return rules

    def name(self) -> str:
        return self.name_edit.text().strip() or "New smart playlist"

    def _update_count(self) -> None:
        try:
            count = len(self.library.query_rules(self.rules()))
        except ValueError as exc:
            self.count_label.setText(f"Not valid yet: {exc}")
            self.save_button.setEnabled(False)
            return
        self.save_button.setEnabled(True)
        summary = rules_module.describe_rules(self.rules())
        self.count_label.setText(f"{count} song(s) match: {summary}")

    def _accept(self) -> None:
        self._update_count()
        if self.save_button.isEnabled():
            self.accept()
