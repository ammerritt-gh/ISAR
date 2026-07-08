"""Boundary-safe read of the cross-q branch the session persists into ``card.state.json`` (Phase 5b).

`isar/drive` is decision-only and cannot import :mod:`isar.session.branch_model` (`test_boundary.py`),
so the branch **math that reads a built branch** is duplicated here as a tiny, pure-numpy evaluator over
the plain dict the session stored (via `branch_model.build_branch`). The heavy part — `build_branch`
(the clean-anchor gating + fits) — stays session-only and is never duplicated; only the ~15-line
`predict` is mirrored. A drift-guard test asserts this `predict_branch` equals
`isar.session.branch_model.predict` on shared inputs.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

# Mirror of isar.session.branch_model's constants (kept in sync by the drift-guard test).
GAMMA_FLOOR = 1e-3
GAMMA_SIGMA_FLOOR = 0.05
A_REL_SIGMA_FLOOR = 0.3
_EDGE = 1e-9
BRANCH_ID = "acoustic"


@dataclass(frozen=True)
class BranchView:
    """The drive-side view of a branch prediction (a boundary-safe subset of
    :class:`isar.session.branch_model.BranchPrediction` — no ``soft_constraints``, which is a fit-time
    concern the drive never needs)."""

    gamma: float
    gamma_sigma: float
    amplitude_norm: float
    amplitude_rel_sigma: float
    in_anchor_range: bool


def predict_branch(branch: dict, omega0: float) -> BranchView:
    """Predict (Γ, A) at ``omega0`` from a persisted branch dict, inflating σ when extrapolating past
    the anchor ω₀ range — the exact math of ``isar.session.branch_model.predict``."""
    w = float(omega0)
    gs, gi, gsig = branch["gamma"]
    ls, li, lsig = branch["logA"]
    w_min, w_max = float(branch["w_min"]), float(branch["w_max"])
    span = max(w_max - w_min, 1e-6)
    in_range = (w_min - _EDGE) <= w <= (w_max + _EDGE)
    dist = 0.0 if in_range else (w_min - w if w < w_min else w - w_max)
    infl = 1.0 + dist / span                               # σ grows linearly outside the anchor range

    gamma = max(gs * w + gi, GAMMA_FLOOR)
    amp = float(np.exp(ls * np.log(max(w, 1e-9)) + li))
    gamma_sigma = max(GAMMA_SIGMA_FLOOR, gsig) * infl
    a_rel = max(A_REL_SIGMA_FLOOR, float(np.exp(lsig) - 1.0)) * infl
    return BranchView(gamma=gamma, gamma_sigma=gamma_sigma, amplitude_norm=amp,
                      amplitude_rel_sigma=a_rel, in_anchor_range=in_range)


def select_branch(branch_models, omega0: float, *, branch_id: str = BRANCH_ID) -> dict | None:
    """Pick the branch (of ``branch_id``) whose anchor ω₀ range best fits ``omega0`` — the tightest
    range that covers it, else the nearest by range-edge distance. Returns ``None`` when no branch of
    that id exists (cold campaign). ω₀-coverage handles multiple (sample, t_bin) branches without the
    drive needing to know the campaign's sample/T; the MVP builds only one ``"acoustic"`` branch."""
    w = float(omega0)
    cands = [b for b in (branch_models or []) if b.get("branch_id", BRANCH_ID) == branch_id]
    if not cands:
        return None
    covering = [b for b in cands if float(b["w_min"]) <= w <= float(b["w_max"])]
    if covering:
        return min(covering, key=lambda b: float(b["w_max"]) - float(b["w_min"]))

    def _edge_dist(b) -> float:
        lo, hi = float(b["w_min"]), float(b["w_max"])
        return 0.0 if lo <= w <= hi else min(abs(w - lo), abs(w - hi))

    return min(cands, key=_edge_dist)
