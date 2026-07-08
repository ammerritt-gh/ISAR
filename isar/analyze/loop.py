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

from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from isar.analyze.ic import EPS_OMEGA_D, greedy_multimode_ic, structure_ic
from isar.analyze.structures import enumerate_multimode, enumerate_structures
from isar.synth import backends, estimators
from isar.synth.backends import BackendResult
from isar.synth.fit import assess_success, omega0_value_error

GAMMA_FLOOR = 1e-3
GAMMA_MAX = 5.0

# A phonon Γ this close to the floor is a resolution-limited δ-spike, not a fitted intrinsic
# width: ω₀ = √(Ω_d²+Γ²) ≈ Ω_d with Γ unmovable, so the parametric bootstrap collapses to a
# degenerate ω₀ interval (halfwidth ~1e-3 meV) that is spuriously tight, not trustworthy. Fits
# with any Γ within this tolerance of GAMMA_FLOOR (or at GAMMA_MAX, a runaway-broad DHO) get
# their ω₀ interval flagged unreliable so the σ-calibration / downstream can filter them out
# (an honestly-flagged interval beats a silently repaired one — ISAR philosophy). 5×floor
# separates resolution-limited spikes (≤0.005 meV) from genuinely-fit widths (≳0.05 meV) with
# wide margin, and is a flag, never a value edit.
GAMMA_BOUND_TOL = 5.0 * GAMMA_FLOOR


def _gamma_at_bound(gammas) -> bool:
    """True if any phonon Γ has railed out — pinned near GAMMA_FLOOR (resolution-limited spike)
    or GAMMA_MAX (runaway-broad) — so its derived ω₀ interval is not trustworthy."""
    for g in gammas:
        g = float(g)
        if g <= GAMMA_FLOOR + GAMMA_BOUND_TOL or g >= GAMMA_MAX * (1.0 - 1e-3):
            return True
    return False

# cost/dof of the best converged 1-mode fit above which the two-stage loop offers the 2-mode
# hypothesis set. A documented default (Baker–Cousins -2lnL/dof; a good fit sits near 1),
# deliberately loose — the runs-test structured-residual clause catches the wide-window trap
# where cost/dof looks acceptable — and NOT tuned to any single toy spectrum.
GATE_COST = 2.0


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
    """A fittable problem for one hypothesis: cost, IC, bounds, free names, sizes.

    ``costv`` is the **data-only** Baker–Cousins cost — the scalar AIC/BIC/GOF and the verdict read
    (fit-vs-decide contract 1). ``penalty`` (a soft-prior term from ``ctx.constraints``, or None) is
    added to the ``objective`` the optimiser minimises, and only there, so the reported cost never
    sees the prior.
    """

    composite: object
    costv: Callable[[np.ndarray], float]
    x0: np.ndarray
    bounds: list[tuple[float, float]]
    free_names: list[str]
    k: int
    n_used: int
    penalty: Callable[[np.ndarray], float] | None = None
    # Hard-fixed params (contract 4, Phase 3b): positions into the FULL param vector and their
    # frozen values. Everything (costv/penalty/objective/composite) stays full-vector; fixing is
    # applied only inside :func:`_minimize_fixed` (which minimises the free subspace) and shrinks
    # ``k``. Empty ⇒ no param frozen ⇒ every path is bit-identical to an ordinary fit.
    fixed_pos: list[int] = field(default_factory=list)
    fixed_vals: list[float] = field(default_factory=list)

    @property
    def ndof(self) -> int:
        return self.n_used - self.k

    @property
    def is_hard_fixed(self) -> bool:
        return bool(self.fixed_pos)

    @property
    def objective(self) -> Callable[[np.ndarray], float]:
        """What the optimiser minimises: data cost + soft-prior penalty (contract 1). Identical to
        ``costv`` when unconstrained, so an unconstrained fit is bit-for-bit unchanged."""
        if self.penalty is None:
            return self.costv
        cost, pen = self.costv, self.penalty
        return lambda theta: cost(theta) + pen(theta)


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
    # N-mode: per-phonon detail (canonicalized ascending by ω₀). Each dict is
    # {omega0, omega0_err, gamma, gamma_err, amplitude}; the scalar omega0/gamma above are the
    # PRIMARY mode's (back-compat). ``n_modes`` == len(modes) (1 for a single-phonon fit).
    modes: list | None = None
    n_modes: int = 1
    # True when any phonon Γ railed out to its bound → the derived ω₀ interval is degenerate and
    # must be flagged unreliable (not silently trusted). See :func:`_gamma_at_bound`.
    gamma_at_bound: bool = False
    # Steering (fit-vs-decide contract 1 + provenance tag, Phase 2+). ``cost`` above is always the
    # DATA-only Baker–Cousins cost (what AIC / cost_per_dof / falsification read). When a soft prior
    # constrains the fit, ``prior_penalty`` is its penalty at θ̂ and ``posterior_cost = cost +
    # prior_penalty`` is what the optimiser minimised. ``fit_kind`` tags provenance so a constrained /
    # one-sided / demoted fit is never mistaken for an ordinary resolved phonon fit (contract 4).
    prior_penalty: float | None = None      # None ⇒ unconstrained (posterior_cost == cost)
    posterior_cost: float | None = None
    fit_kind: str = "unconstrained"         # unconstrained|soft_prior|branch_conditional|stokes_only|descriptor_only


