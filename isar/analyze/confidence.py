"""Computed confidence (§9) — the number that replaces the old high/medium/low self-rating.

Built from quantities steps 2–3 already produce, each mapped to a [0,1] sub-score and combined
by a geometric mean so any single weak factor pulls the trust down (and the sub-scores are
returned for audit, never a black box):

- ``s_structure``  — ω agreement across surviving structures (fold → high, fork → low).
- ``s_interval``   — bootstrap ω₀ interval tightness vs a resolution-scaled reference.
- ``s_paradigm``   — the MINOS↔bootstrap regime (asymptotic → 1, sparse → low).
- ``s_prior``      — distance from the running dispersion prior; **neutral when no prior** (per
                     the §0.4 guard, absence of a prior is not evidence the fit is wrong).
- ``s_provenance`` — P6: the energy-calibration status (provisional inputs → lower confidence).

The spec names the inputs but not the combination rule; the geometric mean + documented
sub-score maps are this layer's choice. Sparse-prior / early-run / resolution-version P6 factors
are future additions to ``s_provenance``.
"""
from __future__ import annotations

import numpy as np

SIGMA_REF = 0.1  # meV — interval-tightness reference (a typical target ω₀ σ)
_REGIME_S = {"asymptotic": 1.0, "unknown": 0.6, "sparse": 0.3}
_PROVENANCE_S = {"absolute": 1.0, "needs_zero": 0.85, "needs_conversion_and_zero": 0.5}


def _s_structure(selection) -> float:
    modality = getattr(selection, "modality", None)
    if modality == "fold":
        return 1.0
    if modality == "fork":
        return 0.2
    return 0.6


def _s_interval(result, *, sigma_ref: float = SIGMA_REF) -> float:
    wm = result.within_model or {}
    fi = wm.get("freq_interval")
    hw = None
    if fi is not None and np.isfinite(fi[0]) and np.isfinite(fi[2]):
        hw = (float(fi[2]) - float(fi[0])) / 2.0
    elif np.isfinite(result.omega0_err_hesse):
        hw = float(result.omega0_err_hesse)
    if hw is None or hw < 0.0:
        return 0.5
    return float(sigma_ref / (sigma_ref + hw))


def _s_paradigm(result) -> float:
    ag = (result.within_model or {}).get("agreement")
    if ag is None:
        return 0.6
    return _REGIME_S.get(getattr(ag, "regime", None), 0.6)


def _s_prior(result, prior) -> float:
    if prior is None:
        return 1.0                                       # neutral: absence is not 'wrong'
    sigma = prior.omega0_sigma if (prior.omega0_sigma and prior.omega0_sigma > 0) else 1.0
    z = (float(result.omega0) - float(prior.omega0)) / sigma
    return float(np.exp(-0.5 * z * z))


def _s_provenance(ctx) -> float:
    return _PROVENANCE_S.get(ctx.energy_status, 0.7)


def compute_confidence(result, ctx, selection, *, prior=None) -> tuple[float, dict]:
    """Return ``(confidence in [0,1], components)`` — the geometric mean of the exposed
    sub-scores. See the module docstring for each factor."""
    comp = {
        "s_structure": _s_structure(selection),
        "s_interval": _s_interval(result),
        "s_paradigm": _s_paradigm(result),
        "s_prior": _s_prior(result, prior),
        "s_provenance": _s_provenance(ctx),
    }
    vals = np.clip(np.array(list(comp.values()), dtype=float), 1e-6, 1.0)
    score = float(np.exp(np.mean(np.log(vals))))         # geometric mean
    return score, comp
