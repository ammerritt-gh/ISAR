"""HeatmapViewModel — view-ready state for the C3 growing (q, E) plane (Task 11, A-5).

GUI_DESIGN item 2: measured spectra render as STRIPS (one measured column per scan), gaps stay gaps,
and the declared projection is always on screen (G9 4D-honesty). The honesty overlays ride on top:
the certifying-tier ω̂ whisker (a warning marker where no tier certifies), the branch-model curve,
per-point assignment-entropy colouring, verdict glyphs, and a visible count of the scans excluded by
the projection window.

ALL derivation lives here; :class:`~isar_gui.views.heatmap_dock.HeatmapDock` only renders the emitted
:class:`~isar.plotspec.spec.PlotSpec` and forwards clicks. isar_gui import contract: workspace_api /
plotspec / PySide6 only — no matplotlib, no isar.session/analyze/drive.

Projection declaration (the plane): among the fitted, non-pooled scans at the viewed temperature we
pick the VARYING q component as the one least concentrated at a single modal value (so a lone
out-of-window scan can never masquerade as the scan axis), fix the other two at their modal values,
and integrate within the card's ``hkl_round_step`` of those modal values. Scans outside that window
are EXCLUDED from the plane and counted — never silently dropped.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Signal

from isar.plotspec import spec as ps
from isar_gui.viewmodels import certify, projection
from isar_gui.viewmodels.base import ViewModel

_LN2 = float(np.log(2.0))
_AXIS = projection.AXIS
_VERDICT_MARKER = {
    "resolved": ps.MarkerKind.VERDICT_RESOLVED,
    "flagged": ps.MarkerKind.VERDICT_FLAGGED,
    "held": ps.MarkerKind.VERDICT_HELD,
}
_T_ROUND = 5.0   # K bin for matching branch-model / prior points to the viewed temperature
_STATE_KEY = "heatmap"


def _g(v) -> str:
    return f"{v:g}"


class HeatmapViewModel(ViewModel):
    """Assembles the C3 (q, E) plane spec from the workspace; emits ``spec_changed`` on every
    rebuild and ``point_activated`` (a :class:`~isar.workspace_api.FitView`) when a ω̂ point is
    picked. Temperature is a VM state (one T at a time); :meth:`set_temperature` switches it."""

    #: Emitted after every :meth:`refresh`/rebuild (including temperature switches).
    spec_changed = Signal()
    #: Emitted with the selected ``FitView`` when a ω̂ point is activated (MainWindow routes it to
    #: the inspector).
    point_activated = Signal(object)

    def __init__(self, ws, parent=None):
        super().__init__(ws, parent)
        self._temperature: float | None = None
        self._spec: ps.PlotSpec | None = None
        self._points: list = []          # [(x, omega0, FitView)] for click hit-testing
        self._excluded_count = 0
        self._temperatures: list = []
        self._projection: ps.ProjectionLegend | None = None
        self._rebuild()

    # ---- public state -----------------------------------------------------
    @property
    def spec(self) -> ps.PlotSpec | None:
        return self._spec

    @property
    def excluded_count(self) -> int:
        return self._excluded_count

    @property
    def temperatures(self) -> list:
        return list(self._temperatures)

    @property
    def temperature(self) -> float | None:
        return self._temperature

    @property
    def projection(self) -> ps.ProjectionLegend | None:
        return self._projection

    # ---- slots ------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild the spec from the (already-refreshed) workspace and emit ``spec_changed``. Cheap
        and idempotent — the WatchBridge wiring in the next task calls this after ``ws.refresh()``."""
        self._rebuild()
        self.spec_changed.emit()

    def on_workspace_changed(self, changed=None) -> None:
        self._rebuild()
        self.spec_changed.emit()
        super().on_workspace_changed(changed)

    def set_temperature(self, T) -> None:
        """Switch the viewed temperature (one T at a time) and rebuild."""
        self._temperature = None if T is None else round(float(T), 6)
        self.refresh()

    def activate_at(self, x, y, tol: float = 0.2) -> None:
        """Emit ``point_activated`` for the ω̂ point nearest ``(x, y)`` (in scale-normalised data
        units) when within ``tol``. The dock calls this on a plot click."""
        fv = self._nearest(x, y, tol)
        if fv is not None:
            self.point_activated.emit(fv)

    def _nearest(self, x, y, tol):
        return projection.nearest_point(self._points, x, y, tol)

    # ---- view-state round trip (G10: view state only, never the workspace) --
    def save_view_state(self, session) -> None:
        session.view_state[_STATE_KEY] = {"temperature": self._temperature}

    def load_view_state(self, session) -> None:
        st = (getattr(session, "view_state", None) or {}).get(_STATE_KEY)
        if not isinstance(st, dict):
            return
        t = st.get("temperature")
        self._temperature = None if t is None else float(t)
        self.refresh()

    # ---- assembly ---------------------------------------------------------
    def _hkl_round_step(self) -> float:
        pol = getattr(self._ws.card, "policy", None)
        return float(getattr(pol, "hkl_round_step", None) or 0.05)

    def _value_label(self) -> str:
        pol = getattr(self._ws.card, "policy", None)
        mode = getattr(pol, "counting_mode", None)
        return f"counts/{mode}" if mode else "counts"

    def _candidates(self) -> list:
        """Fitted, non-pooled results carrying a full Q and a temperature (the plane's population)."""
        return projection.candidates(self._ws.results)

    def _rebuild(self) -> None:
        step = self._hkl_round_step()
        cands = self._candidates()
        self._temperatures = sorted({round(fv.T, 6) for fv in cands})
        if self._temperature is None or self._temperature not in self._temperatures:
            self._temperature = projection.modal_T(cands)

        at_t = [fv for fv in cands if self._temperature is not None
                and abs(fv.T - self._temperature) < 1e-6]

        if not at_t:
            self._spec = self._empty_spec()
            self._points, self._excluded_count, self._projection = [], 0, None
            return

        varying, modal = projection.choose_axes(at_t, step)
        fixed = [i for i in range(3) if i != varying]
        in_window, excluded = projection.split_window(at_t, varying, modal, step)
        self._excluded_count = len(excluded)

        self._spec = self._build_spec(in_window, varying, modal, fixed, step)
        self._projection = self._spec.projection

    def _empty_spec(self) -> ps.PlotSpec:
        return ps.PlotSpec(title="(q, E) plane — no fitted scans", xlabel="q (rlu)",
                           ylabel="energy transfer (meV)", value_label=self._value_label())

    # ---- the spec ---------------------------------------------------------
    def _build_spec(self, fits, varying, modal, fixed, step) -> ps.PlotSpec:
        strips, strip_fits = self._strips(fits, varying, step)
        series, markers, annotations, points, entropy = self._overlays(fits, varying)
        branch = self._branch_series(fits, varying)
        if branch is not None:
            series.insert(0, branch)
        self._points = points

        projection = self._projection_legend(varying, modal, fixed, step)
        if self._excluded_count > 0:
            annotations.append(ps.Annotation(
                text=f"{self._excluded_count} scans outside this projection", xy=None))

        axis = _AXIS[varying]
        T = self._temperature
        title = f"(q, E) plane · {projection.plane}" + (f" · T={_g(T)} K" if T is not None else "")
        return ps.PlotSpec(
            title=title, xlabel=f"{axis} (rlu)", ylabel="energy transfer (meV)",
            series=series, markers=markers, annotations=annotations, strips=strips,
            projection=projection, value_label=self._value_label(),
            meta={"varying": varying, "modal": modal, "temperature": T,
                  "excluded_count": self._excluded_count,
                  "omega_hat_entropy_all": entropy,   # every plotted ω̂ (members + masters); a
                  #                        diagnostics list kept in meta even when the colour channel
                  #                        is not shown (so downstream tools can read entropy per ω̂)
                  "scan_ids": [s.meta.get("scan_id") for s in strips]},
        )

    def _strips(self, fits, varying, step):
        """One MEASURED column per in-window scan that still has a re-readable data file. Width =
        0.8 x the median spacing between distinct varying-q positions (cosmetic; never overlaps)."""
        rows = []
        for fv in fits:
            if fv.pooled:
                continue  # a pooled master has NO persisted spectrum — no strip (Task 10 honesty rule)
            energy, counts, _yerr = self._ws.scan_data(fv.scan_number)
            if energy is None or counts is None or len(energy) == 0:
                continue
            rows.append((float(fv.q[varying]), np.asarray(energy, float),
                         np.asarray(counts, float), fv.scan_number))
        if not rows:
            return [], []
        xs = sorted({round(x, 6) for x, *_ in rows})
        if len(xs) >= 2:
            spacing = float(np.median(np.diff(xs)))
        else:
            spacing = 2.0 * step
        width = 0.8 * spacing if spacing > 0 else 0.8 * (2.0 * step)
        strips = [ps.Strip(x_center=x, width=width, y=e, values=c,
                           meta={"scan_id": sid})
                  for (x, e, c, sid) in rows]
        return strips, rows

    def _overlays(self, fits, varying):
        """ω̂ scatter (whisker only where a tier certifies), warning markers for uncertified ω̂,
        verdict glyphs, and the per-point assignment-entropy channel (kept in meta; 0→calm, ln2→
        undecided). Returns (series, markers, annotations, points, entropy)."""
        rel_x, rel_y, rel_err, rel_ent = [], [], [], []
        pool_x, pool_y, pool_err, pool_ent, pool_n = [], [], [], [], []
        markers: list[ps.Marker] = []
        annotations: list[ps.Annotation] = []
        points: list = []
        entropy: list = []
        any_unreliable = False
        for fv in fits:
            if fv.omega0 is None:
                continue
            x = float(fv.q[varying])
            y = float(fv.omega0)
            points.append((x, y, fv))
            ent = fv.assignment_entropy
            ent = None if ent is None else float(ent)
            entropy.append(ent)
            cert = certify.certifying_interval(fv)
            half = (abs(float(cert[-1]) - float(cert[0])) / 2.0
                    if cert is not None and len(cert) >= 2 else None)
            if fv.pooled:
                # The delivered per-q master: shown on its OWN whiskered scatter (its own interval,
                # certify law), marked POOLED, so it is visible AND distinct from the member points.
                if half is not None:
                    pool_x.append(x)
                    pool_y.append(y)
                    pool_err.append(half)
                    pool_ent.append(ent)
                    pool_n.append(fv.n_members)
                else:
                    any_unreliable = True
                    markers.append(ps.Marker(x=x, y=y, kind=ps.MarkerKind.WARNING,
                                             label="pooled ω̂ (unreliable)"))
            elif half is not None:
                rel_x.append(x)
                rel_y.append(y)
                rel_err.append(half)
                rel_ent.append(ent)
            else:
                any_unreliable = True
                markers.append(ps.Marker(x=x, y=y, kind=ps.MarkerKind.WARNING,
                                         label="ω̂ (unreliable)"))
            kind = _VERDICT_MARKER.get((fv.verdict or "").lower())
            if kind is not None:
                markers.append(ps.Marker(x=x, y=y, kind=kind, label=f"verdict: {fv.verdict}"))

        # The entropy colour channel only rides on the certifying-ω̂ scatter (the whiskered points);
        # attach it — and the legend note explaining it — ONLY when at least one such point carries an
        # entropy value, so the note never asserts an encoding that isn't on screen.
        colored = bool(rel_x) and any(e is not None for e in rel_ent)
        series: list[ps.Series] = []
        if rel_x:
            series.append(ps.Series(
                x=np.array(rel_x), y=np.array(rel_y), yerr=np.array(rel_err),
                label="ω̂ (certifying interval)", role=ps.Role.DATA,
                state=(ps.State.ENTROPY if colored else None),
                color_values=(list(rel_ent) if colored else None),
                color_norm=(ps.LN2 if colored else None)))
        if pool_x:
            p_colored = any(e is not None for e in pool_ent)  # masters: entropy n/a unless present
            ns = [n for n in pool_n if n is not None]
            if len(pool_x) == 1 and ns:
                plabel = f"pooled ({int(ns[0])})"
            elif ns:
                plabel = f"pooled masters ({len(pool_x)})"
            else:
                plabel = "pooled master"
            series.append(ps.Series(
                x=np.array(pool_x), y=np.array(pool_y), yerr=np.array(pool_err),
                label=plabel, role=ps.Role.DATA, state=ps.State.POOLED,
                color_values=(list(pool_ent) if p_colored else None),
                color_norm=(ps.LN2 if p_colored else None)))
            colored = colored or p_colored
        if any_unreliable:
            # One legend-area note, NOT one per point.
            annotations.append(ps.Annotation(text="(unreliable)", xy=None,
                                             role=ps.State.UNRELIABLE))
        if colored:
            annotations.append(ps.Annotation(
                text="color = assignment entropy (ln 2 = undecided)", xy=None))
        return series, markers, annotations, points, entropy

    def _branch_series(self, fits, varying):
        """The dispersion/branch curve ω_b(q) from the accumulated branch anchors (Task 4's
        ``branch_model.dispersion_prior``), matched to this plane's sample + temperature bin. Degrades
        to ``None`` when there are fewer than two distinct anchors."""
        bm = getattr(self._ws, "branch_model", None)
        pts = list(getattr(bm, "dispersion_prior", None) or []) if bm is not None else []
        curve = projection.prior_curve(pts, varying, sample=projection.modal_sample(fits),
                                       T=self._temperature, t_round=_T_ROUND)
        if curve is None:
            return None
        xs, ws = curve
        return ps.Series(x=xs, y=ws, label="branch model", role=ps.Role.BRANCH)

    def _projection_legend(self, varying, modal, fixed, step) -> ps.ProjectionLegend:
        return projection.projection_legend(varying, modal, step)
