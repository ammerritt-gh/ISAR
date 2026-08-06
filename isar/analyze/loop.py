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
import logging
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

from isar.analyze.ic import EPS_OMEGA_D, greedy_multimode_ic, structure_ic
from isar.analyze.structures import (
    Hypothesis, elastic_admissible, enumerate_multimode, enumerate_structures,
)
from isar.analyze import backends, estimators
from isar.analyze.backends import BackendResult
from isar.analyze.phonon import assess_success, omega0_value_error

GAMMA_FLOOR = 1e-3
GAMMA_MAX = 5.0

# STAT-1.5 metadata revisions.  These are descriptive fingerprints only: changing either label
# changes semantic comparability identity, never a fit/cache/scientific identity.
DATA_LIKELIHOOD_REVISION = "isar.baker_cousins_data_likelihood/1"
MODEL_PARAMETERIZATION_REVISION = "isar.fit_model_parameterization/1"

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
    # Full fitted-parameter covariance in ``params`` insertion order.  Retained for posterior-
    # predictive acquisition; a missing/untrusted covariance is represented by ``None``.
    parameter_covariance: list | None = None
    # Additive statistical provenance.  A construction failure leaves this ``None`` while the
    # numerical fit remains usable.
    fit_semantics: dict | None = None


@dataclass
class ShadowCandidateBundle:
    """Point-fit-only evidence for the diagnostics-only STAT-2a comparison.

    ``results`` deliberately remains the caller-owned list when the existing full, unconstrained
    route is reused.  The shadow lane can therefore prove object/list identity without copying or
    re-fitting the common path.
    """

    results: list[StructureFitResult]
    offered_labels: list[str]
    attempted_labels: list[str]
    converged_labels: list[str]
    failed_labels: list[str]
    reused_labels: list[str]
    freshly_fitted_labels: list[str]
    multimode_route: str


def _fingerprint_number(value):
    """Finite JSON representation that preserves non-finite scientific inputs explicitly."""
    if value is None:
        return None
    number = float(value)
    if np.isnan(number):
        return "nan"
    if np.isposinf(number):
        return "positive_infinity"
    if np.isneginf(number):
        return "negative_infinity"
    return number


def _fingerprint_vector(values) -> list:
    return [_fingerprint_number(value) for value in np.asarray(values).reshape(-1)]


def _data_likelihood_payload(ctx) -> dict:
    """Everything that fixes the observed-data likelihood shared by candidate models."""
    missing = [
        name for name in ("x", "counts", "mask")
        if not hasattr(ctx, name)
    ]
    if missing:
        from isar.schema.statistics import SemanticsConstructionError
        raise SemanticsConstructionError(
            "missing_data_likelihood_provenance", ",".join(missing)
        )
    energy = np.asarray(ctx.x)
    counts = np.asarray(ctx.counts)
    mask = np.asarray(ctx.mask, dtype=bool)
    if energy.shape != counts.shape or energy.shape != mask.shape:
        from isar.schema.statistics import SemanticsConstructionError
        raise SemanticsConstructionError("misaligned_data_likelihood_inputs")
    exposure = getattr(ctx, "exposure_scale", None)
    if exposure is not None and np.asarray(exposure).shape != energy.shape:
        from isar.schema.statistics import SemanticsConstructionError
        raise SemanticsConstructionError("misaligned_exposure_provenance")
    return {
        "revision": DATA_LIKELIHOOD_REVISION,
        "estimator": {
            "name": "baker_cousins",
            "model_floor": float(estimators.FMIN),
            "cost_scale": "minus_two_log_likelihood",
        },
        "observed_data": {
            "energy": _fingerprint_vector(energy),
            "counts": _fingerprint_vector(counts),
            "mask": mask.reshape(-1).tolist(),
        },
        "normalization": {
            "kind": (
                "relative_exposure_scale"
                if exposure is not None
                else "uniform_raw_counts"
            ),
            "exposure_scale": (
                None if exposure is None else _fingerprint_vector(exposure)
            ),
            "exposure_reference": _fingerprint_number(
                getattr(ctx, "exposure_reference", None)
            ),
        },
        "fixed_likelihood_context": {
            "temperature": _fingerprint_number(getattr(ctx, "T", None)),
            "center": _fingerprint_number(getattr(ctx, "center", None)),
            "gaussian_fwhm": _fingerprint_number(
                getattr(ctx, "gauss_fwhm", None)
            ),
            "lorentzian_fwhm": _fingerprint_number(
                getattr(ctx, "lorentz_fwhm", None)
            ),
            "quasielastic_fwhm": _fingerprint_number(
                getattr(ctx, "quasielastic_fwhm", None)
            ),
            "instrument": getattr(ctx, "instrument", None),
            "energy_status": getattr(ctx, "energy_status", None),
        },
    }


