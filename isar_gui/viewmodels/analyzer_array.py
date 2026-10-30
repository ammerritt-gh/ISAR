"""Presentation state for the Analyzer array dock: one scan as the grid of its analyzers.

The dock is a viewer. Everything scientific comes from existing reads: the grid, names, hkl and the delta-hkl
steps from ``workspace_api.analyzer_array``, the bundles and curves from ``workspace_api.spectra``, the
authoritative fit, anchor and lock from the inter-spectra state, the normalisation reference from the Scan(s)
page (hard rule 7). This module only arranges them: which scan, which words, which y range.

A cell's identity is its spectrum key (``experiment:scan:analyzer``); the internal names (A4) and the positions
are labels the layout derives and are never stored against data.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from time import perf_counter
from typing import Literal, NamedTuple

import numpy as np
from PySide6.QtCore import Signal

from isar.workspace_api.analyzer_array import ArrayHeader, StepSummary, array_view
from isar.workspace_api.scan_list import Fact, SpectrumRow
from isar.workspace_api.spectra import (CURVE_UNAVAILABLE, MONITOR_NOT_STORED, NO_ENERGY_ZERO, NO_MONITOR,
                                        NO_PASS, NOT_PARSED, ZERO_BEAMLINE, ZERO_NONE, NotDrawn,
                                        spectrum_bundles)
from isar_gui.inter_words import batch_line, batch_tip
from isar_gui.selection import ORIGIN_ARRAY
from isar_gui.viewmodels.base import ViewModel
from isar_gui.viewmodels.scan_list import fact_cell, hkl_cell
from isar_gui.viewmodels.scans_page import RedrawTiming

SCALES = ("normalised", "raw")
Y_MODES = ("auto", "shared")
STATE_KEY = "analyzer_array"
BATCH_STATES = ("waiting", "fitting", "done", "refused", "failed", "not_reached")   # "held" is never a mark
EMPTY_MESSAGE = "Select a scan or a spectrum in the scan list."
POOLED_MESSAGE = "Pooled masters are not an analyzer scan. Select a scan or a spectrum in the scan list."
LIMIT_REASON = "Used when all cells share one y limit"
Y_PAD = 1.08                 # headroom over the tallest point of a cell
X_PAD = 0.02                 # of the energy extent, each side

# One table for the words that stand where a figure would be (core law 9): the word, and what it means.
ABSENCE_WORDS: Mapping[str, tuple[str, str]] = {
    "no_file": ("no file", "The scan has no file for this analyzer, so there is nothing to draw or select."),
    "not_zeroed": ("not zeroed", "This spectrum is held because no energy zero exists for it yet, so it is not "
                   "drawn on the shared energy axis. The Scan(s) plot can show it on its file axis."),
    "not_read": ("not read", "The file of this analyzer could not be read, so there is nothing to draw."),
    "no_monitor": ("no monitor", "No usable monitor is stored for this spectrum."),
    "no_fit": ("no fit", "This spectrum has no fit yet; its data are drawn."),
    "no_curve": ("no curve", "The fit exists but its curve cannot be drawn; the data are drawn."),
    "other": ("not drawn", "This spectrum cannot be drawn."),
}


@dataclass(frozen=True)
class BatchMark:                         # C-cell-marks
    state: str                           # one of BATCH_STATES
    reason: str | None = None            # plain words; set for refused/failed/not_reached, None otherwise


@dataclass(frozen=True)
class BatchOverlay:                      # C-cell-marks
    batch_id: str
    line: str                            # the header's batch line
    line_tip: str                        # the counts per state, for the hover
    marks: Mapping[str, BatchMark]       # spectrum key -> mark; members only, any scan; held members absent


@dataclass(frozen=True)
class CellPlot:
    token: tuple                         # (bundle.revision, fit_id, scale, reference): redraw only when it changes
    x: np.ndarray = field(compare=False)
    y: np.ndarray = field(compare=False)
    kept: np.ndarray = field(compare=False)
    fit_x: np.ndarray | None = field(compare=False)
    total: np.ndarray | None = field(compare=False)
    components: tuple = field(compare=False)


@dataclass(frozen=True)
class CellState:
    beamline: str
    internal: str
    screen_row: int
    screen_col: int
    key: str | None                      # None: "no file", not selectable
    hkl: str
    hkl_tip: str
    q: str
    q_tip: str
    selected: bool
    anchor: bool
    lock: bool
    absence: str | None                  # a value of ABSENCE_WORDS, or None
    absence_tip: str | None
    plot: CellPlot | None
    y_range: tuple[float, float] | None
    x_range: tuple[float, float] | None
    fit_label: str | None                # "Fast solo", "Batch 2 · sweep 3", "No fit in use"
    beamline_zero: bool = False          # the energy axis is the beamline's file axis, not a fitted zero
    batch: BatchMark | None = None       # C-cell-marks


@dataclass(frozen=True)
class ArrayState:
    body: Literal["message", "grid"] = "message"
    message: str = EMPTY_MESSAGE
    title: str = ""
    title_tip: str = ""
    selection_note: str = ""
    layout_line: str = ""
    unplaced_line: str = ""
    unplaced_tip: str = ""
    scale: str = "normalised"
    y_mode: str = "auto"
    limit_text: str = ""
    limit_placeholder: str = ""
    limit_unit: str = ""
    limit_enabled: bool = False
    limit_reason: str | None = LIMIT_REASON
    x_label: str = ""
    x_tip: str = ""
    y_label: str = ""
    y_tip: str = ""
    caption: str = ""
    n_rows: int = 0
    n_cols: int = 0
    cells: tuple[CellState, ...] = ()
    batch_line: str = ""                 # C-cell-marks
    batch_line_tip: str = ""
    timing: RedrawTiming | None = field(default=None, compare=False)


def overlay_from_members(m, shown: frozenset[str]) -> BatchOverlay:
    """The marks and header sentence for the controller's member map ``m`` (``BatchMembers``) while the cells of
    ``shown`` are drawn. Members in other scans are counted in the sentence and have no cell; held members (anchors,
    locks) get no mark. Before the batch's first members event (``m.stage is None``) there are no marks and the
    placeholder's empty counts are never read."""
    if m.stage is None:
        line = batch_line(m.kind, m.direction, None, None, 0, 0, m.ended)
        return BatchOverlay(m.batch_id, line, batch_tip({}, m.ended_reason), {})
    marks = {k: BatchMark(s, m.reasons.get(k)) for k, s in m.states.items() if s in BATCH_STATES}
    elsewhere = sum(1 for k in marks if k not in shown)
    line = batch_line(m.kind, m.direction, m.stage, m.index, m.counts.get("done", 0),
                      m.counts.get("total", 0) - m.counts.get("fixed", 0), m.ended, elsewhere)
    return BatchOverlay(m.batch_id, line, batch_tip(m.states, m.ended_reason), marks)


