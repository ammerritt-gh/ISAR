"""ScansDock — atomic scan-units as draggable chips, split across dimension tabs (Pass 2b).

Home's fundamental browser row is either a pooled master or one physical *scan × analyzer* child.
Physical children show their own q/status and carry the established ``MIME_SCAN_IDS`` drag payload;
masters are selectable derived objects and deliberately cannot be dragged into manual groups. The
workbench and preset views continue to use scan chips for group manipulation.

Layout is a :class:`~PySide6.QtWidgets.QTabWidget` — **Home** leads, then read-only preset lenses:
  * **Home** (id ``"Manual"``) — a scrollable hierarchical master/physical-scan browser above the
    persisted manual groups as editable **trays**, with an always-visible **＋ new box** tile. Group
    membership appears on physical rows; dragging a child reveals the valid tray targets. The flat
    "All" view is retired — the hierarchy is the all-scans view.
  * **Scans** — source-scan groups (``scan NN — #Q [...]``; scan-level metadata lives on the header).
  * **T** / **q** / **Analyzer** / **B** — auto-groups over each dimension (locked lenses, not
    editable trays). Analyzer is hidden on a single-detector workspace; B appears only with field data.

All derivation is in :class:`~isar_gui.viewmodels.scans.ScansViewModel`; this is presentation +
interaction only. The active tab is persisted (by id) as a GuiStore ui-scrap.
"""
from __future__ import annotations

import logging
from datetime import datetime

from PySide6.QtCore import QEvent, QMimeData, QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QDrag, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QComboBox,
    QAbstractItemView,
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
    QSplitter,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from isar_gui.mime import (
    MIME_DRAG_ORIGIN,
    MIME_GROUP_REF,
    MIME_SCAN_IDS,
    decode_drag_origin,
    decode_group_ref,
    decode_scan_ids,
    encode_drag_origin,
    encode_group_ref,
    encode_scan_ids,
)
from isar_gui.scan_filter import ScanFilter, describe
from isar_gui.viewmodels.scans import sibling_hue, status_color, status_text

log = logging.getLogger(__name__)

_DRAG_THRESHOLD = 12
_MASTER_FIT_ROLE = Qt.ItemDataRole.UserRole + 20
_UNIT_KEY_ROLE = Qt.ItemDataRole.UserRole + 21
_DIMMED_ROLE = Qt.ItemDataRole.UserRole + 22
_MASTER_VIEW_KEY_ROLE = Qt.ItemDataRole.UserRole + 23


class _MasterTree(QTreeWidget):
    """Scrollable master/member browser; only physical children can be dragged."""

    master_activated = Signal(object)
    unit_activated = Signal(str)
    fit_requested = Signal(str)
    drag_session = Signal(bool, object)

    def __init__(self, vm, parent=None):
        super().__init__(parent)
        self._vm = vm
        self.setColumnCount(8)
        self.setHeaderLabels(["Master / scan", "q / HKL", "T", "Branch", "omega / interval",
                              "Members / groups", "State", "Updated"])
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setRootIsDecorated(True)
        self.setAlternatingRowColors(True)
        self.setUniformRowHeights(True)
        self.itemClicked.connect(self._activate)

    def _activate(self, item, _column):
        key = item.data(0, _UNIT_KEY_ROLE)
        if key:
            keys = [str(node.data(0, _UNIT_KEY_ROLE)) for node in self.selectedItems()
                    if node.data(0, _UNIT_KEY_ROLE)]
            self._vm.select_keys(keys or [str(key)])
            self.unit_activated.emit(str(key))
            return
        fit = item.data(0, _MASTER_FIT_ROLE)
        if fit is not None:
            self.master_activated.emit(fit)

    def startDrag(self, supported_actions):  # noqa: N802
        keys = [str(item.data(0, _UNIT_KEY_ROLE)) for item in self.selectedItems()
                if item.data(0, _UNIT_KEY_ROLE)]
        if not keys:
            return
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(MIME_SCAN_IDS, encode_scan_ids(keys))
        mime.setData(MIME_DRAG_ORIGIN, encode_drag_origin(""))
        drag.setMimeData(mime)
        self.drag_session.emit(True, keys)
        try:
            drag.exec(Qt.DropAction.CopyAction)
        finally:
            self.drag_session.emit(False, keys)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat(MIME_SCAN_IDS):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat(MIME_SCAN_IDS):
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        mime = event.mimeData()
        origin = decode_drag_origin(mime.data(MIME_DRAG_ORIGIN).data()) \
            if mime.hasFormat(MIME_DRAG_ORIGIN) else ""
        keys = decode_scan_ids(mime.data(MIME_SCAN_IDS).data())
        if origin and keys:
            self._vm.remove_from_group(origin, keys)
        event.acceptProposedAction()

    def contextMenuEvent(self, event):  # noqa: N802
        """Preserve physical-scan context actions on the hierarchical browser.

        Master rows deliberately have no scan actions: they are derived objects and cannot acquire
        manual-group membership.  Child actions use the same VM operations as the former chips.
        """
        item = self.itemAt(event.pos())
        key = None if item is None else item.data(0, _UNIT_KEY_ROLE)
        if not key:
            return
        menu = QMenu(self)
        menu.addAction("Select siblings").setData("siblings")
        menu.addAction("Select in table").setData("select")
        menu.addAction("Copy scan id").setData("copy")
        menu.addSeparator()
        menu.addAction("Fit (sandbox preview)").setData("fit_sandbox")
        menu.addAction("New group from selection…").setData("new_group")
        chosen = menu.exec(event.globalPos())
        self._apply_unit_action(chosen.data() if chosen is not None else None, str(key))

    def _apply_unit_action(self, action, key: str) -> None:
        """Dispatch a physical-child context action; kept as a small testable seam."""
        if action == "siblings":
            self._vm.select_siblings(key, clear=True)
        elif action == "select":
            self._vm.select_keys([key], clear=True)
        elif action == "copy":
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(key)
        elif action == "fit_sandbox":
            self.fit_requested.emit(key)
        elif action == "new_group":
            selected = set(self._vm.selected_keys())
            keys = sorted(selected) if key in selected else [key]
            suggested = self._vm.suggest_group_name(keys)
            name, ok = QInputDialog.getText(
                self, "New group from selection", "Group name:", text=suggested)
            if ok and name.strip():
                self._vm.create_group_from(name.strip(), keys)