def build_problem(hyp, ctx, *, x0=None) -> Problem:
    """Assemble the per-hypothesis :class:`Problem` (composite, IC, bounds, cost).

    ``x0`` overrides the default grid+NNLS initial condition: pass a dict keyed by
    ``composite.param_names()`` (e.g. from :func:`~isar.analyze.ic.greedy_multimode_ic`) or a
    ready array. Defaults to :func:`~isar.analyze.ic.structure_ic` (unchanged 1-mode path)."""
    composite = hyp.build()
    x = ctx.x_used
    counts = ctx.counts_used
    x_max = float(np.max(x))
    cmax = max(float(np.max(counts)), 1.0)

    names = composite.param_names()
    if x0 is None:
        x0 = structure_ic(composite, ctx)
    elif isinstance(x0, dict):
        x0 = np.array([x0[n] for n in names], dtype=float)
    else:
        x0 = np.asarray(x0, dtype=float)
    bounds = [_bound(n, x_max, cmax) for n in names]
    est = estimators.get_estimator("baker_cousins")

    def costv(theta):
        return est.cost(counts, composite.evaluate(x, theta, ctx))

    fixed_pos, fixed_vals = _hard_fixed(getattr(ctx, "constraints", None), names)
    fixed_names = {names[i] for i in fixed_pos}
    penalty = _soft_penalty(getattr(ctx, "constraints", None), names, skip=fixed_names)
    return Problem(composite=composite, costv=costv, x0=x0, bounds=bounds,
                   free_names=names, k=len(names) - len(fixed_pos), n_used=int(x.size),
                   penalty=penalty, fixed_pos=fixed_pos, fixed_vals=fixed_vals)


def _soft_penalty(constraints, names, *, skip=()):
    """A Gaussian-prior penalty ``Σ((θ_i−μ)/σ)²`` over the soft-constrained params present in
    ``names`` (contract 1's soft path), or None. With ``errordef=1`` this contributes Δcost=1 at 1σ,
    so HESSE/MINOS on the objective return the honest data∩prior posterior width. Added to the
    objective only — never the data cost. ``skip`` names (the hard-fixed params) are excluded — a
    param cannot be both frozen and softly penalised."""
    if constraints is None or not getattr(constraints, "soft", None):
        return None
    terms = []
    for name, ms in constraints.soft.items():
        if name in names and name not in skip:
            mu, sig = float(ms[0]), float(ms[1])
            if sig > 0:
                terms.append((names.index(name), mu, sig))
    if not terms:
        return None

    def penalty(theta):
        return float(sum(((float(theta[i]) - mu) / sig) ** 2 for i, mu, sig in terms))

    return penalty


def _hard_fixed(constraints, names):
    """Positions into the full ``names`` vector, and the frozen values, for every hard constraint
    (``ParamConstraints.hard``) whose target is a param of this composite and finite. Returns two
    parallel lists ``(positions, values)`` — empty when nothing is hard-fixed. See
    :func:`_minimize_fixed` for how they are applied (the composite stays full-vector)."""
    hard = getattr(constraints, "hard", None) if constraints is not None else None
    if not hard:
        return [], []
    pos, vals = [], []
    for name, v in hard.items():
        if name in names:
            fv = float(v)
            if np.isfinite(fv):
                pos.append(names.index(name))
                vals.append(fv)
    return pos, vals


