"""ScansDock — atomic scan-units as draggable chips, split across dimension tabs (Pass 2b).

The chip is the fundamental visible unit: one *scan × analyzer* (a ledger entry). A chip shows what is
truly per-unit — the analyzer, its OWN q, and status — coloured at its left edge by ledger status and
carrying a top accent shared by every analyzer of the same source scan (a sibling cue: siblings share
monitor/timing/environment systematics, so correlated units stay visible). Chips are draggable
(``MIME_SCAN_IDS``); a chip lights up in EVERY box it appears in when its unit is selected, and hovering
a chip soft-highlights its siblings everywhere.

Layout is a :class:`~PySide6.QtWidgets.QTabWidget` of dimension pages, each a scroll of group boxes:
  * **All** — every unit as flat chips (the coverage guarantee: nothing is hidden).
  * **Scans** — source-scan groups (``scan NN — #Q [...]``; scan-level metadata lives on the header).
  * **T** / **q** / **Analyzer** / **B** — auto-groups over each dimension. Analyzer is hidden on a
    single-detector workspace; B appears only when field data exists.
  * **Manual** — an Ungrouped box plus the persisted manual groups (drops mutate the GuiStore).

All derivation is in :class:`~isar_gui.viewmodels.scans.ScansViewModel`; this is presentation +
interaction only. The active tab is persisted as a GuiStore ui-scrap.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QMimeData, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QDrag, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from isar_gui.mime import (
    MIME_GROUP_REF,
    MIME_SCAN_IDS,
    decode_scan_ids,
    encode_group_ref,
    encode_scan_ids,
)
from isar_gui.scan_filter import ScanFilter, describe
from isar_gui.viewmodels.scans import sibling_hue, status_color, status_text

log = logging.getLogger(__name__)

_DRAG_THRESHOLD = 12

# The dimension tabs, in fixed order. Each carries the auto-group ``dim`` it renders (or a sentinel
# for the two structural pages). Analyzer/B are filtered out at build time when they do not apply.
_TAB_ALL = "All"
_TAB_MANUAL = "Manual"
_DIM_TABS = [
    (_TAB_ALL, None),
    ("Scans", "scan"),
    ("T", "T"),
    ("q", "q"),
    ("Analyzer", "ana"),
    ("B", "B"),
    (_TAB_MANUAL, None),
]


# ---------------------------------------------------------------------------
# Flow layout (ported pattern from Dungeon Weaver group_panel._FlowLayout)
# ---------------------------------------------------------------------------
class _FlowLayout(QVBoxLayout):
    """Minimal wrapping layout: chips flow into horizontal rows that re-wrap on resize.

    PySide6 ships no QFlowLayout; this rebuilds rows of QHBoxLayouts on each add/clear/resize. Ported
    (not copied) from the reference — same relayout-on-resize event-filter idea, ISAR-specific chips.
    """

    def __init__(self, parent: QWidget | None = None, spacing: int = 5):
        super().__init__()
        if parent is not None:
            parent.setLayout(self)
        self._widgets: list[QWidget] = []
        self._spacing = spacing
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(2)
        if isinstance(parent, QWidget):
            parent.installEventFilter(self)

    def add_chip(self, widget: QWidget) -> None:
        # Append only; the actual row-packing is deferred to :meth:`flush` so a box of N chips is
        # laid out ONCE (O(N)), not once per chip (O(N^2) — seconds at ~300 chips). A Show/Resize
        # event also triggers a relayout, so a box shown before flush still renders.
        self._widgets.append(widget)

    def flush(self) -> None:
        """Pack all appended chips into wrapping rows in a single pass (call after bulk add_chip)."""
        self._relayout()

    def clear_widgets(self) -> None:
        for w in self._widgets:
            w.setParent(None)
            w.deleteLater()
        self._widgets.clear()
        self._relayout()

    def eventFilter(self, watched, event):  # noqa: N802 (Qt override)
        if watched is self.parentWidget() and event.type() in {
                QEvent.Type.Resize, QEvent.Type.Show, QEvent.Type.LayoutRequest}:
            self._relayout()
        return super().eventFilter(watched, event)

    def _relayout(self) -> None:
        while self.count():
            item = self.takeAt(0)
            if item is None:
                continue
            sub = item.layout()
            if sub is not None:
                while sub.count():
                    sub.takeAt(0)
                sub.deleteLater()
        if not self._widgets:
            return
        parent = self.parentWidget()
        available = max(96, parent.contentsRect().width() - 4) if isinstance(parent, QWidget) else 240
        row: QHBoxLayout | None = None
        row_width = 0
        for w in self._widgets:
            width = max(w.minimumSizeHint().width(), w.sizeHint().width())
            if row is None or (row_width and row_width + self._spacing + width > available):
                row = QHBoxLayout()
                row.setContentsMargins(0, 0, 0, 0)
                row.setSpacing(self._spacing)
                self.addLayout(row)
                row_width = 0
            elif row_width:
                row_width += self._spacing
            row.addWidget(w)
            row_width += width
        for index in range(self.count()):
            item = self.itemAt(index)
            sub = item.layout() if item is not None else None
            if isinstance(sub, QHBoxLayout):
                sub.addStretch()


# ---------------------------------------------------------------------------
# Draggable scan-unit chip
# ---------------------------------------------------------------------------
class _ScanChip(QFrame):
    """A single atomic *scan × analyzer* unit as a draggable chip. Selection is external (the shared
    selection model); the chip only paints it. Drag payload is the current multi-selection when this
    chip is selected, else just this chip."""

    clicked = Signal(str, object)         # (unit_key, Qt.KeyboardModifiers)
    context_menu = Signal(str, object)    # (unit_key, global QPoint)
    hovered = Signal(str, bool)           # (unit_key, entered) — drives sibling soft-highlight

    def __init__(self, unit, vm, *, multi, parent=None):
        super().__init__(parent)
        self._key = unit.key
        self._scan_key = unit.scan_key
        self._vm = vm
        self._status = unit.status
        self._scan_number = unit.scan_number
        self._selected = False
        self._active = False
        self._highlight = False
        self._sibling = False
        self._drag_start: QPoint | None = None
        self.setObjectName("scanChip")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFixedSize(QSize(112, 62))

        num = unit.scan_number if unit.scan_number is not None else "?"
        # Multi-detector workspaces disambiguate analyzers on the face; single-detector show the scan.
        title = f"{num}·{unit.det}" if (multi and unit.det) else str(num)
        subtitle = f"{_q_compact(unit.q)} · {status_text(unit.status) or 'unknown'}"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 6, 4)
        layout.setSpacing(1)
        self._num_label = QLabel(title, self)
        self._sub_label = QLabel(subtitle, self)
        self._sub_label.setStyleSheet("color: #888; font-size: 9px; background: transparent;")
        self._active_label = QLabel("Inspecting", self)
        self._active_label.setStyleSheet(
            "color: #174f80; font-size: 9px; font-weight: bold; background: transparent;")
        self._active_label.setVisible(False)
        layout.addWidget(self._num_label)
        layout.addWidget(self._sub_label)
        layout.addWidget(self._active_label)

        tip = [unit.key, f"status: {status_text(unit.status) or 'unknown'}"]
        if unit.file_name:
            tip.append(unit.file_name)
        if unit.det:
            tip.append(f"analyzer: {unit.det}")
        if unit.q_target:
            tip.append("target #Q: [" + ", ".join(f"{float(v):.3g}" for v in unit.q_target) + "]")
        if unit.T is not None:
            tip.append(f"T = {unit.T:g} K")
        if unit.B is not None:
            tip.append(f"B = {unit.B:g} T")
        self.setToolTip("\n".join(tip))
        self._apply_style()

    @property
    def key(self) -> str:
        return self._key

    @property
    def scan_key(self) -> str:
        return self._scan_key

    def set_selected(self, flag: bool) -> None:
        if self._selected != flag:
            self._selected = flag
            self._apply_style()

    def set_active(self, flag: bool) -> None:
        if self._active != flag:
            self._active = flag
            self._active_label.setVisible(flag)
            self._apply_style()

    def set_highlight(self, flag: bool) -> None:
        if self._highlight != flag:
            self._highlight = flag
            self._apply_style()

    def set_sibling(self, flag: bool) -> None:
        if self._sibling != flag:
            self._sibling = flag
            self._apply_style()

    def _apply_style(self) -> None:
        edge = status_color(self._status)
        accent = sibling_hue(self._scan_number).name()
        strike = "text-decoration: line-through;" if self._status == "excluded" else ""
        self._num_label.setStyleSheet(
            f"font-weight: bold; font-size: 12px; background: transparent; {strike}")
        if self._highlight:
            bg = "#fff3b0"
        elif self._selected:
            bg = "rgba(91, 155, 213, 60)"
        elif self._sibling:
            bg = "rgba(158, 158, 158, 40)"
        else:
            bg = "palette(base)"
        # Active inspection is deliberately stronger than multi-selection: a whole group may be
        # selected for manipulation while only one detector is shown in the central inspector.
        border = "#174f80" if self._active else ("#5b9bd5" if self._selected else "palette(mid)")
        border_width = "3px" if self._active else "1px"
        self.setStyleSheet(
            f"#scanChip {{ background: {bg}; border: {border_width} solid {border};"
            f" border-left: 4px solid {edge}; border-top: 3px solid {accent};"
            f" border-radius: 6px; }}")

    # ---- sibling hover ----
    def enterEvent(self, event):  # noqa: N802
        self.hovered.emit(self._key, True)
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.hovered.emit(self._key, False)
        super().leaveEvent(event)

    # ---- drag source ----
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if not (event.buttons() & Qt.MouseButton.LeftButton) or self._drag_start is None:
            return super().mouseMoveEvent(event)
        if (event.pos() - self._drag_start).manhattanLength() < _DRAG_THRESHOLD:
            return super().mouseMoveEvent(event)
        self._drag_start = None
        selected = set(self._vm.selected_keys())
        keys = sorted(selected) if self._key in selected else [self._key]
        drag = QDrag(self)
        md = QMimeData()
        md.setData(MIME_SCAN_IDS, encode_scan_ids(keys))
        drag.setMimeData(md)
        drag.setPixmap(self._drag_pixmap())
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        drag.exec(Qt.DropAction.CopyAction)
        self.setCursor(Qt.CursorShape.OpenHandCursor)

    def _drag_pixmap(self) -> QPixmap:
        grab = self.grab()
        transparent = QPixmap(grab.size())
        transparent.fill(Qt.transparent)
        p = QPainter(transparent)
        p.setOpacity(0.7)
        p.drawPixmap(0, 0, grab)
        p.end()
        return transparent

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            self.clicked.emit(self._key, event.modifiers())
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event):  # noqa: N802
        self.context_menu.emit(self._key, event.globalPos())


# ---------------------------------------------------------------------------
# Group box (collapsible container of chips)
# ---------------------------------------------------------------------------
class _GroupBox(QFrame):
    """One collapsible container. ``manual`` boxes accept unit drops (mutate the store) and offer
    rename/delete + a group-drag header; ``auto`` boxes are muted, reject drops, but still drag as a
    unit. ``all``/``ungrouped`` boxes are neither (no drops, no drag)."""

    def __init__(self, box_id, title, vm, *, kind, parent=None):
        super().__init__(parent)
        self._id = box_id
        self._vm = vm
        self._kind = kind             # "all" | "ungrouped" | "auto" | "manual"
        self._manual = kind == "manual"
        self._draggable = kind in ("auto", "manual")
        self._chips: list[_ScanChip] = []
        self._anchor: str | None = None
        self._drag_start: QPoint | None = None
        self.setObjectName("groupBox")
        self.setAcceptDrops(self._manual)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(6)
        self._arrow = QToolButton(self)
        self._arrow.setAutoRaise(True)
        self._arrow.setArrowType(Qt.ArrowType.DownArrow)
        self._arrow.clicked.connect(self._toggle_collapsed)
        header.addWidget(self._arrow)

        self._title = QLabel(title, self)
        muted = "color: #999;" if kind in ("auto", "all") else ""
        self._title.setStyleSheet(f"font-weight: bold; background: transparent; {muted}")
        if self._manual:
            self._title.setCursor(Qt.CursorShape.PointingHandCursor)
            self._title.mouseDoubleClickEvent = self._on_rename
        header.addWidget(self._title)

        self._count = QLabel("", self)
        self._count.setStyleSheet("color: #888; background: transparent;")
        header.addWidget(self._count)
        header.addStretch(1)

        if self._manual:
            del_btn = QToolButton(self)
            del_btn.setText("✕")
            del_btn.setAutoRaise(True)
            del_btn.setToolTip("Delete group")
            del_btn.clicked.connect(self._on_delete)
            header.addWidget(del_btn)
        outer.addLayout(header)

        self._body = QWidget(self)
        self._flow = _FlowLayout(self._body, spacing=5)
        outer.addWidget(self._body)

        self._empty = QLabel("(empty)", self)
        self._empty.setStyleSheet("color: #999; font-style: italic; background: transparent;")
        outer.addWidget(self._empty)

        self._apply_frame_style()
        collapsed = vm.is_collapsed(box_id)
        self._set_body_visible(not collapsed)

    @property
    def box_id(self):
        return self._id

    @property
    def chips(self) -> list[_ScanChip]:
        return list(self._chips)

    def add_chip(self, chip: _ScanChip) -> None:
        chip.clicked.connect(self._on_chip_clicked)
        chip.context_menu.connect(self._on_chip_context)
        self._chips.append(chip)
        self._flow.add_chip(chip)
        self._empty.setVisible(False)
        self._count.setText(str(len(self._chips)))

    def finalize(self) -> None:
        self._flow.flush()          # single O(N) layout pass for the whole box (see _FlowLayout)
        self._empty.setVisible(not self._chips)
        self._count.setText(str(len(self._chips)))

    # ---- collapse ----
    def _toggle_collapsed(self) -> None:
        visible = not self._body.isVisible()
        self._set_body_visible(visible)
        self._vm.set_collapsed(self._id, not visible)

    def _set_body_visible(self, visible: bool) -> None:
        self._body.setVisible(visible)
        self._empty.setVisible(visible and not self._chips)
        self._arrow.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    # ---- manual rename / delete ----
    def _on_rename(self, _event) -> None:
        name, ok = QInputDialog.getText(self, "Rename group", "Group name:", text=self._title.text())
        if ok and name.strip():
            self._vm.rename_group(self._id, name.strip())

    def _on_delete(self) -> None:
        resp = QMessageBox.question(self, "Delete group",
                                    f"Delete group \"{self._title.text()}\"? "
                                    "The scans themselves are untouched.")
        if resp == QMessageBox.StandardButton.Yes:
            self._vm.delete_group(self._id)

    # ---- chip click selection (plain / ctrl / shift within this box) ----
    def _on_chip_clicked(self, key: str, modifiers) -> None:
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        if ctrl:
            self._vm.toggle_key(key)
            self._anchor = key
        elif shift and self._anchor is not None:
            keys = [c.key for c in self._chips]
            if self._anchor in keys and key in keys:
                i, j = keys.index(self._anchor), keys.index(key)
                lo, hi = (i, j) if i <= j else (j, i)
                self._vm.select_keys(keys[lo:hi + 1], clear=True)
            else:
                self._vm.select_keys([key], clear=True)
                self._anchor = key
        else:
            self._vm.select_keys([key], clear=True)
            self._anchor = key
            # A plain (unmodified) single click also opens the unit in the inspector; ctrl/shift
            # multi-select must NOT steal the inspector (the operator is building a set). A shift-click
            # with no anchor lands here too, so gate on the modifiers, not the branch.
            if not ctrl and not shift:
                self._vm.request_inspect(key)

    def _on_chip_context(self, key: str, global_pos) -> None:
        # Each action carries a stable string id in its data() so the dispatch (:meth:`_apply_chip_
        # action`) is decoupled from menu identity — that dispatch is what the tests exercise, since
        # QMenu.exec is a real modal that can't be safely driven in the offscreen suite.
        menu = QMenu(self)
        if self._manual:
            menu.addAction("Remove from this group").setData("remove")
        _sib_num = next((c._scan_number for c in self._chips if c.key == key), None)
        sib_label = "Select siblings" if _sib_num is None else f"Select siblings (scan {_sib_num})"
        menu.addAction(sib_label).setData("siblings")
        menu.addAction("Select in table").setData("select")
        menu.addAction("Copy scan id").setData("copy")
        menu.addSeparator()
        menu.addAction("New group from selection…").setData("new_group")
        chosen = menu.exec(global_pos)
        self._apply_chip_action(chosen.data() if chosen is not None else None, key)

    def _apply_chip_action(self, action, key: str) -> None:
        """Dispatch a chip context-menu action id for ``key`` (the tested seam; see :meth:`_on_chip_context`)."""
        if action == "remove":
            keys = set(self._vm.selected_keys())
            targets = keys if key in keys else {key}
            self._vm.remove_from_group(self._id, targets)
        elif action == "new_group":
            self._new_group_from(key)
        elif action == "siblings":
            self._vm.select_siblings(key, clear=True)
        elif action == "select":
            self._vm.select_keys([key], clear=True)
        elif action == "copy":
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(key)

    def _new_group_from(self, key: str) -> None:
        """Create a manual group from the current selection (or this chip), name pre-suggested.

        The dialog is pre-filled from :meth:`ScansViewModel.suggest_group_name`; tests MOCK
        ``QInputDialog.getText`` (a live modal would hang the offscreen suite)."""
        selected = set(self._vm.selected_keys())
        keys = sorted(selected) if key in selected else [key]
        suggested = self._vm.suggest_group_name(keys)
        name, ok = QInputDialog.getText(self, "New group from selection", "Group name:",
                                        text=suggested)
        if ok and name.strip():
            self._vm.create_group_from(name.strip(), keys)

    # ---- header context menu (freeze auto / rename+delete manual) ----
    def contextMenuEvent(self, event):  # noqa: N802
        """Right-click on the header: an auto box offers "Make this a manual group" (freeze); a
        manual box offers Rename/Delete parity with the double-click / ✕ affordances. all/ungrouped
        boxes have no header menu. Chip right-clicks are consumed by the chip, not here."""
        menu = QMenu(self)
        if self._kind == "auto":
            menu.addAction("Make this a manual group").setData("freeze")
        elif self._manual:
            menu.addAction("Rename group…").setData("rename")
            menu.addAction("Delete group").setData("delete")
        else:
            return
        chosen = menu.exec(event.globalPos())
        self._apply_header_action(chosen.data() if chosen is not None else None)

    def _apply_header_action(self, action) -> None:
        """Dispatch a header context-menu action id (the tested seam; see :meth:`contextMenuEvent`)."""
        if action == "freeze":
            self._vm.freeze_auto_group(self._id)
        elif action == "rename":
            self._on_rename(None)
        elif action == "delete":
            self._on_delete()

    # ---- drop target (manual only) ----
    def dragEnterEvent(self, event):  # noqa: N802
        if self._manual and event.mimeData().hasFormat(MIME_SCAN_IDS):
            event.acceptProposedAction()
            self._apply_frame_style(hover=True)

    def dragLeaveEvent(self, event):  # noqa: N802
        self._apply_frame_style()

    def dropEvent(self, event):  # noqa: N802
        self._apply_frame_style()
        if not (self._manual and event.mimeData().hasFormat(MIME_SCAN_IDS)):
            return
        keys = decode_scan_ids(event.mimeData().data(MIME_SCAN_IDS).data())
        if keys:
            self._vm.add_to_group(self._id, keys)
            event.acceptProposedAction()

    def _apply_frame_style(self, hover: bool = False) -> None:
        if hover:
            self.setStyleSheet("#groupBox { border: 1px dashed #5b9bd5; border-radius: 6px;"
                               " background: rgba(91,155,213,20); }")
        elif self._kind in ("auto", "all"):
            self.setStyleSheet("#groupBox { border: 1px dashed palette(mid); border-radius: 6px; }")
        else:
            self.setStyleSheet("#groupBox { border: 1px solid palette(mid); border-radius: 6px; }")

    # ---- header drag source (group drags as a unit; auto + manual only) ----
    def mousePressEvent(self, event):  # noqa: N802
        # Record the press for BOTH the drag source (draggable boxes) and the header-click select
        # (any box with chips). mouseMoveEvent clears it once a real drag begins, so the release
        # path can tell "plain click" from "the press became a drag".
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if (self._drag_start is None or not (event.buttons() & Qt.MouseButton.LeftButton)
                or not self._draggable):
            return super().mouseMoveEvent(event)
        if (event.pos() - self._drag_start).manhattanLength() < _DRAG_THRESHOLD:
            return super().mouseMoveEvent(event)
        self._drag_start = None
        member_keys = [c.key for c in self._chips]
        drag = QDrag(self)
        md = QMimeData()
        # BOTH types: a group ref for group-aware targets, flattened member ids for scan-only targets.
        md.setData(MIME_GROUP_REF, encode_group_ref(self._kind, str(self._id), self._title.text()))
        md.setData(MIME_SCAN_IDS, encode_scan_ids(member_keys))
        drag.setMimeData(md)
        drag.exec(Qt.DropAction.CopyAction)

    def mouseReleaseEvent(self, event):  # noqa: N802
        # A left press that never became a drag is a header click: select all member chips
        # (iPhone-folder manipulation). Ctrl extends the current selection; plain replaces it.
        if event.button() == Qt.MouseButton.LeftButton and self._drag_start is not None:
            self._select_members(event.modifiers())
        self._drag_start = None
        super().mouseReleaseEvent(event)

    def _select_members(self, modifiers) -> None:
        keys = [c.key for c in self._chips]
        if not keys:
            return
        ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
        self._vm.select_keys(keys, clear=not ctrl)


def _q_compact(hkl) -> str:
    if not hkl:
        return "—"
    try:
        return "[" + " ".join(f"{float(v):.2g}" for v in hkl) + "]"
    except (TypeError, ValueError) as exc:
        log.debug("_q_compact: non-numeric hkl %r (%s)", hkl, exc)
        return "—"


def _parse_float(text):
    """``float(text)`` or ``None`` for a blank/garbage field — the T-bound line edits never raise."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Filter controls (dumb widgets over the VM's shared ScanFilter)
