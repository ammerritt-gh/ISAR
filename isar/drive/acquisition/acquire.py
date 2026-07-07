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

from dataclasses import dataclass, field
from typing import Any, Callable

from isar.drive.supervisor.stopping import (
    PRODUCTION_NCOUNT,
    QUICKLOOK_NCOUNT,
    GridQ,
    next_kind_for_q,
    summarize_grid,
)

# Energy-window rule constants (LLM_HARNESS_DESIGN acquisition decisions, binding).
LOW_MARGIN_MEV = 2.0          # low side = −(E_pred + LOW_MARGIN): span the anti-Stokes side
HIGH_RES_FACTOR = 1.5         # high side = E_pred + HIGH_RES_FACTOR × resolution FWHM
STEP_RES_FRACTION = 0.25      # step ≈ resolution FWHM / 4
MAX_POINTS = 100              # budget cap on scan length
DEFAULT_FWHM_MEV = 0.5        # fallback energy resolution when /resolution is unavailable / refuses


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


def _energy_window(e_pred: float, fwhm: float) -> tuple[float, float, float, int]:
    """Build ``(e_lo, e_hi, e_step, n_points)`` from the predicted ω and the resolution FWHM."""
    e = max(float(e_pred), 0.0)                    # a phonon energy is non-negative
    fwhm = max(float(fwhm), 1e-3)
    e_lo = -(e + LOW_MARGIN_MEV)
    e_hi = e + HIGH_RES_FACTOR * fwhm
    if e_hi <= e_lo:                               # defensive: never invert
        e_hi = e_lo + fwhm
    e_step = STEP_RES_FRACTION * fwhm
    span = e_hi - e_lo
    n_points = int(span / e_step) + 1
    if n_points > MAX_POINTS:                       # clamp to the budget cap
        e_step = span / (MAX_POINTS - 1)
        n_points = MAX_POINTS
    return e_lo, e_hi, e_step, n_points


def _select_target(grid: list[GridQ], model) -> tuple[GridQ | None, str, str]:
    """Pick the next grid q and its scan kind. Returns ``(gridq, kind, reason)``.

    Unvisited q (quick-look) win first, ranked by model-predicted σ; then the largest-deficit
    non-converged q is escalated to production / confirm. Stuck q (declined ``MAX_DECLINES``×
    without converging) are abandoned — no more beam spent there.
    """
    unvisited = [g for g in grid if not g.visited]
    if unvisited:
        pick = max(unvisited, key=lambda g: model.predict(g.q)[1])
        return pick, "quicklook", (
            f"unvisited q={pick.q:g} (hkl={list(pick.hkl)}); quick-look to seed the ω(q) model and "
            f"count rate (predicted σ={model.predict(pick.q)[1]:g} meV)")
    candidates = [g for g in grid if not (g.converged or g.stuck)]
    if not candidates:
        return None, "", "all grid q converged or abandoned (stuck)"
    pick = max(candidates, key=lambda g: g.deficit)
    kind = next_kind_for_q(pick.prod_fits, pick.tolerance) or "production"
    deficit = pick.deficit
    deficit_str = "unfitted" if deficit == float("inf") else f"{deficit:g} meV over tolerance"
    return pick, kind, (
        f"largest-deficit q={pick.q:g} (hkl={list(pick.hkl)}); {deficit_str}; escalate to {kind}")


def propose_scan(objective, model, results_so_far: list[dict], resolution_info=None,
                 budget_state: dict | None = None) -> ScanProposal | None:
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
    """
    grid = summarize_grid(objective, results_so_far or [])
    pick, kind, reason = _select_target(grid, model)
    if pick is None:
        return None

    e_pred, e_sigma = model.predict(pick.q)
    h, k, l = pick.hkl
    fwhm = _resolve_fwhm(resolution_info, h, k, l, max(float(e_pred), 0.0))
    e_lo, e_hi, e_step, n_points = _energy_window(e_pred, fwhm)
    ncount = QUICKLOOK_NCOUNT if kind == "quicklook" else PRODUCTION_NCOUNT

    return ScanProposal(
        h=h, k=k, l=l, e_lo=e_lo, e_hi=e_hi, e_step=e_step, ncount=ncount, kind=kind,
        reason=reason, e_pred=float(e_pred), fwhm_dE=float(fwhm), n_points=n_points,
        meta={"e_sigma": float(e_sigma), "q": pick.q,
              "budget_state": dict(budget_state) if budget_state else {}},
    )
