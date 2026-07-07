"""The stopping criterion + deterministic convergence core (LLM_HARNESS_DESIGN §2.1).

Teixeira Parente et al. (2022) name the stopping criterion as the untested gap in autonomous TAS; the
driver makes it a first-class, testable output. This module holds the *deterministic* core (no LLM):
path bookkeeping, per-grid-q convergence, and the overall ``met`` / ``unmeetable`` / ``continue``
decision. The LLM supervisor consumes the decision; it does not compute it.

**Per-grid-q convergence** (all three required):

1. the **calibrated** half-width — ``inflation × (interval_hi − interval_lo) / 2`` from
   :mod:`isar.drive.acquisition.calibration`, **never** ISAR's floored prior sigma — is ≤ tolerance;
2. the disposition is **resolved**;
3. it is **confirmed on a dedicated production/confirm scan**: the criterion holds on **two
   consecutive production fits**, and the FIRST crossing carries a conservative margin (``k = 1.2`` on
   the half-width). This is the peeking / optional-stopping-bias guard (ISAR ``PHASE_4_NOTES.md``):
   the live shrinking interval preferentially stops on downward fluctuations, so a single lucky
   crossing must be reproduced by a second scan before it counts.

**Overall decision.** ``met`` when every grid q is converged. ``unmeetable`` (with reasons) when the
budget is exhausted with the path still under-determined, the tolerance is below the resolution-
asymmetry systematic **floor** (default the calibration's ``center_pull_mean``), or every remaining
q is **stuck** (declined ``max_declines``× without converging). A stuck q is abandoned — the
acquisition engine skips it and the campaign keeps mapping the rest — so a single hopeless point
(e.g. Γ, where the acoustic branch is degenerate with the elastic line) does not abort the map.
Otherwise ``continue``.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

FIRST_CROSSING_MARGIN = 1.2   # conservative margin on the half-width at the first crossing
MAX_DECLINES = 3              # a q that declines this many CONSECUTIVE times is abandoned (stuck)
_HKL_TOL = 1e-4              # r.l.u. tolerance for matching an observation to a path grid point
_BIG = float("inf")

# The neutron counts of the escalation ladder (LLM_HARNESS_DESIGN §8): quick-look → production.
QUICKLOOK_NCOUNT = int(1e7)
PRODUCTION_NCOUNT = int(1e8)

PRODUCTION_KINDS = frozenset({"production", "confirm"})


# --------------------------------------------------------------------------- path geometry
def path_scalars(path) -> list[float]:
    """Cumulative arclength (r.l.u.) of each point along ``path`` — the model's monotone q-axis.

    For a straight H-segment this reduces to ``|h − h_start|``; it is monotone for any monotone segment.
    """
    out = [0.0]
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        out.append(out[-1] + math.dist(a, b))
    return out


def match_grid_index(hkl, path, *, tol: float = _HKL_TOL) -> int | None:
    """The index of the path grid point nearest ``hkl`` (within ``tol`` r.l.u.), or ``None``."""
    best_i, best_d = None, _BIG
    for i, p in enumerate(path):
        d = math.dist(p, hkl)
        if d < best_d:
            best_i, best_d = i, d
    if best_i is not None and best_d <= tol:
        return best_i
    return None


# --------------------------------------------------------------------------- per-q convergence core
def q_converged(prod_fits: list[dict], tolerance: float, *,
                first_margin: float = FIRST_CROSSING_MARGIN) -> bool:
    """Is a grid q converged, given its chronological production/confirm fits?

    ``prod_fits`` entries are ``{"halfwidth": float|None, "resolved": bool}`` (production & confirm
    scans only — quick-looks never count). Requires the last two to both be resolved, the first (the
    crossing) to meet ``first_margin × halfwidth ≤ tolerance``, and the second (the confirm) to meet
    ``halfwidth ≤ tolerance``. Fewer than two fits is never converged — a single lucky crossing does
    not stop.
    """
    if len(prod_fits) < 2:
        return False
    a, b = prod_fits[-2], prod_fits[-1]
    ha, hb = a.get("halfwidth"), b.get("halfwidth")
    if ha is None or hb is None:
        return False
    return bool(a.get("resolved") and b.get("resolved")
                and first_margin * ha <= tolerance
                and hb <= tolerance)


def next_kind_for_q(prod_fits: list[dict], tolerance: float, *,
                    first_margin: float = FIRST_CROSSING_MARGIN) -> str | None:
    """The next production-tier scan kind for a q: ``"confirm"``, ``"production"``, or ``None`` (done).

    A resolved first crossing (``first_margin × halfwidth ≤ tolerance``) earns a dedicated ``confirm``
    scan; anything short of that keeps counting with a ``production`` scan.
    """
    if q_converged(prod_fits, tolerance, first_margin=first_margin):
        return None
    if prod_fits:
        last = prod_fits[-1]
        hw = last.get("halfwidth")
        if last.get("resolved") and hw is not None and first_margin * hw <= tolerance:
            return "confirm"
    return "production"


# --------------------------------------------------------------------------- grid summary
@dataclass
class GridQ:
    """A per-grid-point rollup the acquisition function and the stopping criterion both consume."""

    index: int
    hkl: tuple[float, float, float]
    q: float
    tolerance: float
    prod_fits: list[dict] = field(default_factory=list)
    n_quicklook: int = 0
    n_decline: int = 0
    best_omega0: float | None = None
    best_halfwidth: float | None = None
    best_resolved: bool = False

    @property
    def visited(self) -> bool:
        return self.n_quicklook > 0 or bool(self.prod_fits) or self.n_decline > 0

    @property
    def converged(self) -> bool:
        return q_converged(self.prod_fits, self.tolerance)

    @property
    def stuck(self) -> bool:
        """Declined ``MAX_DECLINES``+ consecutive times without converging — stop spending beam here."""
        return not self.converged and self.n_decline >= MAX_DECLINES

    @property
    def deficit(self) -> float:
        """Calibrated σ-above-tolerance deficit (``halfwidth − tolerance``); ``+inf`` if never fit."""
        if self.best_halfwidth is None:
            return _BIG
        return self.best_halfwidth - self.tolerance


def summarize_grid(objective, observations: list[dict]) -> list[GridQ]:
    """Roll a chronological ``observations`` list into one :class:`GridQ` per objective grid point.

    Each observation is a dict ``{"hkl", "kind", "calibrated_halfwidth", "resolved", "disposition",
    "omega0"}``. Observations are matched to a grid point by nearest hkl; anything off-grid is ignored
    (the driver only scans grid points, so this is defensive).
    """
    path = objective.path
    scalars = path_scalars(path)
    grid = [GridQ(index=i, hkl=tuple(path[i]), q=scalars[i], tolerance=objective.tolerance_meV)
            for i in range(len(path))]
    for obs in observations:
        i = match_grid_index(tuple(obs["hkl"]), path)
        if i is None:
            continue
        g = grid[i]
        kind = obs.get("kind")
        resolved = bool(obs.get("resolved"))
        hw = obs.get("calibrated_halfwidth")
        if kind in PRODUCTION_KINDS:
            g.prod_fits.append({"halfwidth": hw, "resolved": resolved, "kind": kind})
        elif kind == "quicklook":
            g.n_quicklook += 1
        if not resolved:
            g.n_decline += 1
        else:
            # Consecutive-decline counter: a resolved fit proves the q CAN deliver, so earlier
            # window-hunting misses (recenter attempts each log an unresolved row) must not count
            # toward abandoning it.
            g.n_decline = 0
        if resolved and obs.get("omega0") is not None:
            # Keep the tightest resolved estimate as the point that feeds the ω(q) model.
            if g.best_halfwidth is None or (hw is not None and hw < g.best_halfwidth):
                g.best_omega0 = float(obs["omega0"])
                g.best_halfwidth = hw
                g.best_resolved = True
    return grid


def model_points(grid: list[GridQ]) -> list[tuple[float, float, float]]:
    """The ``(q, omega0, halfwidth)`` triples for the ω(q) model — one per resolved grid point."""
    pts = []
    for g in grid:
        if g.best_resolved and g.best_omega0 is not None and g.best_halfwidth is not None:
            pts.append((g.q, g.best_omega0, g.best_halfwidth))
    return pts


# --------------------------------------------------------------------------- the decision
@dataclass
class StopDecision:
    """The terminal-or-not verdict: ``state`` in ``continue`` | ``met`` | ``unmeetable`` + reasons."""

    state: str
    reasons: list[str] = field(default_factory=list)
    per_q: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "reasons": list(self.reasons), "per_q": list(self.per_q)}


def systematic_floor(calibration=None, floor: float | None = None) -> float:
    """The tolerance floor: an explicit ``floor``, else the calibration's ``|center_pull_mean|``, else 0.

    Below this the resolution-asymmetry systematic swamps the statistical half-width, so a tighter
    tolerance is unmeetable by construction.
    """
    if floor is not None:
        return float(floor)
    if calibration is not None:
        cp = getattr(calibration, "center_pull_mean", None)
        if cp is not None and math.isfinite(cp):
            return abs(float(cp))
    return 0.0


def evaluate_stop(objective, grid: list[GridQ], budget_state: dict, *,
                  calibration=None, floor: float | None = None,
                  max_declines: int = MAX_DECLINES) -> StopDecision:
    """Decide ``met`` / ``unmeetable`` / ``continue`` for the whole objective.

    Args:
        objective: the :class:`~isar.drive.objective.ObjectiveSpec` (path + tolerance).
        grid: the per-q rollup from :func:`summarize_grid`.
        budget_state: ``{"kind", "amount", "spent"}`` — neutrons or counting-seconds consumed.
        calibration: the loaded σ-calibration (its ``center_pull_mean`` supplies the default floor).
        floor: explicit systematic floor override (meV).
        max_declines: a non-converged q that has declined this many times is unmeetable.
    """
    tol = objective.tolerance_meV
    fl = systematic_floor(calibration, floor)
    per_q = [{"hkl": list(g.hkl), "q": g.q, "converged": g.converged,
              "calibrated_halfwidth": g.best_halfwidth, "resolved": g.best_resolved,
              "n_decline": g.n_decline, "stuck": g.stuck} for g in grid]

    # Tolerance below the systematic floor is unmeetable by construction — check before "met".
    if tol < fl:
        return StopDecision("unmeetable",
                            [f"tolerance {tol:g} meV is below the resolution-asymmetry systematic "
                             f"floor {fl:g} meV (calibration center_pull_mean) — unreachable"], per_q)

    if grid and all(g.converged for g in grid):
        return StopDecision("met",
                            [f"all {len(grid)} grid q converged: calibrated σ(ω₀) ≤ {tol:g} meV, "
                             "resolved, confirmed on two consecutive production fits"], per_q)

    reasons: list[str] = []
    spent = float(budget_state.get("spent", 0.0))
    amount = budget_state.get("amount")
    if amount is not None and spent >= float(amount):
        n_unconverged = sum(1 for g in grid if not g.converged)
        reasons.append(f"budget exhausted ({spent:g}/{amount:g} {budget_state.get('kind', '')}) "
                       f"with {n_unconverged} grid q still under-determined")
        return StopDecision("unmeetable", reasons, per_q)

    # A stuck q (declined max_declines×) is abandoned, not campaign-fatal: the acquisition engine
    # skips it and the campaign keeps mapping the rest. Terminal only when no workable q remains.
    stuck = [g for g in grid if (not g.converged) and g.n_decline >= max_declines]
    workable = [g for g in grid if not g.converged and g.n_decline < max_declines]
    if stuck and not workable:
        n_conv = sum(1 for g in grid if g.converged)
        reasons.append(f"converged {n_conv}/{len(grid)} grid q; the rest cannot deliver the statistics:")
        for g in stuck:
            reasons.append(f"q={g.q:g} (hkl={list(g.hkl)}) declined {g.n_decline}× "
                           f"(≥ {max_declines})")
        return StopDecision("unmeetable", reasons, per_q)

    return StopDecision("continue", [], per_q)
