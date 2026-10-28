"""Ten persistent scan-selection bookmarks for the Scan(s) page."""
from __future__ import annotations

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QComboBox, QHBoxLayout, QLabel,
    QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QTextEdit,
    QVBoxLayout, QWidget,
)

from isar_gui.gui_store import GROUP_SLOTS, GuiStore, GroupRec
from isar_gui.scan_keys import member_matches
from isar_gui.selection import ORIGIN_GROUP
from isar_gui.views.elided_label import ElidedLabel


class GroupChips(QWidget):
    """Fixed numbered slots; only explicit Save or Clear mutates a bookmark."""

    store_dirty = Signal()

    def __init__(self, scan_list_vm, parent=None):
        super().__init__(parent)
        self.scan_list_vm = scan_list_vm
        self.selection = scan_list_vm.selection
        self._store = None
        self._groups = {}
        self._tied = None
        self._edited = None
        self._snapshot_keys = ()
        self._labels_by_key = {}
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(3)
        slots = QHBoxLayout()
        slots.setSpacing(2)
        self._chips = {}
        for slot in GROUP_SLOTS:
            chip = QPushButton(str(slot % 10), self)
            chip.setObjectName(f"groupChip{slot}")
            chip.setFixedSize(24, max(24, self.fontMetrics().height() + 6))
            chip.clicked.connect(lambda _checked=False, n=slot: self._recall(n))
            slots.addWidget(chip)
            self._chips[slot] = chip
        slots.addStretch(1)
        outer.addLayout(slots)
        actions = QHBoxLayout()
        actions.setSpacing(4)
        self._label = QLabel("Groups", self)
        self._label.setToolTip("Scan bookmarks: filled slots are saved; 0 is group 10. Select a slot, then Save.")
        actions.addWidget(self._label)
        self._save_button = QPushButton("Save", self)
        self._save_button.clicked.connect(lambda: self._save_slot(self._tied))
        actions.addWidget(self._save_button)
        self._clear_button = QPushButton("Clear", self)
        self._clear_button.clicked.connect(self._clear_selected)
        actions.addWidget(self._clear_button)
        for button in (self._save_button, self._clear_button):
            button.setStyleSheet("QPushButton {padding: 2px 6px;}")
        self._notice = ElidedLabel("", self)
        actions.addWidget(self._notice, 1)
        outer.addLayout(actions)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._clear_notice)
        self.selection.changed.connect(self._selection_changed)
        self.scan_list_vm.snapshot_changed.connect(self.reload)
        self.reload()

    def set_store(self, store: GuiStore | None):
        self._store = store
        self._tied = None
        self._edited = None
        self._clear_notice()
        self.reload()

    def key_pressed(self, slot: int, save: bool):
        if slot not in GROUP_SLOTS or self._store is None:
            return
        if save:
            self._save_slot(slot)
        elif not self._editor_has_focus():
            self._recall(slot)

    def reload(self):
        self._refresh_snapshot()
        self._groups = self._store.slot_groups() if self._store else {}
        self._update_edited()
        self._update_chips()

    def chip(self, slot):
        return self._chips.get(slot)

    def save_button(self, slot):
        return self._save_button if self._tied == slot else None

    @staticmethod
    def _editor_has_focus() -> bool:
        widget = QApplication.focusWidget()
        while widget is not None:
            if isinstance(widget, (QLineEdit, QAbstractSpinBox, QTextEdit, QPlainTextEdit)):
                return True
            if isinstance(widget, QComboBox) and widget.isEditable():
                return True
            widget = widget.parentWidget()
        return False

    def _refresh_snapshot(self) -> None:
        snapshot = self.scan_list_vm.snapshot
        keys: list[str] = []
        labels: dict[str, str] = {}
        for scan_set in snapshot.sets:
            for spectrum in scan_set.spectra:
                key = str(spectrum.key)
                keys.append(key)
                analyzer = spectrum.analyzer or key
                labels[key] = f"{analyzer} · {scan_set.label}"
        for master in snapshot.pooled:
            key = str(master.key)
            keys.append(key)
            labels[key] = master.label
        self._snapshot_keys = tuple(dict.fromkeys(keys))
        self._labels_by_key = labels

    def _present(self, member: str) -> bool:
        return any(member_matches(member, key) for key in self._snapshot_keys)

    def _keys_for_group(self, group: GroupRec) -> list[str]:
        return [key for key in self._snapshot_keys
                if any(member_matches(member, key) for member in group.members)]

    def _summary(self, group: GroupRec) -> tuple[int, int, str]:
        total = len(group.members)
        present = sum(self._present(member) for member in group.members)
        if present < total:
            text = f"{present} of {total} present"
        else:
            noun = "spectrum" if total == 1 else "spectra"
            text = f"{total} {noun}"
        return present, total, text

    def _full_text(self, slot: int, group: GroupRec) -> str:
        _present, _total, summary = self._summary(group)
        text = f"{slot} · {summary}"
        if self._edited == slot:
            text += " · edited"
        return text

    def _accessible_name(self, slot: int, group: GroupRec) -> str:
        present, total, summary = self._summary(group)
        text = f"Group {slot}, {summary}"
        if present < total:
            text = f"Group {slot}, {present} of {total} present"
        if self._edited == slot:
            text += ", edited"
        return text

    def _tooltip(self, slot: int, group: GroupRec) -> str:
        lines: list[str] = [self._full_text(slot, group)]
        for member in group.members[:15]:
            matches = [key for key in self._snapshot_keys if member_matches(member, key)]
            if not matches:
                lines.append(f"{member} (missing)")
                continue
            names = list(dict.fromkeys(self._labels_by_key.get(key, key) for key in matches))
            lines.append("; ".join(names))
        if len(group.members) > 15:
            lines.append(f"and {len(group.members) - 15} more")
        lines.extend(("", "Click or press the group number to make it the scan-list selection;",
                      f"Ctrl+{slot % 10} saves the selection over it."))
        return "\n".join(lines)

    def _update_edited(self) -> None:
        self._edited = None
        if self._store is None or self._tied is None:
            return
        group = self._store.slot_group(self._tied)
        if group is None:
            return
        if set(self.selection.keys) != set(self._keys_for_group(group)):
            self._edited = self._tied

    def _selection_changed(self, _change) -> None:
        self._update_edited()
        self._update_chips()

    def _update_chips(self):
        for slot, chip in self._chips.items():
            group = self._groups.get(slot)
            selected = self._tied == slot
            chip.setEnabled(self._store is not None)
            chip.setProperty("saved", group is not None)
            chip.setProperty("selected", selected)
            chip.setText(str(slot % 10) + ("*" if self._edited == slot else ""))
            name = self._accessible_name(slot, group) if group else f"Group {slot}, empty"
            chip.setAccessibleName(name + (", selected" if selected else ""))
            tooltip = self._tooltip(slot, group) if group else (
                f"Group {slot}, empty. Click to select this slot, then Save the scan-list selection. "
                f"Ctrl+{slot % 10} also saves here.")
            chip.setToolTip(tooltip if self._store else "Open an experiment to save groups")
            fill = "background: palette(highlight); color: palette(highlighted-text);" if group else "background: transparent;"
            border = "2px solid palette(text)" if selected else "1px solid palette(mid)"
            chip.setStyleSheet(f"QPushButton {{padding: 0; border-radius: 4px; border: {border}; {fill}}}")
        target = f"group {self._tied}" if self._tied else "a group"
        self._save_button.setEnabled(self._store is not None and self._tied is not None and bool(self.selection.keys))
        self._save_button.setAccessibleName(f"Save {target}")
        self._save_button.setToolTip(f"Save the current scan-list selection in {target}" if self._tied else "Select a numbered group slot first")
        self._clear_button.setEnabled(self._store is not None and self._tied in self._groups)
        self._clear_button.setAccessibleName(f"Clear {target}")
        self._clear_button.setToolTip(f"Clear {target}'s bookmark; keep the scans and current selection")

    def _clear_selected(self):
        if self._store is None or self._tied not in self._groups:
            return
        slot = self._tied
        self._store.delete_group(self._groups[slot].id)
        self.store_dirty.emit()
        self.reload()
        self._show_notice(f"Group {slot} cleared")

    def _show_notice(self, text):
        self._notice.set_full_text(text)
        self._notice.setAccessibleName(text)
        self._notice.setToolTip(text)
        self._notice_timer.start(6000)

    def _clear_notice(self):
        self._notice_timer.stop()
        self._notice.set_full_text("")
        self._notice.setToolTip("")

    def _save_slot(self, slot: int) -> None:
        if self._store is None:
            return
        if not self.selection.keys:
            self._show_notice(f"Nothing selected: group {slot} not saved")
            return
        previous = self._store.save_slot(slot, self.selection.keys)
        missing = sum(not self._present(member) for member in previous.members) if previous else 0
        self.store_dirty.emit()
        self._tied = slot
        self._groups = self._store.slot_groups()
        self._update_edited()
        self._update_chips()
        if previous is None:
            message = f"Group {slot} saved"
        else:
            message = f"Group {slot} replaced"
            if missing:
                message += f" · {missing} missing dropped"
        self._show_notice(message)

    def _recall(self, slot: int) -> None:
        if self._store is None:
            return
        group = self._store.slot_group(slot)
        self._tied = slot
        self._update_edited()
        self._update_chips()
        if group is None:
            self._show_notice(f"Group {slot} is empty: Ctrl+{slot % 10} saves the selection as group {slot}")
            return
        keys = self._keys_for_group(group)
        if not keys:
            self._show_notice(f"Group {slot}: none of its {len(group.members)} spectra are present")
            return
        self._tied = slot
        self.selection.set(keys, origin=ORIGIN_GROUP)
        self._update_edited()
        self._update_chips()
