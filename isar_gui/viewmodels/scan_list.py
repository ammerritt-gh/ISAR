"""Scan-list tree and input semantics over the window's single Selection.

The tree is reconciled by stable node id. Its Qt selection model only mirrors
Selection; filters never remove selected keys, and refreshes preserve the active
spectrum while its key remains present.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import Signal

from isar.workspace_api import ImportSummary
from isar.workspace_api.spectra import NOT_JUDGED_TEXT, SpectraCache
from isar.workspace_api.scan_list import (ATTENTION_NOT_READ, Fact, Mark, PooledRow, ScanListSnapshot, ScanSetRow,
                                          SpectrumRow, UnreadFile, natural_key, scan_list)
from isar.workspace_api.inter_spectra import SpectrumInter, is_flagged
from isar_gui import status_words
from isar_gui.inter_words import COLUMN_TIPS, COLUMN_TITLES, SEVERITY_WORDS, reason_sentence
from isar_gui.selection import ORIGIN_LIST, ORIGIN_OTHER, Selection
from isar_gui.plain_words import drop_echo, plain_reason, source_words
from isar_gui.theme import DEEP, FAST, PARSED, QUEUE_STATES, Tone, status_tone, summary_tone
from isar_gui.viewmodels.base import ViewModel

COLUMNS = ("scan", "T", "hkl", "absq", "gpi", "rf", "parsed", "fast", "deep", "anchor", "flag", "lock",
           "inter", "slit", "file")                                  # model order
INTER_COLUMNS = ("anchor", "flag", "lock", "inter")   # a spectrum's inter-spectra state (C-gui-state)
OPTIONAL_COLUMNS = ("absq", "gpi", "rf", "fast", "deep", *INTER_COLUMNS, "slit", "file")   # Columns ▾; the rest always show
RESULT_COLUMNS = ("fast", "deep")     # these switch on by themselves once that pass has a result or a queue mark
SHOWN_BY_DEFAULT = ("gpi", "rf")      # on wherever the card has them (the facade says which); hiding one is remembered
AUTO_COLUMNS = RESULT_COLUMNS + SHOWN_BY_DEFAULT + INTER_COLUMNS   # what the auto rule may switch on
TITLES = {"scan": "Scan", "T": "T", "hkl": "hkl", "absq": "|Q| (nm⁻¹)", "gpi": "gpi", "rf": "rf",
          "parsed": "Parsed", "fast": "Fast pass", "deep": "Deep pass", "slit": "Slit", "file": "File name",
          "anchor": COLUMN_TITLES["anchor"], "flag": COLUMN_TITLES["flag"], "lock": COLUMN_TITLES["lock"],
          "inter": COLUMN_TITLES["fit"]}
SORTS = ("set", "temperature", "analyzer")
# What a column holds, as the header's hover (E1/E2: the result columns are another batch's).
HEADER_TIPS = {
    "scan": "The scan set (one scan, one file per analyzer) and, under it, each analyzer's spectrum.",
    "T": "Sample temperature, as the files give it (or as you entered it).",
    "hkl": "(h, k, l) in r.l.u.; source in each cell's tooltip",
    "absq": "|Q| as the analyzer's file gives it, in nm⁻¹. A dash where the file holds none.",
    "gpi": ("Per analyzer: a check when an accepted gpi file gives its hkl, else a cross with the reason. "
            "On a scan row, a check only when every analyzer is good."),
    "rf": ("Per analyzer: a check when a resolution curve is in force for it, else a cross with the reason. "
           "On a scan row, a check only when every analyzer is good."),
    "parsed": ("Whether ISAR read the file. On a scan set, files read of files received. 'no T' and 'no Q' mean "
               "the file was read but holds no temperature or no Q."),
    "slit": "Slit opening of the scan set, as the files give it (or as you entered it).",
    "file": "The file this spectrum was read from.",
    "anchor": COLUMN_TIPS["anchor"], "flag": COLUMN_TIPS["flag"], "lock": COLUMN_TIPS["lock"],
    "inter": COLUMN_TIPS["fit"],
}
_SEVERITY_TONE = {"info": Tone.OK, "warn": Tone.WARN, "serious": Tone.ERR}
_STATE_KEY = "scan_list"
_DASH = "—"
REMOVED_TIP = ("Removed from the list: hidden here, from the Scans page and from the selection, but its data and "
               "fits are kept. Select it and choose Restore to list to bring it back.")
_TEMP_TERM = re.compile(r"(\d+(?:\.\d+)?)\s*k", re.IGNORECASE)
_NO_SUMMARY = ImportSummary(0, 0, (), (), {}, None, None)     # nothing imported yet: the ledger's lists stand


@dataclass(frozen=True)
class Cell:
    text: str
    tooltip: str | None = None        # None: the view shows the text itself
    tone: Tone | None = None          # a status dot; None for plain text
    stale: bool = False               # the pass's inputs changed since it was made: the word is drawn in WARN
    mark: Mark | None = None          # a gpi or rf cell: drawn as a check or a cross, its text the accessible name


@dataclass(frozen=True)
class Node:
    id: str        # "set:<set_key>" | "ana:<analyzer>" | "spec:<key>" | "section:not_read" |
                   # "file:<path>" | "section:pooled" | "pool:<key>"
    kind: str      # "set" | "analyzer" | "spectrum" | "section" | "file" | "pooled"
    key: str | None                   # the selectable identity (spectrum or pooled key), else None
    cells: dict                       # column id -> Cell
    children: tuple["Node", ...] = ()
    accessible_name: str = ""
    accessible_description: str = ""
    removed: bool = False             # a removed scan shown under Show removed: drawn greyed, named "(removed)"


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


def _hkl_cell(fact: Fact) -> Cell:
    """The hkl in r.l.u. (the header says so); the tooltip names its source in the file's words."""
    text = _fact_text(fact, with_unit=False)
    if text is None:                  # no hkl is blank, the reason on the hover (the design's rule)
        return Cell("", fact.reason or "not available")
    return Cell(text, f"{text} r.l.u. ({source_words(fact.source)})")


