"""AttentionList -- the needs-attention list in the left column, under the setup section.

It shows the backend's ``needs_attention`` groups unchanged (their order, their summaries, their
reasons) and lets the user act on the ones that have an action: tick the scan sets (or analyzers) a gap
concerns, enter one value for them, and save it as the user's value on exactly those ticks, in one
all-or-nothing save. The GUI computes no gap: a group exists because the backend says so, and it closes
when the worker's re-read says so.

After the backend's groups the list keeps up to three of its own, which are not gaps and are not counted:
"Your temperature values" and "Your slit values" (every scan set carrying a value of the user's, so
*Use the file's value* stays reachable after the gap closes) and "Your accepted counts" (the analyzers
whose counts the user declared raw).

Each group keeps one persistent foot (entry, reason, preview, buttons, outcome), and its rows are rebuilt
only when what they show changed, so a background re-read never loses typed text, ticks, expansion or the
scroll place. The list scrolls inside a bounded height so the scan list below it keeps its minimum.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea,
                               QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from isar_gui.plain_words import (KIND_FIX, STAND_IN_MARK, STAND_IN_TIP, affects, fix_sentence, group_title,
                                  plain_hold, stand_in)
from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.views.elided_label import ElidedButton, ElidedLabel
from isar_gui.views.setup_section import LOCATE_LONG, LOCATE_TIP, RUN_TEXT, RUN_TIP
from isar_gui.views.value_entry import ValueEntry

USER_TEMPERATURE = "user-values:temperature"
USER_SLIT = "user-values:slit"
USER_COUNTS = "user-conditions:counts_accepted"
_USER_FACTS = {USER_TEMPERATURE: "temperature", USER_SLIT: "slit"}

_BODY_MAX = 160            # px: the scroll area never grows past this, so the scan list keeps its minimum
_STATES = ("ready", "fallback", "pending", "held", "unreadable")      # the backend's ascending severity
# The remedy that has a button of its own inside the group: action -> (button text, tooltip, signal name).
_FIX_BUTTONS = {
    "locate_folder": (LOCATE_LONG, LOCATE_TIP, "locate_requested"),
    "import_again": ("Add data…", "Read more data files into this experiment. The originals stay where they "
                                   "are and must stay available.", "add_data_requested"),
    "start_analysis": (RUN_TEXT, RUN_TIP, "run_requested"),
}
_FOOT_BUTTONS = {"value": [("save", "Save my value")], "dead": [("dead", "Leave out analyzer")],
                 "declared": [("in_use", "Use analyzer again")], "user": [("file_value", "Use the file's value")],
                 "accepted": [("withdraw", "Take back acceptance")]}
_BUTTON_TIPS = {
    "save": "Use this value for the ticked scan sets instead of the file's. The files are not changed.",
    "dead": "Leave the ticked analyzers out of the analysis of these scan sets. Reversible; the data stay.",
    "in_use": "Put the ticked analyzers back into the analysis.",
    "file_value": "Drop your value for the ticked scan sets and use the value in the files again.",
    "withdraw": "Hold the ticked analyzers again until their counts are accepted.",
    "accept": ("Some of this analyzer's counts are not whole numbers after N = (y/e)², so they may not be raw "
               "counts and the fit is held. Accept to fit them as they are; no data change."),
}
_TICK_HINT = {
    "value": "Tick the scan sets above; then enter the value and press Save my value.",
    "user": "Tick the scan sets above; then use the file's value again.",
    "dead": "Tick the analyzers above; then leave them out of the analysis.",
    "declared": "Tick the analyzers above; then use them again.",
    "accepted": "Tick the analyzers above; then take back your acceptance.",
}
_TITLE_TIPS = {
    "q_missing": "Without Q the campaign planner cannot place this scan; the fit is not affected.",
    "calibration_pending": ("Analysis calibrates these scan sets (energy zero and resolution) when it starts; "
                            "until then they are held and no energy zero is assumed."),
    "gpi_rejected": ("The gpi file's |Q| does not match the #Q= value of every data file it covers (or one is "
                     "missing), so nothing from it is used: no Q, slit or curve set for these scan sets."),
    "gpi_curves_dropped": ("The gpi file names a resolution curve set, but the slit you entered is not that set's "
                           "slit, so your slit chooses the curves for these scan sets."),
    "gpi_folder_changed": ("The gpi folder was changed; the values from its files stay set aside until the data is "
                           "read again or the analysis starts."),
    "sidecar": ("The .var file sits beside the analyzer files and is named after the scan set. It supplies the silicon "
                "order (the incident energy, when the data file gives none), the energy bin size, the number of scans "
                "merged into the set, the analyzers the set should have, and for each analyzer its efficiency, "
                "polarisation factor and elastic-peak centroid and area. Without it those values are unknown, and the "
                "bin size is worked out from the data's own spacing. Put the .var in the same folder as the analyzer "
                "files, then add the data again."),
}
_HEADER_TIP = ("Things missing or unreadable that stop or weaken the analysis. Open a group to see which "
               "scan sets or analyzers, then fix it there.")
_WARN = f"color: {TONE_COLOR[Tone.WARN]};"
_QUIET = "color: #6b7280;"


def _n(count: int, one: str, many: str | None = None) -> str:
    return f"{count} {one if count == 1 else (many or one + 's')}"


@dataclass(frozen=True)
class _Row:
    id: str                     # scan key (scan mode) or unit key "scan:aNN" (unit mode): the member's identity
    text: str
    detail: str = ""            # the quieter second line
    mark: str = ""              # values disagree / changed since the last fit
    parent: str | None = None   # unit mode: the scan key the analyzer belongs to
    info: tuple[str, ...] = ()  # scan mode: the scan set's analyzers, as plain lines
    tooltip: str = ""


@dataclass(frozen=True)
class _Spec:
    key: str
    title: str
    quiet: str
    mode: str                   # "scan" | "unit" | "plain"
    foot: str | None            # None: shown only; else a key of _FOOT_BUTTONS
    fix: str | None = None      # the action whose remedy has a button in the group (a key of _FIX_BUTTONS)
    title_tip: str = ""
    fact: str | None = None
    counts: bool = False        # the foot also offers *Counts accepted*
    mark: str = ""
    mark_tip: str = ""
    rows: tuple[_Row, ...] = ()
    scans: tuple[tuple[str, str], ...] = ()      # unit mode: (scan key, label) in order
    plain: tuple[tuple[str, str], ...] = ()      # (text, tooltip)


class _Line(QLabel):
    """A wrapped line whose width never pins its column; clicking it emits ``clicked``."""
    clicked = Signal()

    def __init__(self, text: str = "", parent: QWidget | None = None, *, quiet: bool = False,
                 warn: bool = False) -> None:
        super().__init__(text, parent)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        if quiet:
            self.setStyleSheet(_QUIET)
        if warn:
            self.setStyleSheet(_WARN)

    def minimumSizeHint(self):  # noqa: N802 (Qt override)
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 (Qt override)
        super().mouseReleaseEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


def _clear(layout) -> None:
    while layout.count():
        widget = layout.takeAt(0).widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


class _Foot(QWidget):
    """The entry at a group's foot: preview, entry or reason, buttons and the save outcome."""

    def __init__(self, spec: _Spec, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("attention_foot")
        self.entry = ValueEntry(spec.fact, self) if spec.foot == "value" else None
        self.reason_edit: QLineEdit | None = None
        if self.entry is None:
            self.reason_edit = QLineEdit(self)
            self.reason_edit.setObjectName("attention_reason")
            self.reason_edit.setPlaceholderText("Reason (optional)")
            self.reason_edit.setAccessibleName("Reason (optional)")
            self.reason_edit.setToolTip("Optional. Kept with the change so you can see later why you made it.")
        self.reason_note = _Line("", self, quiet=True)      # why the value button is off, as text
        self.reason_note.setObjectName("attention_save_reason")
        self.reason_note.hide()
        self.preview = ElidedLabel("", self, Qt.TextElideMode.ElideRight)
        self.preview.setObjectName("attention_preview")
        self.status = _Line("", self, warn=True)
        self.status.setObjectName("attention_status")
        self.status.hide()
        self.buttons: dict[str, QPushButton] = {}
        row = QVBoxLayout()         # one button to a line: the long names must not widen the narrow column
        row.setSpacing(3)
        for name, text in [*([("accept", "Accept counts as they are")] if spec.counts else []),
                           *_FOOT_BUTTONS[spec.foot]]:
            button = ElidedButton(text, self)
            button.setObjectName(f"attention_{name}")
            button.setToolTip(_BUTTON_TIPS[name])
            self.buttons[name] = button
            row.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 2, 0, 4)
        layout.setSpacing(3)
        layout.addWidget(self.preview)
        layout.addWidget(self.entry if self.entry is not None else self.reason_edit)
        layout.addLayout(row)
        layout.addWidget(self.reason_note)
        layout.addWidget(self.status)
        if self.reason_edit is not None:
            self.reason_edit.textChanged.connect(lambda *_a: self.status.hide())

    def reason(self) -> str | None:
        if self.entry is not None:
            return self.entry.reason()
        return self.reason_edit.text().strip() or None

    def show_refusal(self, message: str) -> None:
        """The backend's reason, under the entry."""
        if self.entry is not None:
            self.entry.set_message(message, False)
        else:
            self.status.setText(message)
            self.status.setToolTip(message)
            self.status.setVisible(bool(message))

    def clear(self) -> None:
        if self.entry is not None:
            self.entry.clear()
        else:
            self.reason_edit.clear()
            self.status.hide()