# ---------------------------------------------------------------------------
class _MultiSelectButton(QToolButton):
    """A drop-down of checkable options; the button label carries the active count."""

    changed = Signal()

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self._title = title
        self.setPopupMode(QToolButton.InstantPopup)
        self.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self._menu = QMenu(self)
        self.setMenu(self._menu)
        self._actions: dict = {}
        self._refresh_text(set())

    def set_options(self, values, checked) -> None:
        """Populate (or re-check) the menu. Rebuilds only when the option set actually changed, so
        toggling a box does not tear the open menu out from under the pointer."""
        values = list(values)
        if list(self._actions) == values:
            for v, a in self._actions.items():
                a.blockSignals(True)
                a.setChecked(v in checked)
                a.blockSignals(False)
        else:
            self._menu.clear()
            self._actions = {}
            for v in values:
                a = self._menu.addAction(str(v))
                a.setCheckable(True)
                a.blockSignals(True)
                a.setChecked(v in checked)
                a.blockSignals(False)
                a.toggled.connect(self._on_toggled)
                self._actions[v] = a
        self._refresh_text(self.checked_values())

    def checked_values(self) -> set:
        return {v for v, a in self._actions.items() if a.isChecked()}

    def _on_toggled(self, _checked) -> None:
        self._refresh_text(self.checked_values())
        self.changed.emit()

    def _refresh_text(self, checked) -> None:
        n = len(checked)
        self.setText(self._title if not n else f"{self._title} ({n})")