# The dimension tabs, in fixed order: (persisted_id, display_label, auto-group ``dim``). The id is
# the stable persistence key (GuiStore.scans_tab, page-dict keys); the label is display-only, so the
# Manual page can read "Home" without breaking a stored ``scans_tab == "Manual"`` (plan R1).
# Analyzer/B are filtered out at build time when they do not apply.
_TAB_ALL = "All"        # retired as a tab (the Home Pool subsumes it); kept for the box-kind sentinel
_TAB_MANUAL = "Manual"
# Home (the Pool + editable trays) leads; the rest are read-only preset lenses. The "All" flat tab is
# gone — the Home Pool IS the all-scans view (plan R9). Persistence still keys on the id "Manual".
_DIM_TABS = [
    (_TAB_MANUAL, "Home", None),
    ("Scans", "Scans", "scan"),
    ("T", "T", "T"),
    ("q", "q", "q"),
    ("Analyzer", "Analyzer", "ana"),
    ("B", "B", "B"),
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
    #: A merge drop landed on this chip (plan R3): (target_key, dragged_keys, this_chip_origin_gid).
    #: The dock turns it into a new folder (target loose) or an add-to-group (target already foldered).
    merge_requested = Signal(str, object, str)
    #: A drag from this chip started (True)/ended (False), carrying the dragged keys — drives the
    #: drag-reveal so drop targets light up for the whole drag (plan R12).
    drag_session = Signal(bool, object)

    #: Dwell before a hovered merge arms (ms) — with the 12px drag threshold this guards accidents.
    _DWELL_MS = 180

    def __init__(self, unit, vm, *, multi, droppable=False, origin_group_id="", badge=0, parent=None):
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
        self._dimmed = False
        self._merge_armed = False        # set by the dwell timer; gates the merge drop (R3)
        self._origin_gid = str(origin_group_id or "")   # the folder this chip is shown in ("" = loose)
        self._drag_start: QPoint | None = None
        self.setObjectName("scanChip")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFixedSize(QSize(112, 62))
        # On the home grid a chip is also a DROP target so another chip can be dropped onto it to
        # birth/extend a folder. Preset/auto chips are non-droppable (their tabs are read-only).
        self.setAcceptDrops(bool(droppable))
        self._dwell = QTimer(self)
        self._dwell.setSingleShot(True)
        self._dwell.setInterval(self._DWELL_MS)
        self._dwell.timeout.connect(self._arm_merge)

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

        # Pool membership badge (R9): a small corner count when this scan is in ≥1 group. Absolutely
        # positioned in the fixed-size chip's top-right so it overlays without disturbing the layout.
        self._badge = int(badge or 0)
        self._badge_label = QLabel(str(self._badge), self)
        self._badge_label.setStyleSheet(
            "color: white; background: #5b9bd5; border-radius: 7px; font-size: 9px;"
            " font-weight: bold; padding: 0 3px;")
        self._badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._badge_label.adjustSize()
        self._badge_label.move(self.width() - self._badge_label.width() - 3, 3)
        self._badge_label.setVisible(self._badge > 0)
        self._badge_label.setToolTip(f"in {self._badge} group(s)")

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

    def set_dimmed(self, flag: bool) -> None:
        """Ambient-lens dim (plan R7): a chip not in the active lens bin is greyed. Precedence in
        :meth:`_apply_style` keeps the active/selected/sibling cues winning over the dim."""
        if self._dimmed != flag:
            self._dimmed = flag
            self._apply_style()

    def _apply_style(self) -> None:
        edge = status_color(self._status)
        accent = sibling_hue(self._scan_number).name()
        strike = "text-decoration: line-through;" if self._status == "excluded" else ""
        self._num_label.setStyleSheet(
            f"font-weight: bold; font-size: 12px; background: transparent; {strike}")
        # Precedence (highest first): armed-merge halo > inspected(active) > multi-select > highlight
        # > sibling-hover > lens-dim > normal. The lens never overrides the active/selected/sibling
        # cues, so an operator's current focus stays legible while a lens is on (R7).
        opacity = ""
        if self._merge_armed:
            bg = "rgba(91, 155, 213, 90)"
        elif self._highlight:
            bg = "#fff3b0"
        elif self._selected:
            bg = "rgba(91, 155, 213, 60)"
        elif self._sibling:
            bg = "rgba(158, 158, 158, 40)"
        elif self._dimmed and not self._active:
            bg = "palette(base)"
            opacity = "color: #b0b0b0;"   # greyed text; the whole chip reads as "not in this lens bin"
        else:
            bg = "palette(base)"
        # Active inspection is deliberately stronger than multi-selection: a whole group may be
        # selected for manipulation while only one detector is shown in the central inspector.
        if self._merge_armed:
            border, border_width = "#5b9bd5", "2px"
        elif self._active:
            border, border_width = "#174f80", "3px"
        elif self._selected:
            border, border_width = "#5b9bd5", "1px"
        elif self._dimmed:
            border, border_width = "palette(midlight)", "1px"
        else:
            border, border_width = "palette(mid)", "1px"
        self.setStyleSheet(
            f"#scanChip {{ background: {bg}; border: {border_width} solid {border};"
            f" border-left: 4px solid {edge}; border-top: 3px solid {accent};"
            f" border-radius: 6px; {opacity} }}")

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
        # ACCEPT (do not call super): the chip owns its click/drag. Propagating to the parent folder
        # box would let the box's header-click ALSO fire and select every member — so a single-chip
        # click ended up selecting the whole group (there was no way to select one scan).
        event.accept()

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
        # Stamp the folder this chip is being dragged FROM so a drag-out target removes from the
        # ORIGIN group only (overlap-safe, R2). Loose/preset chips stamp "" (drag-out is a no-op).
        md.setData(MIME_DRAG_ORIGIN, encode_drag_origin(self._origin_gid))
        drag.setMimeData(md)
        drag.setPixmap(self._drag_pixmap())
        self.setCursor(Qt.CursorShape.ClosedHandCursor)
        # Reveal drop targets for the whole (blocking) drag, then reset — Qt paints during exec (R12).
        self.drag_session.emit(True, keys)
        try:
            drag.exec(Qt.DropAction.CopyAction)
        finally:
            self.drag_session.emit(False, keys)
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
        event.accept()   # consume: do not let the release fall through to the folder box (select-all)

    def contextMenuEvent(self, event):  # noqa: N802
        self.context_menu.emit(self._key, event.globalPos())
        event.accept()   # consume: the chip menu, never the folder box's header menu underneath

    # ---- drop target: chip-onto-chip merge with a dwell gate (R3) ----
    @staticmethod
    def _dragged_others(md, own_key):
        """The dragged unit keys other than this chip's own (a self-only drag yields ``[]``).

        A folder-header drag (carries a group ref) is NOT a chip merge — it reorders on the page —
        so it never arms a chip."""
        if not md.hasFormat(MIME_SCAN_IDS) or md.hasFormat(MIME_GROUP_REF):
            return []
        keys = decode_scan_ids(md.data(MIME_SCAN_IDS).data())
        return [k for k in keys if k != own_key]

    def dragEnterEvent(self, event):  # noqa: N802
        if self._dragged_others(event.mimeData(), self._key):
            event.acceptProposedAction()
            self._dwell.start()          # arm after the dwell; drop before that is ignored
        # A self-only drag is not accepted here, so it falls through (no accidental self-group).

    def dragMoveEvent(self, event):  # noqa: N802
        if self._dragged_others(event.mimeData(), self._key):
            event.acceptProposedAction()

    def dragLeaveEvent(self, event):  # noqa: N802
        self._disarm_merge()

    def dropEvent(self, event):  # noqa: N802
        others = self._dragged_others(event.mimeData(), self._key)
        armed = self._merge_armed
        self._disarm_merge()
        if armed and others:
            # target loose -> new folder; target already foldered -> add to that folder (dock routes).
            self.merge_requested.emit(self._key, others, self._origin_gid)
            event.acceptProposedAction()
        # An early (unarmed) drop is intentionally ignored: nothing happens, the chip is unchanged.

    def hideEvent(self, event):  # noqa: N802
        self._disarm_merge()
        super().hideEvent(event)

    def _arm_merge(self) -> None:
        if not self._merge_armed:
            self._merge_armed = True
            self._apply_style()

    def _disarm_merge(self) -> None:
        self._dwell.stop()
        if self._merge_armed:
            self._merge_armed = False
            self._apply_style()


# ---------------------------------------------------------------------------
# Group box (collapsible container of chips)
# ---------------------------------------------------------------------------
class _GroupBox(QFrame):
    """One collapsible container. ``manual`` boxes accept unit drops (mutate the store) and offer
    rename/delete + a group-drag header; ``auto`` boxes are muted, reject drops, but still drag as a
    unit. ``all``/``ungrouped`` boxes are neither (no drops, no drag)."""

    #: (member_keys, target, label) — a "Send to <plot>" request from the folder header menu (2A).
    send_requested = Signal(object, str, str)
    #: (unit_key) — a "Fit (sandbox preview)" request from a chip's menu (2B).
    fit_requested = Signal(str)

    def __init__(self, box_id, title, vm, *, kind, remove_target=False, parent=None):
        super().__init__(parent)
        self._id = box_id
        self._vm = vm
        self._kind = kind             # "all" | "ungrouped" | "auto" | "manual"
        self._manual = kind == "manual"
        self._draggable = kind in ("auto", "manual")
        # The home Ungrouped box doubles as a drag-OUT target: dropping a chip carrying a folder
        # origin removes it from that origin group only (R2). Manual boxes accept chip drops to add.
        self._remove_target = bool(remove_target)
        self._chips: list[_ScanChip] = []
        self._anchor: str | None = None
        self._drag_start: QPoint | None = None
        self._name_edit: QLineEdit | None = None   # inline rename editor while active (R4/R5)
        self._armed = False                        # drag-reveal glow while a scan drag is in flight
        self.setObjectName("groupBox")
        self.setAcceptDrops(self._manual or self._remove_target)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        header = QHBoxLayout()
        header.setSpacing(6)
        self._header = header
        self._arrow = QToolButton(self)
        self._arrow.setAutoRaise(True)
        self._arrow.setArrowType(Qt.ArrowType.DownArrow)
        self._arrow.clicked.connect(self._toggle_collapsed)
        header.addWidget(self._arrow)

        if kind == "auto":
            # A small lock glyph marks a computed lens as read-only, never an editable tray (R10).
            lock = QLabel("\N{LOCK}", self)
            lock.setStyleSheet("color: #999; background: transparent;")
            lock.setToolTip("computed view — read-only")
            header.addWidget(lock)
        self._title = QLabel(title, self)
        muted = "color: #999;" if kind in ("auto", "all") else ""
        self._title.setStyleSheet(f"font-weight: bold; background: transparent; {muted}")
        if self._manual:
            self._title.setCursor(Qt.CursorShape.PointingHandCursor)
            self._title.mouseDoubleClickEvent = lambda _e: self.begin_inline_rename()
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

        # An empty editable tray shows a big faint "＋" (an open mouth to drop into) rather than a
        # word; other containers keep a terse "(empty)" (R10).
        if self._manual:
            self._empty = QLabel("＋", self)
            self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self._empty.setStyleSheet("color: #b8d0ef; font-size: 20px; background: transparent;")
        else:
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

    def set_full_member_count(self, total: int) -> None:
        """Show ``K of M`` when an active filter hides members, so the tile is honest that a
        drag carries the FULL M-member group, not just the K visible chips (plan R8)."""
        shown = len(self._chips)
        self._count.setText(str(shown) if total <= shown else f"{shown} of {total}")

    # ---- collapse ----
    def _toggle_collapsed(self) -> None:
        visible = not self._body.isVisible()
        self._set_body_visible(visible)
        self._vm.set_collapsed(self._id, not visible)

    def _set_body_visible(self, visible: bool) -> None:
        self._body.setVisible(visible)
        self._empty.setVisible(visible and not self._chips)
        self._arrow.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)

    # ---- manual rename (inline, R4/R5) / delete ----
    def begin_inline_rename(self) -> None:
        """Swap the folder name label for a focused, select-all editor (no modal). Used by
        double-click, the header menu, and folder birth (the dock opens the new folder straight into
        this). Commit on Enter/focus-out. A rename triggers a rebuild that discards this box, so no
        post-commit restore is needed on success."""
        if not self._manual or self._name_edit is not None:
            return
        edit = QLineEdit(self._title.text(), self)
        edit.setObjectName("groupNameEdit")
        edit.selectAll()
        self._name_edit = edit
        self._header.replaceWidget(self._title, edit)
        self._title.hide()
        edit.show()
        edit.setFocus()
        edit.editingFinished.connect(self._commit_inline_rename)

    def _commit_inline_rename(self) -> None:
        edit = self._name_edit
        if edit is None:
            return   # editingFinished can fire twice (Enter then focus-out) — commit once
        self._name_edit = None
        name = edit.text().strip()
        self._header.replaceWidget(edit, self._title)
        edit.deleteLater()
        self._title.show()
        if name and name != self._title.text():
            self._vm.rename_group(self._id, name)

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
        menu.addAction("Fit (sandbox preview)").setData("fit_sandbox")
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
        elif action == "fit_sandbox":
            self.fit_requested.emit(key)

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
            self._add_send_to_menu(menu)
        else:
            return
        chosen = menu.exec(event.globalPos())
        self._apply_header_action(chosen.data() if chosen is not None else None)

    #: "Send to <plot>" targets: (label, dock target kind).
    _SEND_TARGETS = (("Dispersion ω(q)", "dispersion"),
                     ("Plane (q, E)", "heatmap"))

    def _add_send_to_menu(self, menu: QMenu) -> None:
        """A "Send to ▸" submenu (2A). Each verb is greyed when the group can't satisfy it, with the
        destination's own refusal reason as the disabled action's tooltip."""
        keys = self._vm.member_keys(self._id)
        sub = menu.addMenu("Send to")
        sub.setToolTipsVisible(True)
        for label, target in self._SEND_TARGETS:
            act = sub.addAction(label)
            act.setData(f"send:{target}")
            ok, reason = self._vm.can_target(target, keys)
            act.setEnabled(ok)
            if not ok:
                act.setToolTip(reason)

    def _apply_header_action(self, action) -> None:
        """Dispatch a header context-menu action id (the tested seam; see :meth:`contextMenuEvent`)."""
        if action == "freeze":
            self._vm.freeze_auto_group(self._id)
        elif action == "rename":
            self.begin_inline_rename()
        elif action == "delete":
            self._on_delete()
        elif action and action.startswith("send:"):
            target = action.split(":", 1)[1]
            self.send_requested.emit(self._vm.member_keys(self._id), target, self._title.text())

    # ---- drop target: manual box = add members; home Ungrouped box = drag-out remove (R2) ----
    @staticmethod
    def _is_chip_drop(md) -> bool:
        """A plain chip drag (scan ids) — NOT a folder-header drag (which also carries a group ref).
        Folder-on-folder drops are handled by the home page's reorder, never a silent member merge."""
        return md.hasFormat(MIME_SCAN_IDS) and not md.hasFormat(MIME_GROUP_REF)

    def dragEnterEvent(self, event):  # noqa: N802
        md = event.mimeData()
        if self._manual and self._is_chip_drop(md):
            event.acceptProposedAction()
            self._apply_frame_style(hover=True)
        elif self._remove_target and self._is_chip_drop(md) and decode_drag_origin(
                md.data(MIME_DRAG_ORIGIN).data()):
            event.acceptProposedAction()
            self._apply_frame_style(hover=True)

    def dragLeaveEvent(self, event):  # noqa: N802
        self._apply_frame_style()

    def dropEvent(self, event):  # noqa: N802
        self._apply_frame_style()
        md = event.mimeData()
        if not self._is_chip_drop(md):
            return
        keys = decode_scan_ids(md.data(MIME_SCAN_IDS).data())
        if not keys:
            return
        if self._manual:
            self._vm.add_to_group(self._id, keys)   # copy into this folder (origin membership kept)
            event.acceptProposedAction()
        elif self._remove_target:
            origin = decode_drag_origin(md.data(MIME_DRAG_ORIGIN).data())
            if origin:   # remove from the ORIGIN folder only; a loose chip ("") is a no-op
                self._vm.remove_from_group(origin, keys)
                event.acceptProposedAction()

    def set_armed(self, flag: bool) -> None:
        """Drag-reveal (R12): while a scan drag is in flight, editable trays glow as valid targets.
        Preset/`auto` (locked) boxes never arm — they are read-only views, not drop targets."""
        if not self._manual or self._armed == flag:
            return
        self._armed = flag
        self._apply_frame_style()

    def _apply_frame_style(self, hover: bool = False) -> None:
        if hover or (self._armed and self._manual):
            # drop-ready: bright dashed border + tint (also the drag-reveal armed state)
            self.setStyleSheet("#groupBox { border: 2px dashed #5b9bd5; border-radius: 8px;"
                               " background: rgba(91,155,213,25); }")
        elif self._kind == "auto":
            # a computed lens — frosted/locked, never reads as an editable tray (council R10)
            self.setStyleSheet("#groupBox { border: 1px dashed palette(mid); border-radius: 6px;"
                               " background: rgba(128,128,128,12); }")
        elif self._manual:
            # an editable TRAY: a raised container with a subtle well, so it reads as "put things here"
            self.setStyleSheet("#groupBox { border: 2px solid #9cc0f0; border-radius: 8px;"
                               " background: rgba(91,155,213,10); }")
        else:
            # the Pool (and other neutral containers): a plain bordered surface
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
        # A filtered manual tray shows only its visible chips, but the folder object still carries
        # its full persisted membership. Computed auto groups are view lenses, so their payload is
        # deliberately the visible chips from which they were derived.
        member_keys = (self._vm.member_keys(self._id) if self._manual
                       else [c.key for c in self._chips])
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
        return "[" + " ".join(f"{float(v):.6g}" for v in hkl) + "]"
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
# "+ new box" tray tile — the always-visible make-a-group affordance (R11)
# ---------------------------------------------------------------------------
class _NewBoxTile(QFrame):
    """A dashed ghost tile with a big ＋. Drop scans onto it to make a group pre-populated, or click
    for an empty one. Brightens while a scan drag is in flight (a drag-reveal target, R12)."""

    create_requested = Signal(object)   # dropped keys (list) — empty list = an empty group via click

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("newBoxTile")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(54)
        self._armed = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 4)
        self._label = QLabel("＋  new group", self)
        self._label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._label.setStyleSheet(
            "color: #5b9bd5; font-weight: bold; font-size: 14px; background: transparent;")
        lay.addWidget(self._label)
        self._apply_style()

    @staticmethod
    def _is_chip_drop(md) -> bool:
        return md.hasFormat(MIME_SCAN_IDS) and not md.hasFormat(MIME_GROUP_REF)

    def set_armed(self, flag: bool) -> None:
        if self._armed != flag:
            self._armed = flag
            self._apply_style()

    def _apply_style(self, hover: bool = False) -> None:
        if hover or self._armed:
            self.setStyleSheet("#newBoxTile { border: 2px dashed #5b9bd5; border-radius: 8px;"
                               " background: rgba(91,155,213,25); }")
        else:
            self.setStyleSheet("#newBoxTile { border: 2px dashed palette(mid); border-radius: 8px; }")

    def dragEnterEvent(self, event):  # noqa: N802
        if self._is_chip_drop(event.mimeData()):
            event.acceptProposedAction()
            self._apply_style(hover=True)

    def dragLeaveEvent(self, event):  # noqa: N802
        self._apply_style()

    def dropEvent(self, event):  # noqa: N802
        md = event.mimeData()
        self._apply_style()
        if not self._is_chip_drop(md):
            return
        keys = decode_scan_ids(md.data(MIME_SCAN_IDS).data())
        if keys:
            self.create_requested.emit(list(keys))
            event.acceptProposedAction()

    def mouseReleaseEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self.create_requested.emit([])
        event.accept()