def _minimize_fixed(backend, objective, x0, bounds, be_names, fixed_pos, fixed_vals,
                    *, minos=False, ncall=None) -> BackendResult:
    """Minimise a **full-vector** ``objective`` with the coordinates in ``fixed_pos`` frozen to
    ``fixed_vals``, returning a :class:`BackendResult` in **full space** (frozen coords embedded,
    covariance expanded with zero rows/cols — a fixed param has zero variance and zero covariance,
    which is exactly right). When ``fixed_pos`` is empty this delegates unchanged, so an ordinary
    fit is bit-for-bit identical.

    Freezing at this boundary keeps every consumer (``costv``/``penalty``/``result_from_backend``'s
    positional per-phonon indexing) full-vector and untouched; the optimiser only ever sees the
    reduced free subspace, so ``k``/HESSE are natively reduced (contract 4)."""
    x0 = np.asarray(x0, dtype=float)
    if not fixed_pos:
        return backends.minimize(backend, objective, x0, bounds, be_names, minos=minos, ncall=ncall)
    n = x0.size
    fixed_set = set(fixed_pos)
    free_pos = [i for i in range(n) if i not in fixed_set]
    template = x0.copy()
    template[list(fixed_pos)] = np.asarray(fixed_vals, dtype=float)

    def embed(theta_free):
        full = template.copy()
        full[free_pos] = np.asarray(theta_free, dtype=float)
        return full

    br = backends.minimize(backend, lambda tf: objective(embed(tf)),
                           x0[free_pos], [bounds[i] for i in free_pos],
                           [be_names[i] for i in free_pos], minos=minos, ncall=ncall)
    x_full = embed(br.x)
    cov_full = None
    if br.covariance is not None:
        sub = np.asarray(br.covariance, dtype=float)
        cov_full = np.zeros((n, n))
        cov_full[np.ix_(free_pos, free_pos)] = sub
    return BackendResult(x_full, br.success, br.message, br.n_eval, cov_full,
                         br.covariance_ok, dict(br.at_limit), br.merrors)


def result_from_backend(label, composite, costv, n_used, k, br,
                        *, prior_omega=None, acoustic_omega=None, penalty=None,
                        hard_fixed=False) -> StructureFitResult:
    """Build a :class:`StructureFitResult` from a backend result, reading the *raw,
    separated* convergence/covariance/profile signals (not the lossy combined success).

    ``costv`` is the **data-only** cost, so ``cost``/``cost_per_dof`` (what AIC and the verdict read)
    never include the prior (contract 1). When a soft ``penalty`` constrained the fit, its value at
    θ̂ is recorded in ``prior_penalty`` and ``posterior_cost = cost + prior_penalty`` (what the
    optimiser minimised); ``fit_kind`` is tagged ``soft_prior``.

    Per-phonon detail is collected into ``modes`` (canonicalized ascending by ω₀); the reported
    scalar ω₀/Γ are the PRIMARY mode's (nearest ``prior_omega`` if given, else largest
    amplitude). Single-phonon fits are unchanged (one mode, itself primary)."""
    theta = np.asarray(br.x, dtype=float)
    cov = br.covariance if br.covariance_ok else None
    errs = np.sqrt(np.diag(cov)) if cov is not None else None
    names = composite.param_names()

    cost = float(costv(theta))                            # DATA-only (contract 1)
    ndof = n_used - k
    cost_per_dof = cost / ndof if ndof > 0 else float("nan")
    prior_penalty = float(penalty(theta)) if penalty is not None else None
    posterior_cost = (cost + prior_penalty) if prior_penalty is not None else None
    # provenance precedence (contract 4): branch_conditional > stokes_only > soft_prior >
    # unconstrained. A hard-fixed (branch-conditional) fit dropped a param to its branch value; a
    # one-sided (Bose-dead) composite is stokes_only even under a soft prior; so a rescued fit
    # never reads as an ordinary two-sided resolved phonon.
    if hard_fixed:
        fit_kind = "branch_conditional"
    elif composite.is_stokes_only():
        fit_kind = "stokes_only"
    elif penalty is not None:
        fit_kind = "soft_prior"
    else:
        fit_kind = "unconstrained"

    params = {n: float(v) for n, v in zip(names, theta)}
    if errs is not None:
        param_errors = {n: float(e) for n, e in zip(names, errs)}
    else:
        param_errors = {n: None for n in names}

    # per-mode collection, canonicalized ascending by ω₀ (kills label switching)
    modes = []
    for i in composite.phonon_indices():
        w0, w0e = composite._omega0_at(theta, cov, i)
        g_err = float(errs[i + 1]) if errs is not None else float("nan")
        modes.append({"omega0": w0, "omega0_err": w0e, "gamma": float(theta[i + 1]),
                      "gamma_err": g_err, "amplitude": float(theta[i + 2])})
    modes.sort(key=lambda m: m["omega0"])
    n_modes = len(modes)

    # primary mode → the reported scalar ω₀ / Γ (back-compat); the acoustic hint (Phase A) selects
    # which mode of a multi-mode fit is the acoustic QoI, ahead of the prior/amplitude fallbacks.
    prim_i = composite.primary_phonon_index(theta, cov, prior_omega, acoustic_omega)
    omega0, omega0_err = composite._omega0_at(theta, cov, prim_i)
    gamma = float(theta[prim_i + 1])
    gamma_err = float(errs[prim_i + 1]) if errs is not None else float("nan")

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

    gamma_at_bound = _gamma_at_bound(m["gamma"] for m in modes)

    return StructureFitResult(
        label=label, k=k, ndof=ndof, cost=cost, cost_per_dof=cost_per_dof,
        optimizer_converged=optimizer_converged, covariance_ok=covariance_ok,
        profile_ok=profile_ok, well_constrained=well_constrained,
        analysis_usable=analysis_usable, params=params, param_errors=param_errors,
        gamma=gamma, gamma_err=gamma_err, omega0=omega0, omega0_err_hesse=omega0_err,
        message=br.message, modes=modes, n_modes=n_modes, gamma_at_bound=gamma_at_bound,
        prior_penalty=prior_penalty, posterior_cost=posterior_cost, fit_kind=fit_kind,
    )


