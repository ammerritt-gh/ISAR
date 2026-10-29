"""ScanListPanel -- the scan list, the main thing in the window's left column.

A ``QTreeView`` over a ``QStandardItemModel`` that mirrors the view model's plain tree of
:class:`~isar_gui.viewmodels.scan_list.Node`. A refresh *reconciles* the items to the nodes by node
id (remove what is gone, insert what is new, set text, tooltip and icon only where they changed) and
never clears the model, which is what keeps expansion, selection and scroll through a background
update. The panel acts only through the view model and accepts no drops: a drop on it reaches the
main window by Qt's propagation, which is the one drop route.
"""
from __future__ import annotations

from contextlib import contextmanager

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, QModelIndex, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMenu, QPushButton, QSizePolicy, QToolButton, QTreeView, QVBoxLayout,
                               QStyle, QStyledItemDelegate, QStyleOption, QStyleOptionViewItem,
                               QToolTip, QWidget)

from isar_gui.selection import ORIGIN_LIST, ORIGIN_REFRESH, ORIGIN_STEP
from isar_gui.theme import TONE_COLOR, Tone, dot_icon
from isar_gui.viewmodels.scan_list import (COLUMNS, OPTIONAL_COLUMNS, SORTS, TITLES,
                                           ScanListViewModel)

_ID_ROLE = Qt.ItemDataRole.UserRole           # the node id, on a row's first item
_KEY_ROLE = Qt.ItemDataRole.UserRole + 1      # the selectable identity, or None
_KIND_ROLE = Qt.ItemDataRole.UserRole + 2
_TONE_ROLE = Qt.ItemDataRole.UserRole + 3     # what the item's dot was drawn for
_STALE_ROLE = Qt.ItemDataRole.UserRole + 4    # whether the item's word was drawn as out of date
_REMOVED_ROLE = Qt.ItemDataRole.UserRole + 5  # whether the item's row was drawn as removed (greyed)
_SORT_LABELS = {"set": "By scan set", "temperature": "By temperature", "analyzer": "By analyzer"}
_HINT = "Select an analyzer row, then double-click it or press Open inspector."
_HINT_NONE = "Select an analyzer row, or a scan-set row for all its analyzers, to open the inspector."
_NEW_CONTAINER_KINDS = ("set", "analyzer")
REMOVE_TEXT, RESTORE_TEXT, READ_TEXT = "Remove from list", "Restore to list", "Read source file again"
REMOVE_TIP = "Hide the selected scans from this list, the Scans page and the selection. Their data and fits are kept."
RESTORE_TIP = "Bring the selected removed scans back into this list."
NO_FAST_TIP = "Select spectra that have a fast-pass result."

GUTTER = 24


