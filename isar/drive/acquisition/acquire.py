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
    meta: dict[str, Any] = field(default_factory=dict)

    def scan_command(self) -> str:
        """The TAVI ``scan_command1`` for this window: ``deltaE <lo> <hi> <step>``."""
        return f"deltaE {self.e_lo:g} {self.e_hi:g} {self.e_step:g}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hkl": [self.h, self.k, self.l], "e_lo": self.e_lo, "e_hi": self.e_hi,
            "e_step": self.e_step, "ncount": self.ncount, "kind": self.kind, "reason": self.reason,
            "e_pred": self.e_pred, "fwhm_dE": self.fwhm_dE, "n_points": self.n_points,
            "scan_command": self.scan_command(), "meta": dict(self.meta),
        }


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


def points_in_window(e_lo: float, e_hi: float, e_step: float) -> int:
    """The number of scan points a ``deltaE e_lo e_hi e_step`` window has (clamped to ``MAX_POINTS``).

    The single source of truth for a window's point count — used by :func:`_energy_window` and by the
    loop's missed-window recenter so ``ScanProposal.n_points`` (hence the charged cost) stays truthful
    after ``e_lo``/``e_hi`` are mutated (they otherwise are not recomputed).
    """
    step = max(float(e_step), 1e-9)
    span = max(float(e_hi) - float(e_lo), 0.0)
    return min(int(span / step) + 1, MAX_POINTS)


def _energy_window(e_pred: float, fwhm: float, e_sigma: float = 0.0,
                   *, survey: bool = False) -> tuple[float, float, float, int]:
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
    if survey:                                     # unconfirmed q → scan the whole plausible band
        e_hi = max(e_hi, SURVEY_E_HI_MEV)
    elif math.isfinite(sig) and sig > fwhm:        # ω(q) not yet localized → survey e_pred + k·σ
        e_hi = max(e_hi, min(e + SURVEY_SIGMA_K * sig, SURVEY_E_HI_MEV))
    if e_hi <= e_lo:                               # defensive: never invert
        e_hi = e_lo + fwhm
    e_step = STEP_RES_FRACTION * fwhm
    span = e_hi - e_lo
    n_points = int(span / e_step) + 1
    if n_points > MAX_POINTS:                       # clamp to the budget cap
        e_step = span / (MAX_POINTS - 1)
        n_points = MAX_POINTS
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


def _select_target(grid: list[GridQ], model, *,
                   fisher=None) -> tuple[GridQ | None, str, str, ProposalScore | None]:
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
        pick = max(unvisited, key=lambda g: model.predict(g.q)[1])
        reason = (f"unvisited q={pick.q:g} (hkl={list(pick.hkl)}); quick-look to seed the ω(q) model "
                  f"and count rate (predicted σ={model.predict(pick.q)[1]:g} meV)")
        return pick, "quicklook", reason, ProposalScore(
            method="explore", c_spent=float(pick.counts_spent), reason=reason)
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
                return eig_per_cost(fisher_eig(sp, ss), _q_next_cost(g))
            pick, sp, ss = max(graded, key=_fkey)
            eig = fisher_eig(sp, ss)
            reason = (f"max-Fisher-EIG/cost q={pick.q:g} (hkl={list(pick.hkl)}); σ_prior={sp:g}→"
                      f"σ_scan={ss:g} meV, C={pick.counts_spent:g}; production scan (identifiability-aware)")
            return pick, "production", reason, ProposalScore(
                method="fisher", eig=_finite_or_none(eig), sigma_prior=_finite_or_none(sp),
                sigma_scan=_finite_or_none(ss), c_spent=float(pick.counts_spent), reason=reason)

    # Counts tier (5a): goal-weighted EIG-per-cost when every candidate has a cost history.
    if all(g.counts_spent > 0.0 for g in candidates):
        def _ckey(g):
            dc = _q_next_cost(g)
            return eig_per_cost(counts_eig(_q_sigma(g), g.tolerance, g.counts_spent, dc), dc)
        pick = max(candidates, key=_ckey)
        dc = _q_next_cost(pick)
        eig = counts_eig(_q_sigma(pick), pick.tolerance, pick.counts_spent, dc)
        reason = (f"max-EIG/cost q={pick.q:g} (hkl={list(pick.hkl)}); σ={_q_sigma(pick):g} meV over "
                  f"tolerance {pick.tolerance:g}, C={pick.counts_spent:g}; production scan (EIG-per-cost ranked)")
        return pick, "production", reason, ProposalScore(
            method="counts", eig=_finite_or_none(eig), sigma_prior=_finite_or_none(_q_sigma(pick)),
            sigma_scan=_finite_or_none(predicted_sigma_after(_q_sigma(pick), pick.counts_spent, dc)),
            c_spent=float(pick.counts_spent), reason=reason)

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
                 fisher=None) -> ScanProposal | None:
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
    pick, kind, reason, score = _select_target(grid, model, fisher=fisher)
    if pick is None:
        return None

    e_pred, e_sigma = model.predict(pick.q)
    h, k, l = pick.hkl
    fwhm = _resolve_fwhm(resolution_info, h, k, l, max(float(e_pred), 0.0))
    # Survey the band until this q has a confirmed resolved fit — a concave dispersion sits above the
    # local-linear e_pred, so a tight window on an unconfirmed q misses the peak and never seeds.
    survey = not bool(getattr(pick, "best_resolved", False))
    e_lo, e_hi, e_step, n_points = _energy_window(e_pred, fwhm, e_sigma, survey=survey)
    ncount = QUICKLOOK_NCOUNT if kind == "quicklook" else PRODUCTION_NCOUNT

    dc = float(n_points) * float(ncount)
    epc = None if score.eig is None else _finite_or_none(eig_per_cost(score.eig, dc))
    score = replace(score, cost=dc, eig_per_cost=epc)
    return ScanProposal(
        h=h, k=k, l=l, e_lo=e_lo, e_hi=e_hi, e_step=e_step, ncount=ncount, kind=kind,
        reason=reason, e_pred=float(e_pred), fwhm_dE=float(fwhm), n_points=n_points,
        meta={"e_sigma": float(e_sigma), "q": pick.q, "score": score.to_dict(),
              "budget_state": dict(budget_state) if budget_state else {}},
    )