def _mark_cell(mark: Mark | None) -> Cell:
    """A gpi or rf cell: no text, the check or cross is drawn from the mark; its detail is the hover."""
    return Cell("", mark.detail, mark=mark) if mark is not None else Cell("")


def _status_cell(status, column: str, mark: str | None = None) -> Cell:
    reason = drop_echo(status.word, status.reason)            # "resolved: resolved: ..." said the word twice
    plain = status_words.plain_word(column, status.word, status.reason, status.provisional)
    # the plain word, the fitting code's word, its meaning
    tip = status_words.tooltip(column, status.word, reason, status.provisional)
    stale = status.current is False
    if stale:
        lines = "\n".join(f"{name}: {used} → {now}" for name, used, now in status.changed)
        tip = f"Inputs changed since this {column} pass:\n{lines}" + (f"\n{tip}")
    if column == DEEP and status.stored and status.current is None:
        plain += " · " + NOT_JUDGED_TEXT
        tip = NOT_JUDGED_TEXT + "\n" + tip
    if mark is not None:       # the queue's word replaces the stored one, which moves to the hover
        word, tone, meaning = QUEUE_STATES[mark]
        return Cell(word, f"{meaning}. Stored {status_words.TITLES[column].lower()}: {plain}\n{tip}", tone)
    return Cell(plain, tip, status_tone(column, status.word), stale)


def _summary_cell(stored: int, total: int, tones, what: str) -> Cell:
    """The ``11/28`` cell of a set or an analyzer's parse count."""
    return Cell(f"{stored}/{total}", f"{stored} of {total} {what}", summary_tone(tones))


def _coverage_cell(spectra, column: str, current, marks, what: str) -> Cell:
    """The ``27/28`` of a set or an analyzer for one pass: how many of its spectra hold a current stored result
    (C-current, the duplicate rule's own predicate). ``current`` is None before the first read; ``marks`` is the
    queue's ``unit_key -> {pass: mark}``, whose live work sets the tone."""
    n = len(spectra)
    stored = sum(getattr(sp, column).stored for sp in spectra)
    stale = sum(getattr(sp, column).current is False for sp in spectra)
    tones = [status_tone(column, getattr(sp, column).word) for sp in spectra]
    tones += [QUEUE_STATES[marks[sp.key][column]][1] for sp in spectra if column in marks.get(sp.key, {})]
    if current is None:
        text, tip = "…", "checking which results are current"
    else:
        have = sum(column in current.get(sp.key, ()) for sp in spectra)
        text = f"{have}/{n}"
        tip = f"{have} of {n} {what} have a current {column}-pass result; ISAR refits the others when you add them"
    if stale:
        tip += f"\n{stale} of {stored} {column}-pass results have changed inputs"
    return Cell(text, tip, summary_tone(tones), stale > 0)


def _count_cell(have: int, total: int, what: str, tone: Tone | None = None) -> Cell:
    """The ``3/28`` of a set or an analyzer's inter-spectra column; blank when none, as the coverage cells read."""
    if not have:
        return Cell("")
    return Cell(f"{have}/{total}", f"{have} of {total} spectra {what}", tone)


def _score(value: float | None) -> str:
    return "not available" if value is None else f"{value:g}"


