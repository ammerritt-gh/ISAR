"""ReadinessLine -- the Processing panel's line above *Run analysis…*: what can start, and what is missing.

One wrapped line from the intake backend's readiness read, for the fit use only::

    56 spectra read · 52 ready for analysis (30 will use a stand-in value) · 4 need resolution curves · 3 running

Every count but the first is a link that opens a small non-modal list of the spectra behind it (scan set
and analyzer), the backend's own reason for each, and one button for the action that resolves it:
*Choose resolution-curve folder…*, *Enter the value…*, *Add data…* and *Show these in the list* leave through
the signals the window routes to the resolution-folder route, the add-files route and the needs-attention list;
*Run analysis…* asks for today's Run dialog.

The line counts spectra by what the backend says (``can_start``, ``running`` and ``action`` of each
analyzer's ``fit`` use) and decides nothing; an analyzer whose file never arrived is not a spectrum read, so it
is no category here (the setup section counts it as an incomplete set): a fact gap that does not stop fitting (a missing Q, action
``none``) is not a block here, because it affects steering and the plane view only. Without a readiness
read (not a BL43LXU experiment, or nothing imported) the line says only how many spectra were read, with the
backend's reason on hover; before the first result it says it is checking, and when the worker's read failed
it says so in the warning colour (the error on hover), as the needs-attention list does.
"""
from __future__ import annotations

import html
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QSizePolicy, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from isar_gui.plain_words import plain_hold
from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.views.setup_section import LOCATE_LONG

# action -> (one spectrum, several spectra), in the order the blocked counts are listed
_BLOCKED = (
    ("locate_folder", "needs resolution curves", "need resolution curves"),
    ("import_again", "needs its file added again", "need their files added again"),
    ("accept_counts", "needs its counts accepted", "need their counts accepted"),
    ("enter_value", "needs a value", "need a value"),
    ("reverse_declaration", "left out by you", "left out by you"),
    ("tell_operator", "needs you to decide", "need you to decide"),
)
_PHRASES = {action: (one, many) for action, one, many in _BLOCKED}
_BUTTONS = {"locate_folder": LOCATE_LONG, "enter_value": "Enter the value…", "accept_counts": "Show these in the list",
            "reverse_declaration": "Show these in the list", "start_analysis": "Run analysis…",
            "import_again": "Add data…"}
_CLICK_HINT = "Click a count in blue to list the spectra behind it and the action that fixes it."
_KIND_OF_ACTION = {"accept_counts": "counts_unaccepted", "reverse_declaration": "declared"}
_WARN = f"color: {TONE_COLOR[Tone.WARN]};"
_POPUP_ROWS = 8                    # an opened list shows this many rows, then scrolls
READY, RUNNING = "ready", "running"


@dataclass(frozen=True)
class Row:
    scan: str
    analyzer: str
    reason: str
    action: str


def classify(attention, missing: frozenset = frozenset()) -> dict[str, list[Row]]:
    """The spectra of a readiness read by what the fit use says, in the order the line lists them:
    ``"ready"`` (``can_start`` True), ``"blocked:<action>"`` or ``"other:<reason>"`` (``can_start`` False) and
    ``"running"``. ``attention`` is a ``NeedsAttention`` with ``evaluated`` True; ``missing`` holds the
    (scan, analyzer) pairs whose file never arrived, which are left out."""
    ready: list[Row] = []
    running: list[Row] = []
    blocked: dict[str, list[Row]] = {}
    for scan in attention.scans.values():
        for a in scan.analyzers.values():
            if (scan.scan, a.analyzer) in missing:
                continue
            use = a.uses.get("fit")
            row = Row(scan.scan, a.analyzer, (use.reason if use is not None and use.reason else ""), a.action)
            if a.running:
                running.append(row)
            elif a.can_start is True:
                ready.append(row)
            elif a.can_start is False:
                key = f"blocked:{a.action}" if a.action in _PHRASES else f"other:{row.reason or 'not ready'}"
                blocked.setdefault(key, []).append(row)
    ordered = {READY: ready}
    for action, _one, _many in _BLOCKED:
        if f"blocked:{action}" in blocked:
            ordered[f"blocked:{action}"] = blocked.pop(f"blocked:{action}")
    ordered.update(sorted(blocked.items()))
    ordered[RUNNING] = running
    return ordered


def with_fallback(attention) -> int:
    """How many ready spectra run on a fallback for the fit use."""
    return sum(1 for s in attention.scans.values() for a in s.analyzers.values()
               if a.can_start is True and not a.running and a.uses.get("fit") is not None
               and a.uses["fit"].state == "fallback")


def _phrase(key: str, count: int) -> str:
    if key == READY:
        return "ready for analysis"
    if key == RUNNING:
        return "running"
    kind, _, rest = key.partition(":")
    if kind == "blocked":
        one, many = _PHRASES[rest]
        return one if count == 1 else many
    return f"not ready: {rest}"