class Drawn(NamedTuple):
    """What one spectrum's cell shows: an absence word (with its hover) or a plot, and the labels that go with it."""
    absence: str | None
    tip: str | None
    plot: CellPlot | None = None
    unit: str | None = None
    beamline_zero: bool = False
    fit_label: str | None = None


_NO_FILE = Drawn("no_file", None)


def _fact_words(fact: Fact, missing: str) -> str:
    return missing if fact.value is None else fact_cell(fact).text


def _three(values) -> str:
    return "(" + ", ".join(f"{round(v, 3) + 0.0:.3f}" for v in values) + ")"


def _step_label(head: str, per: str, steps: StepSummary) -> tuple[str, str]:
    """The axis words with the raw mean step, and the hover with the step between each pair of neighbours."""
    if steps.mean is None:
        return f"{head}   no hkl to compute the step", "No pair of neighbouring analyzers has an hkl, so no step is shown."
    note = f" (from {steps.used} of {steps.possible} pairs)" if steps.used < steps.possible else ""
    tip = "Step in hkl (r.l.u.) between each pair of neighbours:\n" + "\n".join(
        f"{p.a} → {p.b}  (" + ", ".join(f"{v:.12g}" for v in p.delta) + ")" for p in steps.pairs)     # 12 figures: every digit an hkl carries, none of a difference's float noise
    return f"{head}   Δhkl ≈ {_three(steps.mean)} per {per}{note}", tip