class _Group(QWidget):
    """One group: header (toggle, summary, mark), the quiet line, and when open its members and foot."""

    def __init__(self, owner: "AttentionList", spec: _Spec) -> None:
        super().__init__()
        self.setObjectName("attention_group")
        self.setProperty("group", spec.key)
        self.key = spec.key
        self.spec = spec
        self._owner = owner
        self._shown: tuple | None = None        # what the rows were last built from
        self.toggle = QToolButton(self)
        self.toggle.setObjectName("attention_group_toggle")
        self.toggle.setAutoRaise(True)
        self.toggle.clicked.connect(lambda: owner._toggle_group(self.key))
        self.title = _Line("", self)
        self.title.setObjectName("attention_group_title")
        self.title.clicked.connect(lambda: owner._toggle_group(self.key))
        self.mark = _Line("", self, warn=True)       # on its own line: a long mark must not widen the column
        self.mark.setObjectName("attention_group_mark")
        self.mark.hide()
        self.quiet = _Line("", self, quiet=True)
        self.quiet.setObjectName("attention_group_quiet")
        self.fix = ElidedButton("", self)            # the remedy, beside the sentence that names it
        self.fix.setObjectName("attention_fix")
        self.fix.hide()
        self.fix.clicked.connect(lambda: owner._fix(self.key))
        self.hint = _Line("", self, quiet=True)      # what the ticks are for, while none is ticked
        self.hint.setObjectName("attention_tick_hint")
        self.hint.hide()
        self.rows_widget = QWidget(self)
        self.rows = QVBoxLayout(self.rows_widget)
        self.rows.setContentsMargins(18, 0, 0, 0)
        self.rows.setSpacing(1)
        self.rows_widget.hide()
        self.foot = _Foot(spec, self) if spec.foot else None
        if self.foot is not None:
            self.foot.hide()
            if self.foot.entry is not None:
                self.foot.entry.edited.connect(lambda: owner._sync_foot(self.key))
            for name, button in self.foot.buttons.items():
                button.clicked.connect(lambda _c=False, n=name: owner._act(self.key, n))
        head = QHBoxLayout()
        head.setSpacing(2)
        head.addWidget(self.toggle, 0, Qt.AlignmentFlag.AlignTop)
        head.addWidget(self.title, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(1)
        layout.addLayout(head)
        layout.addWidget(self.mark)
        layout.addWidget(self.quiet)
        fix_row = QHBoxLayout()
        fix_row.setContentsMargins(18, 0, 0, 0)
        fix_row.addWidget(self.fix, 0)
        fix_row.addStretch(1)
        layout.addLayout(fix_row)
        layout.addWidget(self.rows_widget)
        layout.addWidget(self.hint)
        if self.foot is not None:
            layout.addWidget(self.foot)
        self.boxes: dict[str, QCheckBox] = {}           # member id -> its check box (open group only)
        self.scan_boxes: dict[str, QCheckBox] = {}      # unit mode: scan key -> its tri-state box
        self.all_box: QCheckBox | None = None

    # ---- content ---------------------------------------------------------------------------
    def refresh(self, spec: _Spec, *, open_: bool, open_scans: frozenset[str]) -> None:
        self.spec = spec
        self.title.setText(spec.title)
        self.title.setToolTip(spec.title_tip or spec.title)
        self.quiet.setText(spec.quiet)
        self.quiet.setVisible(bool(spec.quiet))
        self.apply_fix()
        self.fix.setVisible(spec.fix is not None)
        self.mark.setText(spec.mark)
        self.mark.setToolTip(spec.mark_tip)
        self.mark.setVisible(bool(spec.mark))
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow)
        self.toggle.setAccessibleName(("Collapse " if open_ else "Expand ") + spec.title)
        self.rows_widget.setVisible(open_)
        shown = (spec, open_scans) if open_ else None
        if shown == self._shown:
            return                                  # nothing the rows show changed: keep focus and place
        self._shown = shown
        self.boxes, self.scan_boxes, self.all_box = {}, {}, None
        _clear(self.rows)
        if open_:
            self._fill(open_scans)
        elif self.foot is not None:
            self.foot.hide()

    def apply_fix(self) -> None:
        """The remedy button's words; *Fast-pass all ready* follows the window's run state (count, on or off, why)."""
        if self.spec.fix is None:
            return
        text, tip, _signal = _FIX_BUTTONS[self.spec.fix]
        if self.spec.fix == "start_analysis":
            enabled, text, tip = self._owner.run_state
            self.fix.setEnabled(enabled)
        self.fix.set_full_text(text)
        self.fix.setToolTip(tip)

    def _check(self, text: str, tip: str = "", *, tristate: bool = False) -> QCheckBox:
        box = QCheckBox(self)
        box.setTristate(tristate)
        box.setAccessibleName(text)
        box.setToolTip(tip or text)
        return box

    def _row(self, text: str, tip: str, *, box: QCheckBox | None = None, expander: QToolButton | None = None,
             detail: str = "", indent: int = 0) -> QWidget:
        """One line (expander, check box, wrapped text) with an optional quieter line under it."""
        holder = QWidget(self)
        outer = QVBoxLayout(holder)
        outer.setContentsMargins(indent, 0, 0, 0)
        outer.setSpacing(0)
        line = QHBoxLayout()
        line.setContentsMargins(0, 0, 0, 0)
        line.setSpacing(2)
        for part in (expander, box):
            if part is not None:
                line.addWidget(part, 0, Qt.AlignmentFlag.AlignTop)
        label = _Line(text, holder)
        label.setToolTip(tip or text)
        if box is not None:
            label.clicked.connect(box.click)
        line.addWidget(label, 1)
        outer.addLayout(line)
        if detail:
            quiet = _Line(detail, holder, quiet=True)
            quiet.setToolTip(detail)
            quiet.setContentsMargins(22 if box is not None else 0, 0, 0, 0)
            outer.addWidget(quiet)
        return holder

    def _expander(self, open_: bool, callback, name: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("attention_scan_toggle")
        button.setAutoRaise(True)
        button.setArrowType(Qt.ArrowType.DownArrow if open_ else Qt.ArrowType.RightArrow)
        button.setAccessibleName(name)
        button.clicked.connect(callback)
        return button

    def _member_box(self, row: _Row) -> QCheckBox:
        box = self._check(row.text, row.tooltip)
        box.setObjectName("attention_row_box")
        box.setProperty("member", row.id)
        box.clicked.connect(lambda checked=False, i=row.id: self._owner._tick(self.key, i, checked))
        self.boxes[row.id] = box
        return box

    def _fill(self, open_scans: frozenset[str]) -> None:
        spec, owner = self.spec, self._owner
        tick = spec.foot is not None
        if tick and len(spec.rows) > 1:
            noun = "analyzer" if spec.mode == "unit" else "scan set"
            self.all_box = self._check(f"Tick all {noun}s", tristate=True)
            self.all_box.setObjectName("attention_select_all")
            self.all_box.clicked.connect(lambda _c=False: owner._tick_all(
                self.key, self.all_box.checkState() != Qt.CheckState.Unchecked))
            self.rows.addWidget(self._row(f"Tick all {_n(len(spec.rows), noun)}", "", box=self.all_box))
        if spec.mode == "scan":
            for row in spec.rows:
                is_open = row.id in open_scans
                expander = (self._expander(is_open, lambda _c=False, i=row.id: owner._toggle_scan(self.key, i),
                                           f"{'Hide' if is_open else 'Show'} the analyzers of {row.text}")
                            if row.info else None)
                text = row.text + (f"  ({row.mark})" if row.mark else "")
                self.rows.addWidget(self._row(text, row.tooltip, box=self._member_box(row) if tick else None,
                                              expander=expander, detail=row.detail))
                if is_open:
                    for line in row.info:
                        self.rows.addWidget(self._row(line, line, indent=40))
        elif spec.mode == "unit":
            for scan, label in spec.scans:
                members = [r for r in spec.rows if r.parent == scan]
                is_open = scan in open_scans
                expander = self._expander(is_open, lambda _c=False, s=scan: owner._toggle_scan(self.key, s),
                                          f"{'Hide' if is_open else 'Show'} the analyzers of {label}")
                scan_box = None
                if tick:
                    scan_box = self._check(label, tristate=True)
                    scan_box.setObjectName("attention_scan_box")
                    scan_box.setProperty("member", scan)
                    scan_box.clicked.connect(lambda _c=False, s=scan, b=scan_box: owner._tick_scan(
                        self.key, s, b.checkState() != Qt.CheckState.Unchecked))
                    self.scan_boxes[scan] = scan_box
                self.rows.addWidget(self._row(f"{label}  ({_n(len(members), 'analyzer')})", label,
                                              box=scan_box, expander=expander))
                if is_open:
                    for row in members:
                        text = row.text + (f"  ({row.mark})" if row.mark else "")
                        self.rows.addWidget(self._row(text, row.tooltip, indent=40 if not tick else 20,
                                                      box=self._member_box(row) if tick else None))
        for text, tip in spec.plain:
            self.rows.addWidget(self._row(text, tip))


class AttentionList(QWidget):
    expanded_changed = Signal(bool)
    locate_requested = Signal()         # "Choose resolution-curve folder…" -> the window's locate route
    add_data_requested = Signal()       # "Add data…" -> the files strip's add-files route
    run_requested = Signal()            # "Fast-pass all ready" -> the window's analysis entry point

    def __init__(self, intake, labels: Callable[[str], str] | None = None, parent: QWidget | None = None) -> None:
        """``labels``: scan key -> the scan set's label (the window passes the scan list's, read when a row is
        built); the key stands in when none is given."""
        super().__init__(parent)
        self.setObjectName("attention_list")
        self._intake = intake
        self._label = labels or (lambda scan: scan)
        self._expanded = False
        self._decided = False                       # the user (or a restored arrangement) chose in this experiment
        self._has_body = False
        self._root: Path | None = None
        self._auto_seen: set[Path] = set()          # experiments that already auto-expanded once
        self._groups: dict[str, _Group] = {}
        self._open: set[str] = set()                # group keys opened
        self._open_scans: dict[str, set[str]] = {}  # group key -> scan keys opened
        self._ticks: dict[str, set[str]] = {}
        self._pending_reveal: str | None = None
        self.run_state = (True, RUN_TEXT, RUN_TIP)  # (enabled, label, hover) of "Fast-pass all ready"; the window sets it

        self._header = QToolButton(self)
        self._header.setObjectName("attention_header")
        self._header.setAutoRaise(True)
        self._header.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        self._header.clicked.connect(lambda: self.set_expanded(not self._expanded))
        self._scroll = QScrollArea(self)
        self._scroll.setObjectName("attention_scroll")
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.setMaximumHeight(_BODY_MAX)
        self._scroll.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._content = QWidget()
        self._content.setObjectName("attention_content")
        self._column = QVBoxLayout(self._content)
        self._column.setContentsMargins(0, 0, 4, 0)
        self._column.setSpacing(2)
        self._column.addStretch(1)
        self._scroll.setWidget(self._content)
        self._scroll.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 0, 8, 0)
        layout.setSpacing(0)
        layout.addWidget(self._header)
        layout.addWidget(self._scroll)

        self._worker_done = False                   # the worker answered since the last reload (changed)
        intake.changed.connect(self._on_changed)
        intake.worker_changed.connect(self._on_worker)
        self._rebuild()

    def set_run_state(self, enabled: bool, label: str, tip: str) -> None:
        """*Fast-pass all ready* as the window's entry point has it: ``label`` carries the count, ``tip`` says why
        it is greyed."""
        state = (bool(enabled), label, tip)
        if state != self.run_state:
            self.run_state = state
            for group in self._groups.values():
                group.apply_fix()

    # ---- expansion ---------------------------------------------------------------------------
    def is_expanded(self) -> bool:
        return self._expanded

    def set_expanded(self, expanded: bool) -> None:
        self._decided = True
        self._apply_expanded(bool(expanded))

    def _apply_expanded(self, expanded: bool) -> None:
        if expanded == self._expanded:
            return
        self._expanded = expanded
        self._show_body()
        self.expanded_changed.emit(expanded)

    def reveal(self, group_key: str) -> None:
        """Expand the list and that group, and scroll to it."""
        self._apply_expanded(True)
        if group_key not in self._groups:
            self._pending_reveal = group_key           # the group may arrive with the worker's first result
            return
        self._pending_reveal = None
        self._open.add(group_key)
        self._refresh_group(group_key)
        group = self._groups[group_key]
        self._content.layout().activate()
        self._scroll_to(group)
        QTimer.singleShot(0, lambda: self._scroll_to(group))      # once the layout has settled

    def _scroll_to(self, group: QWidget) -> None:
        self._scroll.verticalScrollBar().setValue(group.y())      # the group's top at the top of the view

    def _toggle_group(self, key: str) -> None:
        self._open.symmetric_difference_update({key})
        self._refresh_group(key)

    def _toggle_scan(self, key: str, scan: str) -> None:
        self._open_scans.setdefault(key, set()).symmetric_difference_update({scan})
        self._refresh_group(key)

    def _refresh_group(self, key: str) -> None:
        group = self._groups[key]
        bar = self._scroll.verticalScrollBar()
        place = bar.value()
        group.refresh(group.spec, open_=key in self._open, open_scans=frozenset(self._open_scans.get(key, ())))
        self._sync_group(key)
        self._sync_foot(key)
        self._content.layout().activate()
        bar.setValue(place)

    # ---- reading the backend --------------------------------------------------------------
    def _specs(self, att) -> list[_Spec]:
        specs = [self._group_spec(g, att) for g in (() if att is None else att.groups)]
        return specs + self._user_specs()

    def _group_spec(self, g, att) -> _Spec:
        scope = ("unit" if any(i.analyzer for i in g.items)
                 else "scan" if any(i.scan for i in g.items) else "plain")
        if g.action == "enter_value" and g.kind in ("temperature", "slit") and scope == "scan":
            foot = "value"
        elif scope == "unit" and g.kind == "declared":
            foot = "declared"
        elif scope == "unit" and g.kind != "running":
            foot = "dead"
        else:
            foot = None
        fix = g.action if g.action in _FIX_BUTTONS else None
        phrase = KIND_FIX.get(g.kind) or fix_sentence(g.action)
        quiet = " · ".join(p for p in (f"Affects: {affects(g.uses)}" if g.uses else "",
                                       f"Fix: {phrase}" if phrase else "") if p)
        rows, plain, scans = [], [], []
        for item in g.items:
            mark = ", ".join(m for m in ("values disagree" if item.values_disagree else "",
                                         "changed since the last fit" if item.values_changed else "") if m)
            tip = "; ".join((item.reason, *item.values_changed))
            if item.scan is None:                              # file, line and experiment items
                plain.append((item.reason, item.file or item.reason))
            elif item.analyzer is not None and scope == "unit":
                if item.scan not in dict(scans):
                    scans.append((item.scan, self._label(item.scan)))
                rows.append(_Row(f"{item.scan}:{item.analyzer}", f"{item.analyzer} · {plain_hold(item.reason)}",
                                 mark=mark, parent=item.scan, tooltip=tip))
            elif scope == "unit":                              # a scan among analyzers (the declared group's exclusions)
                plain.append((f"{self._label(item.scan)}: {item.reason}", tip))
            else:
                label = self._label(item.scan)
                rows.append(_Row(item.scan, label, detail=plain_hold(item.reason), mark=mark,
                                 info=self._analyzer_lines(att, item.scan), tooltip=f"{label}: {tip}"))
        title = group_title(g.kind, g.summary, scope)
        tip = _TITLE_TIPS.get(g.kind, "")
        return _Spec(key=g.kind, title=title, quiet=quiet, mode=scope, foot=foot, fix=fix,
                     title_tip="\n".join(p for p in (tip, g.summary if g.summary != title else "") if p),
                     fact=g.kind if foot == "value" else None, counts=g.kind == "counts_unaccepted",
                     mark=STAND_IN_MARK if g.fallback_in_use else "",
                     mark_tip="\n".join((*dict.fromkeys(stand_in(i.fallback) for i in g.items if i.fallback),
                                         "", STAND_IN_TIP)),
                     rows=tuple(rows), scans=tuple(scans), plain=tuple(plain))

    @staticmethod
    def _analyzer_lines(att, scan: str) -> tuple[str, ...]:
        readiness = att.scans.get(scan)
        if readiness is None:
            return ()
        lines = []
        for name, ar in readiness.analyzers.items():
            worst = max(ar.uses.values(), key=lambda u: _STATES.index(u.state), default=None)
            lines.append(f"{name} · {worst.reason}" if worst is not None and worst.reason else f"{name} · ready")
        return tuple(lines)

    def _user_specs(self) -> list[_Spec]:
        intake = self._intake
        specs = []
        mine = {"temperature": [], "slit": []}
        for fr in intake.fact_rows or ():
            if fr.fact in mine and fr.analyzer is None and fr.user_value is not None:
                mine[fr.fact].append(fr)
        for key, fact in _USER_FACTS.items():
            if not mine[fact]:
                continue
            rows = tuple(_Row(fr.scan, f"{self._label(fr.scan)}: {self._shown(fr)}") for fr in mine[fact])
            specs.append(_Spec(key=key, title=f"Your {fact} values: {_n(len(rows), 'scan set')}",
                               quiet="Used instead of the files' values", mode="scan", foot="user",
                               fact=fact, rows=rows))
        accepted = [c for c in intake.conditions or () if c.condition == "counts_accepted"]
        if accepted:
            rows, scans = [], []
            for c in accepted:
                if c.scan not in dict(scans):
                    scans.append((c.scan, self._label(c.scan)))
                why = f"accepted, {c.user_reason}" if c.user_reason else "accepted"
                rows.append(_Row(f"{c.scan}:{c.analyzer}", f"{c.analyzer} · {self._label(c.scan)} ({why})",
                                 parent=c.scan))
            specs.append(_Spec(key=USER_COUNTS, title=f"Your accepted counts: {_n(len(rows), 'analyzer')}",
                               quiet="Counts you declared raw", mode="unit", foot="accepted",
                               rows=tuple(rows), scans=tuple(scans)))
        return specs

    @staticmethod
    def _shown(fr) -> str:
        value = fr.user_value
        text = f"{value:g} K" if fr.fact == "temperature" and isinstance(value, (int, float)) else (
            f"{value} K" if fr.fact == "temperature" else f"{value} mm")
        return f"{text} (yours, {fr.user_reason})" if fr.user_reason else f"{text} (yours)"

    # ---- rebuilding ------------------------------------------------------------------------
    def _on_changed(self) -> None:
        self._worker_done = False
        self._rebuild()

    def _on_worker(self) -> None:
        self._worker_done = True
        self._rebuild()

    def _rebuild(self) -> None:
        intake = self._intake
        root, att = intake.root, intake.attention
        if root != self._root:                      # another experiment: nothing of the last one stays
            if self._root is not None:
                self._decided = False               # the choice was that experiment's; one made unbound (restored) carries
            self._root = root
            self._open.clear()
            self._open_scans.clear()
            self._pending_reveal = None
            for key in list(self._groups):
                self._drop_group(key)
        specs = self._specs(att) if root is not None else []
        self._set_header(att, bool(specs), root)

        keys = [s.key for s in specs]
        for key in [k for k in self._groups if k not in keys]:
            self._drop_group(key)
        bar = self._scroll.verticalScrollBar()
        place = bar.value()
        for i, spec in enumerate(specs):
            group = self._groups.get(spec.key)
            if group is None:
                group = self._groups[spec.key] = _Group(self, spec)
            else:
                self._column.removeWidget(group)
            self._column.insertWidget(i, group)     # the backend's order, then the user's groups
            members = {r.id for r in spec.rows}
            self._ticks[spec.key] = self._ticks.get(spec.key, set()) & members      # a member that left is unticked
            group.refresh(spec, open_=spec.key in self._open,
                          open_scans=frozenset(self._open_scans.get(spec.key, ())))
            self._sync_group(spec.key)
            self._sync_foot(spec.key)
        self._content.layout().activate()
        bar.setValue(place)

        if att is not None and att.groups and root not in self._auto_seen:
            self._auto_seen.add(root)               # the first time this experiment shows gaps
            if not self._decided:
                self._apply_expanded(True)
        self._show_body()
        if self._pending_reveal in self._groups:
            self.reveal(self._pending_reveal)

    def _drop_group(self, key: str) -> None:
        group = self._groups.pop(key)
        self._column.removeWidget(group)
        group.setParent(None)
        group.deleteLater()
        self._ticks.pop(key, None)
        self._open.discard(key)
        self._open_scans.pop(key, None)

    def _set_header(self, att, has_body: bool, root) -> None:
        tip, warn = "", False
        if root is None:
            text = ""
        elif att is None and self._worker_done and self._intake.error:
            tip, warn = self._intake.error, True        # the read failed: say why, never "checking…" forever
            text = f"Needs attention · not checked: {tip}"
        elif att is None:
            text = "Needs attention · checking…"
        elif not att.evaluated:
            text, tip = "Needs attention", att.note or ""
        elif att.groups:
            text = f"Needs attention · {_n(len(att.groups), 'item')}"
        else:
            text = "Needs attention · nothing"
        self._header.setText(text)
        self._header.setStyleSheet(_WARN if warn else "")
        self._header.setToolTip(tip or (_HEADER_TIP if text else ""))
        self._header.setVisible(bool(text))
        self._header.setEnabled(has_body)
        self._has_body = has_body

    def _show_body(self) -> None:
        shown = self._expanded and self._has_body
        self._header.setArrowType(Qt.ArrowType.DownArrow if shown else Qt.ArrowType.RightArrow)
        self._header.setAccessibleName(("Collapse " if self._expanded else "Expand ") + "the needs-attention list")
        self._scroll.setVisible(shown)

    # ---- ticks -----------------------------------------------------------------------------
    def ticked(self, key: str) -> list[str]:
        """The ticked member ids of a group, in the group's order."""
        group, ticks = self._groups.get(key), self._ticks.get(key, set())
        return [] if group is None else [r.id for r in group.spec.rows if r.id in ticks]

    def _set_ticks(self, key: str, ids, checked: bool) -> None:
        ticks = self._ticks.setdefault(key, set())
        ticks.update(ids) if checked else ticks.difference_update(ids)
        self._sync_group(key)
        self._sync_foot(key)

    def _tick(self, key: str, member: str, checked: bool) -> None:
        self._set_ticks(key, [member], checked)

    def _tick_scan(self, key: str, scan: str, checked: bool) -> None:
        self._set_ticks(key, [r.id for r in self._groups[key].spec.rows if r.parent == scan], checked)

    def _tick_all(self, key: str, checked: bool) -> None:
        self._set_ticks(key, [r.id for r in self._groups[key].spec.rows], checked)

    def _sync_group(self, key: str) -> None:
        """Set every box of the open group from the ticks (tri-state parents from their members)."""
        group, ticks = self._groups[key], self._ticks.get(key, set())

        def put(box: QCheckBox, ids: list[str]) -> None:
            n = sum(i in ticks for i in ids)
            box.blockSignals(True)
            box.setCheckState(Qt.CheckState.Unchecked if n == 0 else
                              Qt.CheckState.Checked if n == len(ids) else Qt.CheckState.PartiallyChecked)
            box.blockSignals(False)

        for member, box in group.boxes.items():
            put(box, [member])
        for scan, box in group.scan_boxes.items():
            put(box, [r.id for r in group.spec.rows if r.parent == scan])
        if group.all_box is not None:
            put(group.all_box, [r.id for r in group.spec.rows])

    def _preview(self, key: str) -> str:
        group, ids = self._groups[key], self.ticked(key)
        if group.spec.mode == "unit":
            scans = {r.parent for r in group.spec.rows if r.id in ids}
            return f"Applies to {_n(len(ids), 'analyzer')} in {_n(len(scans), 'scan set')}"
        return f"Applies to {_n(len(ids), 'scan set')}: {', '.join(self._label(i) for i in ids)}"

    def _sync_foot(self, key: str) -> None:
        group = self._groups.get(key)
        if group is None or group.foot is None:
            return
        foot, ids = group.foot, self.ticked(key)
        foot.setVisible(bool(ids) and key in self._open)
        hint = _TICK_HINT.get(group.spec.foot, "")
        if group.spec.counts:
            hint = "Tick the analyzers above; then accept their counts as they are, or leave them out."
        group.hint.setText(hint)
        group.hint.setVisible(key in self._open and not ids and bool(hint))
        if not ids:
            return
        foot.preview.set_full_text(self._preview(key))
        if foot.entry is not None:
            ok = foot.entry.value() is not None
            save = foot.buttons["save"]
            save.setEnabled(ok)
            error = foot.entry.parse_error()
            why = "" if ok else (error or f"Enter the {foot.entry.fact} to save it.")
            save.setToolTip(_BUTTON_TIPS["save"] if ok else why)
            foot.reason_note.setText(why)
            foot.reason_note.setVisible(bool(why) and error is None)       # a parse error shows under the entry

    # ---- acting ----------------------------------------------------------------------------
    def _fix(self, key: str) -> None:
        """The group's own remedy button: the window's route for it."""
        group = self._groups.get(key)
        if group is not None and group.spec.fix is not None:
            getattr(self, _FIX_BUTTONS[group.spec.fix][2]).emit()

    def _act(self, key: str, action: str) -> None:
        group, ids = self._groups.get(key), self.ticked(key)
        if group is None or group.foot is None or not ids:
            return
        foot, intake, fact, reason = group.foot, self._intake, group.spec.fact, group.foot.reason()
        if action == "save":
            value = foot.entry.value()
            if value is None:
                return
            outcome = intake.save_value(fact, ids, value, reason)
        elif action == "file_value":
            outcome = intake.use_file_value(fact, ids, reason)
        elif action in ("dead", "in_use"):
            outcome = intake.set_dead(ids, action == "dead", reason)
        elif action in ("accept", "withdraw"):
            outcome = intake.set_counts_accepted(ids, action == "accept", reason)
        else:
            raise ValueError(f"unknown attention action {action!r}")
        if self._groups.get(key) is not group:        # the re-read already removed the group
            return
        if outcome.ok:
            self._ticks[key] = set()                   # a refused save keeps the ticks and the typed text
            foot.clear()
            self._sync_group(key)
            self._sync_foot(key)
        else:
            foot.show_refusal(outcome.message)