def fit_hypothesis(hyp, ctx, *, backend="iminuit", minos=False, x0=None) -> StructureFitResult:
    """Fit one structure hypothesis and classify the result. ``x0`` overrides the default IC
    (see :func:`build_problem`) — the two-stage loop passes the greedy multimode seed."""
    prob = build_problem(hyp, ctx, x0=x0)
    be_names = [_sanitize(n) for n in prob.free_names]
    br = _minimize_fixed(backend, prob.objective, prob.x0, prob.bounds, be_names,
                         prob.fixed_pos, prob.fixed_vals, minos=minos)
    return result_from_backend(hyp.label, prob.composite, prob.costv, prob.n_used, prob.k, br,
                               prior_omega=getattr(ctx, "prior_omega", None),
                               acoustic_omega=getattr(ctx, "acoustic_omega", None),
                               penalty=prob.penalty, hard_fixed=prob.is_hard_fixed)


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


def _best_converged(results):
    """The min-AIC converged fit (AIC = cost + 2k), or ``None`` if nothing converged."""
    conv = [r for r in results if r.optimizer_converged]
    if not conv:
        return None
    return min(conv, key=lambda r: r.cost + 2.0 * r.k)


def _offer_multimode(results, ctx) -> bool:
    """Whether to escalate to the 2-mode hypothesis set. Fires when the best converged 1-mode
    fit has ``cost_per_dof > GATE_COST`` OR its residual is structured by the Wald–Wolfowitz
    runs test (:func:`isar.analyze.verdict._residual_structured` — the wide-window trap where
    one DHO spans two peaks and cost/dof still looks acceptable). ``False`` if nothing
    converged (there is no fit to diagnose)."""
    best = _best_converged(results)
    if best is None:
        return False
    if np.isfinite(best.cost_per_dof) and best.cost_per_dof > GATE_COST:
        return True
    from isar.analyze.verdict import _pull, _residual_structured  # lazy: verdict → structures
    pull = _pull(best, ctx)
    return bool(pull is not None and _residual_structured(pull))


def fit_structures_two_stage(ctx, *, backend="iminuit", minos=False, with_errors=False,
                             M=200, seed=12345, peaks=None) -> list[StructureFitResult]:
    """Two-stage, data-gated structure enumeration (§ multiplicity). Fit the 1-mode set; if the
    gate (:func:`_offer_multimode`) fires OR ``peaks`` declares ≥2 expected peaks, also fit the
    2-mode set (:func:`~isar.analyze.structures.enumerate_multimode`) seeded by
    :func:`~isar.analyze.ic.greedy_multimode_ic`, and return the UNION for a single
    :func:`~isar.analyze.select.select_models` pass — AIC/BIC then decide whether the second
    mode earns its parameters. 2-mode hypotheses with no positive dof (``n_used − k ≤ 0``) are
    skipped. ``peaks`` is threaded through untouched (the driver supplies it later); this
    function never imports session/guideline code.

    With ``with_errors`` the bootstrap error layer is attached to every returned fit (so the
    select pass can pool survivors from both stages)."""
    one_hyps = enumerate_structures(ctx)
    pairs = [(fit_hypothesis(h, ctx, backend=backend, minos=minos), h) for h in one_hyps]

    expected_two = peaks is not None and len(peaks) >= 2
    if _offer_multimode([r for r, _ in pairs], ctx) or expected_two:
        best_one = _best_converged([r for r, _ in pairs])
        n_used = int(ctx.x_used.size)
        for h in enumerate_multimode(ctx):
            if n_used - h.k <= 0:                     # ndof guard: no positive dof
                continue
            x0 = greedy_multimode_ic(h.build(), ctx, best_one, peaks=peaks)
            r = fit_hypothesis(h, ctx, backend=backend, minos=minos, x0=x0)
            pairs.append((r, h))

    results = [r for r, _ in pairs]
    if with_errors:
        from isar.analyze import errors as _errors  # lazy: errors imports loop
        for r, h in pairs:
            _errors.attach(r, h, ctx, M=M, seed=seed, backend=backend)
    return results
