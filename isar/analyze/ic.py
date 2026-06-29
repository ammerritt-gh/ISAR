"""Structure-aware initial conditions — the grid+NNLS backbone (§3) generalized to a
:class:`~isar.analyze.components.CompositeModel`.

For each Ω_d grid candidate (Γ fixed at ``gamma0``), the linear amplitudes of the active
components are solved by non-negative least squares and scored by the estimator; the
lowest-cost candidate seeds the fit. For the elastic+stokes+bg composite this reproduces
the frozen ``fit.initial_guess`` exactly. A confident dispersion prior narrows the grid
(prior-first, §3); with none (step-1 default) the full model-free scan is used.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import nnls

from isar.synth import estimators

EPS_OMEGA_D = 1e-3
GAMMA0 = 0.3
STEP = 0.25
PRIOR_HALF_WIDTH = 1.5  # meV grid half-window when a dispersion prior is present


def omega_d_grid(x, x_max, prior_omega=None, *, eps=EPS_OMEGA_D, step=STEP,
                 half_width=PRIOR_HALF_WIDTH) -> np.ndarray:
    """Ω_d candidate grid. Without a prior: ``arange(max(eps, dx), x_max, step)`` (the
    frozen ``initial_guess`` grid). With a prior ω(Q): a narrow window around it."""
    x = np.asarray(x, dtype=float)
    dx = float(x[1] - x[0]) if x.size > 1 else step
    if prior_omega is None:
        return np.arange(max(eps, dx), x_max, step)
    lo = max(eps, float(prior_omega) - half_width)
    hi = min(float(x_max), float(prior_omega) + half_width)
    return np.arange(lo, hi + step / 2.0, step)


def structure_ic(composite, ctx, *, gamma0=GAMMA0, estimator="baker_cousins") -> np.ndarray:
    """Initial parameter vector (in ``composite.param_names()`` order) for this context.

    Scans Ω_d (Γ fixed at ``gamma0``); at each candidate solves the active components'
    linear amplitudes by NNLS and scores with the estimator; returns the lowest-cost seed.
    Fits the masked-in points only (``ctx.x_used``).
    """
    x = ctx.x_used
    counts = ctx.counts_used
    x_max = float(np.max(x))
    est = estimators.get_estimator(estimator)
    linear_set = set(composite.linear_names())

    best_cost, best = np.inf, None
    for od in omega_d_grid(x, x_max, ctx.prior_omega, eps=EPS_OMEGA_D):
        nl = {name: (od if name.endswith(".Omega_d") else gamma0)
              for name in composite.nonlinear_names()}
        phi, lin_names = composite.nnls_basis(x, nl, ctx)
        coef, _ = nnls(phi, counts)
        cost = est.cost(counts, phi @ coef)
        if cost < best_cost:
            best_cost, best = cost, (float(od), dict(zip(lin_names, coef)))

    od, lin = best
    out = []
    for name in composite.param_names():
        if name in linear_set:
            out.append(max(float(lin[name]), 0.0))
        elif name.endswith(".Omega_d"):
            out.append(od)
        else:  # a nonlinear non-Ω_d param (Γ) — fixed at gamma0 during IC
            out.append(gamma0)
    return np.array(out, dtype=float)