def _auto_range(plot: CellPlot) -> tuple[float, float] | None:
    y = plot.y[np.isfinite(plot.y)]
    tops = [plot.y[plot.kept & np.isfinite(plot.y)]] if plot.kept.any() else [y]
    if plot.total is not None:
        total = plot.total[np.isfinite(plot.total)]
        tops.append(total)
        y = np.concatenate([y, total])
    tops = [t for t in tops if t.size]
    if not y.size or not tops:
        return None
    top = max(float(t.max()) for t in tops)
    return min(0.0, float(y.min())), (Y_PAD * top if top > 0 else 1.0)


class AnalyzerArrayViewModel(ViewModel):
    state_changed = Signal(object)
    marks_moved = Signal()                   # a batch's member states moved: the dock redraws the marks, reads nothing

    def __init__(self, ws, scan_list_vm, reference_source: Callable, parent=None):
        super().__init__(ws, parent)
        self.refreshed.connect(self._forget_read)     # connected first, so a refresh forgets the read before the dock redraws
        self.scan_list = scan_list_vm
        self.selection = scan_list_vm.selection
        self.reference_source = reference_source        # the Scan(s) page's reference_for: one reference, shared
        self.inter = None
        self.scale = "normalised"
        self.y_mode = "auto"
        self.limits: dict[str, float | None] = {"raw": None, "normalised": None}
        self.limit_error: str | None = None
        self.active = False                              # the dock does nothing while hidden
        self._overlay: BatchOverlay | None = None
        self._queue = None                               # the controller whose member map the marks show
        self._read = None                                # (snapshot, shown cells and fit choice, bundles) of the last read
        self._scaled: dict = {}
        self._state = ArrayState()

    # ---- controls ------------------------------------------------------------------------------
    def set_active(self, active: bool) -> None:
        self.active = bool(active)

    def set_inter(self, inter) -> None:
        self.inter = inter
        if inter is not None:
            inter.changed.connect(self.refreshed)

    def set_scale(self, value: str) -> None:
        if value not in SCALES:
            raise ValueError(f"unknown scale: {value}")
        if value != self.scale:
            self.scale, self.limit_error = value, None
            self.refreshed.emit()

    def set_y_mode(self, value: str) -> None:
        if value not in Y_MODES:
            raise ValueError(f"unknown y mode: {value}")
        if value != self.y_mode:
            self.y_mode = value
            self.refreshed.emit()

    def set_limit(self, scale: str, text: str) -> bool:
        """Type the shared y limit of ``scale``. Empty clears it; text that is not a number above zero is
        refused, the previous limit stays and ``limit_error`` says why. Returns whether the text was taken."""
        if scale not in SCALES:
            raise ValueError(f"unknown scale: {scale}")
        text = (text or "").strip()
        value = None
        if text:
            try:
                value = float(text)
            except ValueError:
                value = math.nan
            if not math.isfinite(value) or value <= 0:
                self.limit_error = f"“{text}” was not used: type a number above zero, or leave the box empty"
                self.refreshed.emit()
                return False
        self.limit_error = None
        if value != self.limits[scale]:
            self.limits[scale] = value
        self.refreshed.emit()
        return True

    def follow_queue(self, ctrl) -> None:
        """Show the marks of ``ctrl``'s newest batch (its ``batch_members()``, C-members-event). The map is read
        again when the state is built, so a burst of events costs one read per dock redraw and a scan switch sees the
        current map; curves never move on a members event (a cell redraws its plot only when its data token changes)."""
        self._queue = ctrl
        ctrl.members_changed.connect(self._members_moved)     # a bound method: the window's queue outlives a rebind
        self.refreshed.emit()

    def _members_moved(self, _item_id: str) -> None:
        self.marks_moved.emit()                  # the marks only: the spectra read so far stays (see _build)

    def _forget_read(self) -> None:
        self._read = None                        # any other refresh may mean the files changed: read them again

    def set_batch_overlay(self, overlay: BatchOverlay | None) -> None:
        """The batch marks to draw (memory only; ``None`` clears them). An equal overlay changes nothing.
        Ignored once ``follow_queue`` has made the controller the source."""
        if overlay != self._overlay:
            self._overlay = overlay
            self.refreshed.emit()

    def save_view_state(self, session) -> None:
        session.view_state[STATE_KEY] = {"scale": self.scale, "y_mode": self.y_mode,
                                         "limit_raw": self.limits["raw"],
                                         "limit_normalised": self.limits["normalised"]}

    def load_view_state(self, session) -> None:
        outer = getattr(session, "view_state", None)
        saved = outer.get(STATE_KEY) if isinstance(outer, dict) else None
        saved = saved if isinstance(saved, dict) else {}
        scale, mode = saved.get("scale"), saved.get("y_mode")
        self.scale = scale if isinstance(scale, str) and scale in SCALES else "normalised"
        self.y_mode = mode if isinstance(mode, str) and mode in Y_MODES else "auto"
        for name in SCALES:
            value = saved.get(f"limit_{name}")
            ok = isinstance(value, (int, float)) and not isinstance(value, bool) \
                and math.isfinite(value) and value > 0
            self.limits[name] = float(value) if ok else None
        self.limit_error = None
        self.refreshed.emit()

    # ---- gestures ------------------------------------------------------------------------------
    def click_cell(self, key: str | None, ctrl: bool = False) -> None:
        """A click on a cell selects that spectrum in the scan list (Ctrl toggles it). A cell without a
        spectrum does nothing. The dock keeps no selection of its own."""
        if key is None or self.scan_list.snapshot.spectrum(key) is None:
            return
        selected = self.selection.keys
        if not ctrl:
            self.selection.set((key,), clicked=key, origin=ORIGIN_ARRAY)
        elif key in selected:
            self.selection.set([k for k in selected if k != key], origin=ORIGIN_ARRAY, extend=True)
        else:
            self.selection.set([*selected, key], clicked=key, origin=ORIGIN_ARRAY, extend=True)

    def shown_keys(self) -> frozenset[str]:
        """The spectrum keys of the cells drawn now (the shown scan's)."""
        return frozenset(c.key for c in self._state.cells if c.key is not None)

    @property
    def state(self) -> ArrayState:
        return self._state

    # ---- the state -----------------------------------------------------------------------------
    def _shown_set(self, snapshot) -> tuple[str | None, int, bool]:
        """The scan set of the first picked spectrum, how many scans are selected, and whether anything
        selected is only a pooled master."""
        shown, sets, other = None, set(), False
        for key in self.selection.picks:
            row = snapshot.spectrum(key)
            if isinstance(row, SpectrumRow):
                shown = shown or row.set_key
                sets.add(row.set_key)
            elif row is not None:
                other = True
        return shown, len(sets), other

    def recompute(self) -> ArrayState:
        if not self.active:
            return self._state
        started = perf_counter()
        state = self._build(started)
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)
        return self._state

    def _controls(self) -> dict:
        limit = self.limits[self.scale]
        enabled = self.y_mode == "shared"
        return dict(scale=self.scale, y_mode=self.y_mode, limit_enabled=enabled,
                    limit_text="" if limit is None else f"{limit:g}",
                    limit_reason=(self.limit_error or None) if enabled else LIMIT_REASON,
                    limit_unit=_unit_words(self.scale))

    def _message(self, text: str, **header) -> ArrayState:
        return ArrayState(message=text, **header, **self._controls())

    def _build(self, started: float) -> ArrayState:
        snapshot = self.scan_list.snapshot
        set_key, n_scans, pooled = self._shown_set(snapshot)
        if set_key is None:
            return self._message(POOLED_MESSAGE if pooled else EMPTY_MESSAGE)
        view = array_view(snapshot, set_key, self._ws)
        if view is None:
            return self._message(EMPTY_MESSAGE)
        head = view.header
        if view.layout is None:
            name = head.instrument or "this instrument"
            return self._message(f"There is no analyzer layout for {name}; this view covers BL43LXU for now.",
                                 **_header_words(head, n_scans))

        read_started = perf_counter()
        keyed = [c for c in view.cells if c.key is not None and snapshot.spectrum(c.key) is not None]
        choice = {}
        for c in keyed:
            inter = self.inter.row(c.key) if self.inter is not None else None
            if inter is not None and inter.fit_id not in (None, "fast"):
                choice[c.key] = inter.fit_id
        # the files are read again on a refresh, or when the snapshot, the shown cells or the fit in use change (a members event keeps them)
        read_key = (tuple(c.key for c in keyed), tuple(sorted(choice.items())))
        if self._read is None or self._read[0] is not snapshot or self._read[1] != read_key:
            rows = [snapshot.spectrum(k) for k in read_key[0]]
            kw = {"fit_choice": choice} if choice else {}
            self._read = (snapshot, read_key, dict(zip(read_key[0], spectrum_bundles(
                self._ws, rows, cache=self.scan_list.spectra_cache, **kw))))
        bundles = self._read[2]
        reference = None if self.scale == "raw" else self.reference_source(snapshot)
        read_s = perf_counter() - read_started

        drawn = {c.key: self._draw(bundles[c.key], reference, choice.get(c.key)) for c in keyed}
        unit = next((d.unit for d in drawn.values() if d.unit), None)
        autos = {k: r for k, d in drawn.items() if d.plot is not None and (r := _auto_range(d.plot)) is not None}
        limit = self.limits[self.scale]
        if limit is None and autos:
            limit = max(v[1] for v in autos.values())
        x_range = self._x_range(d.plot for d in drawn.values())

        selected = frozenset(self.selection.keys)
        members = self._queue.batch_members() if self._queue is not None else None
        overlay = overlay_from_members(members, frozenset(drawn)) if members is not None else (
            self._overlay if self._queue is None else None)
        marks = overlay.marks if overlay is not None else {}
        cells = []
        for c in view.cells:
            d = drawn.get(c.key, _NO_FILE)           # a key the list no longer has reads as no file, never a crash
            absence, tip, plot = d.absence, d.tip, d.plot
            key = c.key if c.key in drawn else None
            hkl_c, q_c = hkl_cell(c.hkl), fact_cell(c.q_abs, with_unit=False)
            q_text = "" if key is None else f"|Q| {q_c.text} nm⁻¹" if c.q_abs.value is not None else "|Q| —"
            inter = self.inter.row(key) if (self.inter is not None and key) else None
            if self.y_mode == "shared":
                y_range = (0.0, limit) if plot is not None and limit is not None else None
            else:
                y_range = autos.get(c.key)
            cells.append(CellState(
                beamline=c.beamline, internal=c.internal, screen_row=c.screen_row, screen_col=c.screen_col,
                key=key, hkl=hkl_c.text, hkl_tip=hkl_c.tooltip or hkl_c.text,
                q=q_text, q_tip=q_c.tooltip or q_c.text,
                selected=key in selected if key else False,
                anchor=bool(key and self.scan_list.is_anchor(key)),
                lock=bool(inter is not None and inter.locked),
                absence=ABSENCE_WORDS[absence][0] if absence else None,
                absence_tip=(tip or ABSENCE_WORDS[absence][1]) if absence else None,
                plot=plot, y_range=y_range, x_range=x_range,
                fit_label=d.fit_label if plot is not None else None, beamline_zero=d.beamline_zero,
                batch=marks.get(key) if key else None))

        x_label, x_tip = _step_label("In plane — 2θ increases ←", "analyzer", view.in_plane)
        y_label, y_tip = _step_label("Out of plane ↑", "row", view.out_of_plane)
        unplaced = view.unplaced
        shown_unit = unit or _unit_words(self.scale)
        total = perf_counter() - started
        return ArrayState(
            body="grid", **_header_words(head, n_scans),
            layout_line=f"Layout {view.layout.id} ({view.how})",
            unplaced_line=(f"{len(unplaced)} spectrum not in layout {view.layout.id}: {', '.join(_tail(unplaced))}"
                           if len(unplaced) == 1 else
                           f"{len(unplaced)} spectra not in layout {view.layout.id}: {', '.join(_tail(unplaced))}"
                           ) if unplaced else "",
            unplaced_tip="\n".join(unplaced), **{**self._controls(), "limit_unit": shown_unit},
            limit_placeholder=f"largest shown ({limit:g})" if limit is not None else "largest shown",
            x_label=x_label, x_tip=x_tip, y_label=y_label, y_tip=y_tip,
            caption=f"x: energy transfer (meV), the same in every cell · y: {shown_unit}, linear",
            n_rows=view.layout.n_rows, n_cols=view.layout.n_cols, cells=tuple(cells),
            batch_line=overlay.line if overlay else "", batch_line_tip=overlay.line_tip if overlay else "",
            timing=RedrawTiming(len(keyed), read_s, total - read_s, 0.0, total))

    def _scaled_for(self, bundle, reference):
        token = (bundle.key, bundle.revision, bundle.fit_id, self.scale, reference)
        scaled = self._scaled.get(token)
        if scaled is None:
            if len(self._scaled) > 256:          # shortcut: a whole-cache reset, fine for a few scans in play
                self._scaled.clear()
            scaled = self._scaled[token] = bundle.scaled(self.scale, reference)
        return scaled

    def _draw(self, bundle, reference, chosen: str | None = None) -> Drawn:
        """What one spectrum's cell shows: the plot, or the word that stands in for it. ``chosen`` is the refit in use
        (None: the fast solo); a bundle that came back as another fit draws its data with "no curve", never that fit."""
        nd = bundle.not_drawn
        if nd is not None:
            code = {NO_ENERGY_ZERO: "not_zeroed", NOT_PARSED: "not_read"}.get(nd.code, "other")
            return Drawn(code, nd.text if code == "other" else None)
        if bundle.zero == ZERO_NONE:
            return Drawn("not_zeroed", None)
        scaled = self._scaled_for(bundle, reference)
        unit, bzero = scaled.y_unit, bundle.zero == ZERO_BEAMLINE
        if scaled.not_drawn is not None:
            code = "no_monitor" if scaled.not_drawn.code in (NO_MONITOR, MONITOR_NOT_STORED) else "other"
            return Drawn(code, scaled.not_drawn.text, None, unit, bzero)
        fast = scaled.passes.get("fast")
        fitted = None if fast is None or isinstance(fast, NotDrawn) else fast
        absence = tip = None
        if chosen is not None and bundle.fit_id != chosen:       # the read fell back to the solo: no other fit stands in
            fitted, absence = None, "no_curve"
        elif fitted is None:
            code = fast.code if fast is not None else NO_PASS
            absence = "no_fit" if code == NO_PASS else "no_curve" if code == CURVE_UNAVAILABLE else "other"
            tip = fast.text if absence != "no_fit" else None
        plot = CellPlot((bundle.key, bundle.revision, bundle.fit_id, self.scale, reference),
                        scaled.x, scaled.y, scaled.kept,
                        None if fitted is None else scaled.fine_x, None if fitted is None else fitted.total,
                        () if fitted is None else tuple(c.values for c in fitted.components))
        return Drawn(absence, tip, plot, unit, bzero, None if fitted is None else bundle.fit_label)

    @staticmethod
    def _x_range(plots) -> tuple[float, float] | None:
        """The union of the cells' energy extents, padded: every cell has the same energy axis."""
        spans = [v for p in plots if p is not None for v in (p.x, p.fit_x) if v is not None and v.size]
        spans = [v[np.isfinite(v)] for v in spans]
        spans = [v for v in spans if v.size]
        if not spans:
            return None
        lo, hi = min(float(v.min()) for v in spans), max(float(v.max()) for v in spans)
        pad = X_PAD * (hi - lo) if hi > lo else 1.0
        return lo - pad, hi + pad


