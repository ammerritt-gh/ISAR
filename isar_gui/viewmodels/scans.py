"""ScansViewModel + ScanTableModel — the scan as the fundamental visible unit (Phase E).

Two views share ONE model here: the physical chip dock (:mod:`isar_gui.views.scans_dock`) and the
table dock (:mod:`isar_gui.views.scan_table_dock`). Both read :class:`ScanTableModel` and both drive
the SAME :class:`~PySide6.QtCore.QItemSelectionModel`, so a scan selected in one view lights up in
the other — and, because a scan can belong to several groups, in every box it appears in.

Model rows are PHYSICAL scans: ``ws.scans`` is a per-detector ledger (keys ``"exp:scan:det"``); this
collapses those to one row per ``("exp", "scan")`` with the detector ids joined into a cell/tooltip.
The membership key used by groups is the scan-level ``"exp:scan"`` form (:attr:`PhysicalScan.key`).

Groups are of two kinds:
  * **auto-groups** derived from metadata (T-bins and q-bins) — membership is a function of the data,
    never stored, and drops onto them are rejected;
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

from isar_gui.viewmodels.base import ViewModel

log = logging.getLogger(__name__)

# Columns: # | File | Type | T | q/HKL | Status | Groups
COL_NUM, COL_FILE, COL_TYPE, COL_T, COL_Q, COL_STATUS, COL_GROUPS = range(7)
_HEADERS = ["#", "File", "Type", "T", "q / HKL", "Status", "Groups"]

_HIGHLIGHT_MS = 2000
_HIGHLIGHT_COLOR = QColor(255, 243, 176)  # soft amber, matches the Files dock new-row tint

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
    except (TypeError, ValueError):
        return ""


# ---------------------------------------------------------------------------
# Physical scan row (a collapse of the per-detector ledger)
# ---------------------------------------------------------------------------
@dataclass
class PhysicalScan:
    key: str                 # scan-level "exp:scan" — the membership key
    exp: str | None
    scan_number: int | None
    scan_type: str
    source_file: str | None
    T: float | None          # noqa: N815 — matches ScanView.T
    hkl: list | None
    status: str              # representative status (highest-precedence across detectors)
    detectors: list          # detector ids present for this physical scan
    inspect_fit: object | None = None   # a fitted detector's FitView for the C2 inspector, if any

    @property
    def file_name(self) -> str:
        return Path(self.source_file).name if self.source_file else ""


# Status precedence for the collapsed representative (a scan with one fitted detector reads "fitted").
_STATUS_RANK = {"fitted": 5, "held_for_calibration": 4, "held_for_sample": 4,
                "excluded": 3, "non_result": 2, "context_applied": 1, "ingested": 0}


def collapse_scans(scan_views) -> list[PhysicalScan]:
    """Collapse a per-detector ``ws.scans`` list to one :class:`PhysicalScan` per ``(exp, scan)``.

    Detector ids are unioned into :attr:`PhysicalScan.detectors`; the representative status is the
    highest-precedence status seen; T / source_file / hkl are taken from the first entry that carries
    each (they are scan-level provenance, identical across detectors). Insertion order is preserved.
    """
    order: list = []
    by_key: dict = {}
    for sv in scan_views:
        exp = getattr(sv, "experiment_id", None)
        num = getattr(sv, "scan_number", None)
        key = f"{exp}:{num}" if exp is not None else str(num)
        ps = by_key.get(key)
        if ps is None:
            ps = PhysicalScan(
                key=key, exp=exp, scan_number=num,
                scan_type=getattr(sv, "scan_type", "") or "",
                source_file=getattr(sv, "source_file", None),
                T=getattr(sv, "T", None), hkl=getattr(sv, "hkl", None),
                status=getattr(sv, "status", "") or "", detectors=[])
            by_key[key] = ps
            order.append(key)
        else:
            # Fill scan-level fields the first entry lacked.
            if ps.source_file is None:
                ps.source_file = getattr(sv, "source_file", None)
            if ps.T is None:
                ps.T = getattr(sv, "T", None)
            if ps.hkl is None:
                ps.hkl = getattr(sv, "hkl", None)
            cand = getattr(sv, "status", "") or ""
            if _STATUS_RANK.get(cand, -1) > _STATUS_RANK.get(ps.status, -1):
                ps.status = cand
        for det_id, fit in (getattr(sv, "detectors", None) or {}).items():
            if det_id not in ps.detectors:
                ps.detectors.append(det_id)
            if ps.inspect_fit is None and getattr(sv, "status", "") == "fitted":
                ps.inspect_fit = fit
    return [by_key[k] for k in order]


# ---------------------------------------------------------------------------
# Auto-group
# ---------------------------------------------------------------------------
@dataclass
class AutoGroup:
    id: str
    label: str
    scan_keys: list = field(default_factory=list)


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
    """Physical-scan rows for the table AND the chip dock (shared model, shared selection).

    ``Qt.UserRole`` serves the scan key. New keys since the last :meth:`set_rows` get a transient
    background tint cleared by one shared ~2 s timer. The Groups cell is derived on demand from a
    callable the VM injects (so a groups change repaints that column without rebuilding rows)."""

    highlight_changed = Signal()   # tint set or cleared — the chip dock repaints on this

    def __init__(self, group_labels_for=None, parent=None):
        super().__init__(parent)
        self._rows: list[PhysicalScan] = []
        self._prev_keys: set[str] = set()
        self._highlight: set[str] = set()
        # Injected by the VM: scan_key -> list[str] of group labels (manual names + auto labels).
        self._group_labels_for = group_labels_for or (lambda _key: [])
        self._hl_timer = QTimer(self)
        self._hl_timer.setSingleShot(True)
        self._hl_timer.setInterval(_HIGHLIGHT_MS)
        self._hl_timer.timeout.connect(self._clear_highlight)

    # ---- rebuild ----
    def set_rows(self, rows: list[PhysicalScan]) -> None:
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
    def rows(self) -> list[PhysicalScan]:
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
        if role == Qt.BackgroundRole:
            return _HIGHLIGHT_COLOR if row.key in self._highlight else None
        if role == Qt.ToolTipRole:
            if col == COL_FILE:
                return row.source_file or ""
            if col == COL_STATUS:
                return status_tooltip(row.status)
            if col in (COL_NUM, COL_TYPE) and row.detectors:
                return "detectors: " + ", ".join(str(d) for d in row.detectors)
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
            if col == COL_Q:
                return _fmt_q(row.hkl)
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
    """Owns the shared model + selection, the GuiStore handle, and group derivation (Phase E)."""

    #: Structural group change (create/delete/rename/membership/order) — dock rebuilds, window persists.
    groups_changed = Signal()
    #: A ui-scrap mutation (collapsed/order) the window should debounce-persist without a rebuild.
    store_dirty = Signal()
    #: A double-clicked fitted scan asked for the C2 inspector (payload: FitView | None).
    inspect_requested = Signal(object)

    def __init__(self, ws, store=None, parent=None):
        super().__init__(ws, parent)
        self._store = store  # GuiStore | None (None in the empty/pending state)
        self._rows: list[PhysicalScan] = []
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
        self._rows = collapse_scans(scan_views)
        self._auto = self._derive_auto_groups()
        labels: dict[str, list[str]] = {}
        for ag in self._auto:
            for key in ag.scan_keys:
                labels.setdefault(key, []).append(ag.label)
        self._auto_labels_by_key = labels
        # set_rows resets the model, which clears the shared selection. Capture the live selection
        # first and re-select the keys that survived the rebuild, so an ingest tick that merely adds
        # rows does not drop the operator's current selection out from under them.
        selected = self.selected_keys()
        self._model.set_rows(self._rows)
        if selected:
            survivors = [k for k in selected if self._model.row_of_key(k) >= 0]
            if survivors:
                self.select_keys(survivors)

    # ---- group derivation ----
    def _derive_auto_groups(self) -> list[AutoGroup]:
        t_round, q_step = _rounding(self._ws)
        t_groups: dict = {}
        q_groups: dict = {}
        for ps in self._rows:
            if ps.T is not None:
                b = round(round(ps.T / t_round) * t_round, 6)
                t_groups.setdefault(b, []).append(ps.key)
            if ps.hkl and all(v is not None for v in ps.hkl):
                try:
                    gkey = tuple(int(round(float(v) / q_step)) for v in ps.hkl)
                    disp = [round(float(v), 4) for v in ps.hkl]
                except (TypeError, ValueError):
                    continue
                q_groups.setdefault(gkey, (disp, []))[1].append(ps.key)
        out: list[AutoGroup] = []
        for b in sorted(t_groups):
            out.append(AutoGroup(id=f"auto:T:{b:g}", label=f"T = {b:g} K",
                                 scan_keys=t_groups[b]))
        for gkey in sorted(q_groups):
            disp, keys = q_groups[gkey]
            out.append(AutoGroup(id=f"auto:q:{','.join(str(v) for v in gkey)}",
                                 label=f"q = {disp}", scan_keys=keys))
        return out

    def auto_groups(self) -> list[AutoGroup]:
        return list(self._auto)

    def manual_groups(self):
        """Manual groups from the store, ordered by ``group_order`` (unlisted appended in store order)."""
        if self._store is None:
            return []
        groups = self._store.groups()
        by_id = {g.id: g for g in groups}
        order = self._store.group_order()
        ordered = [by_id[gid] for gid in order if gid in by_id]
        ordered += [g for g in groups if g.id not in set(order)]
        return ordered

    def group_labels_for_key(self, scan_key: str) -> list[str]:
        """Manual group names + auto-group labels a scan belongs to (for the Groups column/tooltips)."""
        labels: list[str] = []
        if self._store is not None:
            names = {g.id: g.name for g in self._store.groups()}
            for gid in self._store.memberships_of(scan_key):
                labels.append(names.get(gid, gid))
        labels.extend(self._auto_labels_by_key.get(scan_key, []))
        return labels

    def rows(self) -> list[PhysicalScan]:
        return list(self._rows)

    # ---- collapsed / order ui scraps ----
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

    # ---- group mutations (manual only) ----
    def create_group(self, name: str) -> str | None:
        if self._store is None:
            return None
        gid = self._store.create_group(name)
        self.groups_changed.emit()
        return gid

    def add_to_group(self, gid: str, scan_keys) -> None:
        if self._store is None:
            return
        for key in scan_keys:
            self._store.add_member(gid, key)
        self._model.groups_updated()
        self.groups_changed.emit()

    def remove_from_group(self, gid: str, scan_keys) -> None:
        if self._store is None:
            return
        for key in scan_keys:
            self._store.remove_member(gid, key)
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

    # ---- selection helpers (shared selection model) ----
    def select_keys(self, keys, clear: bool = True) -> None:
        want = set(keys)
        sel = QItemSelection()
        last_col = self._model.columnCount() - 1
        for i, ps in enumerate(self._model.rows()):
            if ps.key in want:
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

    def selected_keys(self) -> list[str]:
        keys: list[str] = []
        for idx in self._selection.selectedRows():
            key = idx.data(Qt.UserRole)
            if key and key not in keys:
                keys.append(key)
        return keys

    # ---- inspector hook ----
    def request_inspect(self, scan_key: str) -> None:
        """A double-click on ``scan_key``: hand its fitted detector to the C2 inspector (if any)."""
        for ps in self._rows:
            if ps.key == scan_key:
                self.inspect_requested.emit(ps.inspect_fit)
                return
        self.inspect_requested.emit(None)
