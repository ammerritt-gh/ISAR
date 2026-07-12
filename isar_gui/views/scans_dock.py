"""ScansDock — scans as physical, draggable chips inside collapsible group boxes (Phase E).

The chip is the fundamental visible unit: a small card showing a scan's number + ``T | q``, coloured
at its left edge by ledger status. Chips are draggable (``MIME_SCAN_IDS``) so future targets accept
them unchanged, and a chip lights up in EVERY box it appears in when its scan is selected — the same
scan is one physical thing shown in many containers (Ungrouped, auto T/q groups, manual groups).

Layout (top to bottom):
  * an **Ungrouped** box — scans in no manual group. Together with the manual boxes this guarantees
    every scan is visible somewhere: a scan is either in a manual group (shown there) or ungrouped
    (shown here). Auto-groups are an additional cross-cut, not a coverage guarantee, so a dedicated
    "All scans" box is deliberately omitted — Ungrouped + manual already cover every scan.
  * the **auto-groups** section — T-bins then q-bins, derived from metadata, drops rejected;
  * the **manual groups** — persisted in the GuiStore, drops mutate membership, drag/rename/delete.

All derivation is in :class:`~isar_gui.viewmodels.scans.ScansViewModel`; this is presentation +
interaction only. Rebuild-on-change is acceptable at hundreds of chips (v1); highlight uses the one
shared model timer, never per-chip timers.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QMimeData, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QDrag, QPainter, QPixmap
from PySide6.QtWidgets import (
    QDockWidget,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
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
from isar_gui.viewmodels.scans import status_color

log = logging.getLogger(__name__)

_DRAG_THRESHOLD = 12


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
        self._widgets.append(widget)
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
# Draggable scan chip
# ---------------------------------------------------------------------------
class _ScanChip(QFrame):
    """A single physical scan as a draggable chip. Selection is external (the shared selection model);
    the chip only paints it. Drag payload is the current multi-selection when this chip is selected,
    else just this chip."""

    clicked = Signal(str, object)         # (scan_key, Qt.KeyboardModifiers)
    context_menu = Signal(str, object)    # (scan_key, global QPoint)

    def __init__(self, ps, vm, parent=None):
        super().__init__(parent)
        self._key = ps.key
        self._vm = vm
        self._status = ps.status
        self._selected = False
        self._highlight = False
        self._drag_start: QPoint | None = None
        self.setObjectName("scanChip")
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.setFixedSize(QSize(94, 46))
        num = ps.scan_number if ps.scan_number is not None else "?"
        tparts = []
        if ps.T is not None:
            tparts.append(f"{ps.T:g}K")
        if ps.hkl:
            try:
                tparts.append("[" + " ".join(f"{float(v):.2g}" for v in ps.hkl) + "]")
            except (TypeError, ValueError):
                pass
        subtitle = " | ".join(tparts) if tparts else "—"

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 6, 4)
        layout.setSpacing(1)
        self._num_label = QLabel(str(num), self)
        self._sub_label = QLabel(subtitle, self)
        self._sub_label.setStyleSheet("color: #888; font-size: 9px; background: transparent;")
        layout.addWidget(self._num_label)
        layout.addWidget(self._sub_label)

        det_txt = ", ".join(str(d) for d in ps.detectors) if ps.detectors else ""
        self.setToolTip(f"{ps.key}\n{ps.file_name}"
                        + (f"\ndetectors: {det_txt}" if det_txt else ""))
        self._apply_style()

    @property
    def key(self) -> str:
        return self._key

    def set_selected(self, flag: bool) -> None:
        if self._selected != flag:
            self._selected = flag
            self._apply_style()

    def set_highlight(self, flag: bool) -> None:
        if self._highlight != flag:
            self._highlight = flag
            self._apply_style()

    def _apply_style(self) -> None:
        edge = status_color(self._status)
        strike = "text-decoration: line-through;" if self._status == "excluded" else ""
        weight = "bold"
        self._num_label.setStyleSheet(
            f"font-weight: {weight}; font-size: 12px; background: transparent; {strike}")
        if self._highlight:
            bg = "#fff3b0"
        elif self._selected:
            bg = "rgba(91, 155, 213, 60)"
        else:
            bg = "palette(base)"
        border = "#5b9bd5" if self._selected else "palette(mid)"
        self.setStyleSheet(
            f"#scanChip {{ background: {bg}; border: 1px solid {border};"
            f" border-left: 4px solid {edge}; border-radius: 6px; }}")

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
    """One collapsible container. ``manual`` boxes accept scan drops (mutate the store) and offer
    rename/delete + a group-drag header; ``auto`` boxes are muted, reject drops, but still drag as a
    unit. The Ungrouped box is neither (no drops, no drag)."""

    def __init__(self, box_id, title, vm, *, kind, parent=None):
        super().__init__(parent)
        self._id = box_id
        self._vm = vm
        self._kind = kind             # "ungrouped" | "auto" | "manual"
        self._manual = kind == "manual"
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
        muted = "color: #999;" if kind == "auto" else ""
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

    def _on_chip_context(self, key: str, global_pos) -> None:
        menu = QMenu(self)
        remove_act = menu.addAction("Remove from this group") if self._manual else None
        select_act = menu.addAction("Select in table")
        copy_act = menu.addAction("Copy scan id")
        chosen = menu.exec(global_pos)
        if chosen is None:
            return
        if chosen is remove_act:
            keys = set(self._vm.selected_keys())
            targets = keys if key in keys else {key}
            self._vm.remove_from_group(self._id, targets)
        elif chosen is select_act:
            self._vm.select_keys([key], clear=True)
        elif chosen is copy_act:
            from PySide6.QtWidgets import QApplication
            QApplication.clipboard().setText(key)

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
        elif self._kind == "auto":
            self.setStyleSheet("#groupBox { border: 1px dashed palette(mid); border-radius: 6px; }")
        else:
            self.setStyleSheet("#groupBox { border: 1px solid palette(mid); border-radius: 6px; }")

    # ---- header drag source (group drags as a unit; auto + manual only) ----
    def mousePressEvent(self, event):  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._kind in ("auto", "manual"):
            self._drag_start = event.pos()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):  # noqa: N802
        if (self._drag_start is None or not (event.buttons() & Qt.MouseButton.LeftButton)
                or self._kind not in ("auto", "manual")):
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
        self._drag_start = None
        super().mouseReleaseEvent(event)


# ---------------------------------------------------------------------------
# Dock
# ---------------------------------------------------------------------------
class ScansDock(QDockWidget):
    """The physical scan view: a scroll of collapsible group boxes full of draggable chips."""

    def __init__(self, vm, parent=None):
        super().__init__("Scans", parent)
        self.setObjectName("scans_dock")
        self._vm = vm
        self._boxes: list[_GroupBox] = []
        self._chips_by_key: dict[str, list[_ScanChip]] = {}

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
        toolbar.addWidget(new_btn)
        toolbar.addStretch(1)
        toolbar.addWidget(expand_btn)
        toolbar.addWidget(collapse_btn)
        root.addLayout(toolbar)

        self._scroll = QScrollArea(body)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(2, 2, 2, 2)
        self._content_layout.setSpacing(8)
        self._scroll.setWidget(self._content)
        root.addWidget(self._scroll, 1)

        self.setWidget(body)

        vm.refreshed.connect(self._rebuild)
        vm.groups_changed.connect(self._rebuild)
        vm.model.highlight_changed.connect(self._sync_highlight)
        vm.selection_model.selectionChanged.connect(self._sync_selection)
        self._rebuild()

    # ---- rebuild ----
    def _rebuild(self) -> None:
        while self._content_layout.count():
            item = self._content_layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._boxes = []
        self._chips_by_key = {}

        rows = self._vm.rows()
        by_key = {ps.key: ps for ps in rows}

        # Ungrouped: scans in no manual group (the visibility guarantee, with the manual boxes).
        manual = self._vm.manual_groups()
        grouped_keys = {m for g in manual for m in g.members}
        ungrouped = _GroupBox("ungrouped", "Ungrouped", self._vm, kind="ungrouped")
        self._populate(ungrouped, [ps for ps in rows if ps.key not in grouped_keys])
        self._add_box(ungrouped)

        autos = self._vm.auto_groups()
        if autos:
            self._add_section_label("Auto-groups")
            for ag in autos:
                box = _GroupBox(ag.id, ag.label, self._vm, kind="auto")
                self._populate(box, [by_key[k] for k in ag.scan_keys if k in by_key])
                self._add_box(box)

        if manual:
            self._add_section_label("Manual groups")
            for g in manual:
                box = _GroupBox(g.id, g.name or "(unnamed)", self._vm, kind="manual")
                self._populate(box, [by_key[k] for k in g.members if k in by_key])
                self._add_box(box)

        self._content_layout.addStretch(1)
        self._sync_selection()
        self._sync_highlight()

    def _add_section_label(self, text: str) -> None:
        lbl = QLabel(text, self._content)
        lbl.setStyleSheet("color: #888; font-size: 10px; font-weight: bold; padding-top: 4px;")
        self._content_layout.addWidget(lbl)

    def _add_box(self, box: _GroupBox) -> None:
        box.finalize()
        self._boxes.append(box)
        self._content_layout.addWidget(box)

    def _populate(self, box: _GroupBox, scans) -> None:
        for ps in scans:
            chip = _ScanChip(ps, self._vm)
            box.add_chip(chip)
            self._chips_by_key.setdefault(ps.key, []).append(chip)

    # ---- selection + highlight sync (a scan lights up in every box) ----
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

    # ---- toolbar ----
    def _new_group(self) -> None:
        name, ok = QInputDialog.getText(self, "New group", "Group name:")
        if ok and name.strip():
            self._vm.create_group(name.strip())

    def _set_all_collapsed(self, flag: bool) -> None:
        for box in self._boxes:
            box._set_body_visible(not flag)
            self._vm.set_collapsed(box.box_id, flag)
