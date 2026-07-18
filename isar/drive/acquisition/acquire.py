"""The acquisition function — propose the next SCAN (LLM_HARNESS_DESIGN §2.2, §8 step 4.1).

**The acquisition unit is a SCAN, not a point.** A proposal is a complete constant-Q energy scan:

- a **Q position** — one objective-path grid point (the driver scans at fixed H,K,L and sweeps deltaE);
- an **energy window** — centered on the predicted ω(q), spanning **both sides of elastic** so the
  Stokes/anti-Stokes pair lands inside when ``kT ≳ ħω`` (low side ``−(E_pred + 2)`` meV), with a tight
  high side (``E_pred + 1.5 × resolution FWHM`` from ``GET /resolution``) to exclude higher-branch
  leakage — ISAR's N-mode fitter is the safety net if something leaks anyway;
- a **step** ≈ resolution FWHM / 4 (steps ≪ FWHM waste points, ≫ it undersample), clamped so the scan
  has ≤ 100 points (the budget cap);
- a **neutron count** — escalating from a cheap quick-look (:data:`QUICKLOOK_NCOUNT`, to seed the count
  rate / ETA) to a production count (:data:`PRODUCTION_NCOUNT`) only where the tolerance demands it.

**Target selection.** Unvisited grid q are quick-looked first (largest model-predicted σ). Once every q
has at least a quick-look, the q with the largest calibrated-σ-above-tolerance **deficit** is escalated:
production, then — after a resolved first crossing — a dedicated ``confirm`` scan (the peeking-bias
guard lives in :mod:`isar.drive.supervisor.stopping`).

"Peak outside the chosen window" is a first-class outcome, not a failure (recover: widen / re-center —
a LOOK, §1, handled in :mod:`isar.drive.supervisor.exceptions`). Cost is movement-aware once TAVI's
virtual instrument clock exists; until then the function counts neutrons only.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Any, Callable

from isar.drive.acquisition.eig import (
    counts_eig,
    eig_per_cost,
    fisher_eig,
    predicted_sigma_after,
)
from isar.drive.acquisition.classify_eig import widen_factor
from isar.drive.supervisor.stopping import (
    PRODUCTION_NCOUNT,
    QUICKLOOK_NCOUNT,
    GridQ,
    summarize_grid,
)
from isar.schema.steering import ProposalScore

# Energy-window rule constants (LLM_HARNESS_DESIGN acquisition decisions, binding).
LOW_MARGIN_MEV = 2.0          # low side = −(E_pred + LOW_MARGIN): span the anti-Stokes side
HIGH_RES_FACTOR = 1.5         # high side = E_pred + HIGH_RES_FACTOR × resolution FWHM
STEP_RES_FRACTION = 0.25      # step ≈ resolution FWHM / 4
MAX_POINTS = 100              # budget cap on scan length
DEFAULT_FWHM_MEV = 0.5        # fallback energy resolution when /resolution is unavailable / refuses

# Survey-window rule: when the ω(q) prediction is uncertain (cold model / far extrapolation) the tight
# high side ``e_pred + 1.5·FWHM`` is unjustified — a dispersing branch can lie far above ``e_pred`` and
# would never land in-window, so the model never gets an anchor and the acquisition deadlocks (no
# anchor → narrow window → no anchor). While σ_pred exceeds a resolution width we instead scan the
# plausible phonon band; as the model seeds, σ_pred drops below FWHM and the window tightens as before.
SURVEY_SIGMA_K = 3.0          # survey high side covers e_pred + k·σ_pred of the ω(q) predictive spread
SURVEY_E_HI_MEV = 7.0         # meV ceiling on the survey high side: spans the acoustic band, clips the optic


@dataclass
class ScanProposal:
    """A complete constant-Q energy-scan request the instrument client can submit."""

    h: float
    k: float
    l: float
    e_lo: float
    e_hi: float
    e_step: float
    ncount: int
    kind: str                # "quicklook" | "production" | "confirm"
    reason: str
    e_pred: float = 0.0
    fwhm_dE: float = 0.0
    n_points: int = 0
    acquisition_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def scan_command(self) -> str:
        """The TAVI ``scan_command1`` for this window: ``deltaE <lo> <hi> <step>``."""
        return f"deltaE {self.e_lo:g} {self.e_hi:g} {self.e_step:g}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hkl": [self.h, self.k, self.l], "e_lo": self.e_lo, "e_hi": self.e_hi,
            "e_step": self.e_step, "ncount": self.ncount, "kind": self.kind, "reason": self.reason,
            "e_pred": self.e_pred, "fwhm_dE": self.fwhm_dE, "n_points": self.n_points,
            "acquisition_id": self.acquisition_id,
            "scan_command": self.scan_command(), "meta": dict(self.meta),
        }


class BudgetInsufficientForMinimumBatch(ValueError):
    """The remaining neutron budget cannot fund one policy-floor scan."""


def _ceil_production_ladder(exposure: float, floor: int) -> int:
    """Smallest ``floor * 2**k`` not below ``exposure`` (``k >= 0``)."""
    floor = max(int(floor), 1)
    exposure = max(float(exposure), float(floor))
    if exposure <= floor:
        return floor
    return floor * (2 ** int(math.ceil(math.log2(exposure / floor))))


def _count_request(gridq: GridQ, *, kind: str, n_points: int, tolerance: float,
                   policy: dict, budget_state: dict | None) -> tuple[int, dict[str, Any]]:
    """Return the next per-point count request and deterministic scaling diagnostics."""
    quicklook_floor = int(policy.get("quicklook_ncount", QUICKLOOK_NCOUNT))
    production_floor = int(policy.get("production_batch_ncount", PRODUCTION_NCOUNT))
    dynamic = bool(policy.get("dynamic_ncount", False))
    diagnostics: dict[str, Any] = {"dynamic_ncount": dynamic, "exposure_required": None,
                                   "exposure_current": None, "exposure_remaining": None}
    if kind == "quicklook":
        requested = quicklook_floor
        minimum = quicklook_floor
    elif not dynamic:
        requested = production_floor
        minimum = production_floor
    else:
        master = gridq.master or {}
        halfwidth = master.get("diagnostic_halfwidth", master.get("halfwidth"))
        if halfwidth is None:
            halfwidth = gridq.cert_halfwidth if gridq.cert_halfwidth is not None else gridq.best_halfwidth
        current = master.get("effective_exposure")
        if current is None:
            current = gridq.submitted_exposure
        try:
            current = float(current)
        except (TypeError, ValueError):
            current = 0.0
        if not math.isfinite(current) or current <= 0.0:
            current = 0.0
        # No interval yet: quicklook is attempt one, the production floor attempt two, and the final
        # generic no-signal attempt grows geometrically. Once an interval exists, tolerance scaling
        # below replaces this fallback with the authoritative remainder calculation.
        remaining = float(production_floor * (2 ** max(int(gridq.n_production), 0)))
        required = None
        if halfwidth is not None and math.isfinite(float(halfwidth)) and current > 0.0:
            margin = float(policy.get("tolerance_margin", 0.8))
            required = current * (float(halfwidth) / (margin * float(tolerance))) ** 2
            remaining = max(required - current, 0.0)
        requested = _ceil_production_ladder(remaining, production_floor)
        previous = int(gridq.last_submitted_ncount or production_floor)
        requested = min(requested,
                        int(math.floor(previous * float(policy.get("ncount_growth_cap", 4.0)))),
                        int(policy.get("max_ncount_per_scan", 4_000_000_000)))
        requested = max(requested, production_floor)
        minimum = production_floor
        diagnostics.update({"exposure_required": required, "exposure_current": current,
                            "exposure_remaining": remaining})

    budget = dict(budget_state or {})
    if budget.get("kind") == "neutrons" and budget.get("amount") is not None:
        remaining_budget = max(float(budget["amount"]) - float(budget.get("spent", 0.0)), 0.0)
        affordable = int(math.floor(remaining_budget / max(int(n_points), 1)))
        if affordable < minimum:
            raise BudgetInsufficientForMinimumBatch(
                f"budget_insufficient_for_minimum_batch: need {minimum * max(int(n_points), 1):g} "
                f"neutrons, have {remaining_budget:g}")
        requested = min(requested, affordable)
        diagnostics["neutron_budget_clamp"] = affordable
    return int(requested), diagnostics


def _resolve_fwhm(resolution_info, h: float, k: float, l: float, deltaE: float) -> float:
    """Extract the energy-resolution FWHM (meV) from ``resolution_info``.

    Accepts a plain float, a dict (``{"fwhm_dE": x}`` or a raw ``GET /resolution`` body with
    ``fwhm.dE``, honoring ``ok == false`` refusals), or a callable ``(h,k,l,deltaE) -> (any of these)``.
    Falls back to :data:`DEFAULT_FWHM_MEV` when nothing usable is present.
    """
    info = resolution_info(h, k, l, deltaE) if callable(resolution_info) else resolution_info
    return _fwhm_from(info)


def _fwhm_from(info) -> float:
    if info is None:
        return DEFAULT_FWHM_MEV
    if isinstance(info, (int, float)):
        val = float(info)
        return val if val > 0 else DEFAULT_FWHM_MEV
    if isinstance(info, dict):
        if info.get("ok") is False:               # /resolution refusal (infeasible geometry)
            return DEFAULT_FWHM_MEV
        if "fwhm_dE" in info and info["fwhm_dE"]:
            return float(info["fwhm_dE"])
        fwhm = info.get("fwhm")
        if isinstance(fwhm, dict) and fwhm.get("dE"):
            return float(fwhm["dE"])
        if isinstance(fwhm, (int, float)) and fwhm > 0:
            return float(fwhm)
    return DEFAULT_FWHM_MEV


def points_in_window(e_lo: float, e_hi: float, e_step: float, *,
                     max_points: int = MAX_POINTS) -> int:
    """The number of scan points a ``deltaE e_lo e_hi e_step`` window has, capped by policy.

    The single source of truth for a window's point count — used by :func:`_energy_window` and by the
    loop's missed-window recenter so ``ScanProposal.n_points`` (hence the charged cost) stays truthful
    after ``e_lo``/``e_hi`` are mutated (they otherwise are not recomputed).
    """
    step = max(float(e_step), 1e-9)
    span = max(float(e_hi) - float(e_lo), 0.0)
    # Mirror TAVI utilities.parse_scan_steps exactly: nearest integral number of intervals, plus the
    # inclusive start point.  This also avoids a 23.999999999999996 quotient losing the last point.
    return min(int(math.floor(span / step + 0.5)) + 1, max(int(max_points), 2))


def _energy_window(e_pred: float, fwhm: float, e_sigma: float = 0.0,
                   *, survey: bool = False, energy_search_meV=None,
                   target_band=None, rival_band=None, widen: float = 1.0,
                   positive_only: bool = False,
                   max_points: int = MAX_POINTS) -> tuple[float, float, float, int]:
    """Build ``(e_lo, e_hi, e_step, n_points)`` from the predicted ω, the resolution FWHM, and the
    predictive uncertainty ``e_sigma`` on ω(q).

    The high side is extended to survey the phonon band (capped at :data:`SURVEY_E_HI_MEV`) when the
    peak is not yet pinned at this q — either because ``e_sigma`` exceeds a resolution width (cold model
    / far extrapolation) or because ``survey`` is forced (the q has no confirmed resolved fit yet). The
    latter matters even when the model *looks* confident: a concave dispersion curves above a local-linear
    prediction, so a tight window centred on a biased-low ``e_pred`` misses the real peak by a hair and
    the q never seeds. Once a q is resolved the caller drops ``survey`` and the tight window returns.
    ``e_sigma=0`` and ``survey=False`` reproduce the tight window.
    """
    e = max(float(e_pred), 0.0)                    # a phonon energy is non-negative
    fwhm = max(float(fwhm), 1e-3)
    e_lo = -(e + LOW_MARGIN_MEV)
    e_hi = e + HIGH_RES_FACTOR * fwhm
    sig = float(e_sigma)
    search_lo, search_hi = ((float(energy_search_meV[0]), float(energy_search_meV[1]))
                            if energy_search_meV is not None else (-2.0, SURVEY_E_HI_MEV))
    if survey:                                     # unconfirmed q → scan the configured plausible band
        e_lo = min(e_lo, search_lo)
        e_hi = max(e_hi, search_hi)
    elif math.isfinite(sig) and sig > fwhm:        # ω(q) not yet localized → survey e_pred + k·σ
        e_hi = max(e_hi, min(e + SURVEY_SIGMA_K * sig, search_hi))
    if target_band is not None:
        target_mu, target_sigma = map(float, target_band)
        e_lo = min(e_lo, target_mu - 2.0 * target_sigma)
        e_hi = max(e_hi, target_mu + 2.0 * target_sigma)
    if rival_band is not None:
        rival_mu, rival_sigma = map(float, rival_band)
        e_lo = min(e_lo, rival_mu - 2.0 * rival_sigma)
        e_hi = max(e_hi, rival_mu + 2.0 * rival_sigma)
    if widen > 1.0:
        e_hi = e + (e_hi - e) * float(widen)
        if widen >= 2.5:  # persisted decline step 3+: full feasible search-band intersection
            e_hi = search_hi
    if target_band is not None or rival_band is not None or widen > 1.0 or positive_only:
        e_lo, e_hi = max(e_lo, search_lo), min(e_hi, search_hi)
    if positive_only:
        e_lo = max(e_lo, 0.5 * fwhm)
    if e_hi <= e_lo:                               # defensive: never invert
        e_hi = e_lo + fwhm
    e_step = STEP_RES_FRACTION * fwhm
    span = e_hi - e_lo
    point_cap = max(int(max_points), 2)
    n_points = int(math.floor(span / e_step + 0.5)) + 1
    if n_points > point_cap:                        # clamp to the frozen steering-policy cap
        e_step = span / (point_cap - 1)
        n_points = point_cap
    return e_lo, e_hi, e_step, n_points


def _q_sigma(g: GridQ) -> float | None:
    """The q's current ω₀ half-width belief: the certified (master/best-production) half-width, else
    the best per-scan/quick-look estimate — ``None`` if the q has never been usably fit."""
    return g.cert_halfwidth if g.cert_halfwidth is not None else g.best_halfwidth


def _q_next_cost(g: GridQ) -> float:
    """The ΔC of this q's next production scan — its most recent production cost, else its accumulated
    cost as a proxy (a scale for the EIG-per-cost key, not a submitted number)."""
    return float(g.last_prod_cost) if g.last_prod_cost else float(g.counts_spent)


def _finite_or_none(x) -> float | None:
    """``x`` if finite, else ``None`` — so ``+inf`` scores round-trip through strict JSON."""
    return float(x) if x is not None and math.isfinite(x) else None


def _driver_inserted_candidate(objective, grid: list[GridQ], observations: list[dict],
                               classify) -> GridQ | None:
    """Choose one deterministic classification probe outside the certification grid.

    The operator path remains the complete certification set.  After every requested point has at
    least a quicklook, O9 permits a higher-classification-value midpoint or segment extension to win
    one scan.  Already observed inserted points are skipped, so resume cannot repeat the same probe.
    """
    if classify is None or not getattr(objective, "hypotheses", None) or len(grid) < 2:
        return None
    if not all(g.visited for g in grid):
        return None

    path = [tuple(map(float, point)) for point in objective.path]
    scalars = [0.0]
    for left, right in zip(path, path[1:]):
        scalars.append(scalars[-1] + math.sqrt(sum((b - a) ** 2 for a, b in zip(left, right))))
    candidates: list[tuple[tuple[float, float, float], float]] = []
    for i, (left, right) in enumerate(zip(path, path[1:])):
        candidates.append((tuple((a + b) / 2.0 for a, b in zip(left, right)),
                           (scalars[i] + scalars[i + 1]) / 2.0))
    first_delta = tuple(b - a for a, b in zip(path[0], path[1]))
    last_delta = tuple(b - a for a, b in zip(path[-2], path[-1]))
    # Multiple deterministic extension shells make the authority genuinely able to leave the
    # requested segment; acquisition value, not a special-case branch rule, chooses among them.
    for shell in (1.0, 2.0, 3.0):
        candidates.append((tuple(a - shell * d for a, d in zip(path[0], first_delta)),
                           scalars[0] - shell * (scalars[1] - scalars[0])))
        candidates.append((tuple(a + shell * d for a, d in zip(path[-1], last_delta)),
                           scalars[-1] + shell * (scalars[-1] - scalars[-2])))

    observed = [tuple(map(float, row["hkl"])) for row in observations
                if isinstance(row.get("hkl"), (list, tuple)) and len(row["hkl"]) == 3]
    transient = []
    seen_candidates: set[tuple[float, float, float]] = set()
    differentiation = (getattr(objective, "steering", None) or {}).get(
        "differentiation_tolerance_meV")
    differentiation = (float(differentiation) if differentiation is not None
                       else 3.0 * float(objective.tolerance_meV))
    for offset, (hkl, q) in enumerate(candidates):
        identity = tuple(round(value, 12) for value in hkl)
        if identity in seen_candidates:
            continue
        seen_candidates.add(identity)
        # Keep a hard geometric separation from every certification point.  Explicit provenance is
        # still the authoritative gate, but this also prevents a pooled off-path result from being
        # nearest-matched to an extremely fine requested grid after resume.
        if any(math.dist(hkl, requested) <= 1e-4 for requested in path):
            continue
        if any(math.dist(hkl, old) <= 1e-4 for old in observed):
            continue
        transient.append(GridQ(index=len(grid) + offset, hkl=hkl, q=q,
                               tolerance=differentiation))
    if not transient:
        return None

    def classification_value(candidate: GridQ) -> float:
        value = float(classify(candidate))
        # The provider estimates mutual information, whose support is non-negative.  Tiny negative
        # numerical artifacts carry no information and must not make another candidate look valuable.
        return max(value, 0.0) if math.isfinite(value) else float("-inf")

    inserted_scores = [(classification_value(candidate), candidate) for candidate in transient]
    inserted_value, best = max(inserted_scores, key=lambda item: item[0])
    requested_value = max(classification_value(candidate) for candidate in grid)
    # An inserted probe may win only by a real comparison.  If the classification provider cannot
    # score any requested point, fall back to ordinary target acquisition rather than interpreting
    # missing/non-finite grid evidence as permission to leave the path.
    if not math.isfinite(requested_value) or inserted_value <= requested_value + 1e-12:
        return None
    return best


def _select_target(grid: list[GridQ], model, *,
                   fisher=None, opening=None, classify=None,
                   classification_weight: float = 0.0) -> tuple[GridQ | None, str, str, ProposalScore | None]:
    """Pick the next grid q and its scan kind. Returns ``(gridq, kind, reason, score)``.

    Tiers, most-specific first:

    1. **Unvisited** q → a quick-look (largest model-predicted σ) to seed the ω(q) model + count rate.
    2. **No-baseline** workable q → a PRODUCTION scan to establish a first usable ω₀ (largest σ-deficit).
    3. **Baselined** workable q → ranked by **cost-normalized EIG on ω₀**:
       - **Fisher tier (5b)** — when a ``fisher`` provider grounds any candidate (an in-anchor branch +
         an unconstrained last fit), rank *only* the grounded candidates by
         ``fisher_eig(σ_prior, σ_scan)/ΔC`` (the identifiability-aware screen). No silent method-mixing:
         a candidate the Fisher can't ground drops to the counts tier only if *none* is grounded.
       - **Counts tier (5a)** — else the goal-weighted counts-EIG per cost, when every candidate carries
         a measured cost history; otherwise the σ-deficit ranking (regression-safe for hand-built /
         pre-Phase-5 callers).

    Stuck q (``MAX_DECLINES`` no-signal, ``MAX_PROD_ATTEMPTS`` pooled without converging, or
    value-of-information saturated) are abandoned. ``score`` carries ``cost=0``/``eig_per_cost=None``;
    :func:`propose_scan` fills the actual ΔC once the window is known.
    """
    unvisited = [g for g in grid if not g.visited]
    if unvisited:
        pick = min(unvisited, key=opening) if opening is not None else max(
            unvisited, key=lambda g: model.predict(g.q)[1])
        purpose = "hypothesis_check" if opening is not None else "survey"
        reason = (f"unvisited q={pick.q:g} (hkl={list(pick.hkl)}); quick-look to seed the ω(q) model "
                  f"and count rate (predicted σ={model.predict(pick.q)[1]:g} meV)")
        return pick, "quicklook", reason, ProposalScore(
            method="explore", c_spent=float(pick.counts_spent), reason=reason, purpose=purpose)
    candidates = [g for g in grid if not (g.converged or g.stuck)]
    if not candidates:
        return None, "", "all grid q converged or abandoned (stuck)", None

    # Baseline-first: a workable q with no usable ω₀ yet gets a production scan to establish one
    # (unchanged behaviour — these carry a +inf σ-deficit and win outright).
    no_baseline = [g for g in candidates if _q_sigma(g) is None]
    if no_baseline:
        pick = max(no_baseline, key=lambda g: g.deficit)
        reason = (f"baseline q={pick.q:g} (hkl={list(pick.hkl)}); no usable ω₀ interval yet; production "
                  f"scan to establish the master")
        return pick, "production", reason, ProposalScore(
            method="explore", c_spent=float(pick.counts_spent), reason=reason)

    # Fisher tier (5b): rank the identifiability-aware EIG for the candidates a grounded rate model can
    # score. Grounding (in-anchor branch + unconstrained last fit) lives entirely in the provider.
    if fisher is not None and model is not None:
        graded = []
        for g in candidates:
            w0 = float(model.predict(g.q)[0])
            sp = _q_sigma(g)
            ss = fisher(g, w0, sp)
            if ss is not None and math.isfinite(ss):
                graded.append((g, sp, ss))
        if graded:
            def _fkey(t):
                g, sp, ss = t
                classification = max(float(classify(g)), 0.0) if classify is not None else 0.0
                return eig_per_cost(
                    fisher_eig(sp, ss) + classification_weight * classification,
                    _q_next_cost(g))
            pick, sp, ss = max(graded, key=_fkey)
            eig = fisher_eig(sp, ss)
            classification = max(float(classify(pick)), 0.0) if classify is not None else 0.0
            combined = eig + classification_weight * classification
            reason = (f"max-Fisher-EIG/cost q={pick.q:g} (hkl={list(pick.hkl)}); σ_prior={sp:g}→"
                      f"σ_scan={ss:g} meV, C={pick.counts_spent:g}; production scan (identifiability-aware)")
            return pick, "production", reason, ProposalScore(
                method="fisher", eig=_finite_or_none(eig), sigma_prior=_finite_or_none(sp),
                sigma_scan=_finite_or_none(ss), c_spent=float(pick.counts_spent), reason=reason,
                classification_eig=_finite_or_none(classification),
                classification_weight=float(classification_weight),
                combined_information=_finite_or_none(combined), purpose="target_or_classification")

    # Counts tier (5a): goal-weighted EIG-per-cost when every candidate has a cost history.
    if all(g.counts_spent > 0.0 for g in candidates):
        def _ckey(g):
            dc = _q_next_cost(g)
            target = counts_eig(_q_sigma(g), g.tolerance, g.counts_spent, dc)
            classification = max(float(classify(g)), 0.0) if classify is not None else 0.0
            return eig_per_cost(target + classification_weight * classification, dc)
        pick = max(candidates, key=_ckey)
        dc = _q_next_cost(pick)
        eig = counts_eig(_q_sigma(pick), pick.tolerance, pick.counts_spent, dc)
        classification = max(float(classify(pick)), 0.0) if classify is not None else 0.0
        combined = eig + classification_weight * classification
        reason = (f"max-EIG/cost q={pick.q:g} (hkl={list(pick.hkl)}); σ={_q_sigma(pick):g} meV over "
                  f"tolerance {pick.tolerance:g}, C={pick.counts_spent:g}; production scan (EIG-per-cost ranked)")
        return pick, "production", reason, ProposalScore(
            method="counts", eig=_finite_or_none(eig), sigma_prior=_finite_or_none(_q_sigma(pick)),
            sigma_scan=_finite_or_none(predicted_sigma_after(_q_sigma(pick), pick.counts_spent, dc)),
            c_spent=float(pick.counts_spent), reason=reason,
            classification_eig=_finite_or_none(classification),
            classification_weight=float(classification_weight),
            combined_information=_finite_or_none(combined), purpose="target_or_classification")

    pick = max(candidates, key=lambda g: g.deficit)
    deficit = pick.deficit
    deficit_str = "unfitted" if deficit == float("inf") else f"{deficit:g} meV over tolerance"
    n_pool = (pick.master or {}).get("n_members") or pick.n_production
    reason = (f"largest-deficit q={pick.q:g} (hkl={list(pick.hkl)}); {deficit_str}; production scan to "
              f"accumulate the master (pool now {n_pool})")
    return pick, "production", reason, ProposalScore(
        method="deficit", sigma_prior=_finite_or_none(_q_sigma(pick)),
        c_spent=float(pick.counts_spent), reason=reason)


def propose_scan(objective, model, results_so_far: list[dict], resolution_info=None,
                 budget_state: dict | None = None, master_rows: list[dict] | None = None,
                 fisher=None, classify=None, rival_band=None) -> ScanProposal | None:
    """Propose the next scan (Q, energy window, neutron count), or ``None`` if nothing remains.

    Args:
        objective: the :class:`~isar.drive.objective.ObjectiveSpec` (path + tolerance).
        model: a fitted ω(q) model exposing ``predict(q) -> (mean, sigma)``
            (:mod:`isar.drive.acquisition.model`).
        results_so_far: the chronological observation list (see
            :func:`isar.drive.supervisor.stopping.summarize_grid`).
        resolution_info: the energy-resolution source — a float, a dict, or a callable
            ``(h,k,l,deltaE) -> …`` (the loop wraps ``GET /resolution``). See :func:`_resolve_fwhm`.
        budget_state: informational; carried onto the proposal meta. The stopping criterion owns the
            budget verdict, not the acquisition function.
        fisher: optional Poisson-Fisher screen provider ``(gridq, omega0_pred, sigma_prior) -> σ_scan``
            (5b). Returns the predicted post-scan σ(ω₀) when the q is Fisher-grounded, else ``None``.
    """
    grid = summarize_grid(objective, results_so_far or [], master_rows or [])
    target_hypothesis = next((h for h in getattr(objective, "hypotheses", [])
                              if h.get("name") == getattr(objective, "target_hypothesis", None)), None)
    opening = None
    if target_hypothesis is not None:
        name = target_hypothesis.get("name")
        if name == "optic":
            opening = lambda g: (g.q, g.index)
        elif name == "acoustic":
            def opening(g):
                mu, _ = model.predict(g.q)
                return (0 if mu >= DEFAULT_FWHM_MEV else 1, g.q, g.index)
    policy = dict(getattr(objective, "steering", {}) or {})
    classification_weight = float(policy.get("classification_weight", 0.0))
    inserted = _driver_inserted_candidate(objective, grid, results_so_far or [], classify)
    if inserted is not None:
        grid = [*grid, inserted]
    pick, kind, reason, score = _select_target(
        grid, model, fisher=fisher, opening=opening, classify=classify,
        classification_weight=classification_weight)
    if pick is None:
        return None
    driver_inserted = not any(math.dist(pick.hkl, tuple(point)) <= 1e-4 for point in objective.path)
    if driver_inserted:
        reason = (f"driver-inserted classification probe q={pick.q:g} (hkl={list(pick.hkl)}); "
                  "higher expected branch-information value than every certification point")
        classification = max(float(classify(pick)), 0.0)
        combined = ((score.eig or 0.0) + classification_weight * classification)
        score = replace(score, reason=reason, purpose="differentiate",
                        classification_eig=_finite_or_none(classification),
                        classification_weight=classification_weight,
                        combined_information=_finite_or_none(combined))

    e_pred, e_sigma = model.predict(pick.q)
    h, k, l = pick.hkl
    fwhm = _resolve_fwhm(resolution_info, h, k, l, max(float(e_pred), 0.0))
    # Survey the band until this q has a confirmed resolved fit — a concave dispersion sits above the
    # local-linear e_pred, so a tight window on an unconfirmed q misses the peak and never seeds.
    survey = not bool(getattr(pick, "best_resolved", False))
    target_band = None
    positive_only = False
    if target_hypothesis is not None and target_hypothesis.get("name") == "optic" and survey:
        zc = target_hypothesis.get("zc_energy") or {}
        target_band = (zc.get("mean", zc.get("mode", e_pred)), zc.get("sigma", e_sigma))
        positive_only = True
    elif target_hypothesis is not None:
        target_band = (max(float(e_pred), 0.0), max(float(e_sigma), 0.5 * fwhm))
    widen = widen_factor(
        getattr(pick, "n_decline", 0), step=float(policy.get("absent_signal_widen_step", 0.5)),
        max_steps=int(policy.get("absent_signal_widen_steps", 3)))
    rb = rival_band(pick) if callable(rival_band) else rival_band
    e_lo, e_hi, e_step, n_points = _energy_window(
        e_pred, fwhm, e_sigma, survey=survey,
        energy_search_meV=getattr(objective, "energy_search_meV", None),
        target_band=target_band, rival_band=rb, widen=widen, positive_only=positive_only,
        max_points=int(policy.get("max_scan_points", MAX_POINTS)))
    ncount, count_diagnostics = _count_request(
        pick, kind=kind, n_points=n_points, tolerance=float(objective.tolerance_meV),
        policy=policy, budget_state=budget_state)

    dc = float(n_points) * float(ncount)
    information = score.combined_information if score.combined_information is not None else score.eig
    epc = None if information is None else _finite_or_none(eig_per_cost(information, dc))
    score = replace(score, cost=dc, eig_per_cost=epc)
    threshold = float(policy.get("minimum_information_per_cost", 0.0))
    if epc is not None and threshold > 0.0 and epc < threshold:
        return None
    return ScanProposal(
        h=h, k=k, l=l, e_lo=e_lo, e_hi=e_hi, e_step=e_step, ncount=ncount, kind=kind,
        reason=reason, e_pred=float(e_pred), fwhm_dE=float(fwhm), n_points=n_points,
        meta={"e_sigma": _finite_or_none(e_sigma), "q": pick.q, "score": score.to_dict(),
              "purpose": score.purpose, "widen_factor": widen,
              "driver_inserted": driver_inserted,
              "certification_eligible": not driver_inserted,
              "insertion_purpose": "differentiate" if driver_inserted else None,
              "max_scan_points": int(policy.get("max_scan_points", MAX_POINTS)),
              "target_band": list(target_band) if target_band is not None else None,
              "rival_band": list(rb) if rb is not None else None,
              "count_diagnostics": count_diagnostics,
              "budget_state": dict(budget_state) if budget_state else {}},
    )
