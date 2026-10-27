"""Human-authored, conflict-safe ``card.policy.toml`` editor."""
from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QDockWidget,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from isar.workspace_api import PolicyValidation, validate_policy_text

_CARD_NAME = "card.policy.toml"


def _fingerprint(path: Path) -> tuple[int, int, str] | None:
    """Return a content-sensitive disk identity, or ``None`` when it cannot be read."""
    try:
        data = path.read_bytes()
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size, hashlib.sha256(data).hexdigest()


class CardDock(QDockWidget):
    """Edit a policy as explicit human intent; never silently discard or overwrite it."""

    saved = Signal(str)          # absolute policy path
    dirty_changed = Signal(bool)

    def __init__(self, ws, parent=None):
        super().__init__("Card", parent)
        self.setObjectName("card_dock")
        self._path: Path | None = None
        self._baseline_fingerprint: tuple[int, int, str] | None = None
        self._dirty = False
        self._loading = False
        self._operation_busy = False

        body = QWidget()
        outer = QVBoxLayout(body)
        outer.setContentsMargins(4, 4, 4, 4)

        self._editor = QPlainTextEdit()
        self._editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._editor.setReadOnly(True)
        self._editor.setAcceptDrops(False)
        self._editor.textChanged.connect(self._on_text_changed)
        outer.addWidget(self._editor)

        row = QWidget()
        row_l = QHBoxLayout(row)
        row_l.setContentsMargins(0, 0, 0, 0)
        self._save_btn = QPushButton("Save card")
        self._save_btn.setToolTip("Save the edits to card.policy.toml: the instrument settings and fitting "
                                  "guidelines of this experiment. A new analysis uses them.")
        self._reload_btn = QPushButton("Reload from disk")
        self._reload_btn.setToolTip("Discard the edits shown here and read card.policy.toml again.")
        self._save_btn.clicked.connect(self.save)
        self._reload_btn.clicked.connect(self.reload)
        row_l.addWidget(self._save_btn)
        row_l.addWidget(self._reload_btn)
        row_l.addStretch(1)
        self._status = QLabel("")
        row_l.addWidget(self._status)
        outer.addWidget(row)

        self.setWidget(body)
        self.set_workspace(ws)

    # ---- binding ----------------------------------------------------------
    def set_workspace(self, ws) -> bool:
        """Point the editor at ``ws``'s card without silently replacing a draft."""
        if self._dirty and not self.resolve_dirty("open another workspace"):
            return False
        root = getattr(ws, "root", None)
        self._path = None if root is None else Path(root) / _CARD_NAME
        return self.reload(discard_dirty=True)

    # ---- state ------------------------------------------------------------
    @property
    def is_dirty(self) -> bool:
        return self._dirty

    @property
    def path(self) -> Path | None:
        return self._path

    def set_operation_busy(self, busy: bool) -> None:
        """Drafting remains possible while an engine writes; disk actions do not."""
        self._operation_busy = bool(busy)
        self._update_enabled()
        if busy and self._path is not None:
            self._status.setText("drafting only — save/reload after the active operation")

    def _set_dirty(self, dirty: bool) -> None:
        dirty = bool(dirty)
        if dirty == self._dirty:
            return
        self._dirty = dirty
        self.dirty_changed.emit(dirty)
        if dirty and self._path is not None and not self._operation_busy:
            self._status.setText("unsaved edits")

    def _on_text_changed(self) -> None:
        if not self._loading:
            self._set_dirty(True)

    # ---- load / save ------------------------------------------------------
    def reload(self, *, discard_dirty: bool = False) -> bool:
        """Reload from disk, asking before replacing unsaved text unless pre-resolved."""
        if self._operation_busy:
            self._status.setText("cannot reload while an operation is active")
            return False
        if self._dirty and not discard_dirty and not self.resolve_dirty("reload this card"):
            return False
        if self._path is None or not self._path.exists():
            self._loading = True
            try:
                self._editor.setPlainText("")
            finally:
                self._loading = False
            self._baseline_fingerprint = None
            self._set_dirty(False)
            self._update_enabled()
            self._status.setText("no workspace" if self._path is None else "card.policy.toml missing")
            return False
        try:
            text = self._path.read_text(encoding="utf-8")
            fingerprint = _fingerprint(self._path)
        except OSError as exc:
            self._status.setText(f"could not read card: {exc}")
            self._update_enabled()
            return False
        self._loading = True
        try:
            self._editor.setPlainText(text)
        finally:
            self._loading = False
        self._baseline_fingerprint = fingerprint
        self._set_dirty(False)
        self._update_enabled()
        self._status.setText(f"loaded {self._path.name}")
        return True

    def save(self) -> bool:
        """Validate then atomically save the current policy after conflict confirmation."""
        if self._path is None:
            self._status.setText("no workspace to save to")
            return False
        if self._operation_busy:
            self._status.setText("cannot save while an operation is active")
            return False
        validation = validate_policy_text(self._editor.toPlainText())
        if not validation.is_valid:
            self._status.setText(f"NOT saved — {validation.errors[0]}")
            return False
        if validation.warnings and not self._confirm_warnings(validation):
            self._status.setText("save cancelled — review unknown card keys")
            return False
        current = _fingerprint(self._path)
        if current != self._baseline_fingerprint and not self._resolve_external_change():
            return False
        tmp = self._path.with_name(
            f"{self._path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        try:
            tmp.write_text(self._editor.toPlainText(), encoding="utf-8")
            os.replace(tmp, self._path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            self._status.setText(f"could not write card: {exc}")
            return False
        self._baseline_fingerprint = _fingerprint(self._path)
        self._set_dirty(False)
        self._status.setText(f"saved {self._path.name}")
        self.saved.emit(str(self._path))
        return True

    def resolve_dirty(self, action: str) -> bool:
        """Ask Save / Discard / Cancel before a lifecycle action loses draft text."""
        if not self._dirty:
            return True
        if self._operation_busy:
            self._status.setText("draft is unsaved; wait for the active operation before continuing")
            return False
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved card edits")
        box.setText("The experiment card has unsaved edits.")
        box.setInformativeText(f"Save them before you {action}?")
        save_btn = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            return self.save()
        if clicked is discard_btn:
            # "Discard" means return the visible editor to the policy an engine will actually
            # read, not merely clear a flag while leaving misleading draft text on screen.
            return self._discard_draft()
        return False

    def _discard_draft(self) -> bool:
        """Restore the on-disk baseline after an explicit discard choice.

        If the card cannot be re-read, retain the dirty draft and block the caller rather than
        letting an analysis proceed with a visible policy that differs from disk.
        """
        return self.reload(discard_dirty=True)

    def _confirm_warnings(self, validation: PolicyValidation) -> bool:
        # Unknown keys and the notes about keys ISAR reads elsewhere (the shipped BL43 file, values entered
        # in ISAR) are different things: each sits under its own heading, and the title names what is there.
        unknown, notes = validation.unknown_keys, validation.notes
        sections = []
        if unknown:
            sections.append("These keys are not part of the current card schema:\n" + "\n".join(unknown))
        if notes:
            sections.append("These keys are not used (ISAR reads them from elsewhere):\n" + "\n".join(notes))
        title = ("Card keys to review" if unknown and notes
                 else "Unknown card keys" if unknown else "Card keys not used")
        answer = QMessageBox.warning(
            self,
            title,
            "\n\n".join(sections) + "\n\nSave anyway?",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        return answer == QMessageBox.StandardButton.Save

    def _resolve_external_change(self) -> bool:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Card changed on disk")
        box.setText("card.policy.toml changed outside this editor.")
        box.setInformativeText("Overwrite it with this draft, reload the on-disk card, or cancel?")
        overwrite_btn = box.addButton("Overwrite", QMessageBox.ButtonRole.DestructiveRole)
        reload_btn = box.addButton("Reload", QMessageBox.ButtonRole.ActionRole)
        cancel_btn = box.addButton("Cancel", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is overwrite_btn:
            return True
        if clicked is reload_btn:
            self.reload(discard_dirty=True)
        else:
            self._status.setText("save cancelled — card changed on disk")
        return False

    # ---- helpers ----------------------------------------------------------
    def _update_enabled(self) -> None:
        editable = self._path is not None and self._path.exists()
        self._editor.setReadOnly(not editable)
        disk_actions = editable and not self._operation_busy
        self._save_btn.setEnabled(disk_actions)
        self._reload_btn.setEnabled(disk_actions)

    @property
    def text(self) -> str:
        return self._editor.toPlainText()

    def set_text(self, text: str) -> None:
        """Test/helper hook: replace the editor contents without touching disk."""
        self._editor.setPlainText(text)
