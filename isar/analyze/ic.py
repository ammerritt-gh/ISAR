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

from isar.analyze import estimators
from isar.analyze.phonon import omega0_from

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

    if best is None:
        raise ValueError(
            f"structure_ic: empty Ω_d candidate grid (x_used spans {float(np.min(x)):g}.."
            f"{x_max:g} meV) — the fit window has no Stokes-side (positive deltaE) coverage")
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


def _peak_omega0(peak):
    """ω₀ hint out of a peak declaration (attribute ``omega0`` or mapping key)."""
    if peak is None:
        return None
    if hasattr(peak, "omega0"):
        return float(peak.omega0)
    try:
        return float(peak["omega0"])
    except (TypeError, KeyError):
        return None


def greedy_multimode_ic(composite, ctx, one_mode_result, peaks=None, *, gamma0=GAMMA0,
                        estimator="baker_cousins") -> dict:
    """Greedy seed for a multi-mode composite: freeze mode-1 nonlinears at the 1-mode best fit
    (§ mode-1 fixed), then grid-scan mode-2's Ω_d over the residual, solving ALL linear
    amplitudes jointly by NNLS at each candidate and keeping the lowest Baker–Cousins cost.

    Mode-1 nonlinears come from ``one_mode_result`` (its fitted ``phonon.Omega_d/Gamma``); when
    it is ``None`` they are seeded internally by :func:`structure_ic` on a single-phonon+bg
    composite. ``peaks`` (declared expectations, ordered — ``peaks[0]`` is mode 1) narrows the
    mode-2 scan to ±``PRIOR_HALF_WIDTH`` meV around ``peaks[1]``'s ω₀ hint. Candidates within
    one resolution FWHM of mode-1 are skipped (identifiability). Returns a full seed dict keyed
    by ``composite.param_names()`` (Γ's at ``gamma0``)."""
    x = ctx.x_used
    counts = ctx.counts_used
    x_max = float(np.max(x))
    est = estimators.get_estimator(estimator)
    linear_set = set(composite.linear_names())

    od_names = [n for n in composite.nonlinear_names() if n.endswith(".Omega_d")]
    prefixes = [n.rsplit(".", 1)[0] for n in od_names]
    mode1, mode2 = prefixes[0], prefixes[1]

    # -- mode-1 nonlinears: from the 1-mode fit, or seeded internally when absent -----------
    if one_mode_result is not None and getattr(one_mode_result, "params", None):
        params = one_mode_result.params
        od1 = float(next(v for k, v in params.items() if k.endswith(".Omega_d")))
        g1 = float(next(v for k, v in params.items() if k.endswith(".Gamma")))
    else:
        from isar.analyze.components import CompositeModel, ConstantBackground, PhononDHO
        tmp = CompositeModel([PhononDHO(prefix=mode1), ConstantBackground()])
        seed = structure_ic(tmp, ctx, gamma0=gamma0, estimator=estimator)
        tnames = tmp.param_names()
        od1 = float(seed[tnames.index(f"{mode1}.Omega_d")])
        g1 = float(seed[tnames.index(f"{mode1}.Gamma")])
    w1 = omega0_from(od1, g1)

    # -- mode-2 grid: full model-free scan, narrowed around a declared peak hint ------------
    prior2 = _peak_omega0(peaks[1]) if peaks is not None and len(peaks) >= 2 else None
    grid = omega_d_grid(x, x_max, prior2, eps=EPS_OMEGA_D)
    res_fwhm = float(getattr(ctx, "gauss_fwhm", 0.0) or 0.0)

    def _scan(skip_identifiable):
        best_cost, best_od2, best_lin = np.inf, None, None
        for od2 in grid:
            if skip_identifiable and abs(omega0_from(od2, gamma0) - w1) < res_fwhm:
                continue
            nl = {}
            for name in composite.nonlinear_names():
                if name == f"{mode1}.Omega_d":
                    nl[name] = od1
                elif name == f"{mode2}.Omega_d":
                    nl[name] = float(od2)
                else:  # every Γ (mode-1 and mode-2) fixed at gamma0 during IC
                    nl[name] = gamma0
            nl[f"{mode1}.Gamma"] = g1
            phi, lin_names = composite.nnls_basis(x, nl, ctx)
            coef, _ = nnls(phi, counts)
            cost = est.cost(counts, phi @ coef)
            if cost < best_cost:
                best_cost, best_od2 = cost, float(od2)
                best_lin = dict(zip(lin_names, coef))
        return best_od2, best_lin

    best_od2, best_lin = _scan(skip_identifiable=True)
    if best_od2 is None:                    # whole grid sat under mode-1: relax the guard
        best_od2, best_lin = _scan(skip_identifiable=False)
    if best_od2 is None:                    # empty grid (degenerate) — nominal offset
        best_od2, best_lin = min(float(x_max), w1 + max(res_fwhm, STEP)), {}

    out = {}
    for name in composite.param_names():
        if name in linear_set:
            out[name] = max(float(best_lin.get(name, 0.0)), 0.0)
        elif name == f"{mode1}.Omega_d":
            out[name] = od1
        elif name == f"{mode1}.Gamma":
            out[name] = g1
        elif name == f"{mode2}.Omega_d":
            out[name] = best_od2
        else:  # mode-2 Γ (and any other nonlinear) at gamma0
            out[name] = gamma0
    return out
