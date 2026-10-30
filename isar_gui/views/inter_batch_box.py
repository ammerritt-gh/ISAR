"""The Processing dock's inter-spectra box: the anchors list, the two batch kinds and their Start buttons.

It applies a ``BoxState`` from ``viewmodels.inter_batch`` and writes only what the user asks for: anchors through
``InterState.set_anchors`` (deltas), and a Start emits the one ``BatchRequest`` the preview was asked for. The window
queues it (AGENTS.md hard rule 7: nothing is computed here).
"""
from __future__ import annotations

from PySide6.QtCore import QSignalBlocker, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QButtonGroup, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
                               QPushButton, QRadioButton, QToolButton, QVBoxLayout, QWidget)

from isar_gui.selection import identity
from isar_gui.viewmodels.inter_batch import (DIRECTIONS, BoxState, anchored_request, box_state, simultaneous_request,
                                             wanted_requests)

DIRECTION_CHOICES = (
    ("q", "Spread in q", "Spread in q: all analyzers of each anchor's scan."),
    ("T", "Spread in T", "Spread in T: each anchor's analyzer at every temperature of the scan set."),
    ("qT", "Spread in q and T", "Spread in q and T: both of those together."),
)
TOGGLE_TIP = "The anchors and the two inter-spectra batch kinds. Click to show or hide them."
ANCHORS_TIP = ("Spectra you trust: an anchored batch grows outward from them. Add or remove them here or in the "
               "scan list's Anchor column; it is one list.")
ADD_TIP = ("Make the scans selected in the scan list anchors. Spectra already anchored, or with no fast-pass "
           "result, are skipped.")
REMOVE_TIP = "Take the selected anchors off the list. Their fits are kept."
ANCHORED_TIP = "Refit outward from the anchors, one ring of neighbours at a time. The anchors keep their fits."
SIMULTANEOUS_TIP = ("Refit the selected spectra together, each against its neighbours in the selection, until the "
                    "fits settle.")


def _show_control(button: QPushButton, control, tip: str, reason: QLabel | None = None) -> None:
    button.setEnabled(control.enabled)
    button.setToolTip(tip if control.enabled else control.reason)
    if reason is not None:
        reason.setText(control.reason or "")
        reason.setVisible(not control.enabled)


