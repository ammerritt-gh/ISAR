"""MINOS profile-likelihood errors (Tier 1, inline) + the exact ω₀ profile (§5.3).

MINOS gives cheap asymmetric, boundary-aware intervals on the *fit* parameters (E0 = ``.omega0``,
Γ). The profile interval of ω₀ comes from an exact 1-D scan of the ``.omega0`` parameter itself:
fix it, re-minimize over the other free parameters (Γ, the heights, BG) within their own bounds,
and find the Δcost=1 crossings (errordef=1 ⇒ 1σ). MINOS is the asymptotic limit the bootstrap converges to; where
they disagree the scan is out of the asymptotic regime (see :mod:`~isar.analyze.errors.agree`).
"""
from __future__ import annotations

import logging
import numpy as np
from scipy.optimize import brentq
from scipy.optimize import minimize as _scipy_minimize

from isar.analyze import loop
from isar.analyze import backends, estimators

log = logging.getLogger(__name__)


class BoundTruncated(float):
    """A profile side that walked to an ``.omega0`` bound before reaching Δcost=`delta`.

    A fit that reaches its bounds normally means something is wrong upstream, so the honest
    output is a refusal a human can see — never a half-interval delivered as if it were 1σ.
    It is a ``nan`` float subclass so every existing numeric consumer keeps behaving exactly as
    it did (``np.isfinite`` is False, arithmetic propagates nan), while the interval-semantics
    layer can tell a bound truncation from a profile that simply was not available.

    **The marker is an in-process signal with a required lifetime, not a durable value.**  It
    survives pickling (including inside a tuple, so the worker-pool hand-back is safe), deepcopy,
    and ``list``/``tuple`` conversion — but ``np.asarray``, ``float()``, arithmetic and any
    text round-trip erase the subclass and leave a plain ``nan`` that ``is_bound_truncated``
    cannot recognise.  That is sound only because the one consumer, ``_interval_semantics_for``,
    runs inside ``attach``/``attach_batch`` in the same process that produced the marker, and
    converts it into the durable ``failure_reason`` string before anything is written.  A change
    that moves interval-semantics construction after a serialisation boundary — notably the
    ``results.csv`` round-trip, which reads ``omega0_profile_lo``/``_hi`` back as plain floats —
    would silently downgrade every bound truncation to ``profile_interval_unavailable`` with no
    test failing.  ``test_the_bound_marker_survives_exactly_the_transports_it_must`` pins this.
    """

    __slots__ = ()

    def __new__(cls, _value=None):
        # ``_value`` exists only so pickle's float-subclass reconstruction (which replays the
        # instance's own nan) round-trips: these sides are computed in a worker process.
        return super().__new__(cls, float("nan"))


def is_bound_truncated(value) -> bool:
    """True when ``value`` is the explicit bound-truncated refusal (not a plain nan)."""
    return isinstance(value, BoundTruncated)


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
    """Absolute MINOS intervals ``{param: (lo, hi, valid)}`` for ``.omega0`` and Γ.

    Re-minimizes warm-started at the fit with MINOS on (cheap; one fit). Returns absolute
    bounds (value + asymmetric error), not relative offsets."""
    prob = loop.build_problem(hyp, ctx)
    names = prob.free_names
    theta_fit = np.array([fit_result.params[n] for n in names], dtype=float)
    be_names = [loop._sanitize(n) for n in names]
    br = loop._minimize_fixed(backend, _cost_closure(prob, ctx), theta_fit, prob.bounds,
                              be_names, prob.fixed_pos, prob.fixed_vals, minos=True, zmap=prob.zmap)
    out: dict = {}
    if br.merrors:
        val_by_name = dict(zip(names, (float(v) for v in br.x)))
        for n, be in zip(names, be_names):
            if n.endswith(".omega0") or n.endswith(".Gamma"):
                me = br.merrors.get(be)
                if me is not None:
                    lo, hi, valid = me
                    v = val_by_name[n]
                    out[n] = (v + float(lo), v + float(hi), bool(valid))
    return out