def data_likelihood_fingerprint(ctx) -> str:
    """Canonical identity of the observed likelihood shared by candidate models."""
    from isar.schema.statistics import semantics_fingerprint

    return semantics_fingerprint(
        "isar.data_likelihood_context/1", _data_likelihood_payload(ctx)
    )


def _model_parameterization_payload(prob: Problem) -> dict:
    """Declarative component, parameter, bound, and effective-free-space identity."""
    names = [str(name) for name in prob.free_names]
    fixed = {
        names[index]: _fingerprint_number(value)
        for index, value in zip(prob.fixed_pos, prob.fixed_vals)
    }
    components = []
    for component in list(getattr(prob.composite, "components", ()) or ()):
        parameters = [
            {"name": str(parameter.name), "kind": str(parameter.kind)}
            for parameter in (getattr(component, "params", ()) or ())
        ]
        item = {
            "type": (
                f"{type(component).__module__}.{type(component).__qualname__}"
            ),
            "prefix": str(getattr(component, "prefix", "")),
            "parameters": parameters,
        }
        if hasattr(component, "peak"):
            item["peak"] = str(component.peak)
        components.append(item)
    if not components:
        components.append(
            {
                "type": (
                    f"{type(prob.composite).__module__}."
                    f"{type(prob.composite).__qualname__}"
                ),
                "prefix": None,
                "parameters": [],
            }
        )
    bounds = [
        {
            "parameter": name,
            "lower": _fingerprint_number(bound[0]),
            "upper": _fingerprint_number(bound[1]),
        }
        for name, bound in zip(names, prob.bounds)
    ]
    return {
        "revision": MODEL_PARAMETERIZATION_REVISION,
        "components": components,
        "parameter_names": names,
        "free_parameter_names": [name for name in names if name not in fixed],
        "fixed_parameters": [
            {"parameter": name, "value": fixed[name]} for name in sorted(fixed)
        ],
        "bounds": bounds,
        "effective_parameter_count": int(prob.k),
    }


def model_parameterization_fingerprint(hypothesis, ctx) -> str:
    """Canonical declarative parameter-space identity for one hypothesis/context."""
    from isar.schema.statistics import semantics_fingerprint

    return semantics_fingerprint(
        "isar.model_parameterization_context/1",
        _model_parameterization_payload(build_problem(hypothesis, ctx)),
    )