class InterBatchBox(QWidget):
    batch_requested = Signal(object)        # a BatchRequest the user started; the window queues it

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("inter_batch_box")
        self._inter = None                  # the window's InterState (set_inter)
        self._scan_list = None              # the scan list view model: the selection and its anchorable scans
        self._controller = None             # the queue controller: bound and blocked (set_controller)
        self._direction = DIRECTIONS[0]
        self._expanded = False
        self._rows: list[tuple[str, str]] = []   # (key, text) as the anchors list shows them

        self._toggle = QToolButton(self)
        self._toggle.setObjectName("inter_box_toggle")
        self._toggle.setAutoRaise(True)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self._toggle.setToolTip(TOGGLE_TIP)
        self._toggle.clicked.connect(self._on_toggle)

        self._body = QWidget(self)
        self._body.setObjectName("inter_box_body")
        self._anchors_heading = QLabel("Anchors", self._body)
        self._anchors_heading.setToolTip(ANCHORS_TIP)
        self._anchors = QListWidget(self._body)
        self._anchors.setObjectName("inter_anchors")
        self._anchors.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._anchors.setMaximumHeight(110)
        self._anchors.setAccessibleName("Anchors list")
        self._anchors.setToolTip(ANCHORS_TIP)
        self._anchors.itemSelectionChanged.connect(self.refresh)
        self._add = QPushButton("Add selected scans as anchors", self._body)
        self._add.setObjectName("inter_add_anchors")
        self._add.clicked.connect(self._add_anchors)
        self._remove = QPushButton("Remove selected anchors", self._body)
        self._remove.setObjectName("inter_remove_anchors")
        self._remove.clicked.connect(self._remove_anchors)
        self._note = QLabel("", self._body)
        self._note.setObjectName("inter_note")
        self._note.setToolTip("A problem the inter-spectra fitting reported; the words say what.")
        self._note.setWordWrap(True)

        self._anchored_heading = QLabel("Anchored batch: grows outward from the anchors, ring by ring", self._body)
        self._anchored_heading.setToolTip(ANCHORED_TIP)
        self._radios: dict[str, QRadioButton] = {}
        group = QButtonGroup(self)
        for key, text, tip in DIRECTION_CHOICES:
            radio = QRadioButton(text, self._body)
            radio.setObjectName(f"inter_direction_{key}")
            radio.setToolTip(tip)
            radio.toggled.connect(lambda checked, k=key: self._on_direction(k) if checked else None)
            group.addButton(radio)
            self._radios[key] = radio
        self._anchored_line = self._line("inter_anchored_line",
                                         "What the anchored batch would refit, and in how many rings.")
        self._start_anchored = QPushButton("Start anchored batch", self._body)
        self._start_anchored.setObjectName("inter_start_anchored")
        self._start_anchored.clicked.connect(self._start_anchored_batch)
        self._anchored_reason = self._line("inter_anchored_reason", "Why Start anchored batch is greyed.")

        self._simultaneous_heading = QLabel("Simultaneous batch: the spectra selected in the scan list", self._body)
        self._simultaneous_heading.setToolTip(SIMULTANEOUS_TIP)
        self._simultaneous_line = self._line("inter_simultaneous_line",
                                             "What the simultaneous batch would refit together.")
        self._start_simultaneous = QPushButton("Start simultaneous batch on the selection", self._body)
        self._start_simultaneous.setObjectName("inter_start_simultaneous")
        self._start_simultaneous.clicked.connect(self._start_simultaneous_batch)
        self._simultaneous_reason = self._line("inter_simultaneous_reason", "Why Start simultaneous batch is greyed.")

        body = QVBoxLayout(self._body)
        body.setContentsMargins(12, 0, 0, 0)
        body.setSpacing(4)
        body.addWidget(self._anchors_heading)
        body.addWidget(self._anchors)
        buttons = QHBoxLayout()
        buttons.addWidget(self._add)
        buttons.addWidget(self._remove)
        body.addLayout(buttons)
        body.addWidget(self._note)
        body.addWidget(self._anchored_heading)
        for radio in self._radios.values():
            body.addWidget(radio)
        body.addWidget(self._anchored_line)
        body.addWidget(self._start_anchored)
        body.addWidget(self._anchored_reason)
        body.addWidget(self._simultaneous_heading)
        body.addWidget(self._simultaneous_line)
        body.addWidget(self._start_simultaneous)
        body.addWidget(self._simultaneous_reason)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(2)
        outer.addWidget(self._toggle)
        outer.addWidget(self._body)
        self._radios[self._direction].setChecked(True)
        self._set_expanded(True)            # open until the user folds it; anchors do not change that

    def _line(self, name: str, tip: str) -> QLabel:
        label = QLabel("", self._body)
        label.setObjectName(name)
        label.setToolTip(tip)
        label.setWordWrap(True)
        label.hide()
        return label

    # ---- wiring ---------------------------------------------------------------------------------
    def set_inter(self, inter, scan_list) -> None:
        """The window's InterState and the scan list view model (called once, when the window is built)."""
        self._inter = inter
        self._scan_list = scan_list
        inter.changed.connect(lambda *_: self.refresh())
        inter.preview_ready.connect(lambda *_: self.refresh())
        inter.preview_failed.connect(lambda *_: self.refresh())
        scan_list.selection_changed.connect(lambda *_: self.refresh())
        scan_list.snapshot_changed.connect(lambda *_: self.refresh())
        self.refresh()

    def set_controller(self, controller) -> None:
        """The queue controller: its ``view().bound`` says an experiment is open, its ``blocked`` a campaign runs."""
        self._controller = controller
        if controller is not None:
            controller.changed.connect(lambda *_: self.refresh())
        self.refresh()

    def save_view_state(self, session) -> None:
        session.view_state["inter_box"] = {"folded": not self._expanded, "direction": self._direction}

    def load_view_state(self, session) -> None:
        saved = (getattr(session, "view_state", None) or {}).get("inter_box")
        if not isinstance(saved, dict):
            return
        self._direction = saved.get("direction") if saved.get("direction") in DIRECTIONS else DIRECTIONS[0]
        self._radios[self._direction].setChecked(True)
        self._set_expanded(saved.get("folded") is not True)     # "expanded" is the old default, never read

    # ---- what the box shows ---------------------------------------------------------------------
    def refresh(self) -> None:
        """Read the window's state and apply it; reads only, and asks for the previews it is still missing."""
        if self._inter is None or self._scan_list is None:
            return
        inter, scan_list = self._inter, self._scan_list
        view = self._controller.view() if self._controller is not None else None
        bound = bool(view and view.bound)
        selection = scan_list.selected_spectra()
        wanted = wanted_requests(bound=bound, direction=self._direction, anchors=inter.anchors, selection=selection)
        chosen = self._fill_anchors(inter.anchors)
        previews = {req.kind: inter.preview_for(req) for req in wanted}
        errors = {req.kind: inter.preview_error_for(req) for req in wanted}
        state = box_state(bound=bound, blocked=view.blocked if bound else None, n_anchors=len(inter.anchors),
                          n_selected=len(selection), n_addable=len(scan_list.anchorable_keys()),
                          n_selected_anchors=len(chosen), anchored=previews.get("anchored"),
                          simultaneous=previews.get("simultaneous"), note=inter.error,
                          anchored_error=errors.get("anchored"), simultaneous_error=errors.get("simultaneous"))
        self._apply(state)
        missing = [req for req in wanted if previews.get(req.kind) is None and errors.get(req.kind) is None]
        if missing:
            inter.request_preview(missing[0])   # one at a time: InterState keeps only the newest request

    def _apply(self, state: BoxState) -> None:
        _show_control(self._add, state.add, ADD_TIP)
        _show_control(self._remove, state.remove, REMOVE_TIP)
        _show_control(self._start_anchored, state.anchored, ANCHORED_TIP, self._anchored_reason)
        _show_control(self._start_simultaneous, state.simultaneous, SIMULTANEOUS_TIP, self._simultaneous_reason)
        for label, text in ((self._anchored_line, state.anchored_line),
                            (self._simultaneous_line, state.simultaneous_line), (self._note, state.note)):
            label.setText(text)
            label.setVisible(bool(text))

    def _fill_anchors(self, keys) -> list[str]:
        """Rebuild the list only when its rows changed (a refresh never undoes a click); returns the selected keys."""
        chosen = self._selected_anchor_keys()
        rows = [(key, self._anchor_text(key)) for key in keys]
        if rows != self._rows:
            self._rows = rows
            with QSignalBlocker(self._anchors):
                self._anchors.clear()
                for key, text in rows:
                    item = QListWidgetItem(text)
                    item.setData(Qt.ItemDataRole.UserRole, key)
                    item.setSelected(key in chosen)
                    self._anchors.addItem(item)
        return chosen

    def _anchor_text(self, key: str) -> str:
        ident = identity(self._scan_list.snapshot, key)
        if ident is None:
            return f"{key} (not in the scan list)"
        return f"{ident.analyzer} · {ident.set_label} · {ident.t}"

    def _selected_anchor_keys(self) -> list[str]:
        return [item.data(Qt.ItemDataRole.UserRole) for item in self._anchors.selectedItems()]

    # ---- the user's actions ---------------------------------------------------------------------
    def _on_toggle(self) -> None:
        self._set_expanded(not self._expanded)

    def _set_expanded(self, expanded: bool) -> None:
        self._expanded = expanded
        self._body.setVisible(expanded)
        self._toggle.setText("Inter-spectra fitting ▾" if expanded else "Inter-spectra fitting ▸")
        self._toggle.setAccessibleName(("Hide" if expanded else "Show") + " the inter-spectra fitting controls")

    def _on_direction(self, key: str) -> None:
        self._direction = key
        self.refresh()

    def _add_anchors(self) -> None:
        keys = self._scan_list.anchorable_keys()      # the same rule as the scan list's Set as anchor: a delta
        if keys:
            self._inter.set_anchors(keys, True)

    def _remove_anchors(self) -> None:
        keys = self._selected_anchor_keys()
        if keys:
            self._inter.set_anchors(keys, False)

    def _start_anchored_batch(self) -> None:
        self.batch_requested.emit(anchored_request(self._direction, self._inter.anchors))

    def _start_simultaneous_batch(self) -> None:
        self.batch_requested.emit(simultaneous_request(self._scan_list.selected_spectra()))