class _FilterBar(QWidget):
    """A compact orthogonal filter over scan units — Analyzer/Status/Verdict/Type/T/q, ANDed.

    The bar is dumb: every control edit rebuilds a :class:`ScanFilter` and hands it to the VM via
    ``set_filter``; ``filter_changed`` calls :meth:`sync_from_vm` to reflect VM state back onto the
    controls (the bidirectional link with the query box). A ``_syncing`` guard breaks the feedback
    loop. The free-text clause (set through the query box) is preserved verbatim on every bar edit.
    """

    def __init__(self, vm, parent=None):
        super().__init__(parent)
        self._vm = vm
        self._syncing = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(2, 0, 2, 0)
        lay.setSpacing(4)

        self._ana = _MultiSelectButton("Analyzer", self)
        self._status = _MultiSelectButton("Status", self)
        self._verdict = _MultiSelectButton("Verdict", self)
        self._type = _MultiSelectButton("Type", self)
        for b in (self._ana, self._status, self._verdict, self._type):
            b.changed.connect(self._on_changed)
            lay.addWidget(b)

        lay.addWidget(QLabel("T", self))
        self._t_min = QLineEdit(self)
        self._t_min.setPlaceholderText("min")
        self._t_min.setFixedWidth(52)
        self._t_max = QLineEdit(self)
        self._t_max.setPlaceholderText("max")
        self._t_max.setFixedWidth(52)
        self._t_min.editingFinished.connect(self._on_changed)
        self._t_max.editingFinished.connect(self._on_changed)
        lay.addWidget(self._t_min)
        lay.addWidget(QLabel("–", self))
        lay.addWidget(self._t_max)

        self._q = QComboBox(self)
        self._q.setToolTip("Filter to one q / HKL bin")
        self._q.currentIndexChanged.connect(self._on_changed)
        lay.addWidget(QLabel("q", self))
        lay.addWidget(self._q)

        lay.addStretch(1)
        self._count = QLabel("", self)
        self._count.setStyleSheet("color: #888;")
        lay.addWidget(self._count)
        self._reset = QPushButton("Reset", self)
        self._reset.clicked.connect(self._vm.clear_filter)
        lay.addWidget(self._reset)

        vm.filter_changed.connect(self.sync_from_vm)
        self.sync_from_vm()

    def sync_from_vm(self) -> None:
        self._syncing = True
        try:
            f = self._vm.filter()
            self._ana.set_options(self._vm.available_analyzers(), f.analyzers)
            self._status.set_options([status_text(s) for s in self._vm.available_statuses()],
                                     {status_text(s) for s in f.statuses})
            verdicts = self._vm.available_verdicts()
            self._verdict.set_options(verdicts, f.verdicts)
            self._verdict.setVisible(bool(verdicts))
            types = self._vm.available_scan_types()
            self._type.set_options(types, f.scan_types)
            self._type.setVisible(len(types) > 1)
            self._t_min.setText("" if f.t_min is None else f"{f.t_min:g}")
            self._t_max.setText("" if f.t_max is None else f"{f.t_max:g}")
            self._rebuild_q(f)
            vis, total = self._vm.filtered_count()
            self._count.setText(f"showing {vis} of {total}" if f.is_active() else f"{total} units")
            self._reset.setVisible(f.is_active())
        finally:
            self._syncing = False

    def _rebuild_q(self, f) -> None:
        opts = self._vm.q_options()
        self._q.blockSignals(True)
        self._q.clear()
        self._q.addItem("any q", None)
        sel = 0
        for i, (label, center) in enumerate(opts, start=1):
            self._q.addItem(label, center)
            if f.q_target is not None and tuple(f.q_target) == tuple(center):
                sel = i
        self._q.setCurrentIndex(sel)
        self._q.setVisible(bool(opts))
        self._q.blockSignals(False)

    def _on_changed(self, *_args) -> None:
        if self._syncing:
            return
        cur = self._vm.filter()
        # Status labels are the display form (parsed↔ingested); map back to raw ledger tokens so the
        # predicate's prefix/alias matching still bites. The raw available list is the source of truth.
        raw_by_display = {status_text(s): s for s in self._vm.available_statuses()}
        statuses = {raw_by_display.get(v, v) for v in self._status.checked_values()}
        f = ScanFilter(
            analyzers=frozenset(str(v) for v in self._ana.checked_values()),
            statuses=frozenset(statuses),
            scan_types=frozenset(self._type.checked_values()),
            verdicts=frozenset(self._verdict.checked_values()),
            t_min=_parse_float(self._t_min.text()),
            t_max=_parse_float(self._t_max.text()),
            q_target=self._q.currentData(),
            text=cur.text,   # preserve the free-text clause owned by the query box
        )
        self._vm.set_filter(f)


