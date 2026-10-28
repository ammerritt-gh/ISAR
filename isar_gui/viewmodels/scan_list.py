"""ScanListViewModel -- the scan list's snapshot, current spectrum, selection rule and tree.

Holds the :class:`~isar.workspace_api.scan_list.ScanListSnapshot` of the open experiment (re-read on a
bind and on a workspace change) and the stable key of the current spectrum. Identity is always a
spectrum's unit key, never a position or a display label, so regrouping or a background update keeps
the selection.

The view model also turns the snapshot, the sort, the filter and the visible columns into a plain
tree of :class:`Node`; the panel reconciles its items to that tree by node id. A change of sort,
filter or columns emits ``snapshot_changed`` like any other rebuild.

No feedback loops: ``set_workspace``, ``on_workspace_changed`` and ``set_active_key`` never emit
``display_requested`` or ``inspect_requested``. Only :meth:`select` asks for the display, so a
background refresh never moves the central page under the user.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Signal

from isar.workspace_api import ImportSummary
from isar.workspace_api.scan_list import (Fact, PooledRow, ScanListSnapshot, ScanSetRow, SpectrumRow,
                                          UnreadFile, natural_key, scan_list)
from isar_gui import status_words
from isar_gui.plain_words import drop_echo, plain_reason
from isar_gui.theme import DEEP, FAST, PARSED, Tone, status_tone, summary_tone
from isar_gui.viewmodels.base import ViewModel

COLUMNS = ("scan", "T", "Q", "parsed", "fast", "deep", "slit", "file")   # model order
OPTIONAL_COLUMNS = ("fast", "deep", "slit", "file")                      # Columns ▾; the rest always show
RESULT_COLUMNS = ("fast", "deep")     # these switch on by themselves when the first result of that pass exists
TITLES = {"scan": "Scan", "T": "T", "Q": "Q", "parsed": "Parsed", "fast": "Fast pass",
          "deep": "Deep pass", "slit": "Slit", "file": "File name"}
SORTS = ("set", "temperature", "analyzer")
# What a column holds, as the header's hover (E1/E2: the result columns are another batch's).
HEADER_TIPS = {
    "scan": "The scan set (one scan, one file per analyzer) and, under it, each analyzer's spectrum.",
    "T": "Sample temperature, as the files give it (or as you entered it).",
    "Q": "Momentum transfer of the spectrum, in the units the file gives.",
    "parsed": ("Whether ISAR read the file. On a scan set, files read of files received. 'no T' and 'no Q' mean "
               "the file was read but holds no temperature or no Q."),
    "slit": "Slit opening of the scan set, as the files give it (or as you entered it).",
    "file": "The file this spectrum was read from.",
}
_STATE_KEY = "scan_list"
_DASH = "—"
_TEMP_TERM = re.compile(r"(\d+(?:\.\d+)?)\s*k", re.IGNORECASE)
_NO_SUMMARY = ImportSummary(0, 0, (), (), {}, None, None)     # nothing imported yet: the ledger's lists stand


@dataclass(frozen=True)
class Cell:
    text: str
    tooltip: str | None = None        # None: the view shows the text itself
    tone: Tone | None = None          # a status dot; None for plain text
    stale: bool = False               # the pass's inputs changed since it was made: the word is drawn in WARN


@dataclass(frozen=True)
class Node:
    id: str        # "set:<set_key>" | "ana:<analyzer>" | "spec:<key>" | "section:not_read" |
                   # "file:<path>" | "section:pooled" | "pool:<key>"
    kind: str      # "set" | "analyzer" | "spectrum" | "section" | "file" | "pooled"
    key: str | None                   # the selectable identity (spectrum or pooled key), else None
    cells: dict                       # column id -> Cell
    children: tuple["Node", ...] = ()


# ---- cell formatting ----------------------------------------------------------------------
def _num(value) -> str:
    return f"{value:g}"


def _fact_text(fact: Fact, with_unit: bool = True) -> str | None:
    """The fact as text, or ``None`` when it is missing."""
    v = fact.value
    if v is None:
        return None
    if isinstance(v, tuple):
        text = ", ".join(_num(x) for x in v)
    elif isinstance(v, (int, float)):
        text = _num(v)
    else:
        text = str(v)
    return f"{text} {fact.unit}" if with_unit and fact.unit else text


def _fact_cell(fact: Fact, with_unit: bool = True) -> Cell:
    text = _fact_text(fact, with_unit)
    if text is None:                  # a missing value is a dash with its reason, never blank
        return Cell(_DASH, fact.reason or "not available")
    return Cell(text, f"{_fact_text(fact)} (from {fact.source})")


def _status_cell(status, column: str) -> Cell:
    reason = drop_echo(status.word, status.reason)            # "resolved: resolved: ..." said the word twice
    plain = status_words.plain_word(column, status.word, status.reason, status.provisional)
    # the plain word, the fitting code's word, its meaning
    tip = status_words.tooltip(column, status.word, reason, status.provisional)
    stale = status.current is False
    if stale:
        lines = "\n".join(f"{name}: {used} → {now}" for name, used, now in status.changed)
        tip = f"Inputs changed since this {column} pass:\n{lines}" + (f"\n{tip}")
    return Cell(plain, tip, status_tone(column, status.word), stale)


def _summary_cell(stored: int, total: int, tones, what: str, statuses=(), column: str = "") -> Cell:
    """The ``11/28`` cell of a set or an analyzer; ``statuses`` are its spectra's passes, any of which
    may have changed inputs."""
    tip = f"{stored} of {total} {what}"
    stale = sum(s.current is False for s in statuses)
    if stale:
        tip += f"\n{stale} of {stored} {column}-pass results have changed inputs"
    return Cell(f"{stored}/{total}", tip, summary_tone(tones), stale > 0)


def _temperature(fact: Fact) -> float | None:
    v = fact.value
    return float(v) if isinstance(v, (int, float)) else None


# ---- the filter: a pure function over comma-separated terms --------------------------------
def filter_terms(text: str) -> list[str]:
    return [t.strip().casefold() for t in (text or "").split(",") if t.strip()]


def _term_hits(term: str, temperature: Fact, haystack) -> bool:
    m = _TEMP_TERM.fullmatch(term)
    t = _temperature(temperature)
    if m and t is not None and abs(t - float(m.group(1))) <= 0.5:
        return True
    return any(term in (h or "").casefold() for h in haystack)


def _status_words(row) -> list[str]:
    """Every word the filter matches for a row's passes: the fitting code's word and the plain word shown."""
    out = []
    for column in (PARSED, FAST, DEEP):
        status = getattr(row, column, None)
        if status is not None:
            out += [status.word, status_words.plain_word(column, status.word, status.reason, status.provisional)]
    return out


def spectrum_matches(terms, ss: ScanSetRow, sp: SpectrumRow) -> bool:
    hay = (sp.analyzer, ss.label, sp.file_name, *_status_words(sp))
    return all(_term_hits(t, ss.temperature, hay) for t in terms)


def pooled_matches(terms, p: PooledRow) -> bool:
    hay = (p.label, "pooled", *_status_words(p))
    return all(_term_hits(t, p.temperature, hay) for t in terms)


def file_matches(terms, f: UnreadFile) -> bool:
    hay = (f.name, f.reason, "not read")
    return all(any(t in h.casefold() for h in hay) for t in terms)


class ScanListViewModel(ViewModel):
    snapshot_changed = Signal()         # after every rebuild: bind, background update, sort/filter/columns
    display_requested = Signal(object)  # FitView | str unit key | None: MainWindow._route_inspect's payload
    current_changed = Signal(object)    # str key | None: the list's current spectrum or pooled row
    inspect_requested = Signal(str)     # open the inspector column on this key
    set_focused = Signal(object)        # str set key | None: a scan-set row was picked (no spectrum is meant)
    selection_changed = Signal(tuple)   # tuple[str, ...]: the selected keys, list order (C-selection)

    def __init__(self, ws, parent=None) -> None:
        super().__init__(ws, parent)
        self._snapshot: ScanListSnapshot = scan_list(ws)
        self._current_key: str | None = None
        self._focus_set: str | None = None    # the scan set whose row the user picked, until a spectrum is
        self._sort = "set"
        self._optional: set[str] = set()      # the optional columns the user switched on
        self._hidden: set[str] = set()        # result columns the user switched off: the auto rule leaves them off
        self._auto: set[str] = set()          # result columns on because a result of that pass exists
        self._filter_text = ""
        self._intake = None
        self._auto = self._auto_columns()

    # ---- reads ---------------------------------------------------------------------------
    @property
    def snapshot(self) -> ScanListSnapshot:
        return self._snapshot

    @property
    def current_key(self) -> str | None:
        return self._current_key

    @property
    def focus_set(self) -> str | None:
        return self._focus_set

    def selected_keys(self) -> tuple[str, ...]:
        """The selected spectrum or pooled keys, list order (C-selection). Today's one selection: the current
        key, and none while a scan-set row is focused, so a panel never names a spectrum the list no longer
        highlights."""
        return () if self._current_key is None or self._focus_set is not None else (self._current_key,)

    def select_keys(self, keys) -> None:
        """Select ``keys`` as a user click would (with ``select``'s signals). One selection for now: the first."""
        keys = tuple(keys)
        self.select(keys[0] if keys else None)

    def set_queue(self, controller) -> None:
        """The analysis queue controller whose marks the list shows (filled by the queue-marks unit)."""

    def _emit_selection(self) -> None:
        self.selection_changed.emit(self.selected_keys())

    @property
    def sort(self) -> str:
        return self._sort

    @property
    def filter_text(self) -> str:
        return self._filter_text

    @property
    def filter_active(self) -> bool:
        return bool(filter_terms(self._filter_text))

    def source_folders(self) -> list[str]:
        """The folders the experiment's data files were read from (the manifest's paths), first-seen order."""
        snap = self._snapshot
        paths = [sp.file_path for ss in snap.sets for sp in ss.spectra if sp.file_path]
        paths += [f.path for f in snap.files_not_read]
        return list(dict.fromkeys(str(Path(p).parent) for p in paths))

    def visible_columns(self) -> tuple[str, ...]:
        return tuple(c for c in COLUMNS if c not in OPTIONAL_COLUMNS or c in self._optional or c in self._auto)

    def layout_choices(self) -> tuple:
        """What the user chose about the layout; a change of it reveals the selection. A column a stored pass
        switches on by itself is not a choice, so its arrival does not move the list."""
        return (self._sort, self._filter_text, frozenset(self._optional), frozenset(self._hidden))

    def _auto_columns(self) -> set[str]:
        """The result columns to show by themselves: a stored record of that pass exists, and the user has not
        switched the column off."""
        snap = self._snapshot
        rows = [sp for ss in snap.sets for sp in ss.spectra] + list(snap.pooled)
        return {c for c in RESULT_COLUMNS if c not in self._hidden and any(getattr(r, c).stored for r in rows)}

    def column_title(self, column: str) -> str:
        """The header; ``Q`` and ``T`` carry their unit when every present value has the same one."""
        if column in ("Q", "T"):
            unit = self._common_q_unit() if column == "Q" else self._common_t_unit()
            if unit:
                return f"{column} ({unit})"
        return TITLES[column]

    def header_tip(self, column: str) -> str:
        return HEADER_TIPS.get(column, "")

    def _common_t_unit(self) -> str | None:
        units = {ss.temperature.unit for ss in self._snapshot.sets if ss.temperature.value is not None}
        units |= {p.temperature.unit for p in self._snapshot.pooled if p.temperature.value is not None}
        return next(iter(units)) if len(units) == 1 else None

    def _common_q_unit(self) -> str | None:
        qs = [sp.q for ss in self._snapshot.sets for sp in ss.spectra]
        qs += [p.q for p in self._snapshot.pooled]
        units = {q.unit for q in qs if q.value is not None}
        return next(iter(units)) if len(units) == 1 else None

    # ---- the view settings ---------------------------------------------------------------
    def set_sort(self, sort: str) -> None:
        if sort in SORTS and sort != self._sort:
            self._sort = sort
            self.snapshot_changed.emit()

    def set_filter(self, text: str) -> None:
        text = text or ""
        if text != self._filter_text:
            self._filter_text = text
            self.snapshot_changed.emit()

    def set_column_visible(self, column: str, visible: bool) -> None:
        if column in OPTIONAL_COLUMNS and visible != (column in self.visible_columns()):
            if visible:
                self._optional.add(column)
                self._hidden.discard(column)
            else:                         # a hand choice: remembered, and the auto rule no longer overrides it
                self._optional.discard(column)
                self._hidden.add(column)
            self._auto = self._auto_columns()
            self.snapshot_changed.emit()

    def set_intake(self, intake) -> None:
        """The window's IntakeState: every re-read passes its fact rows, fit inputs and import summary to
        the scan-list read, so this thread never parses the fit-inputs log. The marks update on its
        ``worker_changed`` (the window re-reads on both of its signals)."""
        self._intake = intake

    # ---- the workspace changed -----------------------------------------------------------
    def set_workspace(self, ws) -> None:
        """Rebind: re-read, keep ``current_key`` only if it still exists, emit ``snapshot_changed``."""
        self._ws = ws
        self._reread()

    def on_workspace_changed(self, changed=None) -> None:
        """Re-read after the facade refresh; emits ``snapshot_changed``; never ``display_requested``."""
        self._reread()

    def _reread(self) -> None:
        intake = self._intake
        reads = {} if intake is None else {
            "fact_rows": intake.fact_rows or (), "fit_inputs": intake.fit_inputs or (),
            "import_summary": intake.summary or _NO_SUMMARY}      # None (not read yet): no marks, no lists
        self._snapshot = scan_list(self._ws, **reads)
        self._auto = self._auto_columns()
        cleared = self._current_key is not None and self._snapshot.spectrum(self._current_key) is None
        if cleared:
            self._current_key = None
        lost_set = self._focus_set is not None and self._snapshot.scan_set(self._focus_set) is None
        if lost_set:
            self._focus_set = None
        self.snapshot_changed.emit()
        if cleared:
            self.current_changed.emit(None)
        if lost_set:
            self.set_focused.emit(None)
        if cleared or lost_set:
            self._emit_selection()

    # ---- selection -----------------------------------------------------------------------
    def select(self, key: str | None) -> None:
        """User selection: set current, emit ``current_changed`` and ``display_requested``.

        A fitted spectrum hands its ``FitView`` to the display, any other key its unit key, and
        ``None`` clears the display: today's rule of ``ScansViewModel.request_inspect``."""
        self._current_key = key
        self._focus_set = None
        self.current_changed.emit(key)
        self._emit_selection()
        row = None if key is None else self._snapshot.spectrum(key)
        fit = None if row is None else row.fit
        self.display_requested.emit(key if fit is None else fit)

    def select_set(self, set_key: str) -> None:
        """The user picked a scan-set row: the inspector (when open) summarises the set. The current spectrum
        and the display stay as they are; ``select`` of a spectrum ends this."""
        if set_key != self._focus_set and self._snapshot.scan_set(set_key) is not None:
            self._focus_set = set_key
            self.set_focused.emit(set_key)
            self._emit_selection()

    def inspect(self, key: str) -> None:
        """Select ``key`` if it is not current, then ask for the inspector column."""
        if key != self._current_key:
            self.select(key)
        self.inspect_requested.emit(key)

    def set_active_key(self, key: str | None) -> None:
        """The active unit changed elsewhere (``ActiveUnitController``): follow it, ``current_changed``
        only, and only when it differs. A key the snapshot does not hold leaves the current one."""
        if key == self._current_key or (key is not None and self._snapshot.spectrum(key) is None):
            return
        self._current_key = key
        self._focus_set = None
        self.current_changed.emit(key)
        self._emit_selection()

    # ---- what the filter lets through -----------------------------------------------------
    def _sorted_sets(self) -> list[ScanSetRow]:
        sets = self._snapshot.sets
        if self._sort == "temperature":
            def by_t(s):
                t = _temperature(s.temperature)
                return (t is None, t or 0.0, natural_key(s.label), s.key)
            return sorted(sets, key=by_t)
        return sorted(sets, key=lambda s: (natural_key(s.label), s.key))

    def _visible(self):
        """``(sets with their matching spectra, matching unread files, matching pooled rows)``."""
        terms = filter_terms(self._filter_text)
        sets = []
        for ss in self._sorted_sets():
            spectra = [sp for sp in ss.spectra if spectrum_matches(terms, ss, sp)]
            if spectra or not terms:
                sets.append((ss, spectra))
        files = [f for f in self._snapshot.files_not_read if file_matches(terms, f)]
        pooled = [p for p in self._snapshot.pooled if pooled_matches(terms, p)]
        return sets, files, pooled

    def counts(self) -> tuple[int, int]:
        """``(visible, total)`` spectra; pooled masters are not counted."""
        sets, _files, _pooled = self._visible()
        return sum(len(spectra) for _ss, spectra in sets), self._snapshot.n_spectra

    def outside_filter(self) -> str | None:
        """The one-line notice when the current spectrum does not pass the filter, else ``None``."""
        key = self._current_key
        row = None if key is None or not self.filter_active else self._snapshot.spectrum(key)
        if row is None:
            return None
        sets, _files, pooled = self._visible()
        if any(sp.key == key for _ss, spectra in sets for sp in spectra) or \
                any(p.key == key for p in pooled):
            return None
        if isinstance(row, PooledRow):
            name = row.label
        else:
            ss = self._snapshot.scan_set(row.set_key)
            name = f"{row.analyzer or _DASH}, {ss.label if ss else row.set_key}"
        return f"The selected spectrum ({name}) is outside the filter"

    # ---- the tree ------------------------------------------------------------------------
    def tree(self) -> tuple[Node, ...]:
        sets, files, pooled = self._visible()
        with_unit = self._common_q_unit() is None     # the header carries a shared unit
        nodes = (self._analyzer_nodes(sets, with_unit) if self._sort == "analyzer"
                 else [self._set_node(ss, spectra, with_unit) for ss, spectra in sets])
        if files:
            nodes.append(Node("section:not_read", "section", None, {"scan": Cell("Files not read")},
                              tuple(self._file_node(f) for f in files)))
        if pooled:
            nodes.append(Node("section:pooled", "section", None, {"scan": Cell("Pooled")},
                              tuple(self._pooled_node(p, with_unit) for p in pooled)))
        return tuple(nodes)

    def _set_node(self, ss: ScanSetRow, spectra, with_unit: bool) -> Node:
        tip = ss.label
        if ss.summed:
            tip += ("\nsummed set: scans " + ", ".join(str(n) for n in ss.scan_numbers)
                    if ss.scan_numbers else
                    f"\nsummed set ({ss.n_merged} scans merged)" if ss.n_merged else "\nsummed set")
        n, unread = len(ss.spectra), len(ss.unread)
        parsed_tones = [status_tone(PARSED, sp.parsed.word) for sp in ss.spectra]
        if unread:
            parsed_tones.append(Tone.ERR)             # an unread member is an ERR
        cells = {
            "scan": Cell(f"{ss.label} ({n} analyzer{'' if n == 1 else 's'})", tip),
            "T": _fact_cell(ss.temperature),
            "Q": Cell(""),
            "parsed": _summary_cell(ss.files_received - unread, ss.files_received, parsed_tones,
                                    "files received were read"),
            "fast": _summary_cell(sum(sp.fast.stored for sp in ss.spectra), n,
                                  [status_tone(FAST, sp.fast.word) for sp in ss.spectra],
                                  "spectra through the fast pass", [sp.fast for sp in ss.spectra], FAST),
            "deep": _summary_cell(sum(sp.deep.stored for sp in ss.spectra), n,
                                  [status_tone(DEEP, sp.deep.word) for sp in ss.spectra],
                                  "spectra through the deep pass", [sp.deep for sp in ss.spectra], DEEP),
            "slit": _fact_cell(ss.slit),
            "file": Cell(""),
        }
        children = tuple(self._spectrum_node(sp, with_unit) for sp in spectra)
        return Node(f"set:{ss.key}", "set", None, cells, children)

    def _spectrum_node(self, sp: SpectrumRow, with_unit: bool, ss: ScanSetRow | None = None) -> Node:
        """A spectrum row under its set (``ss`` is ``None``) or under its analyzer (``ss`` given)."""
        cells = {
            "scan": (Cell(ss.label) if ss is not None else
                     Cell(sp.analyzer or _DASH, None if sp.analyzer else "the analyzer is not known")),
            "T": _fact_cell(ss.temperature) if ss is not None else Cell(""),
            "Q": _fact_cell(sp.q, with_unit),
            "parsed": _status_cell(sp.parsed, PARSED),
            "fast": _status_cell(sp.fast, FAST),
            "deep": _status_cell(sp.deep, DEEP),
            "slit": Cell(""),
            "file": (Cell(sp.file_name, sp.file_path or sp.file_name) if sp.file_name
                     else Cell(_DASH, "the analyzer's file cannot be told")),
        }
        return Node(f"spec:{sp.key}", "spectrum", sp.key, cells)

    def _analyzer_nodes(self, sets, with_unit: bool) -> list[Node]:
        """The analyzer flip: one top row per analyzer, every scan set's spectrum beneath it."""
        everything: dict[str, list] = {}          # unfiltered, so the summaries do not follow the filter
        for ss in self._snapshot.sets:
            for sp in ss.spectra:
                everything.setdefault(sp.analyzer or _DASH, []).append(sp)
        shown: dict[str, list] = {}
        for ss, spectra in sets:
            for sp in spectra:
                shown.setdefault(sp.analyzer or _DASH, []).append((ss, sp))
        nodes = []
        for ana in sorted(shown, key=natural_key):
            all_sp = everything[ana]
            n = len(all_sp)
            children = tuple(self._spectrum_node(sp, with_unit, ss) for ss, sp in
                             sorted(shown[ana], key=lambda p: (natural_key(p[0].label), p[1].key)))
            cells = {
                "scan": Cell(f"{ana} ({n} scan set{'' if n == 1 else 's'})"),
                "Q": Cell(""),
                "parsed": _summary_cell(sum(sp.parsed.stored for sp in all_sp), n,
                                        [status_tone(PARSED, sp.parsed.word) for sp in all_sp],
                                        "spectra read"),
                "fast": _summary_cell(sum(sp.fast.stored for sp in all_sp), n,
                                      [status_tone(FAST, sp.fast.word) for sp in all_sp],
                                      "spectra through the fast pass", [sp.fast for sp in all_sp], FAST),
                "deep": _summary_cell(sum(sp.deep.stored for sp in all_sp), n,
                                      [status_tone(DEEP, sp.deep.word) for sp in all_sp],
                                      "spectra through the deep pass", [sp.deep for sp in all_sp], DEEP),
            }
            nodes.append(Node(f"ana:{ana}", "analyzer", None, cells, children))
        return nodes

    @staticmethod
    def _file_node(f: UnreadFile) -> Node:
        return Node(f"file:{f.path}", "file", None,
                    {"scan": Cell(f"{f.name} · {plain_reason(f.reason)}", f"{f.path}\n{f.reason}", Tone.ERR)})

    @staticmethod
    def _pooled_node(p: PooledRow, with_unit: bool) -> Node:
        cells = {"scan": Cell(p.label), "T": _fact_cell(p.temperature), "Q": _fact_cell(p.q, with_unit),
                 "fast": _status_cell(p.fast, FAST), "deep": _status_cell(p.deep, DEEP)}
        return Node(f"pool:{p.key}", "pooled", p.key, cells)

    # ---- view state (G10: view state only, never the workspace) ---------------------------
    def save_view_state(self, session) -> None:
        session.view_state[_STATE_KEY] = {
            "columns": [c for c in OPTIONAL_COLUMNS if c in self._optional], "sort": self._sort,
            "hidden": [c for c in RESULT_COLUMNS if c in self._hidden]}      # not the auto-on ones: per experiment

    def load_view_state(self, session) -> None:
        st = (getattr(session, "view_state", None) or {}).get(_STATE_KEY)
        if not isinstance(st, dict):
            return
        cols = st.get("columns")
        optional = {c for c in cols if c in OPTIONAL_COLUMNS} if isinstance(cols, list) else self._optional
        sort = st.get("sort") if st.get("sort") in SORTS else self._sort
        off = st.get("hidden")
        hidden = {c for c in off if c in RESULT_COLUMNS} if isinstance(off, list) else self._hidden
        if optional != self._optional or sort != self._sort or hidden != self._hidden:
            self._optional, self._sort, self._hidden = optional, sort, hidden
            self._auto = self._auto_columns()
            self.snapshot_changed.emit()