def _fit_semantics_for(prob: Problem, ctx, fit_kind: str) -> dict:
    """Build semantics from the constraints that actually enter ``prob``."""
    from isar.schema.statistics import (
        FIT_SELECTION_SCHEMA,
        EffectiveConstraint,
        SemanticsConstructionError,
        authority_scopes,
        semantics_fingerprint,
        validate_for_write,
    )

    constraints = getattr(ctx, "constraints", None)
    origins = dict(getattr(constraints, "origins", None) or {})
    names = list(prob.free_names)
    fixed_names = {names[index] for index in prob.fixed_pos}
    effective = []
    if constraints is not None:
        for name, value in (getattr(constraints, "hard", None) or {}).items():
            if name not in fixed_names:
                continue
            effective.append(
                EffectiveConstraint(
                    parameter=str(name),
                    constraint_kind="hard",
                    value=float(value),
                    origin=origins.get(name),
                ).to_dict()
            )
        for name, value in (getattr(constraints, "soft", None) or {}).items():
            if name not in names or name in fixed_names:
                continue
            mean, sigma = float(value[0]), float(value[1])
            if sigma <= 0:
                continue
            effective.append(
                EffectiveConstraint(
                    parameter=str(name),
                    constraint_kind="soft",
                    mean=mean,
                    sigma=sigma,
                    origin=origins.get(name),
                ).to_dict()
            )
    effective.sort(key=lambda item: (item["parameter"], item["constraint_kind"]))
    if any(not item.get("origin") for item in effective):
        missing = ",".join(item["parameter"] for item in effective if not item.get("origin"))
        raise SemanticsConstructionError("missing_constraint_origin", missing)
    origin_set = sorted({item["origin"] for item in effective})
    aggregate_origin = (
        "none" if not origin_set else origin_set[0] if len(origin_set) == 1 else "mixed"
    )
    has_soft = any(item["constraint_kind"] == "soft" for item in effective)
    has_data_conditioning = any(
        item["origin"] in {"data_dependent_rescue", "post_selection"} for item in effective
    )
    has_other_hard = any(
        item["constraint_kind"] == "hard"
        and item["origin"] in {"prespecified_scientific", "prior_predictive"}
        for item in effective
    )
    if has_data_conditioning:
        eligibility = "ineligible_data_dependent_conditioning"
    elif has_soft:
        eligibility = "ineligible_soft_penalized"
    elif has_other_hard:
        eligibility = "ineligible_unapproved_conditioning"
    else:
        eligibility = "eligible"
    reasons = []
    if has_soft:
        reasons.append("effective_soft_constraint")
    if has_data_conditioning:
        reasons.append("data_dependent_or_post_selection_hard_constraint")
    if has_other_hard:
        reasons.append("unapproved_prespecified_or_prior_predictive_hard_constraint")
    estimator_kind = (
        "soft_penalized_likelihood"
        if has_soft
        else "hard_conditioned_likelihood"
        if effective
        else "data_likelihood_mle"
    )
    data_likelihood_fingerprint = semantics_fingerprint(
        "isar.data_likelihood_context/1", _data_likelihood_payload(ctx)
    )
    model_parameterization_fingerprint = semantics_fingerprint(
        "isar.model_parameterization_context/1",
        _model_parameterization_payload(prob),
    )
    constraint_fingerprint = semantics_fingerprint(
        "isar.effective_constraint_context/1",
        {"effective_constraints": effective},
    )
    comparison_context_fingerprint = semantics_fingerprint(
        "isar.fit_comparison_context/1",
        {
            "data_likelihood_fingerprint": data_likelihood_fingerprint,
            "model_parameterization_fingerprint": model_parameterization_fingerprint,
            "constraint_fingerprint": constraint_fingerprint,
            "estimator_kind": estimator_kind,
            "fit_kind": str(fit_kind),
            "initialization_and_qoi_context": {
                "prior_omega": _fingerprint_number(
                    getattr(ctx, "prior_omega", None)
                ),
                "acoustic_omega": _fingerprint_number(
                    getattr(ctx, "acoustic_omega", None)
                ),
            },
        },
    )
    record = {
        "schema": FIT_SELECTION_SCHEMA,
        "estimator_kind": estimator_kind,
        "fit_kind": str(fit_kind),
        "effective_constraints": effective,
        "aggregate_constraint_origin": aggregate_origin,
        "selection_eligibility": eligibility,
        "selection_ineligibility_reasons": reasons,
        "data_likelihood_fingerprint": data_likelihood_fingerprint,
        "model_parameterization_fingerprint": model_parameterization_fingerprint,
        "constraint_fingerprint": constraint_fingerprint,
        "comparison_context_fingerprint": comparison_context_fingerprint,
        # Additive compatibility alias.  Since STAT-1.5 it names the complete comparison context,
        # never merely the constraint payload.
        "conditioning_fingerprint": comparison_context_fingerprint,
        "authority_scopes": authority_scopes(
            single_fit_delivery="active",
            user_diagnostic="active",
        ),
    }
    return validate_for_write(record, FIT_SELECTION_SCHEMA)


