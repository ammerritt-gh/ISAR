"""The running dispersion prior (UNIFICATION_DESIGN §5) — built from accumulated resolved fits.

Generalizes the gold check's leave-one-out `predict_omega` from gold-CSV rows to ISAR's own
accumulated fits. Lives here (not `isar/analyze/prior.py`, which only *consumes* a `PhononPrior`)
because building the prior is orchestration, not analysis.

Gate (the load-bearing hygiene): only `verdict == "resolved"` AND `modality == "fold"` outcomes
enter the prior. A fork averaged into the prior poisons every neighbour (Phase 3 §4), so forks —
and flagged / model-incomplete outcomes — stay out in v1. Points are keyed by scan_id; a re-fit
supersedes its own point.

`prior_for` reproduces `predict_omega`'s logic: pool same-sample points (same-T first, else all),
fit omega0 linearly against the dominant-varying Q component (auto-detected across the pool, which
is K on the gold data), and predict at the query Q with a floored sigma.
"""
from __future__ import annotations

import numpy as np

from isar.analyze.prior import PhononPrior
from isar.schema.card import CardState

SIGMA_FLOOR = 0.3       # meV — the predict_omega floor
SAME_T_TOL = 0.5        # K — "same temperature" for pooling (matches predict_omega)


def add_point(state: CardState, *, scan_id, sample, T, q, omega0, modality, verdict,
              gamma=None, gamma_err=None, intensity=None, fit_kind=None,
              t_round: float = 5.0) -> bool:
    """Append/replace a prior point, gated to resolved + fold. Returns True if a point was stored.

    ``gamma``/``gamma_err`` and ``intensity`` (a portable :class:`~isar.schema.steering.IntensityMeasure`
    dict, steering contract 2) are stored when supplied so a branch model (Phase 2+) can carry Γ(q) and
    A(q) alongside ω₀(q). All three are optional and omitted from the point when None, so older callers
    and stored points are unchanged.
    """
    if verdict != "resolved" or modality != "fold" or omega0 is None:
        return False
    point = {
        "scan_id": scan_id,
        "sample": sample,
        "T": float(T),
        "t_bin": round(float(T) / t_round) * t_round if t_round else float(T),
        "q": [float(v) for v in q],
        "omega0": float(omega0),
        "modality": modality,
        "verdict": verdict,
    }
    if gamma is not None:
        point["gamma"] = float(gamma)
    if gamma_err is not None:
        point["gamma_err"] = float(gamma_err)
    if intensity is not None:
        point["intensity"] = intensity   # already a JSON-able IntensityMeasure dict
    if fit_kind and fit_kind != "unconstrained":
        point["fit_kind"] = fit_kind     # contract 4: a constrained/demoted fit must not seed a branch
    # Replace an existing point for the same scan (a re-fit supersedes).
    state.dispersion_prior = [p for p in state.dispersion_prior if p.get("scan_id") != scan_id]
    state.dispersion_prior.append(point)
    return True


def prior_for(state: CardState, *, sample, T, q, exclude_scan_id=None,
              same_t_tol: float = SAME_T_TOL, sigma_floor: float = SIGMA_FLOOR) -> PhononPrior | None:
    """A `PhononPrior` from the accumulated points near (sample, T, q), or None if too sparse.

    amplitude stays None (detection power abstains, v1) — carrying a neighbour-median amplitude to
    enable the underpowered verdict is the marked Phase-4 extension.
    """
    pts = [p for p in state.dispersion_prior
           if p.get("sample") == sample and p.get("scan_id") != exclude_scan_id]
    if not pts:
        return None
    same_t = [p for p in pts if abs(float(p["T"]) - float(T)) <= same_t_tol]
    pool = same_t if len(same_t) >= 2 else pts

    qs = np.array([p["q"] for p in pool], dtype=float)      # (n, 3)
    ci = int(np.argmax(qs.max(axis=0) - qs.min(axis=0))) if len(pool) >= 2 else 1
    qv = qs[:, ci]
    ov = np.array([p["omega0"] for p in pool], dtype=float)
    query = float(q[ci]) if ci < len(q) else 0.0

    if len(pool) >= 2 and len(set(np.round(qv, 6))) >= 2:
        coef = np.polyfit(qv, ov, 1)
        pred = float(np.polyval(coef, query))
        sig = max(sigma_floor, float(np.std(ov - np.polyval(coef, qv))))
    else:
        pred = float(np.mean(ov))
        sig = max(sigma_floor, float(np.std(ov)))
    return PhononPrior(omega0=pred, amplitude=None, omega0_sigma=sig)