class _ScanTree(QTreeView):
    """Qt owns focus/navigation; Selection owns every selected identity."""
    def __init__(self, panel):
        super().__init__(panel)
        self._panel = panel
        self.hover = None
        self.setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, False)

    def selectionCommand(self, index, event=None):
        return QItemSelectionModel.SelectionFlag.NoUpdate

    def visible_ids(self):
        index = self.model().index(0, 0)
        out = []
        while index.isValid():
            out.append(index.data(_ID_ROLE))
            index = self.indexBelow(index)
        return tuple(out)

    def _hit(self, point):
        index = self.indexAt(point)
        if not index.isValid():
            return QModelIndex(), None
        first = index.siblingAtColumn(0)
        gutter = self.visualRect(first)
        gutter.setWidth(GUTTER)
        zone = "open" if self.model().hasChildren(first) and gutter.contains(point) else "select"
        return first, zone

    def _click(self, index, modifiers):
        self._panel._vm.click(index.data(_ID_ROLE) if index.isValid() else None,
                             ctrl=bool(modifiers & Qt.KeyboardModifier.ControlModifier),
                             shift=bool(modifiers & Qt.KeyboardModifier.ShiftModifier),
                             visible=self.visible_ids())

    def mousePressEvent(self, event):
        index, zone = self._hit(event.position().toPoint())
        super().mousePressEvent(event)
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if zone == "open":
            self.setExpanded(index, not self.isExpanded(index))
        else:
            self._click(index, event.modifiers())

    def mouseDoubleClickEvent(self, event):
        index, zone = self._hit(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and zone == "open":
            self.setExpanded(index, not self.isExpanded(index))
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        super().keyPressEvent(event)
        key, modifiers = event.key(), event.modifiers()
        index = self.currentIndex().siblingAtColumn(0)
        if not index.isValid():
            return
        if key == Qt.Key.Key_Space:
            self._click(index, modifiers)
        elif key in (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_Home, Qt.Key.Key_End,
                     Qt.Key.Key_PageUp, Qt.Key.Key_PageDown):
            shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
            ctrl = bool(modifiers & Qt.KeyboardModifier.ControlModifier)
            if shift or (not ctrl and index.data(_KEY_ROLE)):
                self._click(index, modifiers)

    def selectAll(self):
        self._panel._vm.select_all_visible(self.visible_ids())

    def mouseMoveEvent(self, event):
        index, zone = self._hit(event.position().toPoint())
        hover = (index.data(_ID_ROLE), zone) if index.isValid() else None
        if hover != self.hover:
            self.hover = hover
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.hover = None
        self.viewport().update()
        super().leaveEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.Type.Leave:
            self.hover = None
            self.viewport().update()
        elif event.type() == QEvent.Type.ToolTip:
            index, zone = self._hit(event.pos())
            if zone == "open":
                noun = "analyzer group" if index.data(_KIND_ROLE) == "analyzer" else "scan set"
                QToolTip.showText(event.globalPos(), f"This edge opens or closes the {noun}. "
                                 "It does not change the selection.", self)
                return True
        return super().viewportEvent(event)

    def drawRow(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        option.state &= ~QStyle.StateFlag.State_MouseOver
        if self.hover and index.data(_ID_ROLE) == self.hover[0]:
            rect = QRect(option.rect)
            if self.hover[1] == "open":
                rect = self.visualRect(index.siblingAtColumn(0))
                rect.setWidth(GUTTER)
            color = option.palette.highlight().color()
            color.setAlpha(36)
            painter.fillRect(rect, color)
        super().drawRow(painter, option, index)


class _ContainerDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        if index.column() != 0 or not index.model().hasChildren(index):
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        view = self.parent()
        style = view.style()
        # Let the item style paint the full selected background before drawing the gutter.
        label, separator, suffix = opt.text.rpartition(" (")
        opt.rect.adjust(GUTTER, 0, 0, 0)
        if separator:
            suffix = " (" + suffix
            room = max(0, opt.rect.width() - opt.fontMetrics.horizontalAdvance(suffix) - 6)
            opt.text = opt.fontMetrics.elidedText(label, Qt.TextElideMode.ElideMiddle, room) + suffix
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, view)
        branch = QStyleOption()
        branch.rect = QRect(option.rect.left() + 4, option.rect.center().y() - 7, GUTTER - 8, 14)
        branch.palette = option.palette
        branch.state = QStyle.StateFlag.State_Enabled | QStyle.StateFlag.State_Children
        if view.isExpanded(index):
            branch.state |= QStyle.StateFlag.State_Open
        style.drawPrimitive(QStyle.PrimitiveElement.PE_IndicatorBranch, branch, painter, view)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        if index.column() == 0 and index.model().hasChildren(index):
            size.setWidth(size.width() + GUTTER)
        return size


class ScanListPanel(QWidget):           # acts through the VM; asks the window only to read a source file again
    read_again_requested = Signal(tuple)  # tuple[str, ...]: the selected spectrum keys to read from their files

    def __init__(self, vm: ScanListViewModel, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("scan_list")
        self.setAcceptDrops(False)
        self._vm = vm
        self._busy = 0                        # >0 while the panel itself changes the view
        self._expanded: set[str] = set()      # node ids the user has open
        self._seen: set[str] = set()          # container ids already given their first expansion
        self._by_id: dict[str, QStandardItem] = {}
        self._by_key: dict[str, QStandardItem] = {}
        self._layout_key = None               # the user's layout choices: a change reveals the selection

        self._build_widgets()
        vm.snapshot_changed.connect(self._refresh)
        vm.selection.changed.connect(self._on_selection_changed)
        self._refresh()

    # ---- construction --------------------------------------------------------------------
    def _build_widgets(self) -> None:
        self._filter = QLineEdit(self)
        self._filter.setObjectName("scan_list_filter")
        self._filter.setPlaceholderText("filter: a06, 97K, not run")
        self._filter.setClearButtonEnabled(True)
        self._filter.setMinimumWidth(20)
        self._filter.setAccessibleName("Filter scans")
        self._filter.setToolTip("Comma-separated terms, all of which must match: an analyzer, a "
                                "temperature such as 97K, a scan set, a file name or a status word.")
        self._filter.textChanged.connect(self._vm.set_filter)

        self._sort_actions: dict[str, QAction] = {}
        group = QActionGroup(self)
        sort_menu = QMenu(self)
        for sort in SORTS:
            action = sort_menu.addAction(_SORT_LABELS[sort])
            action.setCheckable(True)
            action.setActionGroup(group)
            action.setChecked(sort == self._vm.sort)
            action.triggered.connect(lambda _checked=False, s=sort: self._vm.set_sort(s))
            self._sort_actions[sort] = action
        self._sort_button = self._menu_button("Sort ▾", sort_menu, "Sort the scan list")
        self._sort_button.setToolTip("Sort the list by scan set, temperature or analyzer.")

        self._column_actions: dict[str, QAction] = {}
        column_menu = QMenu(self)
        column_menu.setToolTipsVisible(True)
        for column in OPTIONAL_COLUMNS:
            action = column_menu.addAction(TITLES[column])
            action.setCheckable(True)
            action.setToolTip(self._vm.header_tip(column))
            action.toggled.connect(lambda on, c=column: self._on_column_toggled(c, on))
            self._column_actions[column] = action
        self._columns_button = self._menu_button("Columns ▾", column_menu, "Choose the columns")
        self._columns_button.setToolTip("Choose which columns the list shows.")

        self._removed_button = QPushButton("Show removed", self)
        self._removed_button.setObjectName("scan_list_show_removed")
        self._removed_button.setCheckable(True)
        self._removed_button.setChecked(self._vm.show_removed)
        self._removed_button.setToolTip("List the scans you removed from this list too, greyed. "
                                        "Select them and choose Restore to list to bring them back.")
        self._removed_button.clicked.connect(self._vm.set_show_removed)
        self._row_menu = QMenu(self)          # the right-click menu on a scan row, rebuilt each time it opens
        self._row_menu.setToolTipsVisible(True)

        self._model = QStandardItemModel(0, len(COLUMNS), self)
        self._view = _ScanTree(self)
        self._view.setObjectName("scan_list_tree")
        self._view.setModel(self._model)
        self._view.setAccessibleName("Scan list")
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._view.setUniformRowHeights(True)
        self._view.setIndentation(GUTTER)
        self._view.setRootIsDecorated(False)
        self._view.setExpandsOnDoubleClick(False)
        self._view.setItemDelegateForColumn(0, _ContainerDelegate(self._view))
        self._view.setAcceptDrops(False)
        self._view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._view.customContextMenuRequested.connect(self._on_context_menu)
        header = self._view.header()
        header.setStretchLastSection(False)
        header.setFirstSectionMovable(False)
        header.setMinimumSectionSize(36)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._view.expanded.connect(lambda idx: self._on_toggled(idx, True))
        self._view.collapsed.connect(lambda idx: self._on_toggled(idx, False))
        self._view.activated.connect(self._on_activated)

        self._empty = QLabel("No scans yet. Drop data files on the box above, or use Add data.", self)
        self._empty.setObjectName("scan_list_empty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setWordWrap(True)

        self._notice = QLabel(self)
        self._notice.setObjectName("scan_list_notice")
        self._notice.setWordWrap(True)
        self._notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self._status = QLabel(self)           # "9 spectra", or "2 of 9 spectra · Clear filter" (a link)
        self._status.setObjectName("scan_list_status")
        self._status.setWordWrap(True)
        self._status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._status.linkActivated.connect(lambda _link: self._filter.setText(""))

        self._inspect = QPushButton("Open inspector", self)
        self._inspect.setObjectName("scan_list_inspect")
        self._inspect.setToolTip("Show the facts, readiness and fit results of the active selected spectrum.")
        self._inspect.clicked.connect(self._on_inspect_clicked)
        self._hint = QLabel(_HINT, self)
        self._hint.setObjectName("scan_list_hint")
        self._hint.setWordWrap(True)
        self._hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        top = QHBoxLayout()
        top.setSpacing(2)
        top.addWidget(self._filter, 1)
        top.addWidget(self._sort_button)
        top.addWidget(self._columns_button)
        top.addWidget(self._removed_button)
        foot = QHBoxLayout()
        foot.addWidget(self._inspect)
        foot.addWidget(self._hint, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)
        layout.addLayout(top)
        layout.addWidget(self._notice)
        layout.addWidget(self._view, 1)
        layout.addWidget(self._empty, 1)
        layout.addWidget(self._status)
        layout.addLayout(foot)

    def _menu_button(self, text: str, menu: QMenu, name: str) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setMenu(menu)
        button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        button.setAccessibleName(name)
        button.setStyleSheet("QToolButton::menu-indicator { image: none; }")   # the label carries the arrow
        return button

    # ---- the user acts: everything goes through the view model ---------------------------------
    def _on_column_toggled(self, column: str, on: bool) -> None:
        if not self._busy:
            self._vm.set_column_visible(column, on)

    def _on_activated(self, index: QModelIndex) -> None:
        key = index.siblingAtColumn(0).data(_KEY_ROLE)
        if key:
            self._vm.inspect(key)

    def _on_inspect_clicked(self) -> None:
        key = self._vm.selection.active
        if key:
            self._vm.inspect(key)

    def _on_context_menu(self, point) -> None:
        """Right-click on a row: select it if it is not already selected (as a click would), then offer the
        actions that apply to the selected scans. Menu actions act on the selection, never on the row alone."""
        vm = self._vm
        index = self._view.indexAt(point).siblingAtColumn(0)
        if index.isValid():
            node_id, key = index.data(_ID_ROLE), index.data(_KEY_ROLE)
            members = (key,) if key else vm.container_keys(node_id)
            if not members or not set(members) <= set(vm.selection.keys):
                vm.click(node_id)
        keys = vm.selected_spectra()
        menu = self._row_menu
        menu.clear()
        if any(not vm.is_removed(k) for k in keys):
            self._add_action(menu, REMOVE_TEXT, vm.remove_selected, REMOVE_TIP)
        if any(vm.is_removed(k) for k in keys):
            self._add_action(menu, RESTORE_TEXT, vm.restore_selected, RESTORE_TIP)
        has_fast = bool(vm.selected_fast_keys())
        if any(not vm.is_anchor(k) for k in keys):
            self._anchor_action(menu, "Set as anchor", True, has_fast)
        if any(vm.is_anchor(k) for k in keys):
            self._anchor_action(menu, "Remove anchor", False, has_fast)
        read = self._add_action(menu, READ_TEXT, lambda: self.read_again_requested.emit(keys))
        read.setEnabled(bool(keys))
        read.setToolTip("Read the selected scans' files again, as a drop does. Hidden scans stay hidden."
                        if keys else "Select one or more scans first.")
        menu.popup(self._view.viewport().mapToGlobal(point))

    def _anchor_action(self, menu: QMenu, text: str, anchored: bool, has_fast: bool) -> None:
        """Set or remove the anchor on the selected spectra; with none holding a fast-pass result it is disabled, and
        the tooltip says why."""
        if not has_fast:
            tip = NO_FAST_TIP
        elif anchored:
            tip = ("Mark the selected spectra as anchors. An anchored batch grows outward from them, "
                   "and they are never refitted.")
        else:
            tip = "Take the selected spectra off the anchors list. Their fits are not changed."
        action = self._add_action(menu, text, lambda: self._vm.set_anchor_selected(anchored), tip)
        action.setEnabled(has_fast)

    @staticmethod
    def _add_action(menu: QMenu, text: str, slot, tip: str = "") -> QAction:
        action = menu.addAction(text)
        action.setToolTip(tip)
        action.triggered.connect(lambda _checked=False: slot())
        return action

    def index_for_key(self, key: str) -> QModelIndex:
        item = self._by_key.get(key)
        return self._model.indexFromItem(item) if item is not None else QModelIndex()

    def _on_toggled(self, index: QModelIndex, opened: bool) -> None:
        node_id = index.siblingAtColumn(0).data(_ID_ROLE)
        if self._busy or node_id is None:
            return
        (self._expanded.add if opened else self._expanded.discard)(node_id)

    def _on_selection_changed(self, change) -> None:
        self._refresh_marks()
        self._restore_selection(reveal=change.origin not in (ORIGIN_LIST, ORIGIN_REFRESH, ORIGIN_STEP))
        self._update_footer()

    def _refresh_marks(self) -> None:
        for node_id, item in self._by_id.items():
            if node_id.startswith(("set:", "ana:")) or node_id == "section:pooled":
                text, name, description, tip = self._vm.container_presentation(node_id)
                item.setText(text)
                item.setToolTip(tip)
                item.setData(name, Qt.ItemDataRole.AccessibleTextRole)
                item.setData(description, Qt.ItemDataRole.AccessibleDescriptionRole)

    # ---- refresh: reconcile in place ------------------------------------------------------------
    @contextmanager
    def _changing(self):
        self._busy += 1
        try:
            yield
        finally:
            self._busy -= 1

    def _refresh(self) -> None:
        vm, view = self._vm, self._view
        snap = vm.snapshot
        has_data = bool(snap.sets or snap.files_not_read or snap.pooled)
        self._empty.setVisible(not has_data)
        view.setVisible(has_data)
        layout_key = vm.layout_choices()
        reveal, self._layout_key = layout_key != self._layout_key, layout_key
        if not snap.sets:                        # an empty list: the next first set opens again
            self._seen.clear()
            self._expanded.clear()
        with self._changing():
            anchor = self._scroll_anchor()
            self._sync_controls()
            self._reconcile(self._model.invisibleRootItem(), vm.tree())
            self._index_items()
            self._apply_columns()
            self._apply_expansion()
            self._apply_spans()
            if anchor is not None and anchor in self._by_id:
                view.scrollTo(self._model.indexFromItem(self._by_id[anchor]),
                              QAbstractItemView.ScrollHint.PositionAtTop)
        self._restore_selection(reveal=reveal)
        self._update_footer()

    def _sync_controls(self) -> None:
        """Make the menus and the filter box show the view model's state (a loaded view state)."""
        vm = self._vm
        self._sort_actions[vm.sort].setChecked(True)
        for column, action in self._column_actions.items():
            action.setChecked(column in vm.visible_columns())
        self._removed_button.setChecked(vm.show_removed)
        if self._filter.text() != vm.filter_text:
            self._filter.setText(vm.filter_text)
        self._model.setHorizontalHeaderLabels([vm.column_title(c) for c in COLUMNS])
        for i, column in enumerate(COLUMNS):
            self._model.setHeaderData(i, Qt.Orientation.Horizontal, vm.header_tip(column),
                                      Qt.ItemDataRole.ToolTipRole)

    def _scroll_anchor(self) -> str | None:
        if self._view.verticalScrollBar().value() == 0:
            return None                          # at the top, new rows above are meant to be seen
        top = self._view.indexAt(QPoint(0, 0))
        return top.siblingAtColumn(0).data(_ID_ROLE) if top.isValid() else None

    def _reconcile(self, parent: QStandardItem, nodes) -> None:
        wanted = {n.id for n in nodes}
        for row in range(parent.rowCount() - 1, -1, -1):
            if parent.child(row, 0).data(_ID_ROLE) not in wanted:
                parent.removeRow(row)
        for i, node in enumerate(nodes):
            have = next((r for r in range(parent.rowCount())
                         if parent.child(r, 0).data(_ID_ROLE) == node.id), None)
            if have is None:
                parent.insertRow(i, [self._new_item() for _ in COLUMNS])
            elif have != i:                      # a reorder (the temperature sort): move, keep children
                parent.insertRow(i, parent.takeRow(have))
            items = [parent.child(i, c) for c in range(len(COLUMNS))]
            self._update_items(items, node)
            self._reconcile(items[0], node.children)

    @staticmethod
    def _new_item() -> QStandardItem:
        item = QStandardItem()
        item.setEditable(False)
        return item

    @staticmethod
    def _update_items(items, node) -> None:
        first = items[0]
        if first.data(_ID_ROLE) != node.id:
            first.setData(node.id, _ID_ROLE)
        if first.data(_KEY_ROLE) != node.key:
            first.setData(node.key, _KEY_ROLE)
        if first.data(_KIND_ROLE) != node.kind:
            first.setData(node.kind, _KIND_ROLE)
        first.setData(node.accessible_name or None, Qt.ItemDataRole.AccessibleTextRole)
        first.setData(node.accessible_description or None, Qt.ItemDataRole.AccessibleDescriptionRole)
        for column, item in zip(COLUMNS, items):
            cell = node.cells.get(column)
            text = cell.text if cell else ""
            tip = (cell.tooltip or cell.text) if cell else ""
            tone = cell.tone if cell else None
            stale = bool(cell and cell.stale)
            if item.text() != text:
                item.setText(text)
            if item.toolTip() != tip:
                item.setToolTip(tip)
            if item.data(_TONE_ROLE) != tone:
                item.setData(tone, _TONE_ROLE)
                item.setIcon(dot_icon(tone) if tone is not None else QIcon())
            if bool(item.data(_STALE_ROLE)) != stale or bool(item.data(_REMOVED_ROLE)) != node.removed:
                item.setData(stale, _STALE_ROLE)           # the out-of-date mark: the word in the warning colour
                item.setData(node.removed, _REMOVED_ROLE)  # a removed scan: its row greyed
                color = TONE_COLOR[Tone.WARN] if stale else TONE_COLOR[Tone.IDLE] if node.removed else None
                item.setData(QColor(color) if color else None, Qt.ItemDataRole.ForegroundRole)

    def _index_items(self) -> None:
        self._by_id.clear()
        self._by_key.clear()

        def walk(parent: QStandardItem) -> None:
            for row in range(parent.rowCount()):
                item = parent.child(row, 0)
                self._by_id[item.data(_ID_ROLE)] = item
                if item.data(_KEY_ROLE):
                    self._by_key[item.data(_KEY_ROLE)] = item
                walk(item)
        walk(self._model.invisibleRootItem())

    def _apply_columns(self) -> None:
        shown = self._vm.visible_columns()
        for i, column in enumerate(COLUMNS):
            self._view.setColumnHidden(i, column not in shown)

    def _apply_expansion(self) -> None:
        """Re-apply the user's open nodes by id; give each new container its first state."""
        first_set_ever = not any(i.startswith(("set:", "ana:")) for i in self._seen)
        filtering = self._vm.filter_active           # matches are shown, not hidden behind a closed row
        for node_id, item in self._by_id.items():
            if not item.hasChildren():
                continue
            if node_id not in self._seen:
                self._seen.add(node_id)
                kind = item.data(_KIND_ROLE)
                if kind == "section" or (kind in _NEW_CONTAINER_KINDS and first_set_ever):
                    self._expanded.add(node_id)      # the first set opens; later sets start closed
                    first_set_ever = False
            index = self._model.indexFromItem(item)
            want = filtering or node_id in self._expanded
            if self._view.isExpanded(index) != want:
                self._view.setExpanded(index, want)

    def _apply_spans(self) -> None:
        for item in self._by_id.values():
            if item.data(_KIND_ROLE) == "file":
                parent = item.parent()
                self._view.setFirstColumnSpanned(
                    item.row(), self._model.indexFromItem(parent) if parent else QModelIndex(), True)

    # ---- selection follows the view model's identity --------------------------------------------
    def _restore_selection(self, reveal: bool) -> None:
        keys = self._vm.selection.keys
        selected = set(keys)
        model_selection = QItemSelection()
        selection = self._view.selectionModel()
        with self._changing():
            for node_id, item in self._by_id.items():
                key = item.data(_KEY_ROLE)
                members = self._vm.container_keys(node_id) if key is None else ()
                if key in selected or (members and set(members) <= selected):
                    index = self._model.indexFromItem(item)
                    model_selection.select(index, index.siblingAtColumn(len(COLUMNS) - 1))
            selection.select(model_selection, QItemSelectionModel.SelectionFlag.ClearAndSelect)
            if reveal:
                for key in keys:
                    item = self._by_key.get(key)
                    parent = item.parent() if item is not None else None
                    while parent is not None:
                        self._expanded.add(parent.data(_ID_ROLE))
                        self._view.setExpanded(self._model.indexFromItem(parent), True)
                        parent = parent.parent()
                first = next((self.index_for_key(key) for key in keys if key in self._by_key), QModelIndex())
                if first.isValid():
                    selection.setCurrentIndex(first, QItemSelectionModel.SelectionFlag.NoUpdate)
                    self._view.scrollTo(first)

    def _update_footer(self) -> None:
        vm = self._vm
        visible, total = vm.counts()
        noun = "spectrum" if total == 1 else "spectra"
        removed = 0 if vm.show_removed else vm.removed_count()
        tail = f" · {removed} removed" if removed else ""
        self._status.setText(f"{visible} of {total} {noun} · <a href='clear'>Clear filter</a>"
                             if vm.filter_active else f"{total} {noun}{tail}" if total or removed else "")
        notice = vm.outside_filter()
        self._notice.setText(notice or "")
        self._notice.setVisible(notice is not None)
        key = vm.selection.active
        can = key is not None and vm.snapshot.spectrum(key) is not None
        self._inspect.setEnabled(can)
        # a disabled button says why in text, not only on hover
        self._hint.setText(_HINT if can else _HINT_NONE)