def _anchor_cell(anchored: bool) -> Cell:
    if not anchored:
        return Cell("")
    return Cell("anchor", COLUMN_TIPS["anchor"], Tone.OK)


def _flag_cell(row: SpectrumInter | None) -> Cell:
    if row is None or not is_flagged(row.flag):
        return Cell("")
    flag = row.flag
    head = (f"Score {_score(flag.score)}: cost of coupling {_score(flag.cost_of_coupling)} plus the largest link "
            f"mismatch {_score(flag.link_mismatch)}.")
    lines = [head, *(reason_sentence(r.code, r.detail) for r in flag.reasons)]
    return Cell(SEVERITY_WORDS[flag.severity], "\n".join(lines), _SEVERITY_TONE[flag.severity])


def _lock_cell(row: SpectrumInter | None) -> Cell:
    if row is None or not row.locked:
        return Cell("")
    tip = "Locked: no batch will refit this spectrum."
    if row.locked_fit_id:
        tip += f" It stays on fit {row.locked_fit_id}."
    if row.lock_stale:
        return Cell("locked · stale", f"{tip}\n{reason_sentence('stale_locked_fit')}", Tone.WARN)
    return Cell("locked", tip, Tone.OK)


def _fit_cell(row: SpectrumInter | None) -> Cell:
    if row is None:
        return Cell("")
    if row.in_use:
        tip = f"{row.label}: the fit ISAR uses for this spectrum. The fast solo fit stays available beside it."
        return Cell(row.label, tip, Tone.OK)
    if row.stale:
        tip = row.stale_text or "Its inter-spectra fits are out of date; the fast solo fit stands in until a new batch."
        return Cell("stale: fast solo stands in", tip, Tone.WARN)
    return Cell("")


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


def spectrum_matches(terms, ss: ScanSetRow, sp: SpectrumRow, inter=()) -> bool:
    hay = (sp.analyzer, ss.label, sp.file_name, *_status_words(sp), *inter)
    return all(_term_hits(t, ss.temperature, hay) for t in terms)


def pooled_matches(terms, p: PooledRow) -> bool:
    hay = (p.label, "pooled", *_status_words(p))
    return all(_term_hits(t, p.temperature, hay) for t in terms)


def file_matches(terms, f: UnreadFile) -> bool:
    hay = (f.name, f.reason, "not read")
    return all(any(t in h.casefold() for h in hay) for t in terms)


