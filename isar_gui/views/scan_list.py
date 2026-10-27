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

from PySide6.QtCore import QItemSelectionModel, QModelIndex, QPoint, Qt
from PySide6.QtGui import QAction, QActionGroup, QIcon, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import (QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
                               QMenu, QPushButton, QSizePolicy, QToolButton, QTreeView, QVBoxLayout,
                               QWidget)

from isar_gui.theme import dot_icon
from isar_gui.viewmodels.scan_list import (COLUMNS, OPTIONAL_COLUMNS, SORTS, TITLES,
                                           ScanListViewModel)

_ID_ROLE = Qt.ItemDataRole.UserRole           # the node id, on a row's first item
_KEY_ROLE = Qt.ItemDataRole.UserRole + 1      # the selectable identity, or None
_KIND_ROLE = Qt.ItemDataRole.UserRole + 2
_TONE_ROLE = Qt.ItemDataRole.UserRole + 3     # what the item's dot was drawn for
_SORT_LABELS = {"set": "By scan set", "temperature": "By temperature", "analyzer": "By analyzer"}
_HINT = "Double-click a row, or Inspect, to open the scan inspector."
_NEW_CONTAINER_KINDS = ("set", "analyzer")


class ScanListPanel(QWidget):           # no public signals; it acts through the VM
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
        self._layout_key = None               # (sort, filter, columns): a change reveals the selection

        self._build_widgets()
        vm.snapshot_changed.connect(self._refresh)
        vm.current_changed.connect(self._on_vm_current_changed)
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

        self._column_actions: dict[str, QAction] = {}
        column_menu = QMenu(self)
        for column in OPTIONAL_COLUMNS:
            action = column_menu.addAction(TITLES[column])
            action.setCheckable(True)
            action.toggled.connect(lambda on, c=column: self._on_column_toggled(c, on))
            self._column_actions[column] = action
        self._columns_button = self._menu_button("Columns ▾", column_menu, "Choose the columns")

        self._model = QStandardItemModel(0, len(COLUMNS), self)
        self._view = QTreeView(self)
        self._view.setObjectName("scan_list_tree")
        self._view.setModel(self._model)
        self._view.setAccessibleName("Scan list")
        self._view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._view.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self._view.setUniformRowHeights(True)
        self._view.setIndentation(14)
        self._view.setAcceptDrops(False)
        header = self._view.header()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(36)
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self._view.expanded.connect(lambda idx: self._on_toggled(idx, True))
        self._view.collapsed.connect(lambda idx: self._on_toggled(idx, False))
        self._view.activated.connect(self._on_activated)
        self._view.selectionModel().currentChanged.connect(self._on_current_index_changed)

        self._empty = QLabel("No scans yet", self)
        self._empty.setObjectName("scan_list_empty")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self._notice = QLabel(self)
        self._notice.setObjectName("scan_list_notice")
        self._notice.setWordWrap(True)
        self._notice.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

        self._status = QLabel(self)           # "9 spectra", or "2 of 9 spectra · Clear filter" (a link)
        self._status.setObjectName("scan_list_status")
        self._status.setWordWrap(True)
        self._status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._status.linkActivated.connect(lambda _link: self._filter.setText(""))

        self._inspect = QPushButton("Inspect", self)
        self._inspect.setObjectName("scan_list_inspect")
        self._inspect.setToolTip("Open the scan inspector on the selected spectrum")
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

    def _on_current_index_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        if not self._busy and current.isValid():
            key = current.siblingAtColumn(0).data(_KEY_ROLE)
            if key:                              # set, analyzer, section and file rows leave the display be
                self._vm.select(key)
        self._update_footer()                    # Inspect follows the visible row, which a set row changes too

    def _on_activated(self, index: QModelIndex) -> None:
        key = index.siblingAtColumn(0).data(_KEY_ROLE)
        if key:
            self._vm.inspect(key)

    def _on_inspect_clicked(self) -> None:
        key = self._visible_key()
        if key:
            self._vm.inspect(key)

    def _visible_key(self) -> str | None:
        index = self._view.currentIndex()
        return index.siblingAtColumn(0).data(_KEY_ROLE) if index.isValid() else None

    def _on_toggled(self, index: QModelIndex, opened: bool) -> None:
        node_id = index.siblingAtColumn(0).data(_ID_ROLE)
        if self._busy or node_id is None:
            return
        (self._expanded.add if opened else self._expanded.discard)(node_id)

    def _on_vm_current_changed(self, _key) -> None:
        self._restore_selection(reveal=True)
        self._update_footer()

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
        layout_key = (vm.sort, vm.filter_text, vm.visible_columns())
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
        if self._filter.text() != vm.filter_text:
            self._filter.setText(vm.filter_text)
        self._model.setHorizontalHeaderLabels([vm.column_title(c) for c in COLUMNS])

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
        for column, item in zip(COLUMNS, items):
            cell = node.cells.get(column)
            text = cell.text if cell else ""
            tip = (cell.tooltip or cell.text) if cell else ""
            tone = cell.tone if cell else None
            if item.text() != text:
                item.setText(text)
            if item.toolTip() != tip:
                item.setToolTip(tip)
            if item.data(_TONE_ROLE) != tone:
                item.setData(tone, _TONE_ROLE)
                item.setIcon(dot_icon(tone) if tone is not None else QIcon())

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
        key = self._vm.current_key
        item = self._by_key.get(key) if key else None
        selection = self._view.selectionModel()
        with self._changing():
            if item is None:
                current = selection.currentIndex()
                if current.isValid() and current.siblingAtColumn(0).data(_KEY_ROLE):
                    selection.clearSelection()
                    selection.clearCurrentIndex()
                return
            index = self._model.indexFromItem(item)
            if reveal:
                parent = item.parent()
                while parent is not None:
                    self._expanded.add(parent.data(_ID_ROLE))
                    self._view.setExpanded(self._model.indexFromItem(parent), True)
                    parent = parent.parent()
            selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.ClearAndSelect
                                      | QItemSelectionModel.SelectionFlag.Rows)
            if reveal:
                self._view.scrollTo(index)

    def _update_footer(self) -> None:
        vm = self._vm
        visible, total = vm.counts()
        noun = "spectrum" if total == 1 else "spectra"
        self._status.setText(f"{visible} of {total} {noun} · <a href='clear'>Clear filter</a>"
                             if vm.filter_active else f"{total} {noun}" if total else "")
        notice = vm.outside_filter()
        self._notice.setText(notice or "")
        self._notice.setVisible(notice is not None)
        key = self._visible_key()
        self._inspect.setEnabled(key is not None and vm.snapshot.spectrum(key) is not None)