class _Wrapped(QLabel):
    """A wrapped line whose width never pins its column."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("", parent)
        self.setWordWrap(True)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def minimumSizeHint(self):  # noqa: N802 (Qt override)
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint


class _Popup(QDialog):
    """The spectra behind one count: scan set / analyzer and the backend's reason, then the resolving buttons."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("readiness_popup")
        self.setModal(False)                 # a list to read while the window stays usable
        self.setMinimumWidth(360)
        self._title = QLabel("", self)
        self._title.setObjectName("readiness_popup_title")
        self.table = QTableWidget(0, 2, self)
        self.table.setObjectName("readiness_popup_rows")
        self.table.setHorizontalHeaderLabels(["Spectrum", "Reason"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setTextElideMode(Qt.TextElideMode.ElideMiddle)
        self.table.setAccessibleName("Spectra behind this count")
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.buttons = QHBoxLayout()
        close = QPushButton("Close", self)
        close.setObjectName("readiness_popup_close")
        close.clicked.connect(self.hide)
        layout = QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self.table, 1)
        row = QHBoxLayout()
        row.addLayout(self.buttons, 1)
        row.addWidget(close)
        layout.addLayout(row)

    def fill(self, title: str, rows: list[tuple[str, str]], buttons: list[tuple[str, str, object]],
             full: list[str] | None = None) -> None:
        """``rows``: (spectrum, reason); ``buttons``: (object name, text, callback); ``full``: each reason's whole
        backend text, for the hover."""
        self.setWindowTitle(title)
        self._title.setText(title)
        self.table.setRowCount(len(rows))
        for i, (spectrum, reason) in enumerate(rows):
            for column, text in enumerate((spectrum, reason)):
                item = QTableWidgetItem(text)
                item.setToolTip(text if full is None or column == 0 else full[i])
                self.table.setItem(i, column, item)
        self.table.setColumnWidth(0, 190)
        shown = min(len(rows), _POPUP_ROWS)
        self.table.setMinimumHeight(self.table.horizontalHeader().height() + 2 * self.table.frameWidth()
                                    + shown * max(self.table.verticalHeader().defaultSectionSize(), 1))
        while self.buttons.count():
            item = self.buttons.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        for name, text, callback in buttons:
            button = QPushButton(text, self)
            button.setObjectName(name)
            button.clicked.connect(callback)
            self.buttons.addWidget(button)


class ReadinessLine(QWidget):
    reveal_gap = Signal(str)         # group key -> AttentionList.reveal
    locate_requested = Signal()      # -> locate_resolution_folder
    run_requested = Signal()         # -> MainWindow.run_analysis
    add_data_requested = Signal()    # -> FilesStrip.choose_files

    def __init__(self, intake, labels: Callable[[str], str] | None = None, parent: QWidget | None = None) -> None:
        """``labels``: scan key -> the scan set's label (the window passes the scan list's, read when a list is
        filled); the key stands in when none is given."""
        super().__init__(parent)
        self.setObjectName("readiness_line")
        self._intake = intake
        self._label_of = labels or (lambda scan: scan)
        self._links: dict[str, str] = {}          # href -> the classify() key behind it
        self._plain = ""
        self._open_key: str | None = None         # the count whose list is open
        self._worker_done = False                 # the worker answered since the last reload (changed)
        self._ready: int | None = None            # spectra that can start; None: no readiness read applies
        self._blockers = ""                       # "9 need resolution curves · ..." (the blocked counts)
        self._checking = False                    # the first read has not answered yet
        self._label = _Wrapped(self)
        self._label.setObjectName("readiness_text")
        self._label.setTextFormat(Qt.TextFormat.RichText)
        self._label.setAccessibleName("Readiness for analysis")
        self._label.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse
                                            | Qt.TextInteractionFlag.LinksAccessibleByKeyboard)
        self._label.linkActivated.connect(self._open)
        self._popup = _Popup(self)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._label)
        intake.changed.connect(self._on_changed)
        intake.worker_changed.connect(self._on_worker)
        self._render()

    # ---- rendering -----------------------------------------------------------------------
    @property
    def text(self) -> str:
        """The line as plain text (what a screen reader and the tests read)."""
        return self._plain

    @property
    def ready_count(self) -> int | None:
        """Spectra the backend says can start; ``None`` when no readiness read applies (not a BL43LXU experiment,
        nothing imported, the read failed or it is still checking)."""
        return self._ready

    @property
    def blockers(self) -> str:
        """The blocked counts as one phrase ("9 need resolution curves · 1 needs a value"), or ""."""
        return self._blockers

    @property
    def checking(self) -> bool:
        return self._checking

    def _on_changed(self) -> None:
        self._worker_done = False
        self._render()

    def _on_worker(self) -> None:
        self._worker_done = True
        self._render()

    def _render(self) -> None:
        intake = self._intake
        summary, att = intake.summary, intake.attention
        self._links, self._plain = {}, ""
        self._ready, self._blockers, self._checking = None, "", False
        warn = False
        if intake.root is None or summary is None:
            self._label.setText("")
            self._label.setToolTip("")
            self._label.setStyleSheet("")
            self.setVisible(False)
            self._close_popup()
            return
        read = f"{summary.n_analyzers} {'spectrum' if summary.n_analyzers == 1 else 'spectra'} read"
        parts, plain, tip = [html.escape(read)], [read], ""
        if att is None and self._worker_done and intake.error:
            tip, warn = intake.error, True                  # the read failed: say why, never "checking…" forever
            parts.append(html.escape(f"not checked: {tip}"))
            plain.append(f"not checked: {tip}")
        elif att is None:
            self._checking = True
            parts.append("checking readiness…")
            plain.append("checking readiness…")
        elif not att.evaluated:
            tip = att.note or ""
        else:
            fallback = with_fallback(att)
            blocked = []
            for i, (key, rows) in enumerate(classify(att, self._missing()).items()):
                if key == READY:
                    self._ready = len(rows)
                elif rows and key != RUNNING:
                    blocked.append(f"{len(rows)} {_phrase(key, len(rows))}")
                if not rows and key != READY:
                    continue
                words = f"{len(rows)} {_phrase(key, len(rows))}"
                if key == READY and fallback:
                    words += f" ({fallback} will use a stand-in value)"
                plain.append(words)
                if rows:
                    self._links[f"g{i}"] = key
                    parts.append(f'<a href="g{i}">{html.escape(words)}</a>')
                else:
                    parts.append(html.escape(words))
            self._blockers = " · ".join(blocked)
        self._plain = " · ".join(plain)
        self._label.setText(" · ".join(parts))
        self._label.setToolTip(tip or (f"{self._plain}\n\n{_CLICK_HINT}" if self._links else self._plain))
        self._label.setStyleSheet(_WARN if warn else "")
        self.setVisible(True)
        self._refresh_popup(att)

    # ---- the list behind a count -----------------------------------------------------------
    def _missing(self) -> frozenset:
        """The (scan, analyzer) pairs the import record lists as having no file or an unreadable one."""
        summary = self._intake.summary
        return frozenset() if summary is None else frozenset(
            (s.scan, a) for s in summary.incomplete_sets for a in s.missing_analyzers)

    def _open(self, href: str) -> None:
        key = self._links.get(href)
        if key is None or self._intake.attention is None:
            return
        self._open_key = key
        self._refresh_popup(self._intake.attention, raise_=True)

    def _close_popup(self) -> None:
        self._open_key = None
        self._popup.hide()

    def _refresh_popup(self, att, raise_: bool = False) -> None:
        """Show (or re-fill, if already open) the list of ``_open_key``; close it when its spectra are gone."""
        key = self._open_key
        if key is None or not (raise_ or self._popup.isVisible()):
            return
        rows = classify(att, self._missing()).get(key) if att is not None and att.evaluated else None
        if not rows:
            self._close_popup()
            return
        title = f"{len(rows)} {_phrase(key, len(rows))}"
        shown = [(f"{self._label_of(r.scan)} / {r.analyzer}", plain_hold(r.reason) or "ready") for r in rows]
        self._popup.fill(title, shown, self._buttons(rows, att), [r.reason or "ready" for r in rows])
        self._popup.show()
        if raise_:
            self._popup.raise_()

    def _buttons(self, rows: list[Row], att) -> list[tuple[str, str, object]]:
        actions = []
        for r in rows:
            if r.action in _BUTTONS and r.action not in actions:
                actions.append(r.action)
        buttons = []
        for action in actions:
            if action == "start_analysis":
                callback = self._then(self.run_requested.emit)
            elif action == "import_again":
                callback = self._then(self.add_data_requested.emit)
            elif action == "locate_folder":
                callback = self._then(self.locate_requested.emit)
            else:
                kind = self._kind(action, {r.scan for r in rows if r.action == action}, att)
                if kind is None:
                    continue
                callback = self._then(lambda k=kind: self.reveal_gap.emit(k))
            buttons.append((f"readiness_{action}", _BUTTONS[action], callback))
        return buttons

    @staticmethod
    def _kind(action: str, scans: set[str], att) -> str | None:
        """The attention-list group that holds the action: the first group with it whose items include one
        of ``scans``, else the first with it at all; counts and declarations have a group of their own."""
        if action in _KIND_OF_ACTION:
            return _KIND_OF_ACTION[action]
        first = None
        for g in att.groups:
            if g.action != action:
                continue
            if any(i.scan in scans for i in g.items):
                return g.kind
            first = first or g.kind
        return first

    def _then(self, emit):
        def run(*_args) -> None:
            self._close_popup()
            emit()
        return run
