"""WaterfallViewModel — view-ready state for the C4 waterfall / comparison view (Task 13, A-6).

GUI_DESIGN item 6: compare across temperature. Two display modes over the SAME fixed-q selection:

* **waterfall** — the measured 1D cut + delivered fit curve at one q, one trace per T bin, stacked
  with a vertical offset. The offset is a pure VIEW-STATE display device (G10): it is never written
  to the workspace/card, and the emitted spec always says so on screen (an Annotation — the honesty
  rule for stacked plots, mirrored from :func:`~isar.plotspec.builders.waterfall_spec`).
* **comparison** — the ω̂(q) dispersion overlaid per T. Built by calling
  :func:`~isar.plotspec.builders.dispersion_spec` **once per T** and merging the results (a T-labeled
  legend entry per call) — reusing the dispersion builder verbatim rather than re-implementing its
  certifying/pooled/entropy honesty rules here.

The q/T plane is derived with the SAME shared helper the heatmap/dispersion views use
(:mod:`isar_gui.viewmodels.projection`) so all three views never disagree about which q component is
"the" axis or how the integration window is defined.

``mode``/``selected_q``/``offset_step`` are VIEW STATE — persisted/restored through
``isar_gui/session.py``'s ``view_state`` dict under the ``"waterfall"`` key. Nothing in this module
writes the workspace or card (G10).

Qt import surface: PySide6 only; specs come from ``isar.plotspec.builders``; curve/data come through
the read-only ``Workspace`` facade (``evaluate_model`` + ``scan_data``) exactly as the C2 inspector
does — this VM never re-fits or writes.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Signal

from isar.plotspec import builders
from isar.plotspec import spec as ps
from isar_gui.viewmodels import projection
from isar_gui.viewmodels.base import ViewModel

_FINE_N = 400
_STATE_KEY = "waterfall"
_MODES = ("waterfall", "comparison")


def _g(v) -> str:
    return f"{v:g}"


class WaterfallViewModel(ViewModel):
    """Assembles the C4 waterfall/comparison spec from the workspace; emits ``spec_changed`` on
    every rebuild and ``point_activated`` (a :class:`~isar.workspace_api.FitView`) when a trace
    (waterfall mode) or a ω̂ point (comparison mode) is picked."""

    spec_changed = Signal()
    point_activated = Signal(object)

    def __init__(self, ws, parent=None):
        super().__init__(ws, parent)
        self._mode = "waterfall"
        self._selected_q: float | None = None
        self._offset_step: float | None = None   # None => auto heuristic (see _auto_offset_step)
        self._spec = None
        self._points: list = []          # comparison mode: [(q, omega0, FitView)]
        self._trace_points: list = []    # waterfall mode: [(x, y, FitView)]
        self._q_values: list = []
        self._varying = None
        self._modal = None
        self._effective_offset = 1.0
        self._rebuild()

    # ---- public state -------------------------------------------------------
    @property
    def spec(self):
        return self._spec

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def selected_q(self) -> float | None:
        return self._selected_q

    @property
    def offset_step(self) -> float:
        """The offset actually in effect: the user's explicit value, else the auto heuristic (1.2x
        the largest trace amplitude in the last build — comment on the heuristic lives there)."""
        return self._offset_step if self._offset_step is not None else self._effective_offset

    @property
    def q_values(self) -> list:
        return list(self._q_values)

    # ---- mode / q / offset switches -----------------------------------------
    def set_mode(self, mode: str) -> None:
        if mode not in _MODES:
            raise ValueError(f"unknown waterfall mode: {mode!r} (expected one of {_MODES})")
        self._mode = mode
        self.refresh()

    def set_selected_q(self, q) -> None:
        self._selected_q = None if q is None else float(q)
        self.refresh()

    def set_offset_step(self, step) -> None:
        """``None`` reverts to the auto heuristic; any other value is the user's explicit override."""
        self._offset_step = None if step is None else float(step)
        self.refresh()

    def default_q_from_selection(self, fit_view) -> None:
        """Browser-selection hook: adopt the nearest available q bucket to ``fit_view``'s q on the
        plane's varying axis. A dock-local selector can still override it afterward."""
        if fit_view is None or fit_view.q is None:
            return
        varying = self._varying if self._varying is not None else 0
        try:
            q = float(fit_view.q[varying])
        except (TypeError, IndexError):
            return
        self.set_selected_q(self._nearest_q(q))

    def refresh(self) -> None:
        """Rebuild from the (already-refreshed) workspace and emit ``spec_changed``. Idempotent."""
        self._rebuild()
        self.spec_changed.emit()

    def on_workspace_changed(self, changed=None) -> None:
        self._rebuild()
        self.spec_changed.emit()
        super().on_workspace_changed(changed)

    def activate_at(self, x, y, tol: float = 0.2) -> None:
        pts = self._points if self._mode == "comparison" else self._trace_points
        fv = projection.nearest_point(pts, x, y, tol)
        if fv is not None:
            self.point_activated.emit(fv)

    # ---- view-state round trip (G10: view state only, never the workspace) --
    def save_view_state(self, session) -> None:
        session.view_state[_STATE_KEY] = {
            "mode": self._mode,
            "selected_q": self._selected_q,
            "offset_step": self._offset_step,
        }

    def load_view_state(self, session) -> None:
        st = (getattr(session, "view_state", None) or {}).get(_STATE_KEY)
        if not isinstance(st, dict):
            return
        mode = st.get("mode")
        if mode in _MODES:
            self._mode = mode
        q = st.get("selected_q")
        self._selected_q = None if q is None else float(q)
        step = st.get("offset_step")
        self._offset_step = None if step is None else float(step)
        self.refresh()

    # ---- assembly ------------------------------------------------------------
    def _hkl_round_step(self) -> float:
        pol = getattr(self._ws.card, "policy", None)
        return float(getattr(pol, "hkl_round_step", None) or 0.05)

    def _nearest_q(self, q: float) -> float:
        if not self._q_values:
            return q
        return min(self._q_values, key=lambda v: abs(v - q))

    def _empty_spec(self, msg: str) -> ps.PlotSpec:
        return ps.PlotSpec(title=msg, xlabel="energy transfer (meV)", ylabel="counts")

    def _rebuild(self) -> None:
        step = self._hkl_round_step()
        cands = projection.candidates(self._ws.results)  # pooled masters included (delivered answer)
        if not cands:
            self._reset_empty("waterfall — no fitted scans")
            return

        varying, modal = projection.choose_axes(cands, step)
        self._varying, self._modal = varying, modal
        in_window, _excluded = projection.split_window(cands, varying, modal, step)
        if not in_window:
            self._reset_empty("waterfall — no scans in this projection")
            return

        buckets: dict = {}
        for fv in in_window:
            key = round(float(fv.q[varying]) / step) * step if step else float(fv.q[varying])
            buckets.setdefault(round(key, 6), []).append(fv)
        self._q_values = sorted(buckets)
        if self._selected_q is None or round(self._selected_q, 6) not in buckets:
            # Default: the most populous q bucket — a robust "modal q" pick (mirrors modal_T).
            self._selected_q = max(buckets, key=lambda q: len(buckets[q]))

        if self._mode == "comparison":
            self._spec = self._build_comparison(in_window, varying, modal, step)
        else:
            fits_at_q = buckets.get(round(self._selected_q, 6), [])
            self._spec = self._build_waterfall(fits_at_q, varying)

    def _reset_empty(self, msg: str) -> None:
        self._spec = self._empty_spec(msg)
        self._points, self._trace_points, self._q_values = [], [], []
        self._varying, self._modal = None, None

    # -- waterfall mode --------------------------------------------------------
    def _build_waterfall(self, fits, varying) -> ps.PlotSpec:
        by_t: dict = {}
        for fv in fits:
            if fv.T is None:
                continue
            by_t.setdefault(round(fv.T, 6), fv)  # one trace per T bin (first fit wins)

        t_bins = sorted(by_t)
        traces = [self._trace_for(by_t[T], T) for T in t_bins]

        # Auto heuristic: 1.2x the largest trace amplitude (max-min of counts/curve), so adjacent
        # traces separate cleanly without the user having to tune it by hand; still user-adjustable
        # via set_offset_step.
        if self._offset_step is None:
            self._effective_offset = self._auto_offset_step(traces)
        offset_step = self.offset_step

        self._trace_points = [self._trace_click_point(tr, i * offset_step, by_t[T])
                              for i, (T, tr) in enumerate(zip(t_bins, traces))]

        q = self._selected_q
        axis = projection.AXIS[varying]
        title = f"waterfall · {axis}={_g(q)}" if q is not None else "waterfall"
        return builders.waterfall_spec(
            traces=traces, offset_step=offset_step, q=q, axis_label=axis, title=title,
            meta={"varying": varying, "selected_q": q, "temperatures": sorted(by_t)},
        )

    def _trace_for(self, fv, T) -> dict:
        pooled = bool(fv.pooled)
        label = f"T={_g(T)} K"
        energy = counts = yerr = None
        if not pooled:
            energy, counts, yerr = self._ws.scan_data(fv.scan_number)
        fine = self._fine_grid(fv, energy)
        curve_x = curve_total = None
        if fine is not None:
            try:
                curve = self._ws.evaluate_model(fv.scan_number, fv.detector_id, x_eval=fine)
                curve_x, curve_total = curve.x, curve.total
            except (KeyError, ValueError, OSError):
                curve_x = curve_total = None
        return {"label": label, "x": energy, "counts": counts, "yerr": yerr,
                "curve_x": curve_x, "curve_total": curve_total,
                "pooled": pooled, "n_members": fv.n_members}

    @staticmethod
    def _fine_grid(fit, energy):
        if energy is not None and len(energy):
            return np.linspace(float(np.min(energy)), float(np.max(energy)), _FINE_N)
        if fit.omega0 is not None:
            c = float(fit.omega0)
            return np.linspace(c - 10.0, c + 10.0, _FINE_N)
        return None

    @staticmethod
    def _auto_offset_step(traces) -> float:
        amps = []
        for tr in traces:
            for key in ("counts", "curve_total"):
                v = tr.get(key)
                if v is not None and len(v):
                    v = np.asarray(v, dtype=float)
                    amps.append(float(np.max(v) - np.min(v)))
        return 1.2 * (max(amps) if amps else 1.0)

    @staticmethod
    def _trace_click_point(tr, offset, fv):
        """A representative (x, y, FitView) for hit-testing a trace click: the curve value at ω₀
        (or the curve's midpoint when ω₀ is unknown), shifted by this trace's own offset."""
        x0 = fv.omega0
        cx, cy = tr.get("curve_x"), tr.get("curve_total")
        if cx is not None and cy is not None and len(cx):
            cx = np.asarray(cx, dtype=float)
            cy = np.asarray(cy, dtype=float)
            if x0 is None:
                x0 = float(cx[len(cx) // 2])
            y0 = float(np.interp(x0, cx, cy)) + offset
        else:
            x0 = 0.0 if x0 is None else x0
            y0 = offset
        return (float(x0), y0, fv)

    # -- comparison mode --------------------------------------------------------
    def _build_comparison(self, in_window, varying, modal, step) -> ps.PlotSpec:
        proj = projection.projection_legend(varying, modal, step)
        temps = sorted({round(fv.T, 6) for fv in in_window if fv.T is not None})

        series: list[ps.Series] = []
        markers: list[ps.Marker] = []
        annotations: list[ps.Annotation] = []
        self._points = []
        for T in temps:
            fits_t = [fv for fv in in_window if fv.T is not None and abs(fv.T - T) < 1e-6]
            points = self._points_for(fits_t, varying)
            sub = builders.dispersion_spec(points=points, projection=proj,
                                           title="", xlabel="", ylabel="")
            for s in sub.series:
                if s.role is ps.Role.DATA:
                    # Re-label for the per-T legend (comparison mode's whole point); the certifying/
                    # pooled/entropy semantics all came from dispersion_spec unchanged.
                    tag = f"T={_g(T)} K"
                    s.label = f"{tag} — {s.label}" if s.label else tag
                series.append(s)
            markers.extend(sub.markers)
            annotations.extend(sub.annotations)

        axis = projection.AXIS[varying]
        title = f"dispersion comparison · {proj.plane}"
        return ps.PlotSpec(
            title=title, xlabel=f"{axis} (rlu)", ylabel="energy transfer (meV)",
            series=series, markers=markers, annotations=annotations, projection=proj,
            meta={"varying": varying, "modal": modal, "temperatures": temps},
        )

    def _points_for(self, fits, varying):
        """Per-ω̂ dicts for :func:`~isar.plotspec.builders.dispersion_spec`, mirroring
        :class:`~isar_gui.viewmodels.dispersion.DispersionViewModel`'s point construction exactly
        (same certifying-interval law) — and records the (x, y, FitView) click list alongside."""
        from isar_gui.viewmodels import certify

        points = []
        for fv in fits:
            if fv.omega0 is None:
                continue
            x = float(fv.q[varying])
            y = float(fv.omega0)
            self._points.append((x, y, fv))
            ent = fv.assignment_entropy
            ent = None if ent is None else float(ent)
            cert = certify.certifying_interval(fv)
            half = (abs(float(cert[-1]) - float(cert[0])) / 2.0
                    if cert is not None and len(cert) >= 2 else None)
            points.append({"q": x, "omega": y, "yerr": half, "entropy": ent,
                           "verdict": fv.verdict, "pooled": bool(fv.pooled),
                           "n_members": fv.n_members})
        return points
