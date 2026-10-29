"""Presentation state for Scan(s); all scientific values come from spectrum bundles."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from time import perf_counter

from PySide6.QtCore import Signal

from isar.plotspec.builders import ScansCurve, ScansTrace, scans_spec, scans_residual_spec, stack_step
from isar.plotspec.spec import PlotSpec
from isar.workspace_api.spectra import (NotDrawn, ZERO_BEAMLINE, ZERO_NONE, normalisation_reference,
                                        spectrum_bundles)
from isar.workspace_api.scan_list import PooledRow
from isar_gui.inter_words import CONDITIONAL_ERRORS, ERRORS_UNAVAILABLE
from isar_gui.selection import identity, unique_labels
from isar_gui.viewmodels.base import ViewModel
from isar_gui.viewmodels.inter_fits import BarState, bar_state

MAX_PLOTTED = 10
PASSES = ('none', 'fast', 'deep')
LAYOUTS = ('offset', 'overlay')
SCALES = ('raw', 'normalised')
STATE_KEY = 'scans_page'
EMPTY, PLOT, LIST = range(3)
DEEP_TOOLTIP = 'Deep pass: the slow nested-sampling refit.'
ENERGY_LABEL = 'Energy transfer (meV)'
NOT_ZEROED_LABEL = 'Energy, not zeroed (meV)'
WAITING_FOR_ZERO = 'waiting for the fast pass to set the energy zero'
WATERFALL_LAYOUT = "waterfall of one spectrum's fits"


@dataclass(frozen=True)
class Control:
    enabled: bool
    reason: str | None = None


@dataclass(frozen=True)
class OverLimitGroup:
    title: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class RedrawTiming:
    n: int
    read_s: float
    build_s: float
    render_s: float
    total_s: float


@dataclass(frozen=True)
class PageState:
    body: int = EMPTY
    view: str | None = None
    main_spec: PlotSpec | None = None
    residual_spec: PlotSpec | None = None
    not_drawn_text: str = ''
    not_drawn_tip: str = ''
    over_limit_note: str = ''
    over_limit: tuple[OverLimitGroup, ...] = ()
    fit: Control = Control(False, 'select spectra')
    layout: Control = Control(False, 'select spectra')
    offset: Control = Control(False, 'applies to the Offset layout')
    step: float | None = None
    step_auto: bool = True
    deep_tooltip: str = DEEP_TOOLTIP
    reset_range: bool = False
    timing: RedrawTiming | None = None
    keys: tuple[str, ...] = ()
    not_drawn: tuple[tuple[str, str], ...] = ()
    traces: tuple[ScansTrace, ...] = ()
    curves_header: str | None = None
    fit_value: str = 'none'
    scale_value: str = 'normalised'
    inter: BarState | None = None


class ScansPageViewModel(ViewModel):
    state_changed = Signal(object)
    store_dirty = Signal()

    def __init__(self, ws, scan_list_vm, parent=None):
        super().__init__(ws, parent)
        self.scan_list = scan_list_vm
        self.selection = scan_list_vm.selection
        self.fit = 'none'
        self.layout = 'offset'
        self.scale = 'normalised'
        self.hand_step = None
        self.zero_lines = True
        self.peak_lines = True
        self.filled_symbols = False
        self._store = None
        self._reference = None
        self._reference_snapshot = None
        self._read_snapshot = None
        self._read_keys = None
        self._bundles = ()
        self._scaled_token = None
        self._scaled = ()
        self._signature = None
        self._frame = None
        self._state = PageState()
        self.inter = None
        self.chosen = {}          # active spectrum key -> fit id; session-local, never saved (hard rule 7)
        self.waterfall = False
        self._active = None
        self._fits = ()           # per bundle: (fit_id, label, stale, errors_available)
        self._wf_entries = ()
        self._wf_total = 0
        self._wf_on = False

    def _set(self, name, value, choices):
        if value not in choices:
            raise ValueError(f'unknown {name}: {value}')
        if getattr(self, name) != value:
            setattr(self, name, value)
            self.refreshed.emit()

    def set_display_option(self, name, value):
        if name not in ("zero_lines", "peak_lines", "filled_symbols"):
            raise ValueError(f"unknown display option: {name}")
        self._set(name, value, (True, False))

    def set_fit(self, value):
        self._set('fit', value, PASSES)

    def set_layout(self, value):
        self._set('layout', value, LAYOUTS)

    def set_scale(self, value):
        if value not in SCALES:
            raise ValueError(f'unknown scale: {value}')
        if value != self.scale:
            self.hand_step = None
            self._set('scale', value, SCALES)

    def set_step(self, value):
        if value is not None:
            value = float(value)
            if not math.isfinite(value) or value < 0:
                raise ValueError('offset must be finite and non-negative')
        if value != self.hand_step:
            self.hand_step = value
            self.refreshed.emit()

    def set_inter(self, inter):
        """Read the inter-spectra state; a change only repaints, writers run on the user's clicks."""
        self.inter = inter
        if inter is not None:
            inter.changed.connect(self.refreshed)
            inter.history_ready.connect(lambda _key: self.refreshed.emit())
        self.refreshed.emit()

    def choose_fit(self, fit_id):
        if fit_id is None:
            self.chosen = {}
        elif self._active is None:
            return
        else:
            self.chosen = {self._active: fit_id}
            self.fit = 'fast'    # a refit shows in the fast slot only
        self.refreshed.emit()

    def set_waterfall(self, on):
        if bool(on) != self.waterfall:
            self.waterfall = bool(on)
            self.refreshed.emit()

    def make_authoritative(self):
        bar = self._state.inter
        if self.inter is None or bar is None or self._active is None or bar.authoritative_off:
            return None
        outcome = self.inter.pick_fit(self._active, bar.viewed)
        if outcome.ok:
            self.chosen = {}
            self.refreshed.emit()
        return outcome

    def toggle_lock(self):
        bar = self._state.inter
        if self.inter is None or bar is None or self._active is None or bar.lock_off:
            return None
        if bar.locked:
            return self.inter.set_lock(self._active, False)
        return self.inter.set_lock(self._active, True, bar.viewed)

    def set_store(self, store):
        self._store = store
        self._reference = None
        self._reference_snapshot = None
        self._signature = None
        self.refreshed.emit()

    def save_view_state(self, session):
        session.view_state[STATE_KEY] = {'fit': self.fit, 'layout': self.layout, 'scale': self.scale,
            'zero_lines': self.zero_lines, 'peak_lines': self.peak_lines,
            'filled_symbols': self.filled_symbols}

    def load_view_state(self, session):
        outer = getattr(session, 'view_state', None)
        saved = outer.get(STATE_KEY) if isinstance(outer, dict) else None
        saved = saved if isinstance(saved, dict) else {}
        for name, default in (("zero_lines", True), ("peak_lines", True), ("filled_symbols", False)):
            value = saved.get(name, default)
            setattr(self, name, value if isinstance(value, bool) else default)
        for name, choices, default in (('fit', PASSES, 'none'), ('layout', LAYOUTS, 'offset'),
                                        ('scale', SCALES, 'normalised')):
            value = saved.get(name)
            setattr(self, name, value if isinstance(value, str) and value in choices else default)
        self.hand_step = None
        self.refreshed.emit()

    def _groups(self, snapshot, keys):
        grouped = {}
        for key in keys:
            row = snapshot.spectrum(key)
            if row is None:
                continue
            group = None if isinstance(row, PooledRow) else row.set_key
            grouped.setdefault(group, []).append(row)
        out = []
        for group, rows in grouped.items():
            if group is None:
                out.append(OverLimitGroup(f'Pooled masters · {len(rows)}', tuple(r.label for r in rows)))
                continue
            info = identity(snapshot, rows[0].key)
            names = tuple(r.analyzer or r.key for r in rows)
            extent = names[0] if len(names) == 1 else f'{names[0]}–{names[-1]}'
            count = f'{len(rows)} analyzer' + ('s' if len(rows) != 1 else '')
            out.append(OverLimitGroup(f'{info.set_label} · {info.t} · {count} ({extent})', names))
        return tuple(out)

    def _normalisation(self, snapshot):
        if self.scale == 'raw':
            return None
        value = self._store.normalisation_reference() if self._store else self._reference
        if value is None and snapshot is not self._reference_snapshot:
            self._reference_snapshot = snapshot
            value = normalisation_reference(
                self._ws, [r.key for ss in snapshot.sets for r in ss.spectra],
                cache=self.scan_list.spectra_cache)
            if value is not None and self._store:
                value = self._store.freeze_normalisation_reference(value)
                self.store_dirty.emit()
            self._reference = value
        return value

    def recompute(self):
        started = perf_counter()
        keys = self.selection.keys
        snapshot = self.scan_list.snapshot
        n = len(keys)
        body = EMPTY if not n else LIST if n > MAX_PLOTTED else PLOT
        active = self.selection.active if self.selection.active in keys else (keys[0] if n == 1 else None)
        self._active = active if body == PLOT else None
        if self.chosen and set(self.chosen) != {self._active}:
            self.chosen = {}
        inter = self.inter
        row = inter.row(active) if inter and self._active else None
        history = inter.history(active) if inter and self._active else None
        wf_off = (None if n == 1 and history is not None and len(history) > 1 else
                  "Select exactly one spectrum to draw a waterfall of its fits." if n != 1 else
                  "Reading this spectrum's fits…" if history is None else
                  "This spectrum has no inter-spectra fits yet. Fast solo is the only fit.")
        wf = self.waterfall and wf_off is None and body == PLOT and inter is not None
        view = ('offset' if wf else 'single' if n == 1 else self.layout) if body == PLOT else None
        labels = unique_labels(snapshot, keys, lambda i: (i.scan, i.analyzer, i.q))
        read_s = 0.0
        reference = None
        choice = self._choice(keys) if body == PLOT else {}
        wf_entries, self._wf_total = self._waterfall_entries(history) if wf else ((), 0)
        self._wf_entries, self._wf_on = wf_entries, wf
        if body == PLOT:
            read_key = (keys, inter.revision if inter else '', tuple(sorted(choice.items())),
                        tuple(e.fit_id for e in wf_entries))
            if read_key != self._read_keys or snapshot is not self._read_snapshot:
                read_started = perf_counter()
                rows = [snapshot.spectrum(key) for key in keys]
                rows = [r for r in rows if r is not None]
                if wf:
                    # one read per fit of the one spectrum; the cache re-serves the raw data
                    self._bundles = tuple(self._read(rows, {} if e.fit_id == 'fast' else {rows[0].key: e.fit_id})[0]
                                          for e in wf_entries)
                else:
                    self._bundles = self._read(rows, choice)
                self._read_keys, self._read_snapshot = read_key, snapshot
                read_s += perf_counter() - read_started
            self._fits = (tuple((e.fit_id, e.label, e.stale, e.errors_available) for e in wf_entries) if wf else
                          tuple(self._fit_info(b.key, choice, history) for b in self._bundles))
            read_started = perf_counter()
            reference = self._normalisation(snapshot)
            revisions = tuple((b.key, b.revision, f[0]) for b, f in zip(self._bundles, self._fits))
            token = (revisions, self.scale, reference)
            if token != self._scaled_token:
                self._scaled = tuple(b.scaled(self.scale, reference) for b in self._bundles)
                self._scaled_token = token
                read_s += perf_counter() - read_started
        else:
            revisions = ()
        signature = (keys, revisions, tuple(labels.items()), self.selection.active,
                     self.fit, self.layout, self.scale, self.hand_step, reference,
                     self.zero_lines, self.peak_lines, self.filled_symbols,
                     inter.revision if inter else '', tuple(self.chosen.items()), wf, history, wf_off,
                     row,
                     self._groups(snapshot, keys) if body == LIST else ())
        if signature == self._signature:
            return self._state
        self._signature = signature
        build_started = perf_counter()
        # Live data may change the automatic spacing; preserve the user's zoom on that refresh.
        frame = (keys, view, self.scale, self.hand_step, body, wf)
        reset_range = frame != self._frame
        self._frame = frame
        common = dict(body=body, view=view, reset_range=reset_range, step_auto=self.hand_step is None)
        if body != PLOT:
            state = PageState(**common,
                over_limit_note=(f'{n} spectra selected — the plot shows at most 10. '
                                 'Select fewer, or recall a group' if body == LIST else ''),
                over_limit=self._groups(snapshot, keys) if body == LIST else ())
        else:
            state = self._plot_state(common, labels, n)
        from dataclasses import replace
        bar = None
        if inter is not None and self._active is not None and body == PLOT:
            bar = bar_state(history, self.chosen.get(self._active) or (row.fit_id if row else 'fast'),
                            locked=bool(row and row.locked), locked_fit=row.locked_fit_id if row else None,
                            waterfall_off=wf_off, waterfall_on=wf)
        state = replace(state, inter=bar, fit_value=self.fit, scale_value=self.scale,
                        timing=RedrawTiming(n, read_s, perf_counter() - build_started,
                                                  0.0, perf_counter() - started))
        self._state = state
        self.state_changed.emit(state)
        return state

    def _read(self, rows, choice):
        # fit_choice is passed only when something is chosen, so a plain read is today's call exactly
        kw = {'fit_choice': choice} if choice else {}
        return spectrum_bundles(self._ws, rows, cache=self.scan_list.spectra_cache, **kw)

    def _choice(self, keys):
        """Each in-use spectrum on its authoritative fit; the active spectrum's own pick wins."""
        out = {}
        for key in keys:
            row = self.inter.row(key) if self.inter else None
            if row is not None and row.in_use:
                out[key] = row.fit_id
        for key, fit_id in self.chosen.items():
            if fit_id != 'fast' or key in out:
                out[key] = fit_id
        return out

    def _fit_info(self, key, choice, history):
        fit_id = choice.get(key, 'fast')
        if fit_id == 'fast':
            return ('fast', 'Fast solo', False, True)
        entry = next((e for e in history or () if e.fit_id == fit_id), None) if key == self._active else None
        if entry is not None:
            return (fit_id, entry.label, entry.stale, entry.errors_available)
        row = self.inter.row(key)
        return (fit_id, row.label if row and row.fit_id == fit_id else fit_id, bool(row and row.stale), True)

    def _waterfall_entries(self, history):
        """Fast solo, every changed refit and the authoritative one, oldest first; capped at MAX_PLOTTED."""
        picked = [e for e in history if e.batch_id is None or e.changed or e.authoritative]
        total = len(picked)
        if len(picked) > MAX_PLOTTED:
            keep = {picked[0].fit_id} | {e.fit_id for e in picked if e.authoritative}
            for entry in reversed(picked):
                if len(keep) >= MAX_PLOTTED:
                    break
                keep.add(entry.fit_id)
            picked = [e for e in picked if e.fit_id in keep]
        return tuple(picked), total

    def _plot_state(self, common, labels, n):
        traces, drawn_keys, not_drawn, deep_summaries, refit_flags = [], [], [], [], []
        wf = self._wf_on
        fit_pass = 'fast' if wf else self.fit
        residuals = None
        y_unit = 'Counts' if self.scale == 'raw' else 'Counts per reference monitor'
        # A raw (not zeroed) axis never shares a plot with zeroed scans: the zeroed ones draw and the rest wait.
        drawable = [b for b, s in zip(self._bundles, self._scaled) if not (b.not_drawn or s.not_drawn)]
        zeroed_present = any(b.zero != ZERO_NONE for b in drawable)
        for index, (bundle, scaled, (fit_id, fit_label, stale, errors_ok)) in enumerate(
                zip(self._bundles, self._scaled, self._fits)):
            nd = bundle.not_drawn or scaled.not_drawn
            if nd:
                not_drawn.append((bundle.key, nd.text))
                continue
            if zeroed_present and bundle.zero == ZERO_NONE:
                not_drawn.append((bundle.key, WAITING_FOR_ZERO))
                continue
            y_unit = scaled.y_unit
            curves, note = (), None
            refit = fit_id != 'fast'
            if fit_pass != 'none' and common['view'] != 'overlay':
                result = scaled.passes[fit_pass]
                summary = bundle.summaries[fit_pass]
                if isinstance(result, NotDrawn):
                    note = result.text
                else:
                    curves = (ScansCurve('Total', 'total', None, result.total),) + tuple(
                        ScansCurve(c.name, c.role, c.mode_number, c.values, c.centers) for c in result.components)
                    note = summary.outdated_text or summary.unjudged_text
                    residuals = result.residuals
                    if fit_pass == 'deep':
                        deep_summaries.append(summary)
                    refit_flags.append(refit)
                    if refit:
                        parts = ([] if wf else [fit_label]) + (['stale'] if stale else []) + [
                            CONDITIONAL_ERRORS if errors_ok else ERRORS_UNAVAILABLE]
                        note = ' · '.join(parts + ([note] if note else []))
            tag = ' (beamline zero)' if bundle.zero == ZERO_BEAMLINE else ''
            identity = fit_label if wf else labels[bundle.key] + tag
            active = self._wf_entries[index].authoritative if wf else bundle.key == self.selection.active
            traces.append(ScansTrace(identity, note, scaled.x, scaled.y, scaled.yerr,
                                     scaled.kept, scaled.fine_x, curves, fit_pass == 'deep', active))
            drawn_keys.append(bundle.key)
        header = None
        if any(t.curves for t in traces):
            if fit_pass == 'fast':
                header = ('Curves · Fast' if not any(refit_flags) else 'Curves · Inter-spectra fits'
                          if all(refit_flags) else 'Curves · Fast and inter-spectra')
            else:
                header = 'Curves · Deep'
            if deep_summaries:
                untrusted = sum(s.trusted is not True for s in deep_summaries)
                if untrusted:
                    header += (' (untrusted)' if untrusted == len(deep_summaries)
                               else f' (untrusted for {untrusted} of {len(deep_summaries)})')
        untrusted = [b for b in self._bundles if b.summaries['deep'].trusted is False]
        untrusted.sort(key=lambda b: b.key != self.selection.active)
        tooltip = DEEP_TOOLTIP
        if untrusted:
            tooltip += ' Untrusted: ' + (untrusted[0].summaries['deep'].trust_reason or 'trust not established')
        step = stack_step(traces) if self.hand_step is None else self.hand_step
        if wf:
            traces.reverse()      # the first trace is drawn on top; the oldest fit goes at the bottom
        ylabel = y_unit + (', traces offset' if common['view'] == 'offset' and len(traces) >= 2 else '')
        # A selected but unavailable single spectrum uses the builder's honest empty-plot state.
        raw_axis = bool(traces) and not zeroed_present
        main = scans_spec(traces=traces, view=common['view'] if traces else 'offset', step=step,
                          ylabel=ylabel, curves_header=header,
                          xlabel=NOT_ZEROED_LABEL if raw_axis else ENERGY_LABEL,
                          zero_lines=self.zero_lines, peak_lines=self.peak_lines,
                          filled_symbols=self.filled_symbols)
        residual = (scans_residual_spec(trace=traces[0], residuals=residuals,
                                       ylabel=f'data − fit ({y_unit})')
                    if common['view'] == 'single' and traces and traces[0].curves else None)
        counts = Counter(reason for _, reason in not_drawn)
        line = (f'{len(not_drawn)} of {n} selected not drawn: ' +
                ', '.join(f'{reason} ({count})' for reason, count in counts.items())) if counts else ''
        if wf and len(self._wf_entries) < self._wf_total:
            line = f'showing {len(self._wf_entries)} of {self._wf_total} fits' + (' · ' + line if line else '')
        return PageState(**common, main_spec=main, residual_spec=residual,
            not_drawn_text=line, not_drawn_tip='\n'.join(f'{labels[k]}: {r}' for k, r in not_drawn),
            fit=Control(common['view'] != 'overlay',
                        'Overlay compares raw data; fits show in Offset or with one spectrum'
                        if common['view'] == 'overlay' else None),
            layout=Control(n > 1 and not wf, WATERFALL_LAYOUT if wf else 'one spectrum' if n == 1 else None),
            offset=Control(common['view'] == 'offset',
                           None if common['view'] == 'offset' else 'applies to the Offset layout'),
            step=step, deep_tooltip=tooltip, keys=tuple(drawn_keys), not_drawn=tuple(not_drawn),
            traces=tuple(traces), curves_header=header)