def _header_words(head: ArrayHeader, n_scans: int) -> dict:
    """The header's title and selection note, for the grid and for the no-layout message alike."""
    parts = [head.experiment_id if head.experiment_id and head.experiment_id != head.scan_label else None,
             f"scan {head.scan_label}", _fact_words(head.sample, "sample not named"),
             _fact_words(head.temperature, "T missing"),
             ("hkl " + hkl_cell(head.hkl_nominal).text) if head.hkl_nominal.value is not None
             else "nominal hkl missing"]
    gaps = [f"{name}: {fact.reason}" for name, fact in (("sample", head.sample), ("T", head.temperature),
                                                         ("nominal hkl", head.hkl_nominal))
            if fact.value is None and fact.reason]          # a missing word says why on hover
    return dict(
        title=" · ".join(p for p in parts if p),
        title_tip=f"Scan {head.scan_label} of {head.experiment_id or 'this experiment'}: "
                  f"{head.n_spectra} spectra. The hkl is the scan's nominal position."
                  + "".join(f"\n{gap}" for gap in gaps),
        selection_note=(f"Showing scan {head.scan_label} — {n_scans} scans selected, only one is shown"
                        if n_scans > 1 else ""))


def _unit_words(scale: str) -> str:
    return "Counts" if scale == "raw" else "Counts per reference monitor"


def _tail(keys) -> list[str]:
    return [k.rsplit(":", 1)[-1] for k in keys]