class _QueryBox(QLineEdit):
    """A terse search/query line — bare text is an ambiguous match, typed prefixes narrow (see
    :func:`isar_gui.scan_filter.parse_query`). Ctrl+F focuses it; an invalid token reddens the border
    and shows the error as a tooltip without crashing. Kept in sync with the bar via the VM."""

    _OK_STYLE = ""
    _ERR_STYLE = "QLineEdit { border: 1px solid #c62828; }"

    def __init__(self, vm, parent=None):
        super().__init__(parent)
        self._vm = vm
        self._syncing = False
        self.setPlaceholderText("search:  372 · ana:9 · T>100 · status:held · q:1.5,0,0")
        self.setClearButtonEnabled(True)
        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(250)
        self._debounce.timeout.connect(self._apply)
        self.textEdited.connect(lambda _t: self._debounce.start())
        self.returnPressed.connect(self._apply)
        vm.filter_changed.connect(self._sync_from_vm)

    def _apply(self) -> None:
        self._debounce.stop()
        err = self._vm.apply_query(self.text())
        if err:
            self.setStyleSheet(self._ERR_STYLE)
            self.setToolTip(err)
        else:
            self.setStyleSheet(self._OK_STYLE)
            self.setToolTip("")

    def _sync_from_vm(self) -> None:
        # A bar edit changed the filter; reflect its description so the box mirrors the active state
        # without clobbering what the user is mid-typing (only update when not focused).
        if self.hasFocus():
            return
        self._syncing = True
        try:
            self.setToolTip(describe(self._vm.filter()))
        finally:
            self._syncing = False


