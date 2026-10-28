"""ScansViewModel + ScanTableModel — the atomic *scan × analyzer* unit as the visible thing (Pass 2b).

Two views share ONE model here: the physical chip dock (:mod:`isar_gui.views.scans_dock`) and the
table dock (:mod:`isar_gui.views.scan_table_dock`). Both read :class:`ScanTableModel` and both drive
the SAME :class:`~PySide6.QtCore.QItemSelectionModel`, so a unit selected in one view lights up in
the other — and, because a unit can belong to several groups, in every box it appears in.

Model rows are ATOMIC UNITS: one row per ledger entry (``ws.scans`` is already keyed
``"exp:scan:det"``). There is no scan-level collapse — each unit shows its OWN analyzer q
(``ScanView.hkl`` resolves per-entry), and the source scan's *target* ``#Q`` (``q_target``) is
group/inspector metadata, not the unit's q. Single-detector instruments look unchanged: one unit per
scan, the detector suffix hidden. The membership/GUI key is the full unit key (:mod:`isar_gui.scan_keys`).

Groups are of two kinds:
  * **auto-groups** derived from metadata, carrying a ``dim`` (``scan``/``T``/``q``/``ana``/``B``) —
    membership is a function of the data, never stored, and drops onto them are rejected;
  * **manual groups** persisted in the :class:`~isar_gui.gui_store.GuiStore`; drops mutate the store.

Derivation lives here; the views are dumb. This VM reads the read-only facade and the GuiStore only —
it never re-fits or writes engine artifacts.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelection,
    QItemSelectionModel,
    QModelIndex,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor

from isar_gui.scan_filter import ScanFilter, parse_query
from isar_gui.scan_keys import member_matches, scan_prefix, split_key
from isar_gui.viewmodels.base import ViewModel

log = logging.getLogger(__name__)

# Columns: # | File | Type | T | Analyzer | q/HKL | Status | Groups
(COL_NUM, COL_FILE, COL_TYPE, COL_T, COL_ANA, COL_Q, COL_STATUS, COL_GROUPS) = range(8)
_HEADERS = ["#", "File", "Type", "T", "Analyzer", "q / HKL", "Status", "Groups"]

_HIGHLIGHT_MS = 2000
_HIGHLIGHT_COLOR = QColor(255, 243, 176)  # soft amber, matches the Files dock new-row tint
_ACTIVE_COLOR = QColor(207, 229, 255)     # persistent inspected-unit cue, distinct from selection
# A public model role lets delegates and tests identify the inspected unit without conflating it
# with QItemSelectionModel's intentionally independent multi-selection.
ACTIVE_UNIT_ROLE = Qt.UserRole + 1

# Ledger status -> chip edge colour. Grey = not-yet-a-number; green fitted; amber held; red excluded.
_STATUS_COLOR = {
    "ingested": "#9e9e9e", "non_result": "#9e9e9e", "context_applied": "#9e9e9e",
    "fitted": "#2e7d32",
    "held_for_calibration": "#b8860b", "held_for_sample": "#b8860b",
    "excluded": "#c62828",
}
_STATUS_DISPLAY = {"ingested": "parsed"}


def status_text(status) -> str:
    """Display label for a ledger status — ``ingested`` reads as ``parsed`` (the rest verbatim)."""
    return _STATUS_DISPLAY.get(status, status or "")


def status_tooltip(status) -> str:
    if status == "ingested":
        return "parsed, not fitted"
    return status or ""


def status_color(status) -> str:
    return _STATUS_COLOR.get(status, "#9e9e9e")


def _fmt_t(t) -> str:
    return "" if t is None else f"{t:g}"


def _fmt_q(hkl) -> str:
    if not hkl:
        return ""
    try:
        return "[" + ", ".join(f"{float(v):.3g}" for v in hkl) + "]"
    except (TypeError, ValueError) as exc:
        log.debug("_fmt_q: non-numeric hkl %r (%s)", hkl, exc)
        return ""


def sibling_hue(scan_number) -> QColor:
    """A deterministic, stable accent colour for a source scan (its analyzer units share it).

    Siblings share monitor/timing/environment systematics, so a shared accent makes correlated
    units visible at a glance (council: correlation blindness is the top risk of atomic units). The
    hue is a fixed function of the scan number; saturation/value are muted so it reads as an accent,
    not an alarm. Non-numeric/absent scan numbers fold to a neutral grey."""
    try:
        n = int(scan_number)
    except (TypeError, ValueError) as exc:
        log.debug("sibling_hue: non-numeric scan_number %r (%s)", scan_number, exc)
        return QColor("#9e9e9e")
    # Golden-angle stepping spreads adjacent scan numbers to visibly different hues.
    hue = int((n * 137) % 360)
    c = QColor()
    c.setHsv(hue, 150, 205)
    return c


# ---------------------------------------------------------------------------
# Atomic scan-unit row (one ledger entry = one scan x analyzer)
# ---------------------------------------------------------------------------
@dataclass
class ScanUnit:
    key: str                 # the full unit key "exp:scan:det" — the membership + GUI key
    exp: str | None
    scan_number: int | None
    det: str | None          # the analyzer / detector id (None on a single-detector legacy entry)
    scan_type: str
    source_file: str | None
    T: float | None          # noqa: N815 — scan-level, matches ScanView.T
    q: list | None           # this unit's OWN analyzer q (ScanView.hkl, per-entry)
    q_target: list | None    # the source scan's target #Q (provenance, S30 only) — group metadata
    B: float | None          # noqa: N815 — sample field setpoint (provenance), when present
    command: str | None      # the acquisition command line (provenance), when present
    status: str
    fit: object | None = None   # this unit's FitView (for the inspector), if the entry carries one

    @property
    def file_name(self) -> str:
        return Path(self.source_file).name if self.source_file else ""

    @property
    def scan_key(self) -> str:
        """The scan-level prefix ``"exp:scan"`` this unit belongs to (siblings share it)."""
        return scan_prefix(self.key)


def _provenance(sv) -> dict:
    p = getattr(sv, "provenance", None)
    return p if isinstance(p, dict) else {}


def _finite_list(v) -> list | None:
    if isinstance(v, (list, tuple)) and v and not any(x is None for x in v):
        try:
            return [float(x) for x in v]
        except (TypeError, ValueError) as exc:
            log.debug("_finite_list: non-numeric element in %r (%s)", v, exc)
            return None
    return None


def unit_rows(scan_views) -> list[ScanUnit]:
    """One :class:`ScanUnit` per ledger entry — no merging, each keeps its own analyzer q.

    The key is ``ScanView.scan_id`` (the canonical ``"exp:scan:det"``); the detector id is split from
    that key, falling back to the sole entry in ``ScanView.detectors``. ``q`` is the unit's OWN
    ``ScanView.hkl`` (already resolved per-entry); ``q_target``/``B``/``command`` come from the
    (tolerantly-absent) provenance. Insertion order is preserved.
    """
    out: list[ScanUnit] = []
    for sv in scan_views:
        key = getattr(sv, "scan_id", None)
        if key is None:
            # Legacy/synthetic view without a scan_id: synthesize the canonical form.
            exp = getattr(sv, "experiment_id", None)
            num = getattr(sv, "scan_number", None)
            dets = getattr(sv, "detectors", None) or {}
            det = next(iter(dets), None)
            key = f"{exp}:{num}:{det}"
        exp, _num, det = split_key(key)
        if det is None:
            det = next(iter(getattr(sv, "detectors", None) or {}), None)
        prov = _provenance(sv)
        fit = None
        dets = getattr(sv, "detectors", None) or {}
        if det is not None and det in dets:
            fit = dets[det]
        elif dets:
            fit = next(iter(dets.values()))
        B = prov.get("B")
        try:
            B = float(B) if B is not None else None
        except (TypeError, ValueError) as exc:
            log.debug("unit_rows: non-numeric B %r for %s (%s)", B, key, exc)
            B = None
        out.append(ScanUnit(
            key=str(key), exp=exp,
            scan_number=getattr(sv, "scan_number", None), det=det,
            scan_type=getattr(sv, "scan_type", "") or "",
            source_file=getattr(sv, "source_file", None),
            T=getattr(sv, "T", None), q=getattr(sv, "hkl", None),
            q_target=_finite_list(prov.get("q_target")), B=B,
            command=(str(prov["command"]) if prov.get("command") else None),
            status=getattr(sv, "status", "") or "", fit=fit))
    return out


# ---------------------------------------------------------------------------
# Auto-group
# ---------------------------------------------------------------------------
@dataclass
class AutoGroup:
    id: str
    label: str
    dim: str                 # "scan" | "T" | "q" | "ana" | "B"
    scan_keys: list = field(default_factory=list)   # member UNIT keys


def _rounding(ws) -> tuple[float, float]:
    """(t_round, q_step) for auto-group binning.

    Prefers the card's collation ``t_round`` and the policy ``hkl_round_step`` so an auto-group means
    the same thing the reducer's collation does. Falls back to 1 K / 0.001 rlu (the brief's defaults)
    when no card is accessible (e.g. the empty/pending workspace)."""
    t_round, q_step = 1.0, 0.001
    try:
        card = getattr(ws, "card", None)
        if card is not None:
            collation = getattr(card, "collation", None) or {}
            tr = collation.get("t_round")
            if tr:
                t_round = float(tr)
            hs = getattr(getattr(card, "policy", None), "hkl_round_step", None)
            if hs:
                q_step = float(hs)
    except Exception as exc:  # noqa: BLE001 — binning params are best-effort, never fatal
        log.debug("auto-group rounding: falling back to defaults (%s)", exc)
    return t_round, q_step


# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------
class ScanTableModel(QAbstractTableModel):
    """Atomic scan-unit rows for the table AND the chip dock (shared model, shared selection).

    ``Qt.UserRole`` serves the unit key. New keys since the last :meth:`set_rows` get a transient
    background tint cleared by one shared ~2 s timer. The Groups cell is derived on demand from a
    callable the VM injects (so a groups change repaints that column without rebuilding rows)."""

    highlight_changed = Signal()   # tint set or cleared — the chip dock repaints on this

    def __init__(self, group_labels_for=None, parent=None):
        super().__init__(parent)
        self._rows: list[ScanUnit] = []
        self._prev_keys: set[str] = set()
        self._highlight: set[str] = set()
        self._active_key: str | None = None
        # Injected by the VM: unit_key -> list[str] of group labels (manual names + auto labels).
        self._group_labels_for = group_labels_for or (lambda _key: [])
        self._hl_timer = QTimer(self)
        self._hl_timer.setSingleShot(True)
        self._hl_timer.setInterval(_HIGHLIGHT_MS)
        self._hl_timer.timeout.connect(self._clear_highlight)

    # ---- rebuild ----
    def set_rows(self, rows: list[ScanUnit]) -> None:
        self.beginResetModel()
        current = {r.key for r in rows}
        new_keys = current - self._prev_keys
        self._rows = list(rows)
        self._prev_keys = current
        self._highlight = new_keys
        self.endResetModel()
        if new_keys:
            self._hl_timer.start()
        self.highlight_changed.emit()

    def _clear_highlight(self) -> None:
        self._highlight = set()
        if self._rows:
            top = self.index(0, 0)
            bot = self.index(self.rowCount() - 1, self.columnCount() - 1)
            self.dataChanged.emit(top, bot, [Qt.BackgroundRole])
        self.highlight_changed.emit()

    def highlighted_keys(self) -> set[str]:
        return set(self._highlight)

    @property
    def active_key(self) -> str | None:
        return self._active_key

    def set_active_key(self, key: str | None) -> None:
        """Mark one row as inspected without altering the shared multi-selection."""
        key = str(key) if key else None
        if key == self._active_key:
            return
        old, self._active_key = self._active_key, key
        for changed in (old, key):
            row = self.row_of_key(changed) if changed is not None else -1
            if row >= 0:
                left = self.index(row, 0)
                right = self.index(row, self.columnCount() - 1)
                self.dataChanged.emit(
                    left, right,
                    [Qt.BackgroundRole, Qt.FontRole, Qt.ToolTipRole, ACTIVE_UNIT_ROLE])

    def set_group_labels_provider(self, fn) -> None:
        self._group_labels_for = fn or (lambda _key: [])

    def groups_updated(self) -> None:
        """Repaint the Groups column after a membership change (rows themselves are unchanged)."""
        if not self._rows:
            return
        top = self.index(0, COL_GROUPS)
        bot = self.index(self.rowCount() - 1, COL_GROUPS)
        self.dataChanged.emit(top, bot, [Qt.DisplayRole])

    # ---- lookup helpers ----
    def rows(self) -> list[ScanUnit]:
        return list(self._rows)

    def row_of_key(self, key: str) -> int:
        for i, r in enumerate(self._rows):
            if r.key == key:
                return i
        return -1

    def key_at(self, row: int) -> str | None:
        return self._rows[row].key if 0 <= row < len(self._rows) else None

    # ---- QAbstractTableModel surface ----
    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_HEADERS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if orientation == Qt.Horizontal and role == Qt.DisplayRole and 0 <= section < len(_HEADERS):
            return _HEADERS[section]
        return None

    def flags(self, index):
        if not index.isValid():
            return Qt.NoItemFlags
        return Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.UserRole:
            return row.key
        if role == ACTIVE_UNIT_ROLE:
            return row.key == self._active_key
        if role == Qt.BackgroundRole:
            if row.key == self._active_key:
                return _ACTIVE_COLOR
            return _HIGHLIGHT_COLOR if row.key in self._highlight else None
        if role == Qt.FontRole and row.key == self._active_key:
            from PySide6.QtGui import QFont
            font = QFont()
            font.setBold(True)
            return font
        if role == Qt.ToolTipRole:
            if row.key == self._active_key:
                return "Inspecting this unit"
            if col == COL_FILE:
                return row.source_file or ""
            if col == COL_STATUS:
                return status_tooltip(row.status)
            if col == COL_Q:
                # The unit shows its OWN analyzer q; the source scan's target #Q rides the tooltip.
                if row.q_target:
                    return "target #Q: " + _fmt_q(row.q_target)
                return None
            if col == COL_ANA:
                return f"analyzer {row.det}" if row.det else None
            return None
        if role == Qt.DisplayRole:
            if col == COL_NUM:
                return "" if row.scan_number is None else str(row.scan_number)
            if col == COL_FILE:
                return row.file_name
            if col == COL_TYPE:
                return row.scan_type
            if col == COL_T:
                return _fmt_t(row.T)
            if col == COL_ANA:
                return "" if row.det is None else str(row.det)
            if col == COL_Q:
                return _fmt_q(row.q)
            if col == COL_STATUS:
                return status_text(row.status)
            if col == COL_GROUPS:
                return ", ".join(self._group_labels_for(row.key))
        return None

    # ---- drag payload (selected rows -> MIME_SCAN_IDS) ----
    def mimeTypes(self):  # noqa: N802
        from isar_gui.mime import MIME_SCAN_IDS
        return [MIME_SCAN_IDS]

    def supportedDragActions(self):  # noqa: N802
        return Qt.CopyAction

    def mimeData(self, indexes):  # noqa: N802
        from PySide6.QtCore import QMimeData

        from isar_gui.mime import MIME_SCAN_IDS, encode_scan_ids
        keys: list[str] = []
        for idx in indexes:
            if idx.column() != 0:
                continue  # one payload entry per row, not per cell
            key = self.key_at(idx.row())
            if key and key not in keys:
                keys.append(key)
        md = QMimeData()
        md.setData(MIME_SCAN_IDS, encode_scan_ids(keys))
        return md


# ---------------------------------------------------------------------------
# ViewModel
# ---------------------------------------------------------------------------
class ScansViewModel(ViewModel):
    """Owns the shared model + selection, the GuiStore handle, and group derivation (Pass 2b)."""

    #: Structural group change (create/delete/rename/membership/order) — dock rebuilds, window persists.
    groups_changed = Signal()
    #: A ui-scrap mutation (collapsed/order/active tab) the window should debounce-persist without rebuild.
    store_dirty = Signal()
    #: The orthogonal filter changed — the dock rebuilds and the filter controls re-sync from the VM.
    filter_changed = Signal()
    #: A single-clicked unit asked for the C2 inspector. Payload is the unit's ``FitView`` when the
    #: entry is fitted, else the unit-key ``str`` (the raw-unit metadata view); ``None`` when unknown.
    inspect_requested = Signal(object)

    def __init__(self, ws, store=None, parent=None):
        super().__init__(ws, parent)
        self._store = store  # GuiStore | None (None in the empty/pending state)
        self._all_rows: list[ScanUnit] = []   # every ledger unit, before the orthogonal filter
        self._filter = ScanFilter()           # the active narrowing predicate (empty = show all)
        self._rows: list[ScanUnit] = []       # _all_rows survivors of _filter — the VISIBLE set
        self._auto: list[AutoGroup] = []
        self._auto_labels_by_key: dict[str, list[str]] = {}
        self._model = ScanTableModel(self.group_labels_for_key, parent=self)
        self._selection = QItemSelectionModel(self._model, self)
        self._rebuild()

    # ---- exposed handles ----
    @property
    def model(self) -> ScanTableModel:
        return self._model

    @property
    def selection_model(self) -> QItemSelectionModel:
        return self._selection

    def set_store(self, store) -> None:
        """Bind the per-workspace GuiStore (a bind, not a user mutation — no persist signal emitted;
        the dock rebuilds on ``refreshed``, which also re-reads the now-present manual groups)."""
        self._store = store
        # Hydrate the persisted filter (a bind, not a mutation — do NOT re-persist or emit store_dirty).
        if store is not None:
            try:
                self._filter = ScanFilter.from_dict(store.scan_filter())
            except Exception as exc:  # noqa: BLE001 — a corrupt stored filter must never block the bind
                log.warning("stored scan filter unreadable, ignoring: %s", exc)
                self._filter = ScanFilter()
        self._rebuild()
        self.refreshed.emit()

    # ---- refresh ----
    def refresh(self) -> None:
        self._rebuild()
        self.refreshed.emit()

    def on_workspace_changed(self, changed=None) -> None:
        self.refresh()

    def _rebuild(self) -> None:
        try:
            scan_views = list(getattr(self._ws, "scans", None) or [])
        except Exception as exc:  # noqa: BLE001 — the facade degrades; never crash the VM
            log.warning("scans facade read failed: %s", exc)
            scan_views = []
        # Pooled masters remain available through Workspace/results for notebooks and the Results
        # tree, but they are derived fits rather than physical scans and do not belong in the Pool.
        physical = [sv for sv in scan_views
                    if not any(bool(getattr(fv, "pooled", False))
                               for fv in (getattr(sv, "detectors", None) or {}).values())]
        self._all_rows = unit_rows(physical)
        self._reapply_filter()

    def _reapply_filter(self) -> None:
        """Recompute the visible rows + derived groups from ``_all_rows`` under the active filter.

        Auto-groups derive from the FILTERED rows, so a group with no surviving member simply vanishes
        (the dock shows an empty-state instead). Structural predicates (``multi_detector``/``has_field``)
        read ``_all_rows`` so the set of dimension tabs never shifts under a filter."""
        q_step = _rounding(self._ws)[1]
        self._rows = [u for u in self._all_rows if self._filter.matches(u, q_step=q_step)]
        self._auto = self._derive_auto_groups()
        labels: dict[str, list[str]] = {}
        for ag in self._auto:
            for key in ag.scan_keys:
                labels.setdefault(key, []).append(ag.label)
        self._auto_labels_by_key = labels
        # set_rows resets the model, which clears the shared selection. Capture the live selection
        # first and re-select the keys that survived the rebuild, so an ingest tick that merely adds
        # rows (or a filter that keeps them) does not drop the operator's current selection.
        selected = self.selected_keys()
        self._model.set_rows(self._rows)
        if selected:
            survivors = [k for k in selected if self._model.row_of_key(k) >= 0]
            if survivors:
                self.select_keys(survivors)

    # ---- orthogonal filter (shared state; bar + query box are dumb) ----
    def filter(self) -> ScanFilter:
        """The active narrowing predicate (the value object itself is frozen — safe to hand out)."""
        return self._filter

    def set_filter(self, new: ScanFilter) -> None:
        """Replace the filter, re-derive the visible set, persist it, and announce the change."""
        self._filter = new if isinstance(new, ScanFilter) else ScanFilter()
        self._reapply_filter()
        if self._store is not None:
            self._store.set_scan_filter(self._filter.to_dict())
            self.store_dirty.emit()
        self.filter_changed.emit()

    def apply_query(self, text: str) -> str | None:
        """Parse ``text`` into a filter and apply it; returns a human error string or ``None``.

        A malformed token still applies the best-effort parse (so the box narrows on the good tokens)
        and returns the error for the box to surface — it never raises."""
        new, err = parse_query(text)
        self.set_filter(new)
        return err

    def clear_filter(self) -> None:
        if self._filter.is_active():
            self.set_filter(ScanFilter())

    def filtered_count(self) -> tuple[int, int]:
        """``(visible, total)`` — the "showing N of M" the bar/banner reports."""
        return len(self._rows), len(self._all_rows)

    # ---- filter option providers (populate the bar's dropdowns) ----
    def available_analyzers(self) -> list[str]:
        return sorted({str(u.det) for u in self._all_rows if u.det is not None}, key=str)

    def available_statuses(self) -> list[str]:
        return sorted({u.status for u in self._all_rows if u.status})

    def available_scan_types(self) -> list[str]:
        return sorted({u.scan_type for u in self._all_rows if u.scan_type})

    def available_verdicts(self) -> list[str]:
        out = set()
        for u in self._all_rows:
            v = getattr(u.fit, "verdict", None) if u.fit is not None else None
            if v:
                out.add(str(v))
        return sorted(out)

    def t_extent(self) -> tuple[float | None, float | None]:
        temps = [u.T for u in self._all_rows if isinstance(u.T, (int, float))]
        return (min(temps), max(temps)) if temps else (None, None)

    def q_options(self) -> list[tuple[str, tuple]]:
        """Distinct q bins present as ``(display_label, center_tuple)`` — the bar's q selector.

        Bins on the same ``q_step`` the q auto-groups and the filter predicate use, so selecting an
        option filters to exactly that auto-group's members."""
        q_step = _rounding(self._ws)[1]
        seen: dict[tuple, list] = {}
        for u in self._all_rows:
            if not u.q or any(v is None for v in u.q):
                continue
            try:
                gkey = tuple(int(round(float(v) / q_step)) for v in u.q)
            except (TypeError, ValueError):
                continue
            seen.setdefault(gkey, [round(i * q_step, 6) for i in gkey])
        return [(_fmt_q(center), tuple(center)) for _gkey, center in sorted(seen.items())]

    # ---- multi-detector predicate (drives Analyzer tab + chip label form) ----
    @property
    def multi_detector(self) -> bool:
        """True when the workspace spans more than one analyzer/detector.

        Single-detector instruments (IN8) then read unchanged: no Analyzer tab, no detector suffix on
        chips, and 1-unit scan-groups are suppressed. Reads ``_all_rows`` so an active filter never
        removes the Analyzer tab (the tabs are a property of the workspace, not the current view)."""
        return len({u.det for u in self._all_rows if u.det}) > 1

    def has_field(self) -> bool:
        """True when any unit carries a sample field setpoint (gates the B dimension tab).

        Reads ``_all_rows`` so the B tab's presence does not flicker as the filter narrows the view."""
        return any(u.B is not None for u in self._all_rows)

    # ---- group derivation ----
    def _derive_auto_groups(self) -> list[AutoGroup]:
        t_round, q_step = _rounding(self._ws)
        multi = len({u.det for u in self._rows if u.det}) > 1
        scan_groups: dict = {}      # scan prefix -> (order, [unit keys])
        t_groups: dict = {}
        q_groups: dict = {}
        ana_groups: dict = {}
        b_groups: dict = {}
        scan_order: list = []
        for u in self._rows:
            pref = u.scan_key
            if pref not in scan_groups:
                scan_groups[pref] = u
                scan_order.append(pref)
            if u.T is not None:
                b = round(round(u.T / t_round) * t_round, 6)
                t_groups.setdefault(b, []).append(u.key)
            if u.q and all(v is not None for v in u.q):
                try:
                    gkey = tuple(int(round(float(v) / q_step)) for v in u.q)
                    # The grouping is by rounded bins, not by the first member's exact q.  Display
                    # the bin centre and its width so the header does not claim false precision.
                    disp = [round(i * q_step, 6) for i in gkey]
                except (TypeError, ValueError) as exc:
                    log.debug("auto-group q-bin: non-numeric q %r for %s (%s)", u.q, u.key, exc)
                else:
                    q_groups.setdefault(gkey, (disp, []))[1].append(u.key)
            if u.det is not None:
                ana_groups.setdefault(u.det, []).append(u.key)
            if u.B is not None:
                bb = round(round(u.B / 0.1) * 0.1, 6)
                b_groups.setdefault(bb, []).append(u.key)

        # Members of each source scan, for the scan-dimension groups (built once, order preserved).
        scan_members: dict = {}
        for u in self._rows:
            scan_members.setdefault(u.scan_key, []).append(u.key)

        out: list[AutoGroup] = []
        for pref in scan_order:
            u = scan_groups[pref]
            members = scan_members[pref]
            # A 1-unit scan-group on a single-detector workspace is just the scan itself — suppress it.
            if not multi and len(members) <= 1:
                continue
            out.append(AutoGroup(id=f"auto:scan:{pref}", label=self._scan_label(u), dim="scan",
                                 scan_keys=members))
        for b in sorted(t_groups):
            out.append(AutoGroup(id=f"auto:T:{b:g}", label=f"T = {b:g} K", dim="T",
                                 scan_keys=t_groups[b]))
        for gkey in sorted(q_groups):
            disp, keys = q_groups[gkey]
            out.append(AutoGroup(id=f"auto:q:{','.join(str(v) for v in gkey)}",
                                 label=f"q bin = {disp} (step {q_step:g} rlu)", dim="q",
                                 scan_keys=keys))
        for det in sorted(ana_groups, key=str):
            out.append(AutoGroup(id=f"auto:ana:{det}", label=f"analyzer {det}", dim="ana",
                                 scan_keys=ana_groups[det]))
        for bb in sorted(b_groups):
            out.append(AutoGroup(id=f"auto:B:{bb:g}", label=f"B = {bb:g} T", dim="B",
                                 scan_keys=b_groups[bb]))
        return out

    @staticmethod
    def _scan_label(u: ScanUnit) -> str:
        """``scan NN — #Q [...]`` for a source-scan group header (target #Q from provenance)."""
        num = "?" if u.scan_number is None else u.scan_number
        q = _fmt_q(u.q_target)
        return f"scan {num} — #Q {q}" if q else f"scan {num}"

    def auto_groups(self, dim: str | None = None) -> list[AutoGroup]:
        if dim is None:
            return list(self._auto)
        return [g for g in self._auto if g.dim == dim]

    def manual_groups(self):
        """Ordinary manual groups ordered by ``group_order``; Scan(s) slots belong to its chips."""
        if self._store is None:
            return []
        groups = [group for group in self._store.groups() if group.slot is None]
        by_id = {g.id: g for g in groups}
        order = self._store.group_order()
        ordered = [by_id[gid] for gid in order if gid in by_id]
        ordered += [g for g in groups if g.id not in set(order)]
        return ordered

    def group_labels_for_key(self, unit_key: str) -> list[str]:
        """Manual group names + auto-group labels a unit belongs to (for the Groups column/tooltips)."""
        labels: list[str] = []
        if self._store is not None:
            names = {g.id: g.name for g in self._store.groups()}
            for gid in self._store.memberships_of(unit_key):
                labels.append(names.get(gid, gid))
        labels.extend(self._auto_labels_by_key.get(unit_key, []))
        return labels

    def rows(self) -> list[ScanUnit]:
        return list(self._rows)

    def master_groups(self):
        """Derived pooled masters from the Qt-free facade, filtered to currently visible members."""
        visible = {row.key for row in self._rows}
        groups = []
        for group in getattr(self._ws, "master_groups", ()) or ():
            members = tuple(member for member in group.members
                            if getattr(member, "unit_id", None) in visible)
            if members or not self._filter.is_active():
                groups.append((group, members))
        return groups

    def request_master_inspect(self, fit) -> None:
        self.inspect_requested.emit(fit)

    def available_keys(self) -> set[str]:
        """Stable unit identities present in the ledger (independent of the active filter)."""
        return {u.key for u in self._all_rows}

    # ---- manual membership resolution (via scan_keys.member_matches) ----
    def is_unit_grouped(self, unit_key: str) -> bool:
        """True when some manual group covers this unit (legacy scan-level members included)."""
        return bool(self._store is not None and self._store.memberships_of(unit_key))

    def membership_count(self, unit_key: str) -> int:
        """How many manual groups a unit belongs to — the Pool chip's badge (plan R9). 0 when the
        unit is loose or there is no store."""
        if self._store is None:
            return 0
        return len(self._store.memberships_of(unit_key))

    def units_in_group(self, group) -> list[ScanUnit]:
        """The current *visible* :class:`ScanUnit` rows a manual group's members cover, in row order."""
        members = list(getattr(group, "members", None) or [])
        return [u for u in self._rows if any(member_matches(m, u.key) for m in members)]

    def member_keys(self, gid: str) -> list[str]:
        """ALL persisted member unit-keys of a manual group, resolved against the UNFILTERED ledger.

        This is the "carry this group" payload (plan R8): a drag of a folder header carries every
        stored member regardless of the active filter, so a narrowed view never silently truncates the
        group. Legacy scan-level members expand to every analyzer unit of that scan via
        :func:`~isar_gui.scan_keys.member_matches`; order follows ``_all_rows``."""
        if self._store is None:
            return []
        group = next((g for g in self._store.groups() if g.id == gid), None)
        if group is None:
            return []
        members = list(group.members)
        return [u.key for u in self._all_rows if any(member_matches(m, u.key) for m in members)]

    def can_target(self, target: str, keys) -> tuple[bool, str]:
        """``(ok, reason)`` for sending ``keys`` to a plot ``target`` (dispersion/heatmap).

        Same pure validation the destination dock uses, so the "Send to" menu can grey a verb the
        group can't satisfy (plan R-route). Reads the read-only results off the facade; writes nothing."""
        from isar_gui import group_targets
        return group_targets.accepts(target, getattr(self._ws, "results", []) or [], keys)

    def lens_bins(self, dim: str) -> list[tuple[str, set]]:
        """``[(bin_label, {unit_key, ...}), ...]`` for the ambient lens over dimension ``dim``.

        A thin wrapper over the SAME :meth:`auto_groups` derivation the dimension tabs use, so a lens
        bin means exactly what the corresponding auto-group means. Bins over the FILTERED rows (the
        lens dims within the currently-visible home grid)."""
        return [(g.label, set(g.scan_keys)) for g in self.auto_groups(dim)]

    def siblings_of(self, unit_key: str) -> list[str]:
        """Unit keys of every analyzer sharing this unit's source scan (includes the unit itself).

        Reads ``_all_rows`` so sibling identity is the true scan structure, not the filtered view."""
        pref = scan_prefix(unit_key)
        return [u.key for u in self._all_rows if u.scan_key == pref]

    # ---- collapsed / order / active-tab ui scraps ----
    def is_collapsed(self, gid: str) -> bool:
        return self._store is not None and gid in self._store.collapsed()

    def set_collapsed(self, gid: str, flag: bool) -> None:
        if self._store is None:
            return
        self._store.set_collapsed(gid, flag)
        self.store_dirty.emit()

    def set_group_order(self, order) -> None:
        if self._store is None:
            return
        self._store.set_group_order(order)
        self.store_dirty.emit()

    def active_tab(self) -> str | None:
        return self._store.scans_tab() if self._store is not None else None

    def set_active_tab(self, name: str) -> None:
        if self._store is None:
            return
        self._store.set_scans_tab(name)
        self.store_dirty.emit()

    # ---- group mutations (manual only) ----
    def create_group(self, name: str) -> str | None:
        if self._store is None:
            return None
        gid = self._store.create_group(name)
        self.groups_changed.emit()
        return gid

    def add_to_group(self, gid: str, unit_keys) -> None:
        if self._store is None:
            return
        for key in unit_keys:
            self._store.add_member(gid, key)
        self._model.groups_updated()
        self.groups_changed.emit()

    def remove_from_group(self, gid: str, unit_keys) -> None:
        """Remove specific units from a manual group WITHOUT dropping their siblings.

        A legacy v1 membership is a scan-level key that covers every analyzer of the scan. Removing
        one unit must not silently delete the whole scan's membership, so when the covering member is
        a scan-level key we expand it into the new-form unit keys of the *other* siblings and drop
        only the requested unit. A direct unit-key membership is removed as-is."""
        if self._store is None:
            return
        group = next((g for g in self._store.groups() if g.id == gid), None)
        if group is None:
            return
        targets = {str(k) for k in unit_keys}
        for unit in targets:
            for member in list(group.members):
                if member == unit:
                    self._store.remove_member(gid, member)   # direct unit membership
                elif member_matches(member, unit):
                    # Legacy scan-level (or otherwise covering) membership: expand siblings, drop one.
                    self._store.remove_member(gid, member)
                    for sib in self.siblings_of(unit):
                        if sib != unit:
                            self._store.add_member(gid, sib)
            # Re-read the (now-mutated) member list for the next requested unit.
            group = next((g for g in self._store.groups() if g.id == gid), group)
        self._model.groups_updated()
        self.groups_changed.emit()

    def rename_group(self, gid: str, name: str) -> None:
        if self._store is None:
            return
        self._store.rename_group(gid, name)
        self._model.groups_updated()
        self.groups_changed.emit()

    def delete_group(self, gid: str) -> None:
        if self._store is None:
            return
        self._store.delete_group(gid)
        self._model.groups_updated()
        self.groups_changed.emit()

    # ---- grouping UX (Phase 5): name suggestion + create-from + freeze ----
    def suggest_group_name(self, unit_keys) -> str:
        """A pre-filled name for a group of ``unit_keys``, from the strongest shared dimension.

        Precedence (first that holds for EVERY unit): a shared T-bin -> ``"T = 300 K"``; a shared
        q-bin -> ``"q = [...]"``; a shared analyzer -> ``"analyzer NN"``; a shared source scan ->
        the scan-group label; else ``"Group of N"``. Binning matches the auto-groups' rounding so a
        suggestion means the same thing the derived groups do."""
        want = {str(k) for k in unit_keys}
        units = [u for u in self._rows if u.key in want]
        if not units:
            return f"Group of {len(want)}"
        t_round, q_step = _rounding(self._ws)
        # shared T-bin
        if all(u.T is not None for u in units):
            t_bins = {round(round(u.T / t_round) * t_round, 6) for u in units}
            if len(t_bins) == 1:
                return f"T = {next(iter(t_bins)):g} K"
        # shared q-bin
        if all(u.q and all(v is not None for v in u.q) for u in units):
            try:
                q_bins = {tuple(int(round(float(v) / q_step)) for v in u.q) for u in units}
            except (TypeError, ValueError) as exc:
                log.debug("suggest_group_name: non-numeric q, skipping q-bin (%s)", exc)
                q_bins = set()
            if len(q_bins) == 1:
                disp = [round(float(v), 4) for v in units[0].q]
                return f"q = {disp}"
        # shared analyzer
        if all(u.det is not None for u in units):
            dets = {u.det for u in units}
            if len(dets) == 1:
                return f"analyzer {next(iter(dets))}"
        # shared source scan
        scans = {u.scan_key for u in units}
        if len(scans) == 1:
            return self._scan_label(units[0])
        return f"Group of {len(units)}"

    def create_group_from_merge(self, target_key: str, dragged_keys) -> str | None:
        """iPhone folder birth: drop chip(s) ``dragged_keys`` onto a loose chip ``target_key``.

        The new folder holds ``{target_key} ∪ dragged_keys`` (deduped, target included once), named
        by :meth:`suggest_group_name` so the dock can open it straight into inline-rename. A drop of a
        chip onto itself with no other dragged keys is a no-op (returns ``None``)."""
        if self._store is None:
            return None
        keys: list[str] = [str(target_key)]
        for k in dragged_keys or []:
            k = str(k)
            if k not in keys:
                keys.append(k)
        if len(keys) < 2:
            return None   # self-only merge: nothing to group
        return self.create_group_from(self.suggest_group_name(keys), keys)

    def create_group_from(self, name: str, unit_keys) -> str | None:
        """Create a manual group ``name`` pre-populated with ``unit_keys`` (one structural signal)."""
        if self._store is None:
            return None
        gid = self._store.create_group(name)
        for key in unit_keys:
            self._store.add_member(gid, key)
        self._model.groups_updated()
        self.groups_changed.emit()
        return gid

    def freeze_auto_group(self, auto_id: str) -> str | None:
        """Snapshot an auto-group into a manual group (copy of its members + label).

        The derived auto-group is untouched — it stays a function of the data — so this is a *fork*:
        the manual copy pins today's membership even as the underlying data shifts. An unknown
        ``auto_id`` is a no-op returning ``None``."""
        if self._store is None:
            return None
        ag = next((g for g in self._auto if g.id == auto_id), None)
        if ag is None:
            return None
        gid = self._store.create_group(ag.label)
        for key in ag.scan_keys:
            self._store.add_member(gid, key)
        self._model.groups_updated()
        self.groups_changed.emit()
        return gid

    # ---- selection helpers (shared selection model) ----
    def select_keys(self, keys, clear: bool = True) -> None:
        want = set(keys)
        sel = QItemSelection()
        last_col = self._model.columnCount() - 1
        for i, u in enumerate(self._model.rows()):
            if u.key in want:
                sel.select(self._model.index(i, 0), self._model.index(i, last_col))
        flags = QItemSelectionModel.Select | QItemSelectionModel.Rows
        if clear:
            flags = QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows
        self._selection.select(sel, flags)

    def toggle_key(self, key: str) -> None:
        sel = QItemSelection()
        row = self._model.row_of_key(key)
        if row < 0:
            return
        last_col = self._model.columnCount() - 1
        sel.select(self._model.index(row, 0), self._model.index(row, last_col))
        self._selection.select(sel, QItemSelectionModel.Toggle | QItemSelectionModel.Rows)

    def select_siblings(self, unit_key: str, clear: bool = True) -> None:
        """Select every analyzer unit of the source scan ``unit_key`` belongs to."""
        self.select_keys(self.siblings_of(unit_key), clear=clear)

    def selected_keys(self) -> list[str]:
        keys: list[str] = []
        for idx in self._selection.selectedRows():
            key = idx.data(Qt.UserRole)
            if key and key not in keys:
                keys.append(key)
        return keys

    # ---- inspector hook ----
    def request_inspect(self, unit_key: str) -> None:
        """A click on ``unit_key`` asks the C2 inspector to show it.

        A fitted entry hands its ``FitView`` to the fit inspector (interval ladder / candidates /
        verdict); a not-yet-fitted unit hands its unit-key ``str`` so the inspector shows the raw
        parsed counts + metadata instead. An unknown key emits ``None`` (the empty state). The window
        routes by payload type (``str`` -> ``set_unit``, else ``set_fit``)."""
        for u in self._rows:
            if u.key == unit_key:
                self.inspect_requested.emit(u.fit if (u.status == "fitted" and u.fit is not None)
                                            else u.key)
                return
        self.inspect_requested.emit(None)