def _attach_fit_semantics(result: StructureFitResult, prob: Problem, ctx) -> None:
    from isar.schema.statistics import SemanticsConstructionError

    try:
        result.fit_semantics = _fit_semantics_for(prob, ctx, result.fit_kind)
    except SemanticsConstructionError as exc:
        log.warning(
            "semantics_construction_failed producer=isar.analyze.loop quantity=%s reason_code=%s",
            result.label,
            exc.reason_code,
        )
        result.fit_semantics = None


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
        parameter_covariance=(np.asarray(cov, dtype=float).tolist() if cov is not None else None),
    )


def fit_hypothesis(hyp, ctx, *, backend="iminuit", minos=False, x0=None) -> StructureFitResult:
    """Fit one structure hypothesis and classify the result. ``x0`` overrides the default IC
    (see :func:`build_problem`) — the two-stage loop passes the greedy multimode seed."""
    prob = build_problem(hyp, ctx, x0=x0)
    be_names = [_sanitize(n) for n in prob.free_names]
    br = _minimize_fixed(backend, prob.objective, prob.x0, prob.bounds, be_names,
                         prob.fixed_pos, prob.fixed_vals, minos=minos)
    result = result_from_backend(
        hyp.label,
        prob.composite,
        prob.costv,
        prob.n_used,
        prob.k,
        br,
        prior_omega=getattr(ctx, "prior_omega", None),
        acoustic_omega=getattr(ctx, "acoustic_omega", None),
        penalty=prob.penalty,
        hard_fixed=prob.is_hard_fixed,
    )
    _attach_fit_semantics(result, prob, ctx)
    return result


def _fit_hypothesis_task(args):
    """Picklable process-worker entry point for one independent candidate fit."""
    hyp, ctx, backend, minos, x0 = args
    return fit_hypothesis(hyp, ctx, backend=backend, minos=minos, x0=x0)


def _fit_hypothesis_batch(tasks, executor=None, *, phase="candidate"):
    """Fit an ordered pure-task batch, with visible deterministic serial recovery."""
    if executor is None or len(tasks) < 2:
        return [_fit_hypothesis_task(task) for task in tasks]
    try:
        return list(executor.map(_fit_hypothesis_task, tasks))
    except Exception as exc:  # noqa: BLE001
        log.warning("parallel %s fit failed; retrying serially: %s: %s",
                    phase, type(exc).__name__, exc)
        return [_fit_hypothesis_task(task) for task in tasks]


def _shadow_result_matches(result, hyp, ctx, data_likelihood_fingerprint: str) -> bool:
    """Whether ``result`` is exact unconstrained evidence for ``hyp`` under ``ctx``.

    This is intentionally stricter than a label join.  STAT-2a reuse requires the same observed
    likelihood, the same declarative parameterization, and an eligible data-likelihood estimate.
    """
    semantics = getattr(result, "fit_semantics", None)
    if not isinstance(semantics, dict):
        return False
    if semantics.get("selection_eligibility") != "eligible":
        return False
    if semantics.get("estimator_kind") != "data_likelihood_mle":
        return False
    if semantics.get("data_likelihood_fingerprint") != data_likelihood_fingerprint:
        return False
    expected_parameterization = model_parameterization_fingerprint(hyp, ctx)
    return (
        semantics.get("model_parameterization_fingerprint")
        == expected_parameterization
    )