class ScanListViewModel(ViewModel):
    snapshot_changed = Signal()         # after every rebuild: bind, background update, sort/filter/columns
    inspect_requested = Signal(str)     # open the inspector column on this key
    selection_changed = Signal(tuple)   # tuple[str, ...]: the selected keys, list order (C-selection)
    store_dirty = Signal()              # the removed set changed: the window saves the experiment's GUI store

    def __init__(self, ws, parent=None) -> None:
        super().__init__(ws, parent)
        self._snapshot: ScanListSnapshot = scan_list(ws)
        self.selection = Selection(self)
        self.spectra_cache = SpectraCache()
        self._anchor: str | None = None
        self._sort = "set"
        self._optional: set[str] = set()      # the optional columns the user switched on
        self._hidden: set[str] = set()        # auto columns the user switched off: the auto rule leaves them off
        self._auto: set[str] = set()          # auto columns on by themselves (a stored result, gpi or rf present)
        self._filter_text = ""
        self._intake = None
        self._queue = None
        self._marks: dict[str, dict[str, str]] = {}    # the queue's marks as the list last took them
        self._store = None                    # the experiment's GuiStore, which keeps the removed scans
        self._removed: set[str] = set()       # scans removed from this list: hidden here, kept everywhere else
        self._show_removed = False            # the Show removed toggle: removed scans listed greyed
        self._inter = None                    # the window's InterState (set_inter): anchors, flags, locks, fits
        self._inter_revision = ""
        self._auto = self._auto_columns()
        self.selection.set_order(self._order_keys)
        self.selection.changed.connect(self._on_selection_changed)

    # ---- reads ---------------------------------------------------------------------------
    @property
    def snapshot(self) -> ScanListSnapshot:
        return self._snapshot

    def selected_keys(self) -> tuple[str, ...]:
        """Queue facade: the same immutable keys the plot and inspector use."""
        return self.selection.keys

    def select_keys(self, keys) -> None:
        """Select and reveal keys requested by another panel, including queue Show."""
        self.selection.set(keys, origin=ORIGIN_OTHER)

    def _on_selection_changed(self, change) -> None:
        if change.keys_changed:
            self.selection_changed.emit(change.keys)

    def set_queue(self, controller) -> None:
        """The analysis queue controller whose marks the list shows; None shows none."""
        if self._queue is not None:
            self._queue.changed.disconnect(self._on_queue_changed)
        self._queue = controller
        if controller is not None:
            controller.changed.connect(self._on_queue_changed)
        self._on_queue_changed()

    def _on_queue_changed(self) -> None:
        """Rebuild only when the marks differ: the controller also listens to this list's ``snapshot_changed``,
        so an unconditional emit would loop."""
        marks = {} if self._queue is None else self._queue.marks()
        if marks != self._marks:
            self._marks = marks
            self._auto = self._auto_columns()
            self.snapshot_changed.emit()

    def set_inter(self, inter) -> None:
        """The window's InterState: the anchors, flags, locks and fits in use the list shows (``None``: none)."""
        if self._inter is not None:
            self._inter.changed.disconnect(self._on_inter_changed)
        self._inter = inter
        if inter is not None:
            inter.changed.connect(self._on_inter_changed)
        self._on_inter_changed()

    def _on_inter_changed(self) -> None:
        """Rebuild only when the revision moved: InterState emits ``changed`` on every read, most with nothing new."""
        revision = "" if self._inter is None else self._inter.revision
        if revision != self._inter_revision:
            self._inter_revision = revision
            self._auto = self._auto_columns()
            self.snapshot_changed.emit()

    def _inter_row(self, key: str) -> SpectrumInter | None:
        return None if self._inter is None else self._inter.row(key)

    def _anchored(self) -> frozenset[str]:
        return frozenset() if self._inter is None else frozenset(self._inter.anchors)

    def is_anchor(self, key: str) -> bool:
        return key in self._anchored()

    def _inter_words(self, key: str) -> list[str]:
        """The words the filter matches for a spectrum's inter-spectra state, as it matches its passes' words."""
        row = self._inter_row(key)
        words = ["anchor"] if key in self._anchored() else []
        if row is not None:
            words += (["flagged"] if is_flagged(row.flag) else []) + (["locked"] if row.locked else [])
            words += [row.label] if row.in_use else []
        return words

    def anchorable_keys(self) -> tuple[str, ...]:
        """The selected spectra Set as anchor acts on: not anchored yet, and holding a fast-pass result."""
        return tuple(key for key in self.selected_spectra()
                     if not self.is_anchor(key) and self._snapshot.spectrum(key).fast.stored)

    def set_anchor_selected(self, anchored: bool) -> None:
        """Set the anchor on the selected spectra that can gain one, or remove it from every selected anchored
        spectrum (a lost fast result must not trap an anchor). InterState shows the outcome (its ``saved`` goes to
        the Log), so a refused write is not repeated here."""
        keys = (self.anchorable_keys() if anchored
                else tuple(k for k in self.selected_spectra() if self.is_anchor(k)))
        if keys and self._inter is not None:
            self._inter.set_anchors(keys, anchored)

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

    def column_present(self, column: str) -> bool:
        """The gpi and rf columns exist only for the cards that read gpi files and tabulated curves."""
        return {"gpi": self._snapshot.has_gpi, "rf": self._snapshot.has_rf}.get(column, True)

    def visible_columns(self) -> tuple[str, ...]:
        return tuple(c for c in COLUMNS if c not in OPTIONAL_COLUMNS
                     or (self.column_present(c) and (c in self._optional or c in self._auto)))

    def layout_choices(self) -> tuple:
        """What the user chose about the layout; a change of it reveals the selection. A column a stored pass
        switches on by itself is not a choice, so its arrival does not move the list."""
        return (self._sort, self._filter_text, frozenset(self._optional), frozenset(self._hidden), self._show_removed)

    def _auto_columns(self) -> set[str]:
        """The columns to show by themselves: a result column while a stored record of its pass exists or the queue
        marks one, the gpi and rf columns wherever the card has them, and an inter-spectra column when a listed
        spectrum has that state; none the user has switched off."""
        snap = self._snapshot
        rows = [sp for ss in snap.sets for sp in ss.spectra] + list(snap.pooled)
        queued = {p for mine in self._marks.values() for p in mine}
        on = {c for c in RESULT_COLUMNS if c in queued or any(getattr(r, c).stored for r in rows)}
        on |= {c for c in SHOWN_BY_DEFAULT if self.column_present(c)}
        anchored = self._anchored()
        states = [i for i in (self._inter_row(r.key) for r in rows) if i is not None]
        lit = {"anchor": any(r.key in anchored for r in rows), "flag": any(is_flagged(s.flag) for s in states),
               "lock": any(s.locked for s in states), "inter": any(s.in_use for s in states)}
        on |= {c for c in INTER_COLUMNS if lit[c]}
        return {c for c in on if c not in self._hidden}

    def column_title(self, column: str) -> str:
        """The header; ``T`` carries its unit when every present value has the same one."""
        if column == "T":
            unit = self._common_t_unit()
            if unit:
                return f"T ({unit})"
        return TITLES[column]

    def header_tip(self, column: str) -> str:
        return HEADER_TIPS.get(column, "")

    def _common_t_unit(self) -> str | None:
        units = {ss.temperature.unit for ss in self._snapshot.sets if ss.temperature.value is not None}
        units |= {p.temperature.unit for p in self._snapshot.pooled if p.temperature.value is not None}
        return next(iter(units)) if len(units) == 1 else None

    # ---- the view settings ---------------------------------------------------------------
    def set_sort(self, sort: str) -> None:
        if sort in SORTS and sort != self._sort:
            self._sort = sort
            self.snapshot_changed.emit()
            self.selection.resync()

    def set_filter(self, text: str) -> None:
        text = text or ""
        if text != self._filter_text:
            self._filter_text = text
            self.snapshot_changed.emit()

    def set_column_visible(self, column: str, visible: bool) -> None:
        if column in OPTIONAL_COLUMNS and self.column_present(column) and visible != (column in self.visible_columns()):
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

    @property
    def _current_map(self):
        """C-current: unit key -> the passes holding a current stored result; None before the first read."""
        return None if self._intake is None else self._intake.current

    # ---- removed scans: hidden from this list only (fits, groups and the queue still count them) -------
    @property
    def show_removed(self) -> bool:
        return self._show_removed

    def set_show_removed(self, flag: bool) -> None:
        if bool(flag) != self._show_removed:
            self._show_removed = bool(flag)
            self.snapshot_changed.emit()

    def set_store(self, store) -> None:
        """Bind the experiment's GuiStore (``None``: no experiment) and load the scans it has removed."""
        self._store = store
        self._removed = store.removed_units() if store is not None else set()
        self.snapshot_changed.emit()

    def is_removed(self, key: str) -> bool:
        return key in self._removed

    def removed_count(self) -> int:
        return sum(1 for ss in self._snapshot.sets for sp in ss.spectra if sp.key in self._removed)

    def selected_spectra(self) -> tuple[str, ...]:
        """The selected spectrum keys; a pooled master is neither removed nor read again."""
        return tuple(key for key in self.selection.keys if isinstance(self._snapshot.spectrum(key), SpectrumRow))

    def source_paths(self, key: str) -> tuple[str, ...]:
        """The data file(s) a spectrum was read from: its analyzer's own file, as the File column shows it, else
        every file that produced its scan set. Empty when none is recorded."""
        row = self._snapshot.spectrum(key)
        if not isinstance(row, SpectrumRow):
            return ()
        if row.file_path:
            return (row.file_path,)
        return tuple(f.path for f in getattr(self._ws, "files", ())
                     if f.classification == "measurement" and row.set_key in f.scan_ids)

    def remove_selected(self) -> None:
        keys = [key for key in self.selected_spectra() if key not in self._removed]
        if keys:
            self._removed.update(keys)
            self._persist_removed()
            self.selection.set(tuple(key for key in self.selection.keys if key not in self._removed),
                               origin=ORIGIN_LIST)
            self.snapshot_changed.emit()

    def restore_selected(self) -> None:
        keys = [key for key in self.selected_spectra() if key in self._removed]
        if keys:
            self._removed.difference_update(keys)
            self._persist_removed()
            self.snapshot_changed.emit()

    def _persist_removed(self) -> None:
        if self._store is not None:
            self._store.set_removed_units(self._removed)
            self.store_dirty.emit()

    def _listed(self, key: str) -> bool:
        return self._show_removed or key not in self._removed

    # ---- the workspace changed -----------------------------------------------------------
    def set_workspace(self, ws) -> None:
        """Rebind and retain only selected keys still in the snapshot."""
        self._ws = ws
        self._reread()

    def on_workspace_changed(self, changed=None) -> None:
        """Re-read after the facade refresh without requesting a page change."""
        self._reread()

    def _reread(self) -> None:
        intake = self._intake
        reads = {} if intake is None else {
            "fact_rows": intake.fact_rows or (), "fit_inputs": intake.fit_inputs or (),
            "import_summary": intake.summary or _NO_SUMMARY,      # None (not read yet): no marks, no lists
            "needs_attention": ATTENTION_NOT_READ if intake.attention is None else intake.attention}
        self._snapshot = scan_list(self._ws, **reads)
        self._auto = self._auto_columns()
        self.snapshot_changed.emit()
        self.selection.resync()

    # ---- selection -----------------------------------------------------------------------
    def select(self, key: str | None) -> None:
        self.selection.set(() if key is None else (key,), clicked=key, origin=ORIGIN_LIST)

    def inspect(self, key: str) -> None:
        if self._snapshot.spectrum(key) is None:
            return
        if key not in self.selection.keys:
            self.select(key)
        else:
            self.selection.set_active(key)
        self.inspect_requested.emit(key)

    def set_active_key(self, key: str | None) -> None:
        if key in self.selection.keys:
            self.selection.set_active(key)

    def _ordered_rows(self):
        pairs = [(ss, sp) for ss in self._sorted_sets() for sp in ss.spectra]
        if self._sort == "analyzer":
            pairs.sort(key=lambda pair: (natural_key(pair[1].analyzer or _DASH),
                                        natural_key(pair[0].label), pair[1].key))
        return [sp for _ss, sp in pairs] + list(self._snapshot.pooled)

    def _order_keys(self, keys) -> tuple[str, ...]:
        wanted = set(keys)
        return tuple(row.key for row in self._ordered_rows() if row.key in wanted)

    @staticmethod
    def _node_key(node_id: str | None) -> str | None:
        if node_id and node_id.startswith(("spec:", "pool:")):
            return node_id.split(":", 1)[1]
        return None

    def container_keys(self, node_id: str) -> tuple[str, ...]:
        """Unfiltered membership of the listed scans (a removed one is not listed), also used for the all/some/none mark."""
        if node_id.startswith("set:"):
            ss = self._snapshot.scan_set(node_id[4:])
            return () if ss is None else tuple(sp.key for sp in ss.spectra if self._listed(sp.key))
        if node_id.startswith("ana:"):
            ana = node_id[4:]
            return tuple(row.key for row in self._ordered_rows()
                         if isinstance(row, SpectrumRow) and (row.analyzer or _DASH) == ana
                         and self._listed(row.key))
        if node_id == "section:pooled":
            return tuple(p.key for p in self._snapshot.pooled)
        return ()

    def click(self, node_id: str | None, *, ctrl=False, shift=False, visible=()) -> None:
        """A select-zone gesture. A Shift range includes visible spectrum rows only."""
        if node_id is None:
            self._anchor = None
            self.selection.clear(origin=ORIGIN_LIST)
            return
        key = self._node_key(node_id)
        if key is None and not node_id.startswith(("set:", "ana:")):
            return
        visible = tuple(visible)
        if shift and self._anchor in visible and node_id in visible:
            first, last = sorted((visible.index(self._anchor), visible.index(node_id)))
            keys = {k for ident in visible[first:last + 1] if (k := self._node_key(ident))}
            if ctrl:
                keys.update(self.selection.keys)
        else:
            self._anchor = node_id
            keys = {key} if key is not None else set(self.container_keys(node_id))
            if ctrl:
                selected = set(self.selection.keys)
                keys = selected - keys if keys <= selected else selected | keys
        self.selection.set(keys, clicked=key, origin=ORIGIN_LIST)

    def select_all_visible(self, visible) -> None:
        self.selection.set((key for ident in visible if (key := self._node_key(ident))),
                           origin=ORIGIN_LIST)

    def container_presentation(self, node_id: str) -> tuple[str, str, str, str]:
        """Text, accessible name, description, tooltip: one source for every mark."""
        keys = self.container_keys(node_id)
        total, selected = len(keys), len(set(keys).intersection(self.selection.keys))
        tip_extra = ""
        if node_id.startswith("set:"):
            ss = self._snapshot.scan_set(node_id[4:])
            label, kind, noun = ss.label, "Scan set", "analyzers"
            if ss.summed:
                tip_extra = ("\nsummed set: scans " + ", ".join(str(n) for n in ss.scan_numbers)
                             if ss.scan_numbers else f"\nsummed set ({ss.n_merged} scans merged)"
                             if ss.n_merged else "\nsummed set")
        elif node_id.startswith("ana:"):
            label, kind, noun = node_id[4:], "Analyzer", "scan sets"
        else:
            label, kind, noun = "Pooled", "", "spectra"
        counted_noun = noun[:-1] if total == 1 and noun in ("analyzers", "scan sets") else noun
        count = f"{selected} of {total} selected" if selected else f"{total} {counted_noun}"
        text = f"{label} ({count})" if kind or selected else label
        mark = f"{selected} of {total} selected" if selected else "none selected"
        name = f"{kind} {label}, {total} {noun}, {mark}".lstrip()
        description = ("The left edge opens or closes it. The rest of the row selects "
                       f"its {total} {noun}; Ctrl-click adds or removes them.") if kind else (
                           "The left edge opens or closes it. The selection stays unchanged.")
        return text, name, description, label + "\n" + description + tip_extra

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
            listed = [sp for sp in ss.spectra if self._listed(sp.key)]
            spectra = [sp for sp in listed if spectrum_matches(terms, ss, sp, self._inter_words(sp.key))]
            if spectra or (listed and not terms):
                sets.append((ss, spectra))
        files = [f for f in self._snapshot.files_not_read if file_matches(terms, f)]
        pooled = [p for p in self._snapshot.pooled if pooled_matches(terms, p)]
        return sets, files, pooled

    def counts(self) -> tuple[int, int]:
        """``(visible, listed)`` spectra; pooled masters are not counted, nor removed scans unless shown."""
        sets, _files, _pooled = self._visible()
        listed = sum(1 for ss in self._snapshot.sets for sp in ss.spectra if self._listed(sp.key))
        return sum(len(spectra) for _ss, spectra in sets), listed

    def outside_filter(self) -> str | None:
        """Selected keys hidden by the filter remain selected and are named here."""
        if not self.filter_active:
            return None
        sets, _files, pooled = self._visible()
        visible = {sp.key for _ss, spectra in sets for sp in spectra} | {p.key for p in pooled}
        missing = [key for key in self.selection.keys if key not in visible]
        if not missing:
            return None
        if len(missing) > 1:
            return f"{len(missing)} selected spectra are outside the filter"
        row = self._snapshot.spectrum(missing[0])
        if isinstance(row, PooledRow):
            name = row.label
        else:
            ss = self._snapshot.scan_set(row.set_key)
            name = f"{row.analyzer or _DASH}, {ss.label if ss else row.set_key}"
        return f"The selected spectrum ({name}) is outside the filter"

    # ---- the tree ------------------------------------------------------------------------
    def tree(self) -> tuple[Node, ...]:
        sets, files, pooled = self._visible()
        nodes = (self._analyzer_nodes(sets) if self._sort == "analyzer"
                 else [self._set_node(ss, spectra) for ss, spectra in sets])
        if files:
            nodes.append(Node("section:not_read", "section", None, {"scan": Cell("Files not read")},
                              tuple(self._file_node(f) for f in files)))
        if pooled:
            text, name, description, tip = self.container_presentation("section:pooled")
            nodes.append(Node("section:pooled", "section", None, {"scan": Cell(text, tip)},
                              tuple(self._pooled_node(p) for p in pooled), name, description))
        return tuple(nodes)

    def _set_node(self, ss: ScanSetRow, spectra) -> Node:
        text, name, description, tip = self.container_presentation(f"set:{ss.key}")
        n, unread = len(ss.spectra), len(ss.unread)
        parsed_tones = [status_tone(PARSED, sp.parsed.word) for sp in ss.spectra]
        if unread:
            parsed_tones.append(Tone.ERR)             # an unread member is an ERR
        cells = {
            "scan": Cell(text, tip),
            "T": _fact_cell(ss.temperature),
            "hkl": Cell(""),
            "absq": Cell(""),
            "gpi": _mark_cell(ss.gpi),
            "rf": _mark_cell(ss.rf),
            "parsed": _summary_cell(ss.files_received - unread, ss.files_received, parsed_tones,
                                    "files received were read"),
            "fast": _coverage_cell(ss.spectra, FAST, self._current_map, self._marks, "analyzers"),
            "deep": _coverage_cell(ss.spectra, DEEP, self._current_map, self._marks, "analyzers"),
            "slit": _fact_cell(ss.slit),
            "file": Cell(""),
            **self._inter_counts(ss.spectra),
        }
        children = tuple(self._spectrum_node(sp) for sp in spectra)
        return Node(f"set:{ss.key}", "set", None, cells, children, name, description)

    def _spectrum_node(self, sp: SpectrumRow, ss: ScanSetRow | None = None) -> Node:
        """A spectrum row under its set (``ss`` is ``None``) or under its analyzer (``ss`` given)."""
        mine = self._marks.get(sp.key, {})
        removed = sp.key in self._removed
        scan = (Cell(ss.label) if ss is not None else
                Cell(sp.analyzer or _DASH, None if sp.analyzer else "the analyzer is not known"))
        if removed:
            scan = Cell(f"{scan.text} (removed)", REMOVED_TIP)
        cells = {
            "scan": scan,
            "T": _fact_cell(ss.temperature) if ss is not None else Cell(""),
            "hkl": _hkl_cell(sp.q),
            "absq": _fact_cell(sp.q_abs, with_unit=False),
            "gpi": _mark_cell(sp.gpi),
            "rf": _mark_cell(sp.rf),
            "parsed": _status_cell(sp.parsed, PARSED),
            "fast": _status_cell(sp.fast, FAST, mine.get(FAST)),
            "deep": _status_cell(sp.deep, DEEP, mine.get(DEEP)),
            "slit": Cell(""),
            "file": (Cell(sp.file_name, sp.file_path or sp.file_name) if sp.file_name
                     else Cell(_DASH, "the analyzer's file cannot be told")),
            "anchor": _anchor_cell(self.is_anchor(sp.key)),
            "flag": _flag_cell(self._inter_row(sp.key)),
            "lock": _lock_cell(self._inter_row(sp.key)),
            "inter": _fit_cell(self._inter_row(sp.key)),
        }
        return Node(f"spec:{sp.key}", "spectrum", sp.key, cells, removed=removed)

    def _inter_counts(self, spectra) -> dict[str, Cell]:
        """The four inter-spectra cells of a set or an analyzer: how many of its spectra are anchors, flagged, locked
        or on an inter-spectra fit."""
        anchored = self._anchored()
        rows = [self._inter_row(sp.key) for sp in spectra]
        n = len(spectra)
        severities = [r.flag.severity for r in rows if r is not None and is_flagged(r.flag)]
        return {
            "anchor": _count_cell(sum(sp.key in anchored for sp in spectra), n, "are anchors"),
            "flag": _count_cell(len(severities), n, "are flagged for review",
                                summary_tone(_SEVERITY_TONE[s] for s in severities)),
            "lock": _count_cell(sum(r is not None and r.locked for r in rows), n, "are locked"),
            "inter": _count_cell(sum(r is not None and r.in_use for r in rows), n, "use an inter-spectra fit"),
        }

    def _analyzer_nodes(self, sets) -> list[Node]:
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
            children = tuple(self._spectrum_node(sp, ss) for ss, sp in
                             sorted(shown[ana], key=lambda p: (natural_key(p[0].label), p[1].key)))
            text, name, description, tip = self.container_presentation(f"ana:{ana}")
            cells = {
                "scan": Cell(text, tip),
                "hkl": Cell(""),
                "absq": Cell(""),
                "parsed": _summary_cell(sum(sp.parsed.stored for sp in all_sp), n,
                                        [status_tone(PARSED, sp.parsed.word) for sp in all_sp],
                                        "spectra read"),
                "fast": _coverage_cell(all_sp, FAST, self._current_map, self._marks, "spectra"),
                "deep": _coverage_cell(all_sp, DEEP, self._current_map, self._marks, "spectra"),
                **self._inter_counts(all_sp),
            }
            nodes.append(Node(f"ana:{ana}", "analyzer", None, cells, children, name, description))
        return nodes

    @staticmethod
    def _file_node(f: UnreadFile) -> Node:
        return Node(f"file:{f.path}", "file", None,
                    {"scan": Cell(f"{f.name} · {plain_reason(f.reason)}", f"{f.path}\n{f.reason}", Tone.ERR)})

    @staticmethod
    def _pooled_node(p: PooledRow) -> Node:
        cells = {"scan": Cell(p.label), "T": _fact_cell(p.temperature), "hkl": _hkl_cell(p.q),
                 "gpi": _mark_cell(p.gpi), "rf": _mark_cell(p.rf),
                 "fast": _status_cell(p.fast, FAST), "deep": _status_cell(p.deep, DEEP)}
        return Node(f"pool:{p.key}", "pooled", p.key, cells)

    # ---- view state (G10: view state only, never the workspace) ---------------------------
    def save_view_state(self, session) -> None:
        session.view_state[_STATE_KEY] = {
            "columns": [c for c in OPTIONAL_COLUMNS if c in self._optional], "sort": self._sort,
            "hidden": [c for c in AUTO_COLUMNS if c in self._hidden],      # not the auto-on ones: per experiment
            "show_removed": self._show_removed}

    def load_view_state(self, session) -> None:
        st = (getattr(session, "view_state", None) or {}).get(_STATE_KEY)
        if not isinstance(st, dict):
            return
        cols = st.get("columns")
        optional = {c for c in cols if c in OPTIONAL_COLUMNS} if isinstance(cols, list) else self._optional
        sort = st.get("sort") if st.get("sort") in SORTS else self._sort
        off = st.get("hidden")
        hidden = {c for c in off if c in AUTO_COLUMNS} if isinstance(off, list) else self._hidden
        show = st.get("show_removed")
        show_removed = show if isinstance(show, bool) else self._show_removed
        if optional != self._optional or sort != self._sort or hidden != self._hidden or show_removed != self._show_removed:
            self._optional, self._sort, self._hidden, self._show_removed = optional, sort, hidden, show_removed
            self._auto = self._auto_columns()
            self.snapshot_changed.emit()
            self.selection.resync()
