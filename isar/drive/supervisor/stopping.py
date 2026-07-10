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
   the half-width). This is the peeking / optional-stopping-bias guard (ISAR ``docs/campaigns/PHASE_4_NOTES.md``):
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

MAX_DECLINES = 3              # a q that declines (no signal) this many CONSECUTIVE times is abandoned
FIRST_CROSSING_MARGIN = 1.2  # retained for back-compat (legacy q_converged); unused by the master path
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
# LEGACY (unused by the driver): the old "two consecutive independent production fits" convergence.
# Replaced by GridQ.converged (the master/pooled fit). Kept only so older callers/tests still import.
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


# A fit whose ω₀ is usable for an ω₀-tolerance objective. `flagged` (model_incomplete) is INCLUDED:
# at low q the 1-D-resolution model has a structured residual (the tas4d ceiling) so the fit flags,
# but ω₀ and its interval are still good — the flag documents peak-shape imperfection, not an ω₀
# error, and the systematic floor guards accuracy. `resolved` and `flagged` both carry a usable ω₀.
USABLE_DISPOSITIONS = frozenset({"resolved", "flagged"})

# A q that has pooled this many production scans without converging is abandoned (its master interval
# has stopped shrinking below tolerance — a systematic floor, not a counts problem).
MAX_PROD_ATTEMPTS = 8