def _fit_shadow_stage(hypotheses, ctx, reusable_by_label, *, backend, minos, executor,
                      phase, x0_for=None):
    """Fit the missing members of one canonical shadow stage and preserve hypothesis order."""
    data_fingerprint = data_likelihood_fingerprint(ctx)
    results_by_label = {}
    reused_labels = []
    tasks = []
    task_hypotheses = []
    for hypothesis in hypotheses:
        reusable = reusable_by_label.get(str(hypothesis.label))
        if reusable is not None and _shadow_result_matches(
                reusable, hypothesis, ctx, data_fingerprint):
            results_by_label[str(hypothesis.label)] = reusable
            reused_labels.append(str(hypothesis.label))
            continue
        x0 = None if x0_for is None else x0_for(hypothesis)
        tasks.append((hypothesis, ctx, backend, minos, x0))
        task_hypotheses.append(hypothesis)
    fitted = _fit_hypothesis_batch(tasks, executor, phase=phase)
    for result, hypothesis in zip(fitted, task_hypotheses):
        results_by_label[str(hypothesis.label)] = result
    return (
        [results_by_label[str(hypothesis.label)] for hypothesis in hypotheses],
        reused_labels,
    )


def shadow_bundle_from_complete_results(results, *, peaks=None) -> ShadowCandidateBundle:
    """Wrap an already-complete unconstrained two-stage result list without copying it."""
    has_multimode = any(int(getattr(result, "n_modes", 1)) > 1 for result in results)
    multimode_route = (
        "guideline_forced"
        if has_multimode and peaks is not None and len(peaks) >= 2
        else "residual_triggered"
        if has_multimode
        else "not_offered"
    )
    labels = [str(result.label) for result in results]
    converged = [
        str(result.label) for result in results if result.optimizer_converged
    ]
    failed = [
        str(result.label) for result in results if not result.optimizer_converged
    ]
    return ShadowCandidateBundle(
        results=results,
        offered_labels=labels,
        attempted_labels=labels,
        converged_labels=converged,
        failed_labels=failed,
        reused_labels=labels,
        freshly_fitted_labels=[],
        multimode_route=multimode_route,
    )


def fit_shadow_candidates(ctx, *, reusable_results=(), backend="iminuit", minos=False,
                          peaks=None, executor=None) -> ShadowCandidateBundle:
    """Fit the complete unconstrained STAT-2a candidate route, reusing exact results.

    The hint itself is intentionally absent from this interface: a previous-structure hint may
    provide a result in ``reusable_results``, but only the ordinary residual/expected-peak route
    can offer a two-mode hypothesis.
    """
    reusable_by_label = {
        str(result.label): result for result in reusable_results
    }
    one_hypotheses = list(enumerate_structures(ctx))
    one_results, reused_one = _fit_shadow_stage(
        one_hypotheses,
        ctx,
        reusable_by_label,
        backend=backend,
        minos=minos,
        executor=executor,
        phase="shadow one-mode candidate",
    )

    expected_two = peaks is not None and len(peaks) >= 2
    residual_two = _offer_multimode(one_results, ctx)
    multi_hypotheses = []
    multi_results = []
    reused_multi = []
    if residual_two or expected_two:
        best_one = _best_converged(one_results)
        n_used = int(ctx.x_used.size)
        multi_hypotheses = [
            hypothesis for hypothesis in enumerate_multimode(ctx)
            if n_used - hypothesis.k > 0
        ]

        def multimode_x0(hypothesis):
            return greedy_multimode_ic(
                hypothesis.build(), ctx, best_one, peaks=peaks
            )

        multi_results, reused_multi = _fit_shadow_stage(
            multi_hypotheses,
            ctx,
            reusable_by_label,
            backend=backend,
            minos=minos,
            executor=executor,
            phase="shadow multi-mode candidate",
            x0_for=multimode_x0,
        )

    results = one_results + multi_results
    offered_labels = [
        str(hypothesis.label) for hypothesis in one_hypotheses + multi_hypotheses
    ]
    return ShadowCandidateBundle(
        results=results,
        offered_labels=offered_labels,
        attempted_labels=[str(result.label) for result in results],
        converged_labels=[
            str(result.label) for result in results if result.optimizer_converged
        ],
        failed_labels=[
            str(result.label) for result in results if not result.optimizer_converged
        ],
        reused_labels=reused_one + reused_multi,
        freshly_fitted_labels=[
            str(result.label)
            for result in results
            if str(result.label) not in set(reused_one + reused_multi)
        ],
        multimode_route=(
            "guideline_forced"
            if expected_two
            else "residual_triggered"
            if residual_two
            else "not_offered"
        ),
    )


