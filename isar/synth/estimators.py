"""Pluggable cost functions for the fit layer: Neyman χ², (modified) Pearson χ²,
and the Baker–Cousins Poisson likelihood.

All three are minimized as scalar costs (y = counts, f = model expected counts),
with the same `errordef = 1` convention so 1σ is the Δcost = 1 contour. The
estimator is a first-class, swappable choice — both the validation gate (default
Baker–Cousins) and the §8 demonstration depend on running all three on identical data.

Note on Pearson: minimizing Σ(y−f)²/f directly (denominator varies with the
parameters) is the **modified / minimum-Pearson χ²** estimator. It is NOT the
iteratively-reweighted (curve_fit) Pearson. It biases the normalization **high**
(upward) — the constant-rate identity makes it the RMS, which lies above the
arithmetic mean (Baker–Cousins) while Neyman's harmonic mean lies below, so the two
χ²'s straddle the unbiased likelihood. That straddle is exactly what the harness is
built to measure, so the direct form is used and labelled honestly.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

# Clip the model so 1/f (Pearson) and log(f) (Baker–Cousins) stay finite.
FMIN = 1e-12


def _prep(y, f):
    y = np.asarray(y, dtype=float)
    f = np.clip(np.asarray(f, dtype=float), FMIN, None)
    return y, f


def neyman_cost(y, f) -> float:
    """Σ (y−f)² / max(y, 1). The y=0 floor matches the production Neyman convention."""
    y, f = _prep(y, f)
    w = np.maximum(y, 1.0)
    return float(np.sum((y - f) ** 2 / w))


def pearson_cost(y, f) -> float:
    """Σ (y−f)² / f  (modified/minimum-Pearson; denominator varies)."""
    y, f = _prep(y, f)
    return float(np.sum((y - f) ** 2 / f))


def baker_cousins_cost(y, f) -> float:
    """−2 ln L = 2 Σ [f − y + y ln(y/f)], with the y=0 term → f (exact, no floor)."""
    y, f = _prep(y, f)
    term = f.copy()  # the y=0 contribution is exactly f
    nz = y > 0
    # Compute y·ln(y/f) only where y>0 to avoid 0·log(0) → NaN warnings.
    term[nz] = f[nz] - y[nz] + y[nz] * np.log(y[nz] / f[nz])
    return float(2.0 * np.sum(term))


@dataclass(frozen=True)
class Estimator:
    name: str
    cost: Callable[[np.ndarray, np.ndarray], float]


ESTIMATORS: dict[str, Estimator] = {
    "neyman": Estimator("neyman", neyman_cost),
    "pearson": Estimator("pearson", pearson_cost),
    "baker_cousins": Estimator("baker_cousins", baker_cousins_cost),
}


def get_estimator(name: str) -> Estimator:
    try:
        return ESTIMATORS[name]
    except KeyError:
        raise KeyError(f"unknown estimator {name!r}; choices: {sorted(ESTIMATORS)}") from None


def constant_rate_estimate(counts, estimator: str) -> float:
    """Closed-form minimizer of each cost for a CONSTANT model f = μ (the §8 Layer-1
    identity): Neyman → harmonic mean, Pearson → RMS, Baker–Cousins → arithmetic mean.

    Neyman is written as the general weighted mean μ = Σ(y/w)/Σ(1/w) with w = max(y,1),
    which reduces to the harmonic mean when all counts are positive and matches the
    numerical argmin of `neyman_cost` under the y=0 floor.
    """
    y = np.asarray(counts, dtype=float)
    n = y.size
    if estimator == "neyman":
        w = 1.0 / np.maximum(y, 1.0)
        return float(np.sum(w * y) / np.sum(w))
    if estimator == "pearson":
        return float(np.sqrt(np.sum(y ** 2) / n))
    if estimator == "baker_cousins":
        return float(np.mean(y))
    raise KeyError(f"unknown estimator {estimator!r}")
