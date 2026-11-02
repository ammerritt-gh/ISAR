"""Presentation state for the analyzer path: the picked analyzers of one scan, plotted against energy.

The path is the Analyzer array's picks that are cells of the shown scan, in click order (``path_keys``). Everything
scientific comes from ``workspace_api.analyzer_path``. This module converts its view to the plain data the plot
builder takes and chooses the words (hard rule 7).
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal

from isar.plotspec import spec as ps
from isar.plotspec.path_builders import analyzer_path_spec
from isar.workspace_api.analyzer_array import array_view
from isar.workspace_api.analyzer_path import PathView, analyzer_path
from isar.workspace_api.scan_list import SpectrumRow
from isar_gui.viewmodels.analyzer_array import path_keys
from isar_gui.viewmodels.base import ViewModel

NO_PICKS = "Select at least two analyzers of one scan in the Analyzer array first."
NO_LAYOUT = "This instrument has no analyzer layout, so its analyzers have no path."
PLOT_TIP = "Plots the picked analyzers in click order: position along the path against energy."
YLABEL = "energy transfer (meV)"


@dataclass(frozen=True)
class PathState:
    keys: tuple[str, ...] = ()           # the path: picked cells of the shown scan, click order
    tip: str = ""
    can_plot: bool = False
    reason: str | None = None            # why the path cannot be plotted yet; None when it can
    spec: ps.PlotSpec | None = None      # only while the pane wants the path


def _shown_set(snapshot, picks) -> str | None:
    """The scan set of the first picked spectrum (as the Analyzer array shows it)."""
    for key in picks:
        row = snapshot.spectrum(key)
        if isinstance(row, SpectrumRow):
            return row.set_key
    return None


def _temperature(fact) -> str | None:
    value = fact.value if fact is not None else None
    return f"T = {value:g} K" if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _stop(stop) -> dict:
    return {"x": stop.x, "beamline": stop.beamline, "note": stop.note, "covered": stop.covered,
            "modes": [{"energy": m.energy, "lo": m.lo, "hi": m.hi, "peak_E": m.peak_E, "name": m.name,
                       "unresolved": m.unresolved,
                       "markers": [{"group": mk.group, "label": mk.label, "words": mk.words} for mk in m.markers]}
                      for m in stop.modes]}


def _to_spec(view: PathView) -> ps.PlotSpec:
    title = " · ".join(p for p in ("Analyzer path", f"scan {view.scan_label}" if view.scan_label else None,
                                   _temperature(view.temperature),
                                   f"links of {view.links_at}" if view.links_at else None) if p)
    if not view.ok:
        return analyzer_path_spec(stops=(), title=title, xlabel="", ylabel=YLABEL, reason=view.reason)
    notes = [view.stale_text] if view.stale_text else []
    if view.skipped:
        notes.append(f"{len(view.skipped)} picked not in this path (another scan, or not a spectrum)")
    return analyzer_path_spec(
        stops=[_stop(s) for s in view.stops], title=title, ylabel=YLABEL,
        xlabel=f"position along the path (cumulative |Δq|, {view.unit}; ticks are analyzers)",
        stale_note=" · ".join(notes) or None)


class AnalyzerPathViewModel(ViewModel):
    state_changed = Signal(object)

    def __init__(self, ws, scan_list_vm, parent=None):
        super().__init__(ws, parent)
        self.scan_list = scan_list_vm
        self.selection = scan_list_vm.selection
        self.inter = None
        self._wanted = False
        self._heavy = None                  # (keys, snapshot, links revision, spec) of the last heavy read
        self._state = PathState()
        self.refreshed.connect(self.recompute)
        self.selection.changed.connect(self.refreshed)
        scan_list_vm.snapshot_changed.connect(self.refreshed)

    @property
    def state(self) -> PathState:
        return self._state

    def set_inter(self, inter) -> None:
        self.inter = inter
        if inter is not None:
            inter.changed.connect(self.refreshed)

    def set_wanted(self, wanted: bool) -> None:
        """The heavy spec (bundles, q points) is read only while the pane shows the path."""
        self._wanted = bool(wanted)
        if not self._wanted:
            self._heavy = None
        self.recompute()

    def recompute(self) -> None:
        picks, snapshot = self.selection.picks, self.scan_list.snapshot
        set_key = _shown_set(snapshot, picks)
        view = array_view(snapshot, set_key, self._ws) if set_key is not None else None
        layout = view is not None and view.layout is not None
        keys = path_keys(view.cells, picks) if layout else ()
        can_plot = len(keys) >= 2
        if can_plot:
            reason = None
        elif set_key is None:
            reason = NO_PICKS
        elif not layout:
            reason = NO_LAYOUT
        else:
            reason = f"A path needs two or more analyzers of this scan; {len(keys)} picked so far."
        spec = self._spec(keys, snapshot) if can_plot and self._wanted else None
        self._state = PathState(keys=keys, tip=PLOT_TIP if can_plot else "", can_plot=can_plot,
                                reason=reason, spec=spec)
        self.state_changed.emit(self._state)

    def _spec(self, keys, snapshot) -> ps.PlotSpec:
        links = self.inter.links if self.inter is not None else None
        revision = self.inter.revision if self.inter is not None else ""
        heavy = self._heavy
        if heavy is not None and heavy[0] == keys and heavy[1] is snapshot and heavy[2] == revision:
            return heavy[3]
        spec = _to_spec(analyzer_path(snapshot, self._ws, keys, links=links, cache=self.scan_list.spectra_cache))
        self._heavy = (keys, snapshot, revision, spec)
        return spec