# ---------------------------------------------------------------------------
# Dock
# ---------------------------------------------------------------------------
class ScansDock(QDockWidget):
    """The atomic scan-unit view: a tab per dimension, each a scroll of collapsible chip boxes."""

    def __init__(self, vm, parent=None):
        super().__init__("Scans", parent)
        self.setObjectName("scans_dock")
        self._vm = vm
        self._boxes: list[_GroupBox] = []           # all boxes across all built pages
        self._page_boxes: dict[str, list[_GroupBox]] = {}
        self._pages: dict[str, QWidget] = {}         # tab name -> content layout host
        self._page_layouts: dict[str, QVBoxLayout] = {}
        self._built: set[str] = set()                # names of pages whose chips are populated
        self._chips_by_key: dict[str, list[_ScanChip]] = {}
        self._active_key: str | None = None          # inspected unit; not the multi-selection

        body = QWidget(self)
        root = QVBoxLayout(body)
        root.setContentsMargins(4, 4, 4, 4)

        toolbar = QHBoxLayout()
        new_btn = QPushButton("New Group", body)
        new_btn.clicked.connect(self._new_group)
        expand_btn = QPushButton("Expand all", body)
        expand_btn.clicked.connect(lambda: self._set_all_collapsed(False))
        collapse_btn = QPushButton("Collapse all", body)
        collapse_btn.clicked.connect(lambda: self._set_all_collapsed(True))
        query_box = _QueryBox(vm, body)
        toolbar.addWidget(new_btn)
        toolbar.addWidget(query_box, 1)
        toolbar.addWidget(expand_btn)
        toolbar.addWidget(collapse_btn)
        root.addLayout(toolbar)
        self._query_box = query_box

        # Loud persisted-filter banner: a filter restored from gui.state.json must never hide data
        # silently, so it announces itself at the top of the dock until cleared (user decision).
        self._banner = QLabel(body)
        self._banner.setObjectName("filterBanner")
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            "QLabel#filterBanner { background: #fff3b0; color: #5b4a00; border: 1px solid #e0c95a;"
            " border-radius: 3px; padding: 4px 6px; }")
        self._banner.linkActivated.connect(lambda _l: self._vm.clear_filter())
        self._banner.setVisible(False)
        root.addWidget(self._banner)

        # The orthogonal filter bar sits above the tabs, so it persists across tab rebuilds.
        self._filter_bar = _FilterBar(vm, body)
        root.addWidget(self._filter_bar)

        self._tabs = QTabWidget(body)
        self._tabs.currentChanged.connect(self._on_tab_changed)
        root.addWidget(self._tabs, 1)

        self.setWidget(body)

        # Ctrl+F (the platform Find sequence) drops focus into the query box.
        find = QShortcut(QKeySequence.Find, self)
        find.setContext(Qt.WidgetWithChildrenShortcut)
        find.activated.connect(self._focus_query)

        vm.refreshed.connect(self._rebuild)
        vm.groups_changed.connect(self._rebuild)
        vm.filter_changed.connect(self._rebuild)
        vm.filter_changed.connect(self._sync_banner)
        vm.model.highlight_changed.connect(self._sync_highlight)
        vm.selection_model.selectionChanged.connect(self._sync_selection)
        self._rebuild()
        self._sync_banner()

    def _focus_query(self) -> None:
        self._query_box.setFocus()
        self._query_box.selectAll()

    def _sync_banner(self) -> None:
        f = self._vm.filter()
        if f.is_active():
            vis, total = self._vm.filtered_count()
            desc = describe(f)
            self._banner.setText(
                f"<b>Filter active</b> — showing {vis} of {total} scans"
                + (f" · {desc}" if desc else "")
                + '  <a href="#clear">Clear</a>')
            self._banner.setVisible(True)
        else:
            self._banner.setVisible(False)

    # ---- tab scaffolding -------------------------------------------------
    def _visible_tabs(self) -> list[tuple[str, str | None]]:
        tabs = []
        for name, dim in _DIM_TABS:
            if name == "Analyzer" and not self._vm.multi_detector:
                continue
            if name == "B" and not self._vm.has_field():
                continue
            tabs.append((name, dim))
        return tabs

    def _make_scroll_page(self) -> tuple[QScrollArea, QVBoxLayout, QWidget]:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(8)
        scroll.setWidget(content)
        return scroll, layout, content

    # ---- rebuild ---------------------------------------------------------
    def _rebuild(self) -> None:
        want = self._visible_tabs()
        prev = self._tabs.currentIndex()
        prev_name = self._tabs.tabText(prev) if prev >= 0 else self._vm.active_tab()

        self._tabs.blockSignals(True)
        while self._tabs.count():
            w = self._tabs.widget(0)
            self._tabs.removeTab(0)
            w.setParent(None)
            w.deleteLater()
        self._boxes = []
        self._page_boxes = {}
        self._pages = {}
        self._page_layouts = {}
        self._built = set()
        self._chips_by_key = {}

        self._tab_dims: dict[str, str | None] = {}
        for name, dim in want:
            scroll, layout, _content = self._make_scroll_page()
            self._tabs.addTab(scroll, name)
            self._pages[name] = scroll
            self._page_layouts[name] = layout
            self._page_boxes[name] = []
            self._tab_dims[name] = dim
        self._tabs.blockSignals(False)

        # Restore the persisted / previously-active tab if it still exists.
        target = prev_name if prev_name in self._pages else self._vm.active_tab()
        idx = 0
        if target in self._pages:
            idx = list(self._pages).index(target)
        self._tabs.setCurrentIndex(idx)
        self._build_page(self._tabs.tabText(idx))

    def _build_page(self, name: str) -> None:
        if name in self._built or name not in self._page_layouts:
            return
        self._built.add(name)
        layout = self._page_layouts[name]
        dim = self._tab_dims.get(name)
        rows = self._vm.rows()
        by_key = {u.key: u for u in rows}

        if name == _TAB_ALL:
            box = _GroupBox("all", "All units", self._vm, kind="ungrouped")
            self._populate(name, box, rows)
            self._add_box(name, layout, box)
        elif name == _TAB_MANUAL:
            # A persistent hint (not only when empty) keeps grouping discoverable — the operator's
            # first-session complaint was "no way to make a group" (user issue 2).
            hint = QLabel("Display-only groups: create one here, then drag scans in. They do not "
                          "change fitting or collation.",
                          self._pages[name].widget())
            hint.setObjectName("manualHint")
            hint.setStyleSheet("color: #999; font-style: italic; padding: 6px;")
            layout.addWidget(hint)
            manual = self._vm.manual_groups()
            ungrouped = _GroupBox("ungrouped", "Ungrouped", self._vm, kind="ungrouped")
            self._populate(name, ungrouped,
                           [u for u in rows if not self._vm.is_unit_grouped(u.key)])
            self._add_box(name, layout, ungrouped)
            if manual:
                self._add_section_label(layout, "Manual groups — display only")
                for g in manual:
                    box = _GroupBox(g.id, g.name or "(unnamed)", self._vm, kind="manual")
                    self._populate(name, box, self._vm.units_in_group(g))
                    self._add_box(name, layout, box)
        else:
            groups = self._vm.auto_groups(dim)
            for ag in groups:
                box = _GroupBox(ag.id, ag.label, self._vm, kind="auto")
                self._populate(name, box, [by_key[k] for k in ag.scan_keys if k in by_key])
                self._add_box(name, layout, box)
            if not groups and not self._vm.filter().is_active():
                empty = QLabel("(no groups on this dimension)", self._pages[name].widget())
                empty.setStyleSheet("color: #999; font-style: italic; padding: 6px;")
                layout.addWidget(empty)

        # A filter that narrows the whole view to nothing must say so — and offer a one-click way out
        # — instead of showing a blank pane (council pitfall: the empty-state trap).
        if self._vm.filter().is_active() and not rows:
            self._add_filter_empty(name, layout)

        layout.addStretch(1)
        self._sync_selection()
        self._sync_highlight()
        self._sync_active()

    def _add_filter_empty(self, name: str, layout) -> None:
        holder = QWidget(self._pages[name].widget())
        row = QVBoxLayout(holder)
        row.setContentsMargins(6, 6, 6, 6)
        msg = QLabel("No scans match the current filter.", holder)
        msg.setStyleSheet("color: #999; font-style: italic;")
        reset = QPushButton("Reset filter", holder)
        reset.clicked.connect(self._vm.clear_filter)
        row.addWidget(msg)
        reset_row = QHBoxLayout()
        reset_row.addWidget(reset)
        reset_row.addStretch(1)
        row.addLayout(reset_row)
        layout.addWidget(holder)

    def _on_tab_changed(self, index: int) -> None:
        if index < 0:
            return
        name = self._tabs.tabText(index)
        self._build_page(name)          # lazy: populate on first show
        self._vm.set_active_tab(name)   # persist the active dimension

    def _add_section_label(self, layout, text: str) -> None:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #888; font-size: 10px; font-weight: bold; padding-top: 4px;")
        layout.addWidget(lbl)

    def _add_box(self, name: str, layout, box: _GroupBox) -> None:
        box.finalize()
        self._boxes.append(box)
        self._page_boxes[name].append(box)
        layout.addWidget(box)

    def _populate(self, name: str, box: _GroupBox, units) -> None:
        multi = self._vm.multi_detector
        for u in units:
            chip = _ScanChip(u, self._vm, multi=multi)
            chip.hovered.connect(self._on_chip_hovered)
            box.add_chip(chip)
            self._chips_by_key.setdefault(u.key, []).append(chip)

    # ---- selection + highlight sync (a unit lights up in every box) ----
    def _sync_selection(self, *_args) -> None:
        selected = set(self._vm.selected_keys())
        for key, chips in self._chips_by_key.items():
            on = key in selected
            for chip in chips:
                chip.set_selected(on)

    def _sync_highlight(self, *_args) -> None:
        hot = self._vm.model.highlighted_keys()
        for key, chips in self._chips_by_key.items():
            on = key in hot
            for chip in chips:
                chip.set_highlight(on)

    def set_active_key(self, key: str | None) -> None:
        """Reflect the central inspector's unit without changing multi-selection."""
        self._active_key = str(key) if key else None
        self._sync_active()

    def _sync_active(self) -> None:
        for key, chips in self._chips_by_key.items():
            for chip in chips:
                chip.set_active(key == self._active_key)

    def _on_chip_hovered(self, unit_key: str, entered: bool) -> None:
        """Soft-highlight the hovered unit's siblings in every visible box (correlation cue)."""
        sibs = set(self._vm.siblings_of(unit_key)) if entered else set()
        for key, chips in self._chips_by_key.items():
            on = key in sibs
            for chip in chips:
                chip.set_sibling(on)

    # ---- toolbar ----
    def _new_group(self) -> None:
        name, ok = QInputDialog.getText(self, "New group", "Group name:")
        if ok and name.strip():
            gid = self._vm.create_group(name.strip())
            if gid is not None and _TAB_MANUAL in self._pages:
                self._tabs.setCurrentIndex(list(self._pages).index(_TAB_MANUAL))

    def _current_boxes(self) -> list[_GroupBox]:
        idx = self._tabs.currentIndex()
        name = self._tabs.tabText(idx) if idx >= 0 else None
        return self._page_boxes.get(name, [])

    def _set_all_collapsed(self, flag: bool) -> None:
        for box in self._current_boxes():
            box._set_body_visible(not flag)
            self._vm.set_collapsed(box.box_id, flag)
