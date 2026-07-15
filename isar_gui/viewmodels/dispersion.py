"""DispersionViewModel — view-ready state for the C5 dispersion view (Task 12, A-7).

GUI_DESIGN item 3/4 (view half): ω̂ vs q — the heatmap's 1-D sibling. Same projection discipline
(the declared plane + integration window are always on screen, G9), the same honesty law on the ω̂
overlay (a certifying-tier whisker only; a warning marker where no tier certifies; the assignment-
entropy colour channel riding ONLY on the whiskered subset; verdict glyphs), plus the dispersion
prior curve, branch-belief overlays, and off-curve flags for ω̂ that sit far from the prior.

ALL derivation lives here; :class:`~isar_gui.views.dispersion_dock.DispersionDock` only renders the
emitted :class:`~isar.plotspec.spec.PlotSpec` and forwards clicks. The plane/window/varying-component
derivation and the prior-curve degrade rule are shared with the heatmap via
:mod:`isar_gui.viewmodels.projection` (so the two views never disagree about the plane). isar_gui
import contract: workspace_api / plotspec / PySide6 only.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Signal

from isar.plotspec import builders
from isar_gui.viewmodels import certify, projection
from isar_gui.viewmodels.base import ViewModel

_T_ROUND = 5.0        # K bin for matching prior / branch anchors to the viewed temperature
_OFF_CURVE_K = 3.0    # a 3σ gate: flag ω̂ only when it is grossly off the prior (a ~0.3% two-sided
#                       tail under Gaussian noise), so the flag reads as "the model and the number
#                       genuinely disagree", not as ordinary scatter.
_STATE_KEY = "dispersion"


def _g(v) -> str:
    return f"{v:g}"


class DispersionViewModel(ViewModel):
    """Assembles the C5 ω̂(q) spec from the workspace; emits ``spec_changed`` on every rebuild and
    ``point_activated`` (a :class:`~isar.workspace_api.FitView`) when a ω̂ point is picked. One
    temperature at a time (``set_temperature`` switches it), matching the heatmap."""

    #: Emitted after every :meth:`refresh`/rebuild (including temperature switches).
    spec_changed = Signal()
    #: Emitted with the selected ``FitView`` when a ω̂ point is activated (MainWindow routes it to
    #: the inspector).
    point_activated = Signal(object)

    #: Group-drop target kind (plan Part 2A). One displayed T, so a multi-T group is refused.
    TARGET = "dispersion"

    def __init__(self, ws, parent=None):
        super().__init__(ws, parent)
        self._temperature: float | None = None
        self._restriction: set | None = None   # None = all fitted scans; else covered unit ids
        self._restriction_stale = False        # set when a refresh auto-cleared a now-stale restriction
        self._spec = None
        self._points: list = []          # [(x, omega0, FitView)] for click hit-testing
        self._excluded_count = 0
        self._temperatures: list = []
        self._projection = None
        self._rebuild()

    # ---- public state -----------------------------------------------------
    @property
    def spec(self):
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
    def projection(self):
        return self._projection

    # ---- slots ------------------------------------------------------------
    def refresh(self) -> None:
        """Rebuild from the (already-refreshed) workspace and emit ``spec_changed``. Idempotent."""
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

    # ---- group-drop restriction (plan Part 2A) ------------------------------
    def set_scan_restriction(self, keys) -> None:
        """Restrict the view to a manual group's covered unit ids; ``None`` restores all fitted scans.
        Pure view filter — no re-fit, no workspace write (G10)."""
        self._restriction = {str(k) for k in keys} if keys is not None else None
        self.refresh()

    @property
    def restriction_active(self) -> bool:
        return self._restriction is not None

    def take_restriction_stale(self) -> bool:
        """True once after a refresh auto-cleared a now-stale restriction (consumed by the dock)."""
        stale, self._restriction_stale = self._restriction_stale, False
        return stale

    def can_accept(self, scan_ids) -> tuple[bool, str]:
        """``(ok, reason)`` for dropping ``scan_ids`` here — refuses a group spanning >1 temperature."""
        from isar_gui import group_targets
        return group_targets.accepts(self.TARGET, self._ws.results, scan_ids)

    def activate_at(self, x, y, tol: float = 0.2) -> None:
        """Emit ``point_activated`` for the ω̂ point nearest ``(x, y)`` (scale-normalised) within
        ``tol``. The dock calls this on a plot click."""
        fv = projection.nearest_point(self._points, x, y, tol)
        if fv is not None:
            self.point_activated.emit(fv)

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

    def _rebuild(self) -> None:
        step = self._hkl_round_step()
        cands, stale = projection.restrict_or_clear(
            projection.candidates(self._ws.results), self._restriction)
        if stale:
            self._restriction = None
            self._restriction_stale = True
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
        in_window, excluded = projection.split_window(at_t, varying, modal, step)
        self._excluded_count = len(excluded)
        self._projection = projection.projection_legend(varying, modal, step)

        self._spec = self._build_spec(in_window, varying, modal, step)

    def _empty_spec(self):
        # No projection can be declared with no in-window scans, so this is the one spec allowed
        # without a ProjectionLegend (nothing to project); dispersion_spec would (correctly) reject
        # projection=None, so build the empty plane directly.
        from isar.plotspec import spec as ps
        return ps.PlotSpec(title="dispersion ω̂(q) — no fitted scans", xlabel="q (rlu)",
                           ylabel="energy transfer (meV)")

    def _build_spec(self, fits, varying, modal, step):
        sample = projection.modal_sample(fits)
        T = self._temperature
        bm = getattr(self._ws, "branch_model", None)

        prior = projection.prior_curve(
            list(getattr(bm, "dispersion_prior", None) or []) if bm is not None else [],
            varying, sample=sample, T=T, t_round=_T_ROUND, want_sigma=True)
        branch = projection.prior_curve(
            list(getattr(bm, "branch_belief", None) or []) if bm is not None else [],
            varying, sample=sample, T=T, t_round=_T_ROUND)

        points, entropy = [], []
        self._points = []
        for fv in fits:
            if fv.omega0 is None:
                continue
            x = float(fv.q[varying])
            y = float(fv.omega0)
            self._points.append((x, y, fv))
            ent = fv.assignment_entropy
            ent = None if ent is None else float(ent)
            entropy.append(ent)
            cert = certify.certifying_interval(fv)
            half = (abs(float(cert[-1]) - float(cert[0])) / 2.0
                    if cert is not None and len(cert) >= 2 else None)
            points.append({"q": x, "omega": y, "yerr": half, "entropy": ent,
                           "verdict": fv.verdict, "pooled": bool(fv.pooled),
                           "n_members": fv.n_members})

        prior_xy = None if prior is None else (prior[0], prior[1])
        off_curve = self._off_curve(points, prior)
        branches = [] if branch is None else [("branch belief", branch[0], branch[1])]

        plane = self._projection.plane
        title = "dispersion ω̂(q) · " + plane + (f" · T={_g(T)} K" if T is not None else "")
        return builders.dispersion_spec(
            points=points, projection=self._projection, prior=prior_xy,
            branches=branches, off_curve=off_curve, title=title,
            xlabel=f"{projection.AXIS[varying]} (rlu)",
            meta={"varying": varying, "modal": modal, "temperature": T,
                  "excluded_count": self._excluded_count,
                  # every plotted ω̂ (members + pooled masters), aligned to the full population.
                  "omega_hat_entropy_all": entropy},
        )

    @staticmethod
    def _off_curve(points, prior):
        """ω̂ that sit > k·σ_effective off the prior curve. σ_effective = the point's certifying
        half-width when it has one, else the prior's σ at that q (assignment-inflated belief width).
        prior(q) and σ_prior(q) are read off the prior *belief* curve by linear interpolation between
        its anchors — this evaluates a genuinely continuous belief, NOT the forbidden interpolation
        *between measured strips*. Skipped entirely when there is no prior curve."""
        if prior is None:
            return []
        xs_p, ys_p, sig_p = prior
        # prior_curve sorts its anchors ascending, so xs_p[0]/xs_p[-1] bound the prior's q-domain.
        flagged = []
        for p in points:
            x, y = p["q"], p["omega"]
            # Outside the prior's q-domain np.interp would clamp to an end anchor, comparing ω̂
            # against a value the prior never actually asserts there -> a bogus flag. Skip it.
            if x < xs_p[0] or x > xs_p[-1]:
                continue
            prior_y = float(np.interp(x, xs_p, ys_p))
            sig_eff = p["yerr"]
            if sig_eff is None:
                s = float(np.interp(x, xs_p, sig_p))
                sig_eff = s if np.isfinite(s) else None
            if sig_eff is None or sig_eff <= 0:
                continue
            dz = abs(y - prior_y) / sig_eff
            if dz > _OFF_CURVE_K:
                flagged.append({"q": x, "omega": y, "n_sigma": dz})
        return flagged
