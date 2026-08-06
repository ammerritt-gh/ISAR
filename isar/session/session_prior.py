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


def is_unconstrained_prior_point(point: dict) -> bool:
    """Whether a stored prior point is independent unconstrained fit evidence (contract 4: a
    constrained/demoted fit never seeds a branch — neither the Γ/A branch model nor the ω₀
    assignment belief). Points without a ``fit_kind`` tag predate the tag and are unconstrained."""
    return point.get("fit_kind", "unconstrained") == "unconstrained"


def add_point(state: CardState, *, scan_id, sample, T, q, omega0, modality, verdict,
              gamma=None, gamma_err=None, intensity=None, fit_kind=None,
              branch_id=None, weight=None, branch_posterior=None, assignment_entropy=None,
              t_round: float = 5.0,
              require_fold: bool = True) -> bool:
    """Append/replace a prior point, gated to resolved + fold. Returns True if a point was stored.

    ``gamma``/``gamma_err`` and ``intensity`` (a portable :class:`~isar.schema.steering.IntensityMeasure`
    dict, steering contract 2) are stored when supplied so a branch model (Phase 2+) can carry Γ(q) and
    A(q) alongside ω₀(q). ``branch_id`` + ``weight`` are the Phase-A soft branch assignment (which branch
    this point supports, and how strongly) — the assignment posterior consumes them instead of the old
    hard clean-anchor gate. All are optional and omitted when None, so older callers/points are unchanged.

    ``require_fold=False`` relaxes only the fold-modality requirement — for points whose hygiene is
    carried by the Phase-A triage instead (an acoustic delivery that passed the assignment
    posterior's P/dominance/entropy gates, or a rival-branch banking anchor that marks a branch's
    existence rather than a portable primary ω₀ — the scans that actually see the rival are the
    wide unpooled surveys; a tight folded production window excludes it by construction). A
    ``fork`` (bimodal across-model ω₀) NEVER enters — a fork averaged into the prior poisons every
    neighbour — and the resolved-verdict gate always holds.
    """
    if (verdict != "resolved" or omega0 is None or modality == "fork"
            or (require_fold and modality != "fold")):
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
    if branch_id is not None:
        point["branch_id"] = str(branch_id)
    if weight is not None:
        point["weight"] = float(weight)
    if branch_posterior is not None:
        point["branch_posterior"] = {
            str(key): float(value) for key, value in branch_posterior.items()}
    if assignment_entropy is not None:
        point["assignment_entropy"] = float(assignment_entropy)
    # Replace an existing point for the same scan (a re-fit supersedes).
    state.dispersion_prior = [p for p in state.dispersion_prior if p.get("scan_id") != scan_id]
    state.dispersion_prior.append(point)
    return True


def prior_for(state: CardState, *, sample, T, q, exclude_scan_id=None, period: float | None = None,
              same_t_tol: float = SAME_T_TOL, sigma_floor: float = SIGMA_FLOOR,
              t_round: float = 5.0, branch_id: str = "acoustic") -> PhononPrior | None:
    """A `PhononPrior` from the accumulated points near (sample, T, q), or None if too sparse.

    When ``period`` is given (the card's reduced-zone fold period, Phase A) the prediction is the
    **acoustic** :class:`~isar.session.branch_assignment.BranchBelief` at this q — a weighted local
    dispersion anchored at the Γ-point zero and fed only by acoustic-tagged points, so an off-branch
    (optic) anchor cannot bias it. Without ``period`` it falls back to the pre-steering global polyfit
    (unchanged for older callers/samples). amplitude stays None (detection power abstains, v1).
    """
    if period is not None:
        from isar.session import branch_assignment as BA
        pts = [p for p in state.dispersion_prior if p.get("scan_id") != exclude_scan_id]
        t_bin = round(float(T) / t_round) * t_round if t_round else float(T)
        beliefs = BA.build_beliefs(pts, sample=sample, t_bin=t_bin, period=float(period),
                                   t_round=t_round)
        belief = beliefs.get(branch_id)
        if belief is None:
            return None
        mu, sig = belief.predict(BA.reduced_q(q, float(period)))
        return PhononPrior(omega0=float(mu), amplitude=None,
                           omega0_sigma=max(float(sig), sigma_floor))

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