def fit_admissible_structures(ctx, *, backend="iminuit", minos=False, errors=False,
                              M=200, seed=12345, executor=None) -> list[StructureFitResult]:
    """Enumerate the admissible structures and fit each (the §2→§3 loop). Step-2 model
    selection will consume this list; step 1 just returns it.

    With ``errors=True`` the frequentist error layer (bootstrap, §5.2) is attached to each
    converged result's ``within_model``. Off by default so step 1's fast path is unchanged."""
    hyps = enumerate_structures(ctx)
    tasks = [(h, ctx, backend, minos, None) for h in hyps]
    results = _fit_hypothesis_batch(tasks, executor, phase="admissible-candidate")
    if errors:
        from isar.analyze import errors as _errors  # lazy: errors imports loop
        items = [(r, h, ctx) for r, h in zip(results, hyps)]
        _errors.attach_batch(
            items, M=M, seed=seed, backend=backend,
            boot_workers=(max(2, len(items)) if executor is not None else 1), executor=executor)
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


def _enumerate_and_fit(ctx, *, backend="iminuit", minos=False, peaks=None, executor=None):
    """The full two-stage enumeration: fit the 1-mode set, escalate to the 2-mode set when the
    gate fires or ``peaks`` declares ≥2 peaks. Returns ``[(result, hypothesis), ...]`` (both stages
    unioned). Split out of :func:`fit_structures_two_stage` so the structure-hint shortcut
    (:func:`fit_structures_two_stage_hinted`) can fall back to it verbatim."""
    one_hyps = enumerate_structures(ctx)
    one_tasks = [(h, ctx, backend, minos, None) for h in one_hyps]
    one_results = _fit_hypothesis_batch(one_tasks, executor, phase="one-mode candidate")
    pairs = list(zip(one_results, one_hyps))

    expected_two = peaks is not None and len(peaks) >= 2
    if _offer_multimode([r for r, _ in pairs], ctx) or expected_two:
        best_one = _best_converged([r for r, _ in pairs])
        n_used = int(ctx.x_used.size)
        multi_hyps = []
        multi_tasks = []
        for h in enumerate_multimode(ctx):
            if n_used - h.k <= 0:                     # ndof guard: no positive dof
                continue
            x0 = greedy_multimode_ic(h.build(), ctx, best_one, peaks=peaks)
            multi_hyps.append(h)
            multi_tasks.append((h, ctx, backend, minos, x0))
        multi_results = _fit_hypothesis_batch(
            multi_tasks, executor, phase="multi-mode candidate")
        pairs.extend(zip(multi_results, multi_hyps))
    return pairs


def fit_structures_two_stage(ctx, *, backend="iminuit", minos=False, with_errors=False,
                             M=200, seed=12345, peaks=None, executor=None) -> list[StructureFitResult]:
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
    pairs = _enumerate_and_fit(
        ctx, backend=backend, minos=minos, peaks=peaks, executor=executor)
    results = [r for r, _ in pairs]
    if with_errors:
        from isar.analyze import errors as _errors  # lazy: errors imports loop
        items = [(r, h, ctx) for r, h in pairs]
        _errors.attach_batch(
            items, M=M, seed=seed, backend=backend,
            boot_workers=(max(2, len(items)) if executor is not None else 1), executor=executor)
    return results