# --------------------------------------------------------------------------- grid summary
@dataclass
class GridQ:
    """A per-grid-point rollup the acquisition function and the stopping criterion both consume.

    Convergence is decided by the q's **master** (pooled) fit — the combine-before-divide fit of all
    production scans at this q, which ISAR refits as each new scan lands. Per-scan fits feed the ω(q)
    steering model and the abandon counters, but the number the campaign certifies is the master's.
    """

    index: int
    hkl: tuple[float, float, float]
    q: float
    tolerance: float
    n_production: int = 0          # production scans taken at this q (each grows the pool)
    n_quicklook: int = 0
    n_decline: int = 0             # consecutive per-scan fits with no usable ω₀ (junk/no_signal)
    # Decline sub-labels (subsets of n_decline — abandon-count semantics unchanged, labeling only):
    # a validation-rejected proposal (unreachable by the instrument) vs a signal the model could not
    # account for (disposition "unmodeled"). Used only to refine the terminal stuck_reason.
    n_infeasible: int = 0
    n_unmodeled: int = 0
    # S3 (DECISIONS): the THIRD state. A production/confirm delivery whose ω₀ interval is
    # interval_reliable=False (γ-at-bound) or never left the untrusted fast tier — it delivers a
    # center but carries no certifiable interval. Counted here, NOT as a no-signal decline (it is not
    # evidence of signal absence) and NOT as a convergence (its interval is degenerate, not precise).
    n_uncertifiable: int = 0
    saw_uncertifiable_gamma: bool = False   # ≥1 uncertifiable delivery was γ-at-bound (flagged interval)
    saw_uncertifiable_fast: bool = False    # ≥1 uncertifiable delivery stayed on the untrusted fast tier
    counts_spent: float = 0.0      # accumulated monitor-count budget (Σ proposal_cost) at this q (Phase 5)
    last_prod_cost: float | None = None  # most recent production scan's cost (ΔC proxy for the next)
    last_fit_kind: str | None = None     # provenance of the most recent usable fit (5b grounding gate)
    best_omega0: float | None = None
    best_halfwidth: float | None = None
    best_resolved: bool = False
    best_prod_halfwidth: float | None = None  # tightest usable+reliable SINGLE production fit
    best_prod_omega0: float | None = None
    master: dict | None = None     # {"omega0","halfwidth","reliable","usable","disposition","n_members"}

    @property
    def visited(self) -> bool:
        return self.n_quicklook > 0 or self.n_production > 0 or self.n_decline > 0

    @property
    def _master_hw(self) -> float | None:
        m = self.master
        if not m or not m.get("usable") or m.get("halfwidth") is None:
            return None
        # S3: convergence seats only on a CERTIFIABLE interval. `certifiable` (reliable interval AND
        # interval_reliable≠False, or a trusted-HESSE fallback) is authoritative; fall back to the
        # legacy `reliable` flag for pre-S3 master rows that carry no `certifiable` field (additive
        # contract — old results.csv behaves as before).
        cert = m.get("certifiable")
        if cert is None:
            cert = m.get("reliable")
        return float(m["halfwidth"]) if cert else None

    @property
    def cert_halfwidth(self) -> float | None:
        """The tightest trustworthy ω₀ half-width for this q: the master (pooled) fit when it is
        usable+reliable, OR the best single production fit — whichever is smaller. A single production
        fit is the fallback when pooling to higher statistics DEGRADES a model-limited q (the pooled
        residual reads as junk), where accumulating more counts cannot help — only a better model can."""
        cands = [h for h in (self._master_hw, self.best_prod_halfwidth) if h is not None]
        return min(cands) if cands else None

    @property
    def cert_omega0(self) -> float | None:
        mh, ph = self._master_hw, self.best_prod_halfwidth
        if mh is not None and (ph is None or mh <= ph):
            return (self.master or {}).get("omega0")
        return self.best_prod_omega0

    @property
    def converged(self) -> bool:
        """A trustworthy ω₀ interval (master OR best single production) is within tolerance."""
        hw = self.cert_halfwidth
        return hw is not None and hw <= self.tolerance

    @property
    def info_saturated(self) -> bool:
        """Value-of-information stop (Phase 5): NOT converged, but one more scan would shrink σ(ω₀) by
        less than :data:`~isar.drive.acquisition.eig.VOI_SIGMA_FRAC` — diminishing returns. Durable by
        construction: a pure function of the accumulated ``counts_spent`` / certified half-width, so it
        re-derives every iteration from the persisted observations (no side-channel state that could go
        stale). Needs a measured cost history; dormant under the 5a √N model until the 5b Fisher σ
        (which does not shrink when ω₀ is unidentifiable) feeds it teeth."""
        if self.converged:
            return False
        sigma = self.cert_halfwidth if self.cert_halfwidth is not None else self.best_halfwidth
        if sigma is None or self.last_prod_cost is None or self.counts_spent <= 0.0:
            return False
        from isar.drive.acquisition.eig import voi_saturated
        return voi_saturated(sigma, self.tolerance, self.counts_spent, self.last_prod_cost)

    @property
    def stuck(self) -> bool:
        """Abandoned: can't get signal (``MAX_DECLINES`` consecutive junk fits), delivered centers but
        every calibrated interval is uncertifiable ``MAX_DECLINES``× (S3: γ-at-bound or fast-tier-only),
        the master has pooled ``MAX_PROD_ATTEMPTS`` scans without converging (interval floored above
        tolerance), or the q is value-of-information saturated (:attr:`info_saturated`)."""
        if self.converged:
            return False
        return (self.n_decline >= MAX_DECLINES or self.n_uncertifiable >= MAX_DECLINES
                or self.n_production >= MAX_PROD_ATTEMPTS or self.info_saturated)

    @property
    def stuck_reason(self) -> str | None:
        """The machine-readable reason this q is stuck (S3 stop-state taxonomy), or ``None`` if it is
        not stuck. One of: ``interval_floored`` (a certifiable interval sits above tolerance after many
        pooled scans), ``info_saturated`` (VOI diminishing returns), ``uncertifiable_interval``
        (delivered centers, every interval flagged γ-at-bound), ``fast_tier_only`` (delivered centers,
        no fit ever left the untrusted fast tier), ``infeasible`` (every decline was a validation
        rejection — the point is unreachable by the instrument), ``signal_unmodeled`` (signal present
        but the model could not fit it, disposition ``unmodeled``), ``no_signal`` (no usable fit ever
        existed here)."""
        if not self.stuck:
            return None
        # Floored: a genuinely CERTIFIABLE interval exists but cannot shrink below tolerance.
        if self.n_production >= MAX_PROD_ATTEMPTS and self.cert_halfwidth is not None:
            return "interval_floored"
        if self.info_saturated:
            return "info_saturated"
        # Uncertifiable third state — a usable center was delivered, but no certifiable interval.
        if self.saw_uncertifiable_gamma or self.saw_uncertifiable_fast or self.n_uncertifiable > 0:
            return "uncertifiable_interval" if self.saw_uncertifiable_gamma else "fast_tier_only"
        if self.n_production >= MAX_PROD_ATTEMPTS:
            return "interval_floored"
        # Terminal no-signal fall-through, refined by decline sub-labels: ALL declines being
        # validation rejections means the point is instrument-infeasible; an "unmodeled" disposition
        # means the signal is present but unfit. Otherwise it is a genuine no-signal q.
        if self.n_infeasible > 0 and self.n_infeasible >= self.n_decline:
            return "infeasible"
        if self.n_unmodeled > 0:
            return "signal_unmodeled"
        return "no_signal"

    @property
    def deficit(self) -> float:
        """Calibrated σ-above-tolerance deficit; ``+inf`` if never fit. Uses the certified half-width
        (master or best single production), falling back to the best per-scan/quick-look estimate for
        a q that has only been quick-looked."""
        hw = self.cert_halfwidth
        if hw is None:
            hw = self.best_halfwidth
        return _BIG if hw is None else hw - self.tolerance