# ---------------------------------------------------------------------------
# Home (Manual) page body — accepts a folder-header drop to reorder folders (R6)
# ---------------------------------------------------------------------------
class _HomePage(QWidget):
    """The Home grid's content widget. A folder-header drag (:data:`MIME_GROUP_REF`) dropped in the
    gaps between folders reorders them; the dock resolves the drop-y to an index. A chip drag is not
    handled here (it lands on a chip or a group box)."""

    reorder_requested = Signal(str, int)   # (dragged manual-group id, drop y in content coords)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat(MIME_GROUP_REF):
            event.acceptProposedAction()

    def dragMoveEvent(self, event):  # noqa: N802
        if event.mimeData().hasFormat(MIME_GROUP_REF):
            event.acceptProposedAction()

    def dropEvent(self, event):  # noqa: N802
        md = event.mimeData()
        if not md.hasFormat(MIME_GROUP_REF):
            return
        ref = decode_group_ref(md.data(MIME_GROUP_REF).data())
        if ref is None or ref.get("kind") != "manual":
            return   # only manual folders reorder; an auto-group ref is not orderable
        pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
        self.reorder_requested.emit(ref["group_id"], int(pos.y()))
        event.acceptProposedAction()


# ---------------------------------------------------------------------------
# Dock
# ---------------------------------------------------------------------------
class ScansDock(QDockWidget):
    """The atomic scan-unit view: a tab per dimension, each a scroll of collapsible chip boxes."""

    #: (member_keys, target, label) — a folder's "Send to <plot>" request; MainWindow routes it (2A).
    send_group_requested = Signal(object, str, str)
    #: (active, keys) — a scan drag started/ended; MainWindow arms the visible plot dock (R12).
    scan_drag_active = Signal(bool, object)
    #: (unit_key) — a chip's "Fit (sandbox preview)" request; MainWindow runs the sandbox (2B).
    fit_requested = Signal(str)

    def __init__(self, vm, parent=None):
        super().__init__("Scans", parent)
        self.setObjectName("scans_dock")
        self._vm = vm
        self._boxes: list[_GroupBox] = []           # all boxes across all built pages
        self._page_boxes: dict[str, list[_GroupBox]] = {}
        self._pages: dict[str, QWidget] = {}         # tab id -> the widget added to the QTabWidget
        self._page_layouts: dict[str, QVBoxLayout] = {}   # tab id -> layout to add boxes to (None for Home)
        self._page_content: dict[str, QWidget] = {}  # tab id -> the widget to parent transient children
        # Home (Manual) page is a Pool/Workbench splitter (R15); these hold its live layouts.
        self._pool_layout = None                     # the Pool pane's box layout
        self._work_layout = None                     # the Workbench pane's tray layout (scrolls)
        self._work_pinned_layout = None              # the Workbench pane's pinned top (the + tile)
        self._home_splitter = None                   # preserve operator sizing across live refreshes
        self._built: set[str] = set()                # names of pages whose chips are populated
        self._chips_by_key: dict[str, list[_ScanChip]] = {}
        self._active_key: str | None = None          # inspected unit; not the multi-selection
        self._pending_rename_gid: str | None = None  # a just-born folder to open inline (R4)
        self._new_box_tile = None                     # the Home "+ new box" tile (R11), when built
        # Ambient lens (R7): dimension + bin, session-transient (never persisted). Cleared when the
        # dimension no longer applies; dims chips outside the selected bin, in place.
        self._lens_dim: str | None = None
        self._lens_bin: str | None = None

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

        # Reset the lens when the DATA refreshes (workspace change / ingest tick) so freshly-arrived
        # scans are never silently dimmed by a stale bin (R7). Group/filter edits keep the lens.
        vm.refreshed.connect(self._reset_lens)
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
    def _visible_tabs(self) -> list[tuple[str, str, str | None]]:
        """(id, label, dim) for each tab that applies to this workspace."""
        tabs = []
        for tab_id, label, dim in _DIM_TABS:
            if tab_id == "Analyzer" and not self._vm.multi_detector:
                continue
            if tab_id == "B" and not self._vm.has_field():
                continue
            tabs.append((tab_id, label, dim))
        return tabs

    def _make_scroll_page(self, tab_id: str) -> tuple[QScrollArea, QVBoxLayout, QWidget]:
        """A plain single-scroll page (the preset lens tabs). Home uses :meth:`_make_home_page`."""
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(2, 2, 2, 2)
        layout.setSpacing(8)
        scroll.setWidget(content)
        return scroll, layout, content

    def _make_scroll(self) -> tuple[QScrollArea, QVBoxLayout, QWidget]:
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        lay = QVBoxLayout(content)
        lay.setContentsMargins(2, 2, 2, 2)
        lay.setSpacing(6)
        scroll.setWidget(content)
        return scroll, lay, content

    def _make_home_page(self) -> tuple[QWidget, QWidget]:
        """The Home page: a vertical splitter with the Pool (all scans) on top and the Workbench
        (pinned ＋ tile + a scroll of group trays) below, so the trays/＋ tile are ALWAYS visible and
        never buried under the pool (R15). Returns ``(page, content-parent)``."""
        host = QWidget(self)
        v = QVBoxLayout(host)
        v.setContentsMargins(2, 2, 2, 2)
        v.setSpacing(4)
        splitter = QSplitter(Qt.Orientation.Vertical, host)
        self._home_splitter = splitter
        splitter.setChildrenCollapsible(False)

        pool_host = QWidget(splitter)
        self._pool_layout = QVBoxLayout(pool_host)
        self._pool_layout.setContentsMargins(2, 2, 2, 2)
        self._pool_layout.setSpacing(4)

        work_host = QWidget()
        self._work_pinned_layout = QVBoxLayout(work_host)
        self._work_pinned_layout.setContentsMargins(0, 0, 0, 0)
        self._work_pinned_layout.setSpacing(4)
        work_scroll = QScrollArea(self)
        work_scroll.setWidgetResizable(True)
        work_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # The workbench scroll content accepts a folder-header drop to REORDER folders (R6).
        work_content = _HomePage()
        work_content.reorder_requested.connect(self._on_folder_reorder)
        self._work_layout = QVBoxLayout(work_content)
        self._work_layout.setContentsMargins(2, 2, 2, 2)
        self._work_layout.setSpacing(6)
        work_scroll.setWidget(work_content)
        self._work_pinned_layout.addWidget(work_scroll, 1)

        splitter.addWidget(pool_host)
        splitter.addWidget(work_host)
        splitter.setSizes([550, 450])
        v.addWidget(splitter, 1)
        return host, host

    # ---- rebuild ---------------------------------------------------------
    def _rebuild(self) -> None:
        master_view_state = self._capture_master_view_state()
        want = self._visible_tabs()
        prev = self._tabs.currentIndex()
        # Tabs are keyed by id (not the display label), so read the previous id from the page order.
        old_ids = list(self._pages)
        prev_name = old_ids[prev] if 0 <= prev < len(old_ids) else self._vm.active_tab()

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
        self._page_content = {}
        self._built = set()
        self._chips_by_key = {}
        self._pool_tree = None

        self._tab_dims: dict[str, str | None] = {}
        for tab_id, label, dim in want:
            if tab_id == _TAB_MANUAL:
                page, content = self._make_home_page()   # Pool/Workbench splitter (R15)
                self._tabs.addTab(page, label)
                self._pages[tab_id] = page
                self._page_content[tab_id] = content
                self._page_layouts[tab_id] = None        # Home uses _pool_layout/_work_layout instead
            else:
                scroll, layout, content = self._make_scroll_page(tab_id)
                self._tabs.addTab(scroll, label)
                self._pages[tab_id] = scroll
                self._page_layouts[tab_id] = layout
                self._page_content[tab_id] = content
            self._page_boxes[tab_id] = []
            self._tab_dims[tab_id] = dim
        self._tabs.blockSignals(False)

        # Restore the persisted / previously-active tab (by id) if it still exists.
        target = prev_name if prev_name in self._pages else self._vm.active_tab()
        idx = 0
        if target in self._pages:
            idx = list(self._pages).index(target)
        self._tabs.setCurrentIndex(idx)
        self._build_page(list(self._pages)[idx])
        self._restore_master_view_state(master_view_state)

    @staticmethod
    def _master_view_key(fit) -> str:
        """Stable pooled-group identity across revisions of the same ``(q, T, branch)`` master."""
        try:
            q = ",".join(f"{float(value):.12g}" for value in (getattr(fit, "q", None) or ()))
        except (TypeError, ValueError):
            q = repr(getattr(fit, "q", None))
        try:
            raw_temperature = getattr(fit, "T", None)
            temperature = "" if raw_temperature is None else f"{float(raw_temperature):.12g}"
        except (TypeError, ValueError):
            temperature = repr(getattr(fit, "T", None))
        return "|".join((str(getattr(fit, "sample", None) or ""), q, temperature,
                         str(getattr(fit, "branch_id", None) or ""),
                         str(getattr(fit, "detector_id", None) or "")))

    @staticmethod
    def _tree_item_view_key(item):
        unit_key = item.data(0, _UNIT_KEY_ROLE)
        if unit_key:
            return ("unit", str(unit_key))
        master_key = item.data(0, _MASTER_VIEW_KEY_ROLE)
        return ("master", str(master_key)) if master_key else None

    def _capture_master_view_state(self):
        """Snapshot operator-owned tree state before a live data refresh destroys the widgets."""
        tree = getattr(self, "_pool_tree", None)
        if tree is None:
            return None
        expanded = set()
        iterator = QTreeWidgetItemIterator(tree)
        while iterator.value():
            item = iterator.value()
            key = self._tree_item_view_key(item)
            if key is not None and item.isExpanded():
                expanded.add(key)
            iterator += 1
        current = self._tree_item_view_key(tree.currentItem()) if tree.currentItem() else None
        selected = {key for key in (self._tree_item_view_key(item)
                                    for item in tree.selectedItems()) if key is not None}
        splitter = getattr(self, "_home_splitter", None)
        return {
            "expanded": expanded,
            "current": current,
            "selected": selected,
            "vertical": tree.verticalScrollBar().value(),
            "horizontal": tree.horizontalScrollBar().value(),
            "columns": [tree.columnWidth(column) for column in range(tree.columnCount())],
            "splitter": splitter.sizes() if splitter is not None else None,
        }

    def _restore_master_view_state(self, state) -> None:
        """Restore a captured master-browser view without following newly arrived scans."""
        tree = getattr(self, "_pool_tree", None)
        if not state or tree is None:
            return
        by_key = {}
        iterator = QTreeWidgetItemIterator(tree)
        while iterator.value():
            item = iterator.value()
            key = self._tree_item_view_key(item)
            if key is not None:
                by_key[key] = item
                if item.childCount():
                    item.setExpanded(key in state["expanded"])
            iterator += 1
        tree.clearSelection()
        for key in state["selected"]:
            if key in by_key:
                by_key[key].setSelected(True)
        if state["current"] in by_key:
            tree.setCurrentItem(by_key[state["current"]])
        for column, width in enumerate(state["columns"]):
            if column < tree.columnCount():
                tree.setColumnWidth(column, width)
        splitter = getattr(self, "_home_splitter", None)
        if splitter is not None and state["splitter"]:
            splitter.setSizes(state["splitter"])

        # Apply once immediately as well: if two workspace notifications are coalesced before the
        # event loop runs, the second rebuild must capture the operator's position, not a transient 0.
        tree.verticalScrollBar().setValue(state["vertical"])
        tree.horizontalScrollBar().setValue(state["horizontal"])

        # Scroll ranges settle after the replacement tree has been laid out. Guard the callback so
        # a second rapid refresh cannot apply this generation's position to a newer tree.
        def restore_scrollbars():
            if getattr(self, "_pool_tree", None) is not tree:
                return
            tree.verticalScrollBar().setValue(state["vertical"])
            tree.horizontalScrollBar().setValue(state["horizontal"])

        # ``tree`` is the CONTEXT object, not decoration: without it Qt keeps the zero-timer armed
        # even after the tree is destroyed, and the closure then calls setValue() on scrollbars whose
        # C++ objects are gone — a hard access violation (PySide6 does not invalidate wrappers for
        # C++-owned children reaped by a parent's destructor, so there is no RuntimeError to catch).
        # A rebind (load_workspace) that lands before the event loop turns destroys this dock with
        # the timer still pending, which is exactly that case. With the context object Qt cancels the
        # callback when the tree dies, and the guard above still covers a live-tree replacement.
        QTimer.singleShot(0, tree, restore_scrollbars)

    def _build_page(self, name: str) -> None:
        if name in self._built or name not in self._page_content:
            return
        self._built.add(name)
        rows = self._vm.rows()
        by_key = {u.key: u for u in rows}

        if name == _TAB_MANUAL:
            self._build_home_page(rows)
            self._finish_build()
            return

        layout = self._page_layouts[name]
        dim = self._tab_dims.get(name)
        groups = self._vm.auto_groups(dim)
        for ag in groups:
            box = _GroupBox(ag.id, ag.label, self._vm, kind="auto")
            self._populate(name, box, [by_key[k] for k in ag.scan_keys if k in by_key])
            self._add_box(name, layout, box)
        if not groups and not self._vm.filter().is_active():
            empty = QLabel("(no groups on this dimension)", self._page_content[name])
            empty.setStyleSheet("color: #999; font-style: italic; padding: 6px;")
            layout.addWidget(empty)
        # A filter that narrows the whole view to nothing must say so — and offer a one-click way out
        # — instead of showing a blank pane (council pitfall: the empty-state trap).
        if self._vm.filter().is_active() and not rows:
            self._add_filter_empty(name, layout)
        layout.addStretch(1)
        self._finish_build()

    def _build_home_page(self, rows) -> None:
        """Populate the Home splitter: Pool (all scans) on top, Workbench (＋ tile + trays) below (R15)."""
        name = _TAB_MANUAL
        parent = self._page_content[name]
        # Master browser replaces the flat chip river. Physical children preserve the exact existing
        # drag payload and remain the drop-out target for detaching a tray member.
        self._add_lens_bar(name, self._pool_layout)
        self._add_section_label(self._pool_layout, "Pooled masters — expand for physical scans")
        self._pool_tree = _MasterTree(self._vm, parent)
        self._pool_tree.master_activated.connect(self._vm.request_master_inspect)
        self._pool_tree.unit_activated.connect(self._vm.request_inspect)
        self._pool_tree.fit_requested.connect(self.fit_requested)
        self._pool_tree.drag_session.connect(self._on_drag_session)
        self._populate_master_tree(self._pool_tree, rows)
        self._pool_layout.addWidget(self._pool_tree, 1)
        if self._vm.filter().is_active() and not rows:
            self._add_filter_empty(name, self._pool_layout)
        # Workbench pane — the ＋ new-box tile pinned at the top (always visible), trays scroll below.
        self._new_box_tile = _NewBoxTile(parent)
        self._new_box_tile.create_requested.connect(self._create_box)
        self._work_pinned_layout.insertWidget(0, self._new_box_tile)
        for g in self._vm.manual_groups():
            box = _GroupBox(g.id, g.name or "(unnamed)", self._vm, kind="manual")
            box.send_requested.connect(self.send_group_requested)   # "Send to" -> window (2A)
            self._populate(name, box, self._vm.units_in_group(g))
            self._add_box(name, self._work_layout, box)
            box.set_full_member_count(len(self._vm.member_keys(g.id)))
        self._work_layout.addStretch(1)

    def _populate_master_tree(self, tree: _MasterTree, rows) -> None:
        by_key = {row.key: row for row in rows}
        assigned: set[str] = set()
        groups = list(self._vm.master_groups())
        branch_ids = sorted({str(group.master.branch_id) for group, _members in groups
                             if group.master.branch_id})
        branch_number = {branch: index + 1 for index, branch in enumerate(branch_ids)}
        for group, members in sorted(
                groups,
                key=lambda pair: tuple(pair[0].master.q or (float("inf"),) * 3)):
            fit = group.master
            q = _q_compact(fit.q)
            branch = str(fit.branch_id or "")
            if str(fit.track_state or "").lower() == "confirmed" and branch:
                label = branch.replace("_", " ").title()
            else:
                label = f"Branch {branch_number.get(branch, 1)}"
            interval = fit.interval
            omega = "—" if fit.omega0 is None else f"{float(fit.omega0):.3g} meV"
            if interval and len(interval) >= 2:
                omega += f" [{float(interval[0]):.3g}, {float(interval[-1]):.3g}]"
            pool_consistent = getattr(fit, "pool_consistent", None)
            modality = getattr(fit, "modality", None)
            linewidth_state = getattr(fit, "linewidth_state", None)
            certifying = bool(fit.interval_reliable and interval
                              and pool_consistent is not False
                              and modality != "fork"
                              and fit.track_state not in {"ambiguous", "missing", "provisional", "blocked"})
            state = "certifying interval" if certifying else "uncertified"
            if pool_consistent is False:
                state += "; pool inconsistent"
            if modality == "fork":
                state += "; multimodal"
            if linewidth_state == "resolution_limited":
                state += "; Γ resolution-limited"
            if getattr(group, "stale_count", 0):
                state += f"; {group.stale_count} scans awaiting collation"
            if (fit.disposition or "").lower() not in ("", "resolved"):
                state = f"{state}; {fit.disposition}"
            top = QTreeWidgetItem([
                "Master", q, "" if fit.T is None else f"{float(fit.T):g} K", label, omega,
                str(fit.n_members or len(members)), state,
                (datetime.fromtimestamp(group.updated_at).strftime("%H:%M:%S")
                 if getattr(group, "updated_at", None) is not None else "—")])
            top.setData(0, _MASTER_FIT_ROLE, fit)
            top.setData(0, _MASTER_VIEW_KEY_ROLE, self._master_view_key(fit))
            revision = getattr(fit, "master_revision", None) or "unknown"
            top.setToolTip(6, (f"Master revision {revision}; {state}. "
                               + "; ".join(str(reason) for reason in (fit.reasons or ()))))
            top.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            tree.addTopLevelItem(top)
            for member in members:
                key = getattr(member, "unit_id", None)
                row = by_key.get(key)
                if row is None:
                    continue
                assigned.add(row.key)
                groups_text = ", ".join(self._vm.group_labels_for_key(row.key))
                child = QTreeWidgetItem([
                    f"scan {row.scan_number}" + (f" · {row.det}" if row.det else ""),
                    _q_compact(row.q), "" if row.T is None else f"{row.T:g} K",
                    str(getattr(member, "branch_id", None) or ""),
                    "" if member.omega0 is None else f"{float(member.omega0):.3g} meV",
                    groups_text, status_text(row.status), ""])
                child.setData(0, _UNIT_KEY_ROLE, row.key)
                child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                               Qt.ItemFlag.ItemIsDragEnabled)
                top.addChild(child)
        loose = [row for row in rows if row.key not in assigned]
        if loose:
            top = QTreeWidgetItem(["Unpooled scans", "", "", "", "", str(len(loose)), "", ""])
            top.setData(0, _MASTER_VIEW_KEY_ROLE, "unpooled")
            top.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            tree.addTopLevelItem(top)
            for row in loose:
                child = QTreeWidgetItem([
                    f"scan {row.scan_number}" + (f" · {row.det}" if row.det else ""),
                    _q_compact(row.q), "" if row.T is None else f"{row.T:g} K", "", "",
                    ", ".join(self._vm.group_labels_for_key(row.key)), status_text(row.status), ""])
                child.setData(0, _UNIT_KEY_ROLE, row.key)
                child.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable |
                               Qt.ItemFlag.ItemIsDragEnabled)
                top.addChild(child)
        tree.expandToDepth(0)
        for column in range(tree.columnCount()):
            tree.resizeColumnToContents(column)

    def _finish_build(self) -> None:
        self._sync_selection()
        self._sync_highlight()
        self._sync_active()
        self._sync_lens()

    def _add_filter_empty(self, name: str, layout) -> None:
        holder = QWidget(self._page_content[name])
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
        ids = list(self._pages)
        if not (0 <= index < len(ids)):
            return
        name = ids[index]               # the page id, not the display label
        self._build_page(name)          # lazy: populate on first show
        self._vm.set_active_tab(name)   # persist the active dimension (by id)
        self._sync_lens()               # lens applies on Home only; re-apply/undim on tab switch

    def _add_section_label(self, layout, text: str) -> None:
        lbl = QLabel(text)
        lbl.setStyleSheet("color: #888; font-size: 10px; font-weight: bold; padding-top: 4px;")
        layout.addWidget(lbl)

    def _add_box(self, name: str, layout, box: _GroupBox) -> None:
        box.finalize()
        box.fit_requested.connect(self.fit_requested)   # chip "Fit (sandbox preview)" -> window (2B)
        self._boxes.append(box)
        self._page_boxes[name].append(box)
        layout.addWidget(box)
        # A just-born folder (chip-onto-chip) opens straight into inline rename (R4).
        if self._pending_rename_gid is not None and box.box_id == self._pending_rename_gid:
            self._pending_rename_gid = None
            box.begin_inline_rename()

    def _populate(self, name: str, box: _GroupBox, units, pool: bool = False) -> None:
        multi = self._vm.multi_detector
        # Chips are drop targets (chip-onto-chip merge) only on the Home grid; preset tabs are
        # read-only. A chip's origin is the folder it is shown in ("" when loose/preset) so a
        # drag-out removes from that folder only (R2).
        droppable = name == _TAB_MANUAL
        origin = box.box_id if getattr(box, "_manual", False) else ""
        for u in units:
            # A Pool chip carries a membership badge (how many groups it's in); inside a box the
            # membership is implicit, so no badge there (R9).
            badge = self._vm.membership_count(u.key) if pool else 0
            chip = _ScanChip(u, self._vm, multi=multi, droppable=droppable,
                             origin_group_id=str(origin), badge=badge)
            chip.hovered.connect(self._on_chip_hovered)
            if droppable:
                chip.merge_requested.connect(self._on_chip_merge)
                chip.drag_session.connect(self._on_drag_session)   # drag-reveal broadcast (R12)
            box.add_chip(chip)
            self._chips_by_key.setdefault(u.key, []).append(chip)

    # ---- selection + highlight sync (a unit lights up in every box) ----
    def _sync_selection(self, *_args) -> None:
        selected = set(self._vm.selected_keys())
        tree = getattr(self, "_pool_tree", None)
        if tree is not None:
            tree.blockSignals(True)
            iterator = QTreeWidgetItemIterator(tree)
            while iterator.value():
                item = iterator.value()
                key = item.data(0, _UNIT_KEY_ROLE)
                item.setSelected(bool(key and str(key) in selected))
                iterator += 1
            tree.blockSignals(False)
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
        ids = list(self._pages)
        name = ids[idx] if 0 <= idx < len(ids) else None
        return self._page_boxes.get(name, [])

    def _set_all_collapsed(self, flag: bool) -> None:
        tree = getattr(self, "_pool_tree", None)
        if tree is not None:
            tree.collapseAll() if flag else tree.expandAll()
        for box in self._current_boxes():
            box._set_body_visible(not flag)
            self._vm.set_collapsed(box.box_id, flag)

    # ---- chip-onto-chip merge (birth / extend a folder) ------------------
    def _on_chip_merge(self, target_key: str, dragged_keys, origin_gid: str) -> None:
        """A chip was dropped onto a chip (after the dwell). Origin-foldered target -> add to that
        folder (copy); loose target -> birth a new folder and open it for inline rename (R3/R4)."""
        keys = list(dragged_keys or [])
        if origin_gid:
            self._vm.add_to_group(origin_gid, keys)
            return
        gid = self._vm.create_group_from_merge(target_key, keys)
        if gid is None:
            return
        # The create already fired groups_changed -> a rebuild WITHOUT the pending flag. Set it now,
        # move to the Home tab, and rebuild once more so _add_box opens the new folder inline.
        self._pending_rename_gid = gid
        if _TAB_MANUAL in self._pages:
            idx = list(self._pages).index(_TAB_MANUAL)
            self._tabs.blockSignals(True)
            self._tabs.setCurrentIndex(idx)
            self._tabs.blockSignals(False)
        self._rebuild()

    # ---- "+ new box" tile: create a group from a drop (or an empty one on click) (R11) ----
    def _create_box(self, keys) -> None:
        keys = [str(k) for k in (keys or [])]
        if keys:
            gid = self._vm.create_group_from(self._vm.suggest_group_name(keys), keys)
        else:
            gid = self._vm.create_group("Group")
        if gid is None:
            return
        self._pending_rename_gid = gid   # open the new tray inline for naming (R4 path)
        if _TAB_MANUAL in self._pages:
            idx = list(self._pages).index(_TAB_MANUAL)
            self._tabs.blockSignals(True)
            self._tabs.setCurrentIndex(idx)
            self._tabs.blockSignals(False)
        self._rebuild()

    # ---- drag-reveal: arm the editable trays + the new-box tile while a scan drag is in flight (R12) ----
    def _on_drag_session(self, active: bool, keys) -> None:
        for box in self._page_boxes.get(_TAB_MANUAL, []):
            if getattr(box, "_manual", False):
                box.set_armed(active)
        tile = getattr(self, "_new_box_tile", None)
        if tile is not None:
            tile.set_armed(active)
        self.scan_drag_active.emit(active, list(keys or []))   # MainWindow arms the visible plot dock

    # ---- folder reorder (drag a folder header into the gaps) -------------
    def _on_folder_reorder(self, gid: str, y: int) -> None:
        """Reorder manual folders by a header drag dropped at content-y ``y`` (plan R6). The new
        order is persisted via ``set_group_order``; a rebuild reflects it."""
        boxes = [b for b in self._page_boxes.get(_TAB_MANUAL, []) if getattr(b, "_manual", False)]
        order = [b.box_id for b in boxes]
        if gid not in order:
            return
        index = sum(1 for b in boxes if b.box_id != gid and b.geometry().center().y() < y)
        new = [x for x in order if x != gid]
        new.insert(min(index, len(new)), gid)
        if new != order:
            self._vm.set_group_order(new)
            self._rebuild()

    # ---- ambient lens (dimension + bin, dims non-matching in place) ------
    def _add_lens_bar(self, name: str, layout) -> None:
        bar = QWidget(self._page_content[name])
        h = QHBoxLayout(bar)
        h.setContentsMargins(2, 0, 2, 2)
        h.setSpacing(4)
        h.addWidget(QLabel("Lens:", bar))
        dim_combo = QComboBox(bar)
        dim_combo.addItem("(none)", None)
        for label, d in self._available_lens_dims():
            dim_combo.addItem(label, d)
        if self._lens_dim is not None:
            i = dim_combo.findData(self._lens_dim)
            if i >= 0:
                dim_combo.setCurrentIndex(i)
            else:
                self._lens_dim = self._lens_bin = None   # dimension no longer applies -> clear
        bin_combo = QComboBox(bar)
        self._fill_lens_bins(bin_combo)
        dim_combo.currentIndexChanged.connect(
            lambda _i: self._on_lens_dim_changed(dim_combo.currentData(), bin_combo))
        bin_combo.currentIndexChanged.connect(
            lambda _i: self._on_lens_bin_changed(bin_combo.currentData()))
        h.addWidget(dim_combo)
        h.addWidget(bin_combo)
        h.addStretch(1)
        layout.addWidget(bar)

    def _available_lens_dims(self) -> list[tuple[str, str]]:
        dims: list[tuple[str, str]] = []
        if self._vm.t_extent() != (None, None):
            dims.append(("T", "T"))
        if self._vm.q_options():
            dims.append(("q", "q"))
        if self._vm.multi_detector:
            dims.append(("Analyzer", "ana"))
        if self._vm.has_field():
            dims.append(("B", "B"))
        return dims

    def _fill_lens_bins(self, bin_combo: QComboBox) -> None:
        bin_combo.blockSignals(True)
        bin_combo.clear()
        bin_combo.addItem("(all)", None)
        sel = 0
        if self._lens_dim is not None:
            for i, (label, _keys) in enumerate(self._vm.lens_bins(self._lens_dim), start=1):
                bin_combo.addItem(label, label)
                if label == self._lens_bin:
                    sel = i
        bin_combo.setVisible(self._lens_dim is not None)
        bin_combo.setCurrentIndex(sel)
        bin_combo.blockSignals(False)

    def _on_lens_dim_changed(self, dim, bin_combo: QComboBox) -> None:
        self._lens_dim = dim
        self._lens_bin = None
        self._fill_lens_bins(bin_combo)
        self._sync_lens()

    def _on_lens_bin_changed(self, bin_label) -> None:
        self._lens_bin = bin_label
        self._sync_lens()

    def _reset_lens(self) -> None:
        """Clear the ambient lens on a data refresh so new scans are not silently dimmed (R7)."""
        self._lens_dim = None
        self._lens_bin = None

    def _sync_lens(self) -> None:
        """Dim every physical scan outside the active lens bin; nothing dims without a lens (R7).

        The Home hierarchy and any workbench chips reflect the same lens. Read-only preset tabs stay
        untouched — leaving Home undims everything, returning re-applies it."""
        idx = self._tabs.currentIndex()
        ids = list(self._pages)
        on_home = 0 <= idx < len(ids) and ids[idx] == _TAB_MANUAL
        match: set | None = None
        if on_home and self._lens_dim is not None and self._lens_bin is not None:
            for label, keys in self._vm.lens_bins(self._lens_dim):
                if label == self._lens_bin:
                    match = keys
                    break
        tree = getattr(self, "_pool_tree", None)
        if tree is not None:
            iterator = QTreeWidgetItemIterator(tree)
            while iterator.value():
                item = iterator.value()
                key = item.data(0, _UNIT_KEY_ROLE)
                dim = bool(key and match is not None and str(key) not in match)
                item.setData(0, _DIMMED_ROLE, dim)
                brush = QBrush(QColor("#b0b0b0")) if dim else QBrush()
                for column in range(tree.columnCount()):
                    item.setForeground(column, brush)
                iterator += 1
        for key, chips in self._chips_by_key.items():
            dim = match is not None and key not in match
            for chip in chips:
                chip.set_dimmed(bool(dim))
