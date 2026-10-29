"""InterSettingsDialog -- *Config > Inter-spectra fitting settings…* (S1U10).

Rows come from SETTING_DEFS in table order; hidden settings are not shown. Save sends only the values the user
touched (``changes`` drops the rest), through ``InterState.save_settings``; a refusal keeps the dialog open with
its reason. Cancel and close write nothing.
"""
from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QLabel, QLineEdit, QPushButton, QSpinBox, QVBoxLayout, QWidget)

from isar.analyze.coupled.settings import SETTING_DEFS, SettingDef
from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.viewmodels.inter_settings import LABELS, Row, changes, default_text, rows

_DECIMALS = 3   # shown decimals of a float setting; a strict minimum (the table's "> 0") is one step above 0
_TIPS = {False: "A starting value from the developers' tests on synthetic data; not yet checked against your data.",
         True: "Calibrated on synthetic tracks, not on real data; not yet checked against your data."}
_WARN = f"color: {TONE_COLOR[Tone.WARN]};"


class _NoWheel:
    """Takes the mouse wheel only once focused, so scrolling the dialog never edits a value (as ``_OffsetBox``)."""

    def wheelEvent(self, event):  # noqa: N802
        if self.hasFocus():
            super().wheelEvent(event)
        else:
            event.ignore()


class _FloatBox(_NoWheel, QDoubleSpinBox):
    pass


class _IntBox(_NoWheel, QSpinBox):
    pass


def _widget_for(d: SettingDef) -> QWidget:
    """The editor for one setting. The table carries float, int and choice; anything else is a bug and raises."""
    suffix = f" {d.unit}" if d.unit else ""
    if d.kind == "float":
        box = _FloatBox()
        box.setDecimals(_DECIMALS)
        box.setRange(d.minimum + (10 ** -_DECIMALS if d.strict_minimum else 0), d.maximum)
        box.setSingleStep(0.1)
        box.setSuffix(suffix)
        return box
    if d.kind == "int":
        box = _IntBox()
        box.setRange(d.minimum, d.maximum)
        box.setSuffix(suffix)
        return box
    if d.kind == "choice":
        box = QComboBox()
        box.addItems(list(d.choices))
        return box
    raise ValueError(f"no editor for inter-spectra setting {d.name!r} of kind {d.kind!r}")


def _set(editor: QWidget, value: object) -> None:
    if isinstance(editor, QComboBox):
        editor.setCurrentText(str(value))
    else:
        editor.setValue(value)


def _value(editor: QWidget) -> object:
    return editor.currentText() if isinstance(editor, QComboBox) else editor.value()


class InterSettingsDialog(QDialog):
    def __init__(self, inter, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inter_settings_dialog")
        self.setWindowTitle("Inter-spectra fitting settings")
        self._inter = inter
        self._rows = rows(SETTING_DEFS, inter.settings)
        self._editors: dict[str, QWidget] = {}
        self._touched: set[str] = set()     # the names the user edited; only these are sent

        header = QLabel("These settings belong to this experiment. A batch records the settings it used; changing "
                        "them affects only batches you start afterwards.", self)
        header.setObjectName("inter_settings_header")
        header.setWordWrap(True)

        front, advanced = QFormLayout(), QFormLayout()
        for row in self._rows:
            (front if row.setting.group == "front" else advanced).addRow(LABELS[row.setting.name], self._field(row))
        section = QWidget(self)
        section.setObjectName("inter_settings_advanced_section")
        section.setLayout(advanced)
        section.setVisible(False)
        toggle = QPushButton("Advanced settings", self)
        toggle.setObjectName("inter_settings_advanced")
        toggle.setCheckable(True)
        toggle.setToolTip("Show or hide the advanced settings: penalty shape, temperature step exponent, sweep "
                          "tolerance and sweep cap.")
        toggle.toggled.connect(section.setVisible)

        self._message = QLabel("", self)
        self._message.setObjectName("inter_settings_message")
        self._message.setWordWrap(True)
        self._message.setStyleSheet(_WARN)
        self._message.hide()
        buttons = QDialogButtonBox(self)
        save = buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        save.setObjectName("inter_settings_save")
        save.setToolTip("Save the values you changed. Settings you did not touch are not written.")
        save.setDefault(True)
        save.clicked.connect(self._save)
        cancel = buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        cancel.setObjectName("inter_settings_cancel")
        cancel.setToolTip("Close without saving anything.")
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(header)
        layout.addLayout(front)
        layout.addWidget(toggle)
        layout.addWidget(section)
        layout.addWidget(self._message)
        layout.addWidget(buttons)
        self.resize(480, self.sizeHint().height())

    def _field(self, row: Row) -> QVBoxLayout:
        d = row.setting
        editor = _widget_for(d)
        editor.setObjectName(f"inter_{d.name}")
        editor.setAccessibleName(LABELS[d.name])
        editor.setToolTip(d.help)
        for part in editor.findChildren(QLineEdit):   # a spin box's own text field
            part.setAccessibleName(LABELS[d.name])
            part.setToolTip(d.help)
        _set(editor, row.value)
        signal = editor.currentIndexChanged if isinstance(editor, QComboBox) else editor.valueChanged
        signal.connect(lambda *_, name=d.name: self._touched.add(name))
        self._editors[d.name] = editor

        use_default = QPushButton("Use default", self)
        use_default.setObjectName(f"inter_{d.name}_default")
        use_default.setToolTip(f"Put {LABELS[d.name]} back to its default value.")
        use_default.clicked.connect(lambda _=False, e=editor, v=d.default: _set(e, v))
        note = QLabel(default_text(row), self)
        note.setObjectName(f"inter_{d.name}_default_text")
        note.setToolTip(_TIPS[d.calibrated])

        line = QHBoxLayout()
        line.addWidget(editor, 1)
        line.addWidget(use_default)
        box = QVBoxLayout()
        box.addLayout(line)
        box.addWidget(note)
        return box

    def _save(self) -> None:
        delta = changes(self._rows, {name: _value(self._editors[name]) for name in self._touched})
        if not delta:
            self.accept()
            return
        outcome = self._inter.save_settings(delta)
        self._show(outcome)
        if outcome.ok:
            self.accept()

    def _show(self, outcome) -> None:
        refused = not outcome.ok
        self._message.setText(outcome.message if refused else "")
        self._message.setVisible(refused)
