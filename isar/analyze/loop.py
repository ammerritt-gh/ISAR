"""The structure-hypothesis loop: build a per-hypothesis problem, fit it, classify the fit.

``build_problem`` is the per-hypothesis analogue of ``synth.fit._build_problem`` but driven
by a :class:`~isar.analyze.components.CompositeModel`, so each hypothesis is a genuine model
variant with its own free-parameter count ``k`` and ``ndof`` (review F1) — what makes the
step-2 AIC/BIC comparison valid. ``StructureFitResult`` separates optimizer convergence from
error availability (review F2): a converged but covariance-less fit stays *usable* and is
carried by the step-3 bootstrap rather than discarded. The reported ω₀ error here is
HESSE-propagated and **provisional** (review F4); the authoritative interval is the bootstrap.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

from isar.analyze.ic import EPS_OMEGA_D, structure_ic
from isar.analyze.structures import enumerate_structures
from isar.synth import backends, estimators
from isar.synth.fit import assess_success

GAMMA_FLOOR = 1e-3
GAMMA_MAX = 5.0


def _sanitize(name: str) -> str:
    """Backend parameter names must be plain identifiers (iminuit); drop the dotted prefix."""
    return name.replace(".", "_")


def _bound(name: str, x_max: float, cmax: float) -> tuple[float, float]:
    """Per-parameter bounds, mirroring ``synth.fit._build_problem`` exactly."""
    if name.endswith(".Omega_d"):
        return (EPS_OMEGA_D, x_max)
    if name.endswith(".Gamma"):
        return (GAMMA_FLOOR, GAMMA_MAX)
    if name.endswith(".A") or name.endswith(".E"):
        return (0.0, 1e4 * cmax)
    if name.endswith(".BG"):
        return (0.0, 5.0 * cmax + 10.0)
    return (-np.inf, np.inf)


@dataclass
class Problem:
    """A fittable problem for one hypothesis: cost, IC, bounds, free names, sizes."""

    composite: object
    costv: Callable[[np.ndarray], float]
    x0: np.ndarray
    bounds: list[tuple[float, float]]
    free_names: list[str]
    k: int
    n_used: int

    @property
    def ndof(self) -> int:
        return self.n_used - self.k


@dataclass
class StructureFitResult:
    """One hypothesis's fit. AIC/BIC-ready (``k``/``ndof``/``cost``); status flags separate
    optimizer convergence from error availability; ω₀ error is provisional (HESSE)."""

    label: str
    k: int
    ndof: int
    cost: float
    cost_per_dof: float
    # separated status flags (review F2)
    optimizer_converged: bool
    covariance_ok: bool
    profile_ok: bool
    well_constrained: bool   # strict Phase-1 assess_success (regression vs results_s30.csv)
    analysis_usable: bool
    # fitted parameters
    params: dict[str, float]
    param_errors: dict[str, float | None]
    gamma: float
    gamma_err: float
    # derived, reported quantity — ω₀ with PROVISIONAL HESSE error (review F4)
    omega0: float
    omega0_err_hesse: float
    message: str
    # forward-design slots for the error/selection layers (reviews F3) — unused at step 1
    within_model: dict | None = None
    across_model: dict | None = None


def build_problem(hyp, ctx) -> Problem:
    """Assemble the per-hypothesis :class:`Problem` (composite, IC, bounds, cost)."""
    composite = hyp.build()
    x = ctx.x_used
    counts = ctx.counts_used
    x_max = float(np.max(x))
    cmax = max(float(np.max(counts)), 1.0)

    x0 = structure_ic(composite, ctx)
    names = composite.param_names()
    bounds = [_bound(n, x_max, cmax) for n in names]
    est = estimators.get_estimator("baker_cousins")

    def costv(theta):
        return est.cost(counts, composite.evaluate(x, theta, ctx))

    return Problem(composite=composite, costv=costv, x0=x0, bounds=bounds,
                   free_names=names, k=len(names), n_used=int(x.size))


def result_from_backend(label, composite, costv, n_used, k, br) -> StructureFitResult:
    """Build a :class:`StructureFitResult` from a backend result, reading the *raw,
    separated* convergence/covariance/profile signals (not the lossy combined success)."""
    theta = np.asarray(br.x, dtype=float)
    cov = br.covariance if br.covariance_ok else None
    errs = np.sqrt(np.diag(cov)) if cov is not None else None
    names = composite.param_names()

    omega0, omega0_err = composite.omega0(theta, cov)
    cost = float(costv(theta))
    ndof = n_used - k
    cost_per_dof = cost / ndof if ndof > 0 else float("nan")

    params = {n: float(v) for n, v in zip(names, theta)}
    if errs is not None:
        param_errors = {n: float(e) for n, e in zip(names, errs)}
    else:
        param_errors = {n: None for n in names}

    gname = next(n for n in names if n.endswith(".Gamma"))
    gamma = params[gname]
    gamma_err = param_errors[gname]
    gamma_err = float(gamma_err) if gamma_err is not None else float("nan")

    optimizer_converged = bool(br.success)
    covariance_ok = bool(br.covariance_ok)
    merrors = br.merrors or {}
    profile_ok = bool(merrors) and all(v[2] for v in merrors.values())

    # strict Phase-1 rule, adapting the Ω_d at-limit key (Ω_d is the first free param)
    od_key = _sanitize(names[0])
    at_limit_adpt = {"omega_d": bool((br.at_limit or {}).get(od_key, False))}
    well_constrained = bool(assess_success(theta, errs, at_limit_adpt, br.success,
                                           br.covariance_ok, EPS_OMEGA_D))
    analysis_usable = optimizer_converged

    return StructureFitResult(
        label=label, k=k, ndof=ndof, cost=cost, cost_per_dof=cost_per_dof,
        optimizer_converged=optimizer_converged, covariance_ok=covariance_ok,
        profile_ok=profile_ok, well_constrained=well_constrained,
        analysis_usable=analysis_usable, params=params, param_errors=param_errors,
        gamma=gamma, gamma_err=gamma_err, omega0=omega0, omega0_err_hesse=omega0_err,
        message=br.message,
    )


def fit_hypothesis(hyp, ctx, *, backend="iminuit", minos=False) -> StructureFitResult:
    """Fit one structure hypothesis and classify the result."""
    prob = build_problem(hyp, ctx)
    be_names = [_sanitize(n) for n in prob.free_names]
    br = backends.minimize(backend, prob.costv, prob.x0, prob.bounds, be_names, minos=minos)
    return result_from_backend(hyp.label, prob.composite, prob.costv, prob.n_used, prob.k, br)


def fit_admissible_structures(ctx, *, backend="iminuit", minos=False, errors=False,
                              M=200, seed=12345) -> list[StructureFitResult]:
    """Enumerate the admissible structures and fit each (the §2→§3 loop). Step-2 model
    selection will consume this list; step 1 just returns it.

    With ``errors=True`` the frequentist error layer (bootstrap, §5.2) is attached to each
    converged result's ``within_model``. Off by default so step 1's fast path is unchanged."""
    hyps = enumerate_structures(ctx)
    results = [fit_hypothesis(h, ctx, backend=backend, minos=minos) for h in hyps]
    if errors:
        from isar.analyze import errors as _errors  # lazy: errors imports loop
        for r, h in zip(results, hyps):
            _errors.attach(r, h, ctx, M=M, seed=seed, backend=backend)
    return results
