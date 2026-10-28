"""Saved scan-selection groups for the Scan(s) page."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from PySide6.QtCore import QSize, QTimer, Qt, Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QToolButton,
    QWidget,
)

from isar_gui.gui_store import GROUP_SLOTS, GuiStore, GroupRec
from isar_gui.scan_keys import member_matches
from isar_gui.selection import ORIGIN_GROUP

log = logging.getLogger(__name__)

_NOTICE_MS = 6000
_BAR_STYLE = "QPushButton, QToolButton { padding: 1px 2px; }"
_COMPACT_STYLE = "QPushButton { padding: 1px 2px; }"


@dataclass
class _SlotWidgets:
    frame: QFrame
    chip: "_ChipButton"
    save: QPushButton


class _ChipButton(QPushButton):
    """A chip whose size hint follows its compact or full presentation."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._presentation_size = QSize(0, 0)

    def set_presentation_size(self, width: int, height: int) -> None:
        self._presentation_size = QSize(width, height)
        self.setFixedSize(width, height)

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(self._presentation_size)


class GroupChips(QWidget):
    """A compact, per-experiment bar for saving and recalling scan-list selections."""

    store_dirty = Signal()

    def __init__(self, scan_list_vm, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.scan_list_vm = scan_list_vm
        self.selection = scan_list_vm.selection
        self._store: GuiStore | None = None
        self._groups: dict[int, GroupRec] = {}
        self._tied: int | None = None
        self._edited: int | None = None
        self._visible_slots: set[int] = set()
        self._undo: tuple[int, GroupRec | None] | None = None
        self._notice_text = ""
        self._snapshot_keys: tuple[str, ...] = ()
        self._labels_by_key: dict[str, str] = {}

        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self.setStyleSheet(_BAR_STYLE)

        self._slot_widgets: dict[int, _SlotWidgets] = {}
        for slot in GROUP_SLOTS:
            frame = QFrame(self)
            frame.setObjectName(f"savedGroup{slot}")
            frame_layout = QHBoxLayout(frame)
            frame_layout.setContentsMargins(0, 0, 0, 0)
            frame_layout.setSpacing(0)
            chip = _ChipButton(frame)
            chip.setObjectName(f"groupChip{slot}")
            chip.clicked.connect(lambda _checked=False, n=slot: self._recall(n))
            save = QPushButton("Save", frame)
            save.setObjectName(f"groupSave{slot}")
            save.clicked.connect(lambda _checked=False, n=slot: self._save_slot(n))
            frame_layout.addWidget(chip)
            frame_layout.addWidget(save)
            self._layout.addWidget(frame)
            self._slot_widgets[slot] = _SlotWidgets(frame, chip, save)
            frame.hide()

        self._plus = QPushButton("+", self)
        self._plus.setObjectName("saveNewGroup")
        self._plus.setAccessibleName("Save the selection as a new group")
        self._plus.clicked.connect(self._save_next)
        self._layout.addWidget(self._plus)

        self._more_menu = QMenu(self)
        self._more = QToolButton(self)
        self._more.setObjectName("moreGroups")
        self._more.setText("»")
        self._more.setAccessibleName("More groups")
        self._more.setToolTip("More groups")
        self._more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self._more.setMenu(self._more_menu)
        self._layout.addWidget(self._more)

        self._notice = QLineEdit(self)
        self._notice.setObjectName("groupNotice")
        self._notice.setReadOnly(True)
        self._notice.setFrame(False)
        self._notice.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._layout.addWidget(self._notice, 1)

        self._undo_button = QPushButton("Undo", self)
        self._undo_button.setObjectName("undoGroupSave")
        self._undo_button.clicked.connect(self._undo_save)
        self._layout.addWidget(self._undo_button)

        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._clear_notice)

        self.selection.changed.connect(self._selection_changed)
        self.scan_list_vm.snapshot_changed.connect(self.reload)
        self._more.hide()
        self._notice.hide()
        self._undo_button.hide()
        self._set_small_controls()
        self._refresh_snapshot()
        self._update_plus()
        self._refit()

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        return QSize(0, max(24, super().minimumSizeHint().height()))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._refit()

    def set_store(self, store: GuiStore | None) -> None:
        self._store = store
        self._tied = None
        self._edited = None
        self._undo = None
        self._clear_notice()
        self.reload()

    def key_pressed(self, slot: int, save: bool) -> None:
        if slot not in GROUP_SLOTS or self._store is None:
            return
        if save:
            self._save_slot(slot)
            return
        if self._editor_has_focus():
            return
        self._recall(slot)

    def reload(self) -> None:
        """Re-read groups and the scan-list snapshot, then lay out the bar."""
        self._refresh_snapshot()
        self._groups = self._store.slot_groups() if self._store is not None else {}
        if self._tied not in self._groups:
            self._tied = None
        self._update_edited()
        self._update_chips()
        self._update_plus()
        self._refit()

    def chip(self, slot: int) -> QAbstractButton | None:
        widgets = self._slot_widgets.get(slot)
        if widgets is None or slot not in self._visible_slots or widgets.frame.isHidden():
            return None
        return widgets.chip

    def save_button(self, slot: int) -> QAbstractButton | None:
        widgets = self._slot_widgets.get(slot)
        if (widgets is None or slot not in self._visible_slots or self._edited != slot
                or widgets.frame.isHidden()):
            return None
        return widgets.save

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
                      f"Ctrl+{slot} saves the selection over it."))
        return "\n".join(lines)

    def _update_edited(self) -> None:
        self._edited = None
        if self._store is None or self._tied is None:
            return
        group = self._store.slot_group(self._tied)
        if group is None:
            self._tied = None
            return
        if set(self.selection.keys) != set(self._keys_for_group(group)):
            self._edited = self._tied

    def _selection_changed(self, _change) -> None:
        self._update_edited()
        self._update_chips()
        self._update_plus()
        self._refit()

    def _update_chips(self) -> None:
        for slot, widgets in self._slot_widgets.items():
            group = self._groups.get(slot)
            if group is None:
                widgets.frame.hide()
                continue
            full = self._full_text(slot, group)
            widgets.chip.setText(full)
            widgets.chip.setAccessibleName(self._accessible_name(slot, group))
            widgets.chip.setToolTip(self._tooltip(slot, group))
            widgets.save.setAccessibleName(f"Save group {slot}")
            widgets.save.setToolTip(f"Save the selection over group {slot}")
            widgets.save.setVisible(self._edited == slot)
            widgets.chip.setStyleSheet("")
            widgets.save.setStyleSheet("")

    def _update_plus(self) -> None:
        if self._store is None:
            self._plus.setEnabled(False)
            self._plus.setToolTip("Open an experiment to save groups")
            return
        if self._store.free_slot() is None:
            self._plus.setEnabled(False)
            self._plus.setToolTip("all 9 groups used: recall one and save over it")
            return
        if not self.selection.keys:
            self._plus.setEnabled(False)
            self._plus.setToolTip("Select spectra before saving a group")
            return
        self._plus.setEnabled(True)
        self._plus.setToolTip("Save the selection as a new group")

    def _save_next(self) -> None:
        if self._store is None:
            return
        slot = self._store.free_slot()
        if slot is not None:
            self._save_slot(slot)

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
        self._update_plus()
        if previous is None:
            message = f"Group {slot} saved"
        else:
            message = f"Group {slot} replaced"
            if missing:
                message += f" · {missing} missing dropped"
        self._show_notice(message, undo=(slot, previous))
        self._refit()

    def _recall(self, slot: int) -> None:
        if self._store is None:
            return
        group = self._store.slot_group(slot)
        if group is None:
            self._show_notice(f"Group {slot} is empty: Ctrl+{slot} saves the selection as group {slot}")
            return
        keys = self._keys_for_group(group)
        if not keys:
            self._show_notice(f"Group {slot}: none of its {len(group.members)} spectra are present")
            return
        self._tied = slot
        self.selection.set(keys, origin=ORIGIN_GROUP)
        self._update_edited()
        self._update_chips()
        self._refit()

    def _show_notice(self, text: str, undo: tuple[int, GroupRec | None] | None = None) -> None:
        self._notice_timer.stop()
        self._notice_text = text
        self._undo = undo
        self._notice.setText(text)
        self._notice.setAccessibleName(text)
        self._notice.setToolTip(text)
        self._notice.show()
        self._undo_button.setVisible(undo is not None)
        self._notice_timer.start(_NOTICE_MS)
        self._refit()

    def _clear_notice(self) -> None:
        self._notice_timer.stop()
        self._notice_text = ""
        self._undo = None
        self._notice.clear()
        self._notice.hide()
        self._undo_button.hide()
        self._refit()

    def _undo_save(self) -> None:
        if self._store is None or self._undo is None:
            return
        slot, previous = self._undo
        self._store.restore_slot(slot, previous)
        self.store_dirty.emit()
        self._tied = slot if previous is not None else None
        self._groups = self._store.slot_groups()
        self._update_edited()
        self._update_chips()
        self._update_plus()
        self._clear_notice()
        self._refit()

    def _set_small_controls(self) -> None:
        height = max(24, self.fontMetrics().height() + 6)
        for button, width in ((self._plus, 18), (self._more, 18), (self._undo_button, 28)):
            button.setFixedSize(width, height)
        self._notice.setFixedHeight(height)

    def _chip_width(self, slot: int, compact: bool) -> tuple[int, int]:
        widgets = self._slot_widgets[slot]
        group = self._groups[slot]
        full = self._full_text(slot, group)
        if compact:
            present, total, _summary = self._summary(group)
            if self._edited == slot:
                shown = f"{slot} · edited"
            elif present < total:
                shown = f"{slot} · {present}/{total}"
            else:
                shown = str(slot)
            widgets.chip.setText(shown)
            widgets.chip.setStyleSheet(_COMPACT_STYLE)
            widgets.save.setStyleSheet(_COMPACT_STYLE)
            chip_pad = 8
        else:
            shown = full
            widgets.chip.setText(full)
            widgets.chip.setStyleSheet("")
            widgets.save.setStyleSheet("")
            chip_pad = 16
        width = self.fontMetrics().horizontalAdvance(shown) + chip_pad
        save_width = self.fontMetrics().horizontalAdvance("Save") + (8 if compact else 14)
        chip_width = max(20, width)
        widgets.chip.set_presentation_size(chip_width, self._plus.height())
        if self._edited == slot:
            save_width = max(26, save_width)
            widgets.save.setFixedSize(save_width, self._plus.height())
            frame_width = chip_width + save_width
        else:
            widgets.save.setFixedSize(0, self._plus.height())
            frame_width = chip_width
        widgets.frame.setFixedSize(frame_width, self._plus.height())
        return frame_width, chip_width

    def _refit(self) -> None:
        if not hasattr(self, "_slot_widgets"):
            return
        self._set_small_controls()
        slots = [slot for slot in GROUP_SLOTS if slot in self._groups]
        if not slots:
            self._visible_slots = set()
            for widgets in self._slot_widgets.values():
                widgets.frame.hide()
            self._more.hide()
            self._more_menu.clear()
            self._update_menu([])
            return

        width = max(0, self.width())
        undo_width = self._undo_button.width() if self._undo is not None else 0
        base = self._plus.width() + undo_width
        full_widths = {slot: self._chip_width(slot, False)[0] for slot in slots}
        if sum(full_widths.values()) + base <= width:
            self._visible_slots = set(slots)
            for slot in slots:
                self._chip_width(slot, False)
            self._more.hide()
            self._apply_visibility(slots)
            self._update_menu([])
            return

        compact_widths = {slot: self._chip_width(slot, True)[0] for slot in slots}
        if sum(compact_widths.values()) + base <= width:
            self._visible_slots = set(slots)
            self._more.hide()
            self._apply_visibility(slots)
            self._update_menu([])
            return

        more_width = self._more.width()
        available = max(0, width - base - more_width)
        pinned = self._edited if self._edited in slots else None
        visible: list[int] = []
        used = 0
        if pinned is not None and compact_widths[pinned] <= available:
            visible.append(pinned)
            used = compact_widths[pinned]
        for slot in slots:
            if slot == pinned:
                continue
            if used + compact_widths[slot] <= available:
                visible.append(slot)
                used += compact_widths[slot]
        self._visible_slots = set(visible)
        for slot in visible:
            self._chip_width(slot, True)
        hidden = [slot for slot in slots if slot not in self._visible_slots]
        self._more.setVisible(bool(hidden))
        self._apply_visibility(visible)
        self._update_menu(hidden)

    def _apply_visibility(self, visible: list[int]) -> None:
        self._visible_slots = set(visible)
        for slot, widgets in self._slot_widgets.items():
            widgets.frame.setVisible(slot in self._visible_slots)
            widgets.save.setVisible(slot in self._visible_slots and self._edited == slot)

    def _update_menu(self, hidden: list[int]) -> None:
        self._more_menu.clear()
        for slot in hidden:
            group = self._groups[slot]
            full = self._full_text(slot, group)
            recall = QAction(f"Group {full}", self._more_menu)
            recall.triggered.connect(lambda _checked=False, n=slot: self._recall(n))
            self._more_menu.addAction(recall)
            if self._edited == slot:
                save = QAction(f"Save group {slot}", self._more_menu)
                save.triggered.connect(lambda _checked=False, n=slot: self._save_slot(n))
                self._more_menu.addAction(save)