def _omega0_profile_crossing(hyp, ctx, fit_result, direction, delta: float = 1.0) -> float:
    """One independent side of the exact ω₀ profile (picklable worker body)."""
    prob = loop.build_problem(hyp, ctx)
    names = prob.free_names
    zm = prob.zmap          # the widths are minimised in the z coordinate: the box [0, 1] follows omega0
    bounds = dict(zip(names, prob.bounds if zm is None else zm.box(prob.bounds)))
    w_name = next(n for n in names if n.endswith(".omega0"))
    # Params frozen by a branch-conditional hard-fix are held at their values through the whole
    # profile (not re-minimised) — the ω₀ interval must reflect the *conditioned* model.
    fixed_map = {names[i]: float(v) for i, v in zip(prob.fixed_pos, prob.fixed_vals)}
    # The scan stays inside the fitted model's own .omega0 bounds: a side that reaches one before
    # delta-cost=1 is a refusal, never a crossing manufactured outside the fitted parameter space.
    omega0_edge, omega0_ceiling = (float(v) for v in bounds[w_name])
    reduced = [n for n in names if n != w_name and n not in fixed_map]   # free: (Γ), h, (QE), BG
    costv = _cost_closure(prob, ctx)
    theta_fit = {n: float(fit_result.params[n]) for n in names}
    if zm is not None:
        theta_fit = dict(zip(names, (float(v) for v in zm.to_z(np.array([theta_fit[n] for n in names])))))

    def _full(omega0, red):
        vals = {n: float(red[reduced.index(n)]) for n in reduced}
        vals.update(fixed_map)                            # frozen params at their branch values
        vals[w_name] = float(omega0)
        vec = np.array([vals[n] for n in names], dtype=float)
        return vec if zm is None else zm.to_theta(vec)

    def profile_cost(omega0: float) -> float:
        if omega0 < omega0_edge:
            return float("inf")
        if not reduced:                                   # nothing left to minimise: the cost at ω₀
            return float(costv(_full(omega0, np.empty(0))))
        rbounds, red0 = [], []
        for n in reduced:
            lo, hi = (float(v) for v in bounds[n])
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
            reached_edge = (nxt <= omega0_edge if direction < 0 else nxt >= omega0_ceiling)
            if reached_edge:
                nxt = omega0_edge if direction < 0 else omega0_ceiling
            if g(nxt) > 0:
                a, b = (prev, nxt) if direction > 0 else (nxt, prev)
                return float(brentq(g, a, b))
            if reached_edge:
                # The profile ran out of parameter space before Δcost=delta. That is a refusal,
                # not a number, and it must stay distinguishable from "no profile available".
                log.warning("omega0 profile truncated at the %s omega0 bound (edge=%.6g) "
                            "before reaching delta-cost=%.3g",
                            "lower" if direction < 0 else "upper",
                            omega0_edge if direction < 0 else omega0_ceiling, delta)
                return BoundTruncated()
            prev, step = nxt, step * 1.4
        return float("nan")

    return crossing(direction)


def _omega0_profile_side(args):
    """Picklable entry point for one profile side."""
    hyp, ctx, fit_result, direction, delta = args
    return _omega0_profile_crossing(hyp, ctx, fit_result, direction, delta)


def omega0_profile_interval(hyp, ctx, fit_result, *, delta: float = 1.0,
                            executor=None) -> tuple[float, float]:
    """Exact 1-D profile interval on ω₀ (Δcost = `delta`; 1σ at delta=1).

    When supplied, ``executor`` evaluates the independent lower and upper crossings on the
    caller-owned pool and restores their canonical ``(lower, upper)`` order. A failed process
    batch is logged and recomputed serially from the same pure inputs.
    """
    tasks = [(hyp, ctx, fit_result, -1, delta), (hyp, ctx, fit_result, +1, delta)]
    if executor is not None:
        try:
            return tuple(executor.map(_omega0_profile_side, tasks))
        except Exception as exc:  # noqa: BLE001
            log.warning("parallel omega0 profile failed; retrying serially: %s: %s",
                        type(exc).__name__, exc)
    return tuple(_omega0_profile_side(task) for task in tasks)