def summarize_grid(objective, observations: list[dict], master_rows: list[dict] | None = None):
    """One :class:`GridQ` per objective grid point from the per-scan ``observations`` + ``master_rows``.

    Per-scan observations ``{"hkl","kind","calibrated_halfwidth","resolved","disposition","omega0"}``
    feed the ω(q) steering model and the abandon counters. ``master_rows`` (the pooled fits, read fresh
    each iteration — their interval shrinks as scans accumulate) decide convergence. Both match to a
    grid point by nearest hkl; off-grid entries are ignored.
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
        # `resolved` on the observation is "usable for steering" — set by loop._observation_from_row
        # for resolved-or-flagged fits carrying a calibrated half-width.
        usable = bool(obs.get("resolved"))
        hw = obs.get("calibrated_halfwidth")
        cost = float(obs.get("proposal_cost") or 0.0)   # actual submitted scan cost (Phase 5)
        g.counts_spent += cost
        if kind in PRODUCTION_KINDS:
            g.n_production += 1
            if cost > 0.0:
                g.last_prod_cost = cost                 # the next production scan's ΔC proxy
        elif kind == "quicklook":
            g.n_quicklook += 1
        # Phase A: only ACOUSTIC fits steer the ω(q) model / certify a q — a non-acoustic (optic) ω₀
        # can never raise the acoustic window centre. ``None`` counts as acoustic DELIBERATELY: legacy
        # ledgers and posterior-off mode (no [dispersion] zone_period ⇒ branch_id is None for every
        # row) carry no branch assignment, and this gate is then fully permissive by design. It only
        # bites once branch assignment is active (posterior on), rejecting rows tagged non-acoustic.
        acoustic = obs.get("branch_id") in (None, "acoustic")
        is_prod = kind in PRODUCTION_KINDS
        # S3: `certifiable` (loop._observation_from_row) is True only for a reliable interval
        # (interval_reliable≠False, or a trusted-HESSE fast fallback). Default True keeps synthetic /
        # pre-S3 observations behaving as before (additive contract).
        certifiable = obs.get("certifiable", True)
        if not usable:
            # Sub-label the decline (abandon-count semantics unchanged — both still increment
            # n_decline): a validation-rejected proposal is instrument-infeasible; an "unmodeled"
            # disposition is signal the model could not fit. Only refines the terminal stuck_reason.
            if obs.get("verdict") == "validation_rejected":
                g.n_infeasible += 1
            if obs.get("disposition") == "unmodeled":
                g.n_unmodeled += 1
            g.n_decline += 1
        elif is_prod and not certifiable:
            # THIRD STATE: a delivered production/confirm center whose interval is uncertifiable. It
            # neither converges (no calibrated interval) NOR declines (a center WAS delivered — not
            # evidence of signal absence). n_decline is left untouched (not reset, not incremented).
            g.n_uncertifiable += 1
            if obs.get("uncertifiable_kind") == "fast_tier":
                g.saw_uncertifiable_fast = True
            else:
                g.saw_uncertifiable_gamma = True
        else:
            g.n_decline = 0        # a certifiable fit (or a steering quicklook) proves the q CAN deliver
            if obs.get("fit_kind"):
                g.last_fit_kind = obs.get("fit_kind")   # provenance of the most recent usable fit
            if acoustic and obs.get("omega0") is not None and (g.best_halfwidth is None
                                                  or (hw is not None and hw < g.best_halfwidth)):
                g.best_omega0 = float(obs["omega0"])
                g.best_halfwidth = hw
                g.best_resolved = True
            # A usable single PRODUCTION fit with a real (reliable, certifiable) interval is a
            # convergence candidate in its own right — it beats the master when pooling degrades a
            # model-limited q. Uncertifiable production fits were routed above and never reach here.
            if acoustic and is_prod and hw is not None and obs.get("omega0") is not None and (
                    g.best_prod_halfwidth is None or hw < g.best_prod_halfwidth):
                g.best_prod_halfwidth = hw
                g.best_prod_omega0 = float(obs["omega0"])
    for mr in (master_rows or []):
        i = match_grid_index(tuple(mr["hkl"]), path)
        if i is not None:
            grid[i].master = mr
    return grid


def model_points(grid: list[GridQ]) -> list[tuple[float, float, float]]:
    """The ``(q, omega0, halfwidth)`` triples for the ω(q) model — one per grid point with a usable fit."""
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


def _stuck_why(g: GridQ) -> str:
    """The human why-string for a stuck q, DERIVED from :attr:`GridQ.stuck_reason` (S3 §6.1: stop
    messages must be generated from the classification, never canned). One line, no leading q label."""
    reason = g.stuck_reason
    if reason == "interval_floored":
        return f"pooled {g.n_production} scans without converging (interval floored above tolerance)"
    if reason == "info_saturated":
        return ("value-of-information saturated (another scan would shrink σ(ω₀) by less than "
                "the VOI floor — diminishing returns)")
    if reason == "uncertifiable_interval":
        return (f"delivered ω₀ center(s) but every calibrated interval is unreliable "
                f"(γ-at-bound: resolution-limited spike, degenerate bootstrap) — uncertifiable, "
                f"not no-signal")
    if reason == "fast_tier_only":
        return ("delivered ω₀ center(s) but no fit produced a trusted interval (stayed on the "
                "untrusted fast HESSE tier) — uncertifiable, not no-signal")
    if reason == "infeasible":
        return f"unreachable by the instrument: all {g.n_infeasible} proposals validation-rejected"
    if reason == "signal_unmodeled":
        return ("signal present but the model cannot account for it (disposition 'unmodeled') — "
                "model problem, not absence of signal")
    return f"declined {g.n_decline}× (no signal)"


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
        max_declines: kept for signature back-compat; the abandon rule lives in ``GridQ.stuck``.
    """
    tol = objective.tolerance_meV
    fl = systematic_floor(calibration, floor)
    per_q = []
    for g in grid:
        m = g.master or {}
        # S3 machine-readable stop-state per q: the stuck reason for a stuck q, "met" when converged,
        # else None (in progress). Human why-strings below are DERIVED from the same classification.
        reason = g.stuck_reason if g.stuck else ("met" if g.converged else None)
        per_q.append({"hkl": list(g.hkl), "q": g.q, "converged": g.converged,
                      "calibrated_halfwidth": g.cert_halfwidth, "omega0": g.cert_omega0,
                      "master_halfwidth": m.get("halfwidth"), "master_n_members": m.get("n_members"),
                      "master_members": m.get("members"),
                      "single_prod_halfwidth": g.best_prod_halfwidth,
                      "source": ("master" if (g._master_hw is not None and g.cert_halfwidth == g._master_hw)
                                 else "single_production" if g.cert_halfwidth is not None else None),
                      "n_production": g.n_production, "n_decline": g.n_decline,
                      "n_uncertifiable": g.n_uncertifiable, "reason": reason, "stuck": g.stuck})

    # Tolerance below the systematic floor is unmeetable by construction — check before "met".
    if tol < fl:
        return StopDecision("unmeetable",
                            [f"tolerance {tol:g} meV is below the resolution-asymmetry systematic "
                             f"floor {fl:g} meV (calibration center_pull_mean) — unreachable"], per_q)

    if grid and all(g.converged for g in grid):
        return StopDecision("met",
                            [f"all {len(grid)} grid q converged: the pooled master ω₀ interval "
                             f"(inflation-calibrated) ≤ {tol:g} meV at each point"], per_q)

    reasons: list[str] = []
    spent = float(budget_state.get("spent", 0.0))
    amount = budget_state.get("amount")
    if amount is not None and spent >= float(amount):
        n_unconverged = sum(1 for g in grid if not g.converged)
        reasons.append(f"budget exhausted ({spent:g}/{amount:g} {budget_state.get('kind', '')}) "
                       f"with {n_unconverged} grid q still under-determined")
        return StopDecision("unmeetable", reasons, per_q)

    # A stuck q is abandoned, not campaign-fatal: the acquisition engine skips it and the campaign
    # keeps mapping the rest. Terminal only when no workable (non-converged, non-stuck) q remains.
    stuck = [g for g in grid if g.stuck]
    workable = [g for g in grid if not g.converged and not g.stuck]
    if stuck and not workable:
        n_conv = sum(1 for g in grid if g.converged)
        reasons.append(f"converged {n_conv}/{len(grid)} grid q; the rest cannot deliver the statistics:")
        for g in stuck:
            reasons.append(f"q={g.q:g} (hkl={list(g.hkl)}) {_stuck_why(g)}")
        return StopDecision("unmeetable", reasons, per_q)

    return StopDecision("continue", [], per_q)
