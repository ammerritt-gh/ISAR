"""MINOS profile-likelihood errors (Tier 1, inline) + the derived-ω₀ profile (§5.3).

MINOS gives cheap asymmetric, boundary-aware intervals on the *fit* parameters (Ω_d, Γ).
The reported quantity ω₀=√(Ω_d²+Γ²) is derived, so its profile interval comes from an exact
1-D scan: fix ω₀, re-minimize over (Γ, A, E, BG) with Ω_d=√(ω₀²−Γ²), and find the Δcost=1
crossings (errordef=1 ⇒ 1σ). MINOS is the asymptotic limit the bootstrap converges to; where
they disagree the scan is out of the asymptotic regime (see :mod:`~isar.analyze.errors.agree`).
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import brentq
from scipy.optimize import minimize as _scipy_minimize

from isar.analyze import loop
from isar.synth import backends, estimators

_OD_FLOOR = 1e-6  # keep Ω_d = √(ω₀²−Γ²) strictly real/positive in the constrained profile


def _cost_closure(prob, ctx):
    """The objective the profile scans: data cost + soft-prior penalty (contract 1). Unconstrained
    fits scan the data cost exactly as before, so the ω₀ profile interval is the honest posterior
    (data∩prior) width when constrained and unchanged otherwise."""
    est = estimators.get_estimator("baker_cousins")
    x, counts = ctx.x_used, ctx.counts_used
    penalty = prob.penalty

    def costv(theta):
        c = est.cost(counts, prob.composite.evaluate(x, theta, ctx))
        return c + penalty(theta) if penalty is not None else c

    return costv


def minos_intervals(hyp, ctx, fit_result, *, backend="iminuit") -> dict:
    """Absolute MINOS intervals ``{param: (lo, hi, valid)}`` for Ω_d and Γ.

    Re-minimizes warm-started at the fit with MINOS on (cheap; one fit). Returns absolute
    bounds (value + asymmetric error), not relative offsets."""
    prob = loop.build_problem(hyp, ctx)
    names = prob.free_names
    theta_fit = np.array([fit_result.params[n] for n in names], dtype=float)
    be_names = [loop._sanitize(n) for n in names]
    br = loop._minimize_fixed(backend, _cost_closure(prob, ctx), theta_fit, prob.bounds,
                              be_names, prob.fixed_pos, prob.fixed_vals, minos=True)
    out: dict = {}
    if br.merrors:
        val_by_name = dict(zip(names, (float(v) for v in br.x)))
        for n, be in zip(names, be_names):
            if n.endswith(".Omega_d") or n.endswith(".Gamma"):
                me = br.merrors.get(be)
                if me is not None:
                    lo, hi, valid = me
                    v = val_by_name[n]
                    out[n] = (v + float(lo), v + float(hi), bool(valid))
    return out


def omega0_profile_interval(hyp, ctx, fit_result, *, delta: float = 1.0) -> tuple[float, float]:
    """Exact 1-D profile interval on ω₀ (Δcost = `delta`; 1σ at delta=1)."""
    prob = loop.build_problem(hyp, ctx)
    names = prob.free_names
    bounds = dict(zip(names, prob.bounds))
    od_name = next(n for n in names if n.endswith(".Omega_d"))
    g_name = next(n for n in names if n.endswith(".Gamma"))
    # Params frozen by a branch-conditional hard-fix are held at their values through the whole
    # profile (not re-minimised) — the ω₀ interval must reflect the *conditioned* model. When Γ
    # itself is frozen, ω₀↔Ω_d is 1:1 and the scan re-minimises only the remaining free amplitudes.
    fixed_map = {names[i]: float(v) for i, v in zip(prob.fixed_pos, prob.fixed_vals)}
    g_fixed = g_name in fixed_map
    reduced = [n for n in names if n != od_name and n not in fixed_map]   # free: (Γ), A, (E), BG
    costv = _cost_closure(prob, ctx)
    theta_fit = {n: float(fit_result.params[n]) for n in names}

    def _gamma_of(red):
        return fixed_map[g_name] if g_fixed else float(red[reduced.index(g_name)])

    def _full(omega0, red):
        g = _gamma_of(red)
        od = float(np.sqrt(max(omega0 ** 2 - g ** 2, _OD_FLOOR ** 2)))
        vals = {n: float(red[reduced.index(n)]) for n in reduced}
        vals.update(fixed_map)                            # frozen params at their branch values
        vals[od_name] = od
        return np.array([vals[n] for n in names], dtype=float)

    def profile_cost(omega0: float) -> float:
        rbounds, red0 = [], []
        for n in reduced:
            lo, hi = bounds[n]
            if n == g_name:
                hi = min(hi, omega0 - _OD_FLOOR)         # Γ < ω₀ so Ω_d stays real
            lo, hi = float(lo), float(hi)
            rbounds.append((lo, hi))
            red0.append(min(max(theta_fit[n], lo), hi))
        res = _scipy_minimize(lambda r: costv(_full(omega0, r)), np.array(red0),
                              method="L-BFGS-B", bounds=rbounds)
        return float(res.fun)

    best = float(fit_result.omega0)
    # Scan the objective vs the objective's minimum: the posterior cost when soft-constrained (so
    # the Δcost=1 crossings are the posterior interval), else the data cost (unchanged).
    base = fit_result.posterior_cost if fit_result.posterior_cost is not None else fit_result.cost
    target = float(base) + delta
    step0 = max(fit_result.omega0_err_hesse if np.isfinite(fit_result.omega0_err_hesse) else 0.1,
                0.02)

    def g(o):
        return profile_cost(o) - target

    def crossing(direction):
        prev, step = best, step0
        # The bracket [prev, nxt] must straddle the target for brentq. That relies on
        # g(best) ≤ 0 — the profile re-minimization reaching the reported cost at the best-fit ω₀.
        # A weaker reduced-space L-BFGS-B can land ABOVE a well-converged MIGRAD minimum, making
        # g(best) > 0; there is then no clean 1σ crossing on this side, so report nan (the
        # function's existing "profile unavailable" sentinel) rather than hand brentq a
        # same-sign bracket.
        if g(prev) > 0:
            return float("nan")
        for _ in range(80):
            nxt = prev + direction * step
            if nxt <= _OD_FLOOR:
                return float("nan")
            if g(nxt) > 0:
                a, b = (prev, nxt) if direction > 0 else (nxt, prev)
                return float(brentq(g, a, b))
            prev, step = nxt, step * 1.4
        return float("nan")

    return (crossing(-1), crossing(+1))
