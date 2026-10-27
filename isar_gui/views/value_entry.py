"""The user-value entry, shared by the needs-attention list and *Set my value…*.

:class:`ValueEntry` takes one temperature (K) or one slit (horizontal × vertical, mm) and an optional
reason. It only parses text into numbers (``float``, independent of the locale) and never range-checks:
the intake backend validates every save and its reason is shown under the entry as given. While typed
text is not saved the entry says "Not saved".

:class:`SetValueDialog` holds one entry, the sentence saying what the value applies to, and *Save* /
*Cancel*; *Save* calls ``intake.save_value`` and stays open showing the reason when the save is refused.
"""
from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout,
                               QWidget)

from isar_gui.theme import TONE_COLOR, Tone

NOT_SAVED = "Not saved"
_UNITS = {"temperature": "K", "slit": "mm"}
_TITLES = {"temperature": "Set my temperature", "slit": "Set my slit"}


def _parse(text: str) -> tuple[float | None, str | None]:
    """``(number, None)``, ``(None, None)`` for empty text, or ``(None, reason)``."""
    text = text.strip()
    if not text:
        return None, None
    try:
        return float(text), None
    except ValueError:
        if "," in text:
            try:
                float(text.replace(",", "."))
                return None, f"Not a number (use a point: {text.replace(',', '.')})"
            except ValueError:
                pass
        return None, "Not a number"


class ValueEntry(QWidget):
    edited = Signal()

    def __init__(self, fact: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        if fact not in _UNITS:
            raise ValueError(f"fact must be 'temperature' or 'slit', got {fact!r}")
        self.setObjectName("value_entry")
        self._fact = fact
        self._message: str | None = None        # the last save outcome, until the next edit
        self._message_ok = True

        if fact == "temperature":
            self._fields = [QLineEdit(self)]
            self._fields[0].setPlaceholderText("temperature")
            self._fields[0].setAccessibleName("Temperature in K")
        else:
            self._fields = [QLineEdit(self), QLineEdit(self)]
            self._fields[0].setPlaceholderText("horizontal")
            self._fields[0].setAccessibleName("Slit horizontal in mm")
            self._fields[1].setPlaceholderText("vertical")
            self._fields[1].setAccessibleName("Slit vertical in mm")
        self._reason = QLineEdit(self)
        self._reason.setObjectName("value_reason")
        self._reason.setPlaceholderText("Reason (optional)")
        self._reason.setAccessibleName("Reason (optional)")
        self._status = QLabel("", self)
        self._status.setObjectName("value_status")
        self._status.setWordWrap(True)
        self._status.hide()

        row = QHBoxLayout()
        row.setSpacing(4)
        for i, field in enumerate(self._fields):
            if i:
                row.addWidget(QLabel("×", self), 0)
            row.addWidget(field, 1)
            field.textChanged.connect(self._on_edit)
        row.addWidget(QLabel(_UNITS[fact], self), 0)
        self._reason.textChanged.connect(self._on_edit)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addLayout(row)
        layout.addWidget(self._reason)
        layout.addWidget(self._status)

    @property
    def fact(self) -> str:
        return self._fact

    def value(self):
        """float (K) | (float, float) (mm) | None when empty or unparseable."""
        parsed = [_parse(f.text()) for f in self._fields]
        if any(v is None for v, _err in parsed):
            return None
        return parsed[0][0] if self._fact == "temperature" else (parsed[0][0], parsed[1][0])

    def reason(self) -> str | None:
        return self._reason.text().strip() or None

    def parse_error(self) -> str | None:
        """Why the typed text is not a value ("Not a number" etc.); None when parseable or empty."""
        parsed = [_parse(f.text()) for f in self._fields]
        for _v, err in parsed:
            if err is not None:
                return err
        if self._fact == "slit" and sum(v is not None for v, _err in parsed) == 1:
            return "Enter both the horizontal and the vertical opening"
        return None

    def set_message(self, text: str, ok: bool) -> None:
        """Show a save outcome under the entry (kept until the next edit)."""
        self._message, self._message_ok = text, ok
        self._show_status()

    def clear(self) -> None:
        for field in (*self._fields, self._reason):
            field.blockSignals(True)
            field.clear()
            field.blockSignals(False)
        self._message = None
        self._show_status()
        self.edited.emit()

    def _typed(self) -> bool:
        return any(f.text().strip() for f in (*self._fields, self._reason))

    def _on_edit(self, *_args) -> None:
        self._message = None
        self._show_status()
        self.edited.emit()

    def _show_status(self) -> None:
        error = self.parse_error()
        if self._message is not None:
            text, warn = self._message, not self._message_ok
        elif error is not None:
            text, warn = error, True
        elif self._typed():
            text, warn = NOT_SAVED, False
        else:
            text, warn = "", False
        self._status.setText(text)
        self._status.setStyleSheet(f"color: {TONE_COLOR[Tone.WARN]};" if warn else "")
        self._status.setVisible(bool(text))


class SetValueDialog(QDialog):
    def __init__(self, intake, fact: str, targets: Sequence[str], applies_to: str,
                 parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("set_value_dialog")
        self.setWindowTitle(_TITLES.get(fact, "Set my value"))
        self._intake = intake
        self._fact = fact
        self._targets = list(targets)
        self._applies = QLabel(applies_to, self)
        self._applies.setObjectName("set_value_applies_to")
        self._applies.setWordWrap(True)
        self.entry = ValueEntry(fact, self)
        self._buttons = QDialogButtonBox(self)
        self._save = self._buttons.addButton("Save", QDialogButtonBox.ButtonRole.AcceptRole)
        self._save.setObjectName("set_value_save")
        self._save.setDefault(True)
        self._buttons.addButton(QDialogButtonBox.StandardButton.Cancel)
        self._save.clicked.connect(self._on_save)
        self._buttons.rejected.connect(self.reject)
        self.entry.edited.connect(self._sync)
        layout = QVBoxLayout(self)
        layout.addWidget(self._applies)
        layout.addWidget(self.entry)
        layout.addWidget(self._buttons)
        self._sync()

    def _sync(self) -> None:
        self._save.setEnabled(self.entry.value() is not None)

    def _on_save(self) -> None:
        value = self.entry.value()
        if value is None:
            return
        outcome = self._intake.save_value(self._fact, self._targets, value, self.entry.reason())
        self.entry.set_message(outcome.message, outcome.ok)
        if outcome.ok:
            self.accept()
