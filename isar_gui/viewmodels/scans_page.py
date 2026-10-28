"""Presentation state for Scan(s); all scientific values come from spectrum bundles."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from time import perf_counter

from PySide6.QtCore import Signal

from isar.plotspec.builders import ScansCurve, ScansTrace, scans_spec, scans_residual_spec, stack_step
from isar.plotspec.spec import PlotSpec
from isar.workspace_api.spectra import NotDrawn, normalisation_reference, spectrum_bundles
from isar.workspace_api.scan_list import PooledRow
from isar_gui.selection import identity, unique_labels
from isar_gui.viewmodels.base import ViewModel

MAX_PLOTTED = 10
PASSES = ('none', 'fast', 'deep')
LAYOUTS = ('offset', 'overlay')
SCALES = ('raw', 'normalised')
STATE_KEY = 'scans_page'
EMPTY, PLOT, LIST = range(3)
DEEP_TOOLTIP = 'Deep pass: the slow nested-sampling refit.'


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

    def _set(self, name, value, choices):
        if value not in choices:
            raise ValueError(f'unknown {name}: {value}')
        if getattr(self, name) != value:
            setattr(self, name, value)
            self.refreshed.emit()

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

    def set_store(self, store):
        self._store = store
        self._reference = None
        self._reference_snapshot = None
        self._signature = None
        self.refreshed.emit()

    def save_view_state(self, session):
        session.view_state[STATE_KEY] = {'fit': self.fit, 'layout': self.layout, 'scale': self.scale}

    def load_view_state(self, session):
        outer = getattr(session, 'view_state', None)
        saved = outer.get(STATE_KEY) if isinstance(outer, dict) else None
        saved = saved if isinstance(saved, dict) else {}
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
        view = ('single' if n == 1 else self.layout) if body == PLOT else None
        labels = unique_labels(snapshot, keys, lambda i: (i.scan, i.analyzer, i.q))
        read_s = 0.0
        reference = None
        if body == PLOT:
            if keys != self._read_keys or snapshot is not self._read_snapshot:
                read_started = perf_counter()
                rows = [snapshot.spectrum(key) for key in keys]
                self._bundles = spectrum_bundles(self._ws, [r for r in rows if r is not None],
                                                 cache=self.scan_list.spectra_cache)
                self._read_keys, self._read_snapshot = keys, snapshot
                read_s += perf_counter() - read_started
            read_started = perf_counter()
            reference = self._normalisation(snapshot)
            revisions = tuple((b.key, b.revision) for b in self._bundles)
            token = (revisions, self.scale, reference)
            if token != self._scaled_token:
                self._scaled = tuple(b.scaled(self.scale, reference) for b in self._bundles)
                self._scaled_token = token
                read_s += perf_counter() - read_started
        else:
            revisions = ()
        signature = (keys, revisions, tuple(labels.items()), self.selection.active,
                     self.fit, self.layout, self.scale, self.hand_step, reference,
                     self._groups(snapshot, keys) if body == LIST else ())
        if signature == self._signature:
            return self._state
        self._signature = signature
        build_started = perf_counter()
        # Live data may change the automatic spacing; preserve the user's zoom on that refresh.
        frame = (keys, view, self.scale, self.hand_step, body)
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
        state = replace(state, fit_value=self.fit, scale_value=self.scale,
                        timing=RedrawTiming(n, read_s, perf_counter() - build_started,
                                                  0.0, perf_counter() - started))
        self._state = state
        self.state_changed.emit(state)
        return state

    def _plot_state(self, common, labels, n):
        traces, drawn_keys, not_drawn, deep_summaries = [], [], [], []
        residuals = None
        y_unit = 'Counts' if self.scale == 'raw' else 'Counts per reference monitor'
        for bundle, scaled in zip(self._bundles, self._scaled):
            nd = bundle.not_drawn or scaled.not_drawn
            if nd:
                not_drawn.append((bundle.key, nd.text))
                continue
            y_unit = scaled.y_unit
            curves, note = (), None
            if self.fit != 'none' and common['view'] != 'overlay':
                result = scaled.passes[self.fit]
                summary = bundle.summaries[self.fit]
                if isinstance(result, NotDrawn):
                    note = result.text
                else:
                    curves = (ScansCurve('Total', 'total', None, result.total),) + tuple(
                        ScansCurve(c.name, c.role, c.mode_number, c.values) for c in result.components)
                    note = summary.outdated_text or summary.unjudged_text
                    residuals = result.residuals
                    if self.fit == 'deep':
                        deep_summaries.append(summary)
            traces.append(ScansTrace(labels[bundle.key], note, scaled.x, scaled.y, scaled.yerr,
                                     scaled.kept, scaled.fine_x, curves, self.fit == 'deep',
                                     bundle.key == self.selection.active))
            drawn_keys.append(bundle.key)
        header = None
        if any(t.curves for t in traces):
            header = 'Curves · Fast' if self.fit == 'fast' else 'Curves · Deep'
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
        ylabel = y_unit + (', traces offset' if common['view'] == 'offset' and n >= 2 else '')
        # A selected but unavailable single spectrum uses the builder's honest empty-plot state.
        main = scans_spec(traces=traces, view=common['view'] if traces else 'offset', step=step,
                          ylabel=ylabel, curves_header=header)
        residual = (scans_residual_spec(trace=traces[0], residuals=residuals,
                                       ylabel=f'data − fit ({y_unit})')
                    if common['view'] == 'single' and traces and traces[0].curves else None)
        counts = Counter(reason for _, reason in not_drawn)
        line = (f'{len(not_drawn)} of {n} selected not drawn: ' +
                ', '.join(f'{reason} ({count})' for reason, count in counts.items())) if counts else ''
        return PageState(**common, main_spec=main, residual_spec=residual,
            not_drawn_text=line, not_drawn_tip='\n'.join(f'{labels[k]}: {r}' for k, r in not_drawn),
            fit=Control(common['view'] != 'overlay',
                        'Overlay compares raw data; fits show in Offset or with one spectrum'
                        if common['view'] == 'overlay' else None),
            layout=Control(n > 1, 'one spectrum' if n == 1 else None),
            offset=Control(common['view'] == 'offset',
                           None if common['view'] == 'offset' else 'applies to the Offset layout'),
            step=step, deep_tooltip=tooltip, keys=tuple(drawn_keys), not_drawn=tuple(not_drawn),
            traces=tuple(traces), curves_header=header)