def _null_aic(ctx, *, backend="iminuit") -> float | None:
    """AIC of the no-phonon baseline for this context (``elastic+bg`` when the elastic line is
    admissible, else background-only), or ``None`` when that null does not converge.

    Fit directly (not through :func:`fit_hypothesis`, whose ω₀ extraction assumes a phonon) so the
    structure-hint shortcut can ask "does the hinted phonon actually earn its parameters over
    fitting nothing?" without ever admitting a phonon-less result into the selection layer."""
    from isar.analyze.components import ConstantBackground, Elastic

    comps = (Elastic, ConstantBackground) if elastic_admissible(ctx) else (ConstantBackground,)
    hyp = Hypothesis("elastic+bg" if len(comps) == 2 else "bg", comps)
    prob = build_problem(hyp, ctx)
    be_names = [_sanitize(n) for n in prob.free_names]
    br = _minimize_fixed(backend, prob.objective, prob.x0, prob.bounds, be_names,
                         prob.fixed_pos, prob.fixed_vals, minos=False)
    if not br.success:
        return None
    cost = float(prob.costv(np.asarray(br.x, dtype=float)))
    return cost + 2.0 * prob.k


def _try_structure_hint(ctx, structure_hint, *, backend="iminuit", minos=False):
    """Fit ONLY the hinted structure (cold grid-IC) plus the no-phonon null, and accept the
    shortcut iff every gate the full enumeration relies on still passes. Returns the one-element
    result list on acceptance, or ``None`` to signal "fall through to the full enumeration".

    Rejected (→ ``None``) when: the hint label is no longer admissible for this context; the hinted
    fit does not converge; the two-stage gate (:func:`_offer_multimode`: cost/dof > ``GATE_COST`` or
    a structured residual) fires, i.e. one structure is not enough; or the no-phonon null is
    AIC-preferred, i.e. the phonon does not earn its parameters here. On any of these the full
    enumeration must run — the hint is a computational shortcut, never a correctness override."""
    by_label = {h.label: h for h in enumerate_structures(ctx)}
    by_label.update({h.label: h for h in enumerate_multimode(ctx)})
    hyp = by_label.get(structure_hint)
    if hyp is None:
        return None                                    # label no longer in the hypothesis set
    r = fit_hypothesis(hyp, ctx, backend=backend, minos=minos)   # cold grid-IC, same as full path
    if not r.optimizer_converged:
        return None
    if _offer_multimode([r], ctx):                     # cost/dof or structured-residual gate
        return None
    null_aic = _null_aic(ctx, backend=backend)
    if null_aic is not None and null_aic <= (r.cost + 2.0 * r.k):
        return None                                    # no-phonon preferred → hint assumption void
    return [r]


def fit_structures_two_stage_hinted(ctx, *, backend="iminuit", minos=False, peaks=None,
                                    structure_hint=None, executor=None) -> tuple[list[StructureFitResult], bool]:
    """:func:`fit_structures_two_stage` with an optional ``structure_hint``. Returns
    ``(results, structure_skipped)``.

    When ``structure_hint`` is ``None`` (the default, every non-master path) this is bit-for-bit
    the full enumeration and ``structure_skipped`` is always ``False``. When a label is given
    (a master refit reusing its previous winner, card policy ``[fitting].reuse_master_structure``)
    the shortcut in :func:`_try_structure_hint` is attempted first: on acceptance only the hinted
    structure + the no-phonon null are fit and ``structure_skipped`` is ``True``; on any gate
    failure the hint is discarded and the full enumeration runs (``structure_skipped`` ``False``).

    **Statistically-equivalent, not bit-identical:** the accepted shortcut hands the selection
    layer a smaller candidate set (one structure, not the full admissible enumeration), so the
    AIC comparison, ``modality``, ``confidence`` and ``best_label`` can differ from what the full
    enumeration would have produced. The gates keep the difference to fits where one structure
    already explains the data; it is opt-in and excluded from the bit-identity harness."""
    if structure_hint is not None:
        hinted = _try_structure_hint(ctx, structure_hint, backend=backend, minos=minos)
        if hinted is not None:
            return hinted, True
    return [r for r, _ in _enumerate_and_fit(
        ctx, backend=backend, minos=minos, peaks=peaks, executor=executor)], False
