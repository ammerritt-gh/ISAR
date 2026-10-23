"""The structure-hypothesis loop: build a per-hypothesis problem, fit it, classify the fit.

``build_problem`` is the per-hypothesis analogue of ``synth.fit._build_problem`` but driven
by a :class:`~isar.analyze.components.CompositeModel`, so each hypothesis is a genuine model
variant with its own free-parameter count ``k`` and ``ndof`` (review F1) — what makes the
step-2 AIC/BIC comparison valid. ``StructureFitResult`` separates optimizer convergence from
error availability (review F2): a converged but covariance-less fit stays *usable* and is
carried by the step-3 bootstrap rather than discarded. The reported ω₀ error here is
HESSE-propagated and **provisional** (review F4); the authoritative interval is the bootstrap.

Rung 1 of the count ladder (design 2026-10-22, §3.1) lives here too: every count from zero to the
mode ceiling, with and without the elastic line, fitted from several independent starts, the
highest-likelihood start kept per model, and no gate deciding whether a higher count is tried
(:func:`fit_structures_two_stage`, :class:`Rung1Record`).
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import logging
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

from isar.analyze import backends, estimators
from isar.analyze.backends import BackendResult
from isar.analyze.catalogue import enumerate_count_structures
from isar.analyze.phonon import (
    EPS_OMEGA_D, SPLIT_SEPARATIONS, _gamma_at_bound, _mode_prefixes, _peak_omega0,
    assess_success, default_mode_ceiling, enumerate_phonon_structures, greedy_multimode_ic, omega0_value_error, phonon_structure_ic,
    placed_modes_ic, split_mode_ic,
)
from isar.analyze.prior import mode_bounds, recipe_provenance, resolution_fwhm
# Re-imported for their module attributes, not for use here: scan_delivery.py and the
# analyze tests read ``loop.GAMMA_FLOOR`` / ``loop.GAMMA_MAX``.
from isar.analyze.phonon import GAMMA_FLOOR, GAMMA_MAX  # noqa: F401

# STAT-1.5 metadata revisions.  These are descriptive fingerprints only: changing either label
# changes semantic comparability identity, never a fit/cache/scientific identity.
DATA_LIKELIHOOD_REVISION = "isar.baker_cousins_data_likelihood/1"
MODEL_PARAMETERIZATION_REVISION = "isar.fit_model_parameterization/1"

def _sanitize(name: str) -> str:
    """Backend parameter names must be plain identifiers (iminuit); drop the dotted prefix."""
    return name.replace(".", "_")


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
    # Per-parameter boundary-pin flags keyed by the REAL (dotted) parameter names, in ``params``
    # order. What the evidence validity ladder reads (SCENE-1 PR-b): a pinned free parameter
    # invalidates the Laplace curvature and escalates to nested sampling.
    at_limit: dict | None = None
    # Additive statistical provenance.  A construction failure leaves this ``None`` while the
    # numerical fit remains usable.
    fit_semantics: dict | None = None
    # The catalogue constrained variant this candidate realises (SCENE-1 PR-c), e.g.
    # ``"near_degenerate"``; ``None`` for an ordinary candidate. Provenance only — the candidate's
    # identity is its label, and ``fit_kind`` already carries the hard-conditioned nature of the fit.
    variant: str | None = None
    # The catalogue phenomenon identity this candidate claims (SCENE-1 PR-c commit E), carried
    # from its hypothesis at fit time; ``None`` for an identity-agnostic candidate. RECORDS ONLY —
    # nothing in the verdict, confidence, stopping or steering path reads it. The object that OWNS
    # an identity lives in the scene store (SCENE-3); this labels one scan's evidence contribution.
    identity: str | None = None


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
            "exposure_source": getattr(ctx, "exposure_source", None),
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
    # Only a soft constraint the ruling did NOT recast costs eligibility (H4(c)); a prespecified
    # one is comparable. ``has_soft`` still drives estimator_kind — the fit is a MAP either way,
    # which is what keeps it out of acquisition (isar/eig/eligibility.py).
    from isar.schema.statistics import PRESPECIFIED_SOFT_ORIGINS

    has_nonprespecified_soft = any(
        item["constraint_kind"] == "soft"
        and item["origin"] not in PRESPECIFIED_SOFT_ORIGINS
        for item in effective
    )
    has_data_conditioning = any(
        item["origin"] in {"data_dependent_rescue", "post_selection"} for item in effective
    )
    # ANY hard constraint, whatever its origin — H4(c) recast prespecified SOFT priors only, and
    # a hard pin conditions the likelihood however prespecified the physics behind it is. This is
    # the producer-side mirror of the write-time invariant in isar.schema.statistics
    # (eligible ⇒ every effective constraint soft AND prespecified): keying on
    # PRESPECIFIED_SOFT_ORIGINS instead let a hard pin with an unlisted origin (a catalogue
    # variant's Γ floor) walk through to "eligible" and then fail validation at write time.
    # ("other" = not already named by has_data_conditioning, so the two reasons stay disjoint.)
    has_other_hard = any(
        item["constraint_kind"] == "hard"
        and item["origin"] not in {"data_dependent_rescue", "post_selection"}
        for item in effective
    )
    if has_data_conditioning:
        eligibility = "ineligible_data_dependent_conditioning"
    elif has_nonprespecified_soft:
        eligibility = "ineligible_soft_penalized"
    elif has_other_hard:
        eligibility = "ineligible_unapproved_conditioning"
    else:
        eligibility = "eligible"
    reasons = []
    if has_nonprespecified_soft:
        reasons.append("effective_soft_constraint")
    if has_data_conditioning:
        reasons.append("data_dependent_or_post_selection_hard_constraint")
    if has_other_hard:
        reasons.append("unapproved_hard_constraint")
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
        # The mode prior recipe that bounded this fit (and so set its flat priors).
        "prior_recipe": recipe_provenance(ctx),
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
    ``composite.param_names()`` (e.g. from :func:`~isar.analyze.phonon.greedy_multimode_ic`) or a
    ready array. Defaults to :func:`~isar.analyze.phonon.phonon_structure_ic` (unchanged 1-mode path)."""
    composite = hyp.build()
    x = ctx.x_used
    counts = ctx.counts_used

    names = composite.param_names()
    if x0 is None:
        x0 = phonon_structure_ic(composite, ctx)
    elif isinstance(x0, dict):
        x0 = np.array([x0[n] for n in names], dtype=float)
    else:
        x0 = np.asarray(x0, dtype=float)
    bounds = mode_bounds(names, ctx)   # the prior recipe: declared quantities, never the counts
    lo, hi = np.array(bounds, dtype=float).T
    x0 = np.clip(x0, lo, hi)           # a seed outside the recipe's box starts on its edge
    est =estimators.get_estimator("baker_cousins")

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
                        hard_fixed=False, T=None) -> StructureFitResult:
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

    # per-mode collection, canonicalized ascending by ω₀ (kills label switching). The fitted A is
    # INTRINSIC (H4); ``amplitude`` stays the MEASURED Stokes strength A·(n+1)(ω₀,T) so every
    # delivery-space consumer (intensity portability, branch features, detection power, reports)
    # keeps its counts-scale meaning, with the intrinsic value alongside. With T unknown the two
    # coincide by convention ((n+1)=1 — no occupation information).
    from isar.analyze.phonon import bose_occupation
    modes = []
    for i in composite.phonon_indices():
        w0, w0e = composite._omega0_at(theta, cov, i)
        g_err = float(errs[i + 1]) if errs is not None else float("nan")
        occ = bose_occupation(w0, T) if T is not None else 1.0
        modes.append({"omega0": w0, "omega0_err": w0e, "gamma": float(theta[i + 1]),
                      "gamma_err": g_err, "amplitude": float(theta[i + 2]) * occ,
                      "amplitude_intrinsic": float(theta[i + 2])})
    modes.sort(key=lambda m: m["omega0"])
    n_modes = len(modes)

    # primary mode → the reported scalar ω₀ / Γ (back-compat); the acoustic hint (Phase A) selects
    # which mode of a multi-mode fit is the acoustic QoI, ahead of the prior/amplitude fallbacks.
    prim_i = composite.primary_phonon_index(theta, cov, prior_omega, acoustic_omega, T=T)

    optimizer_converged = bool(br.success)
    covariance_ok = bool(br.covariance_ok)
    merrors = br.merrors or {}
    profile_ok = bool(merrors) and all(v[2] for v in merrors.values())

    if prim_i is None:
        # Phonon-less structure (elastic+bg / bg, SCENE-1 PR-a): there is no ω to report. NaN,
        # never 0.0 — a fake zero would read as a real zone-centre energy downstream. Ω_d
        # separability is vacuous here, so well-constrained reduces to converged + covariance.
        omega0 = omega0_err = gamma = gamma_err = float("nan")
        at_limit_adpt = {"omega_d": False}
        well_constrained = optimizer_converged and covariance_ok
    else:
        omega0, omega0_err = composite._omega0_at(theta, cov, prim_i)
        gamma = float(theta[prim_i + 1])
        gamma_err = float(errs[prim_i + 1]) if errs is not None else float("nan")
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
        # backend keys are sanitized identifiers; persist under the real dotted names. A
        # hard-fixed param is absent from the backend map (it was never free) → False.
        at_limit={n: bool((br.at_limit or {}).get(_sanitize(n), False)) for n in names},
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
        T=getattr(ctx, "T", None),
    )
    result.identity = getattr(hyp, "identity", None)
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


def shadow_bundle_from_complete_results(results, *, multimode_route=None) -> ShadowCandidateBundle:
    """Wrap an already-complete unconstrained count-ladder result list without copying it.

    ``multimode_route`` should be the route the production fit already recorded for these SAME
    results (this call aliases them, it never re-fits); omitted, it is the ladder's
    ``"count_ladder"``."""
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
        multimode_route=multimode_route or "count_ladder",
    )


def fit_shadow_candidates(ctx, *, reusable_results=(), backend="iminuit", minos=False,
                          peaks=None, executor=None, mode_ceiling=None) -> ShadowCandidateBundle:
    """Fit the complete unconstrained STAT-2a candidate route: rung 1 of the count ladder
    (:func:`_rung1_ladder`), every count with every start.

    An exact reusable result (same likelihood, same parameterization, eligible) never replaces a
    model's search: it joins that model's starts and is reported as reused only when it is the
    representative. The hint itself is intentionally absent from this interface: a previous-
    structure hint may provide a result in ``reusable_results``, nothing more.
    """
    reusable_by_label = {
        str(result.label): result for result in reusable_results
    }
    record = _rung1_ladder(ctx, backend=backend, minos=minos, peaks=peaks, executor=executor,
                           mode_ceiling=mode_ceiling, reusable_by_label=reusable_by_label)
    results = [model.representative for model in record.models]
    reused = [model.label for model in record.models
              if model.starts[model.representative_start].source == "reused"]
    return ShadowCandidateBundle(
        results=results,
        offered_labels=[model.label for model in record.models],
        attempted_labels=[str(result.label) for result in results],
        converged_labels=[
            str(result.label) for result in results if result.optimizer_converged
        ],
        failed_labels=[
            str(result.label) for result in results if not result.optimizer_converged
        ],
        reused_labels=reused,
        freshly_fitted_labels=[
            str(result.label) for result in results if str(result.label) not in set(reused)
        ],
        multimode_route="count_ladder",
    )


def fit_admissible_structures(ctx, *, backend="iminuit", minos=False, errors=False,
                              M=200, seed=12345, executor=None) -> list[StructureFitResult]:
    """Enumerate the admissible structures and fit each (the §2→§3 loop). Step-2 model
    selection will consume this list; step 1 just returns it.

    With ``errors=True`` the frequentist error layer (bootstrap, §5.2) is attached to each
    converged result's ``within_model``. Off by default so step 1's fast path is unchanged."""
    hyps = enumerate_phonon_structures(ctx)
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


@dataclass
class Rung1Start:
    """One start of one rung-1 model: where its seed came from and how its fit ended.

    ``source``: ``"residual"`` (the previous count's fit plus the largest residual feature; at one
    mode the cold grid IC), ``"split"`` (a fitted mode of the previous count split in two),
    ``"descriptor"`` (the model-free descriptor's features), ``"declared"`` (the card's
    ``[fitting]`` peaks), ``"hint"`` (the cold grid IC of a hinted master structure, on its own
    count and variant; at counts 0 and 1 the residual start already is that fit and carries
    ``detail["hint"]``), ``"reused"`` (an exact earlier result) or ``"variant"`` (a catalogue
    constrained variant, seeded from its parent)."""

    source: str
    detail: dict
    cost: float          # data-only Baker–Cousins cost
    objective: float     # what the optimizer minimised (cost + any soft-prior penalty); inf if failed
    converged: bool
    result: StructureFitResult
    # The initial parameter vector by name (the cold grid IC resolved, never left implicit), so a
    # reader can state the seed and refit from it; ``None`` only for "reused", whose seed is not known.
    x0: dict | None = None


@dataclass
class Rung1Model:
    """One count and elastic variant of rung 1: its starts and its representative, the start with
    the lowest objective (the highest likelihood), kept whether or not it converged — a
    non-converged representative is marked by :attr:`converged`, never dropped. ``at_upper_bound``
    names any amplitude or background at the prior recipe's upper bound (the recipe's ceiling is a
    declared constant; a too-tight one must show, not bend the count). The evidence fields stay
    ``None`` until :meth:`Rung1Record.apply_selection`."""

    label: str
    count: int
    elastic: bool
    quasielastic: bool
    variant: str | None
    hypothesis: object
    starts: list
    representative_start: int
    at_upper_bound: list
    log_evidence: float | None = None
    evidence_method: str | None = None
    probability: float | None = None

    @property
    def representative(self) -> StructureFitResult:
        return self.starts[self.representative_start].result

    @property
    def converged(self) -> bool:
        return bool(self.representative.optimizer_converged)

    @property
    def seeds_by_source(self) -> dict:
        out = {}
        for start in self.starts:
            out[start.source] = out.get(start.source, 0) + 1
        return out


@dataclass
class Rung1Record:
    """Rung 1 for one spectrum: every count 0..``ceiling`` and elastic variant tried (plus any
    catalogue constrained variant), each with its starts and representative. Handed to rung 2
    (S1U4) and the outputs (S1U5); nothing here is persisted."""

    ceiling: int
    models: list

    def by_label(self) -> dict:
        return {model.label: model for model in self.models}

    def apply_selection(self, selection) -> None:
        """Copy each model's log-evidence, evidence method and posterior probability from a
        :class:`~isar.analyze.select.Selection` over these candidates. A model the selection did
        not score (its representative did not converge) keeps ``None``: no evidence, not zero."""
        scores = {score.label: score for score in selection.scores}
        for model in self.models:
            score = scores.get(model.label)
            model.log_evidence = None if score is None else score.log_evidence
            model.evidence_method = None if score is None else score.evidence_method
            model.probability = None if score is None else score.posterior_probability

    def count_probabilities(self) -> dict:
        """Posterior probability of each count, summed over its elastic and constrained variants
        (only models the selection scored)."""
        out = {}
        for model in self.models:
            if model.probability is not None:
                out[model.count] = out.get(model.count, 0.0) + model.probability
        return out


def _variant_key(hyp) -> tuple:
    """Which elastic / quasi-elastic variant (and identity) a structure is, whatever its count."""
    tokens = str(hyp.label).split("@", 1)[0].split("+")
    return ("elastic" in tokens, "qe" in tokens, getattr(hyp, "identity", None))


def _objective(result) -> float:
    value = result.posterior_cost if result.posterior_cost is not None else result.cost
    return float(value) if np.isfinite(value) else float("inf")


def _start(source, detail, result, x0=None) -> Rung1Start:
    return Rung1Start(source=source, detail=dict(detail), cost=float(result.cost),
                      objective=_objective(result), converged=bool(result.optimizer_converged),
                      result=result, x0=None if x0 is None else dict(x0))


def _at_upper_bound(result, ctx) -> list:
    """Amplitudes and background of ``result`` at the prior recipe's upper bound."""
    names = [n for n in result.params if n.endswith((".A", ".E", ".QE", ".BG"))]
    if not names:
        return []
    return [n for n, (_lo, hi) in zip(names, mode_bounds(names, ctx))
            if result.params[n] >= hi * (1.0 - 1e-6)]


def _descriptor_omegas(ctx) -> list:
    """The model-free descriptor's energy-loss features as ω₀ = E − centre (meV), most significant
    first."""
    from isar.analyze.descriptor import describe_spectrum

    try:
        described = describe_spectrum(
            ctx.x, ctx.counts, center=ctx.center, gauss_fwhm=ctx.gauss_fwhm,
            lorentz_fwhm=ctx.lorentz_fwhm, mask=ctx.mask,
            exposure_scale=getattr(ctx, "exposure_scale", None))
    except ValueError as exc:
        log.warning("descriptor seeds unavailable: %s", exc)
        return []
    peaks = sorted((p for p in described.peaks if p.energy - ctx.center > 0.0),
                   key=lambda p: (-p.significance, -p.integrated_intensity))
    return [float(p.energy - ctx.center) for p in peaks]


def _count_seeds(hyp, n, ctx, previous, descriptor_omegas, declared_omegas, peaks,
                 hinted=False) -> list:
    """``(source, detail, x0)`` for every start of the ``n``-mode model ``hyp``; ``x0`` is the full
    initial vector by parameter name, the cold grid IC resolved here. ``previous`` is the same
    variant's (n-1)-mode representative. ``hinted``: ``hyp`` is the structure a master refit was
    hinted with, which adds its cold start. A seed that repeats an earlier one is dropped and named
    in the kept seed's ``detail["also"]``."""
    composite = hyp.build()
    names = composite.param_names()
    cold = dict(zip(names, phonon_structure_ic(composite, ctx)))
    if n == 0:
        return [("residual", {"hint": True} if hinted else {}, cold)]
    seeds = []
    if n == 1:
        seeds.append(("residual", {"hint": True} if hinted else {}, cold))
    else:
        if hinted:
            seeds.append(("hint", {}, cold))
        seeds.append(("residual", {}, greedy_multimode_ic(composite, ctx, previous, peaks=peaks)))
        if previous is not None:
            width = resolution_fwhm(ctx)
            for parent in _mode_prefixes(composite)[:-1]:
                for j, separation in enumerate(SPLIT_SEPARATIONS):
                    heavier = "low" if j % 2 == 0 else "high"
                    x0 = split_mode_ic(composite, ctx, previous, parent, separation * width,
                                       heavier=heavier)
                    if x0 is not None:
                        seeds.append(("split", {"parent": parent, "separation_fwhm": separation,
                                                "heavier": heavier}, x0))
    if descriptor_omegas:
        seeds.append(("descriptor", {"features": min(n, len(descriptor_omegas))},
                      placed_modes_ic(composite, ctx, descriptor_omegas[:n])))
    if declared_omegas:
        seeds.append(("declared", {"peaks": min(n, len(declared_omegas))},
                      placed_modes_ic(composite, ctx, declared_omegas[:n])))
    kept = []
    for source, detail, x0 in seeds:
        vec = np.array([x0[name] for name in names])
        twin = next((k for k in kept if np.allclose(vec, k[3], rtol=1e-9)), None)
        if twin is None:
            kept.append((source, dict(detail), x0, vec))
        else:
            twin[1].setdefault("also", []).append(source)
    return [(source, detail, x0) for source, detail, x0, _vec in kept]


def _rung1_ladder(ctx, *, backend="iminuit", minos=False, peaks=None, executor=None,
                  mode_ceiling=None, reusable_by_label=None, structure_hint=None) -> Rung1Record:
    """Rung 1 of the count ladder: every count 0..ceiling, each elastic / quasi-elastic variant the
    gates admit, several independent starts per model, every parameter free, the lowest-objective
    start kept as the model's representative. No early stop and no gate: a count is skipped only
    when it leaves no degree of freedom.

    Counts 0 and 1 are fitted in one batch; each higher count follows in its own batch, because
    its residual and split seeds come from the count below. ``mode_ceiling`` ``None`` is
    :func:`~isar.analyze.phonon.default_mode_ceiling`. ``reusable_by_label``: exact earlier results
    (the shadow lane), each joining its model's starts ahead of the fresh ones. ``structure_hint``:
    a master refit's previous winning label, a seed source on its own count and variant like the
    declared peaks; it gates nothing."""
    ceiling = default_mode_ceiling() if mode_ceiling is None else int(mode_ceiling)
    n_used = int(ctx.x_used.size)
    descriptor_omegas = _descriptor_omegas(ctx)
    declared_omegas = [w for w in (_peak_omega0(p) for p in (peaks or ())) if w is not None and w > 0]
    reusable = reusable_by_label or {}
    fingerprint = data_likelihood_fingerprint(ctx) if reusable else None
    previous = {}
    models = []
    batches = [[1, 0] if ceiling >= 1 else [0]] + [[n] for n in range(2, ceiling + 1)]
    for counts in batches:
        tasks, meta, entries = [], [], []
        for n in counts:
            for hyp in enumerate_count_structures(ctx, n):
                if n_used - hyp.k <= 0:                       # ndof guard: no positive dof
                    continue
                entry = {"hyp": hyp, "n": n, "starts": []}
                reused = reusable.get(str(hyp.label))
                if reused is not None and _shadow_result_matches(reused, hyp, ctx, fingerprint):
                    entry["starts"].append(_start("reused", {}, reused))
                for source, detail, x0 in _count_seeds(
                        hyp, n, ctx, previous.get((n - 1, _variant_key(hyp))), descriptor_omegas,
                        declared_omegas, peaks, hinted=str(hyp.label) == structure_hint):
                    tasks.append((hyp, ctx, backend, minos, x0))
                    meta.append((entry, source, detail, x0))
                entries.append(entry)
        fitted = _fit_hypothesis_batch(tasks, executor, phase=f"count {'/'.join(map(str, counts))}")
        for result, (entry, source, detail, x0) in zip(fitted, meta):
            entry["starts"].append(_start(source, detail, result, x0))
        for entry in entries:
            hyp, starts = entry["hyp"], entry["starts"]
            best = min(range(len(starts)), key=lambda i: starts[i].objective)
            tokens = str(hyp.label).split("@", 1)[0].split("+")
            model = Rung1Model(
                label=str(hyp.label), count=entry["n"], elastic="elastic" in tokens,
                quasielastic="qe" in tokens, variant=None, hypothesis=hyp, starts=starts,
                representative_start=best,
                at_upper_bound=_at_upper_bound(starts[best].result, ctx))
            previous[(entry["n"], _variant_key(hyp))] = model.representative
            models.append(model)
    return Rung1Record(ceiling=ceiling, models=models)


def candidate_context(hyp, ctx):
    """The context a candidate is DEFINED under: its own derived context when it carries one
    (a catalogue constrained variant, SCENE-1 PR-c), else ``ctx``.

    Every layer that rebuilds a candidate's problem from ``(hypothesis, context)`` — prior
    resolution, the evidence rungs, the parameterization fingerprint — must resolve through this,
    or a variant's pinned parameter comes back free and its k, prior bundle and lnZ describe a
    model that was never fitted. It is deliberately NOT applied inside :func:`build_problem`:
    the bootstrap legitimately rebuilds the same hypothesis against RESAMPLED contexts, and
    overriding there would fit the original counts."""
    return getattr(hyp, "context", None) or ctx


def _variant_context(ctx, hard, origins, *, variant: str, label: str):
    """``ctx`` with the variant's pins merged into its constraints, or ``None`` to skip.

    The scan's own constraints win: a parameter this context already constrains (from steering,
    transport, a rescue, or the scientist) is not silently re-pinned by the catalogue — the
    variant is dropped instead, and says so."""
    from isar.schema.steering import ParamConstraints

    base = getattr(ctx, "constraints", None)
    soft = dict(getattr(base, "soft", None) or {})
    existing_hard = dict(getattr(base, "hard", None) or {})
    clashing = sorted(name for name in hard if name in existing_hard or name in soft)
    if clashing:
        log.info("catalogue variant %r not offered for %s: the context already constrains %s",
                 variant, label, ", ".join(clashing))
        return None
    merged = ParamConstraints(
        soft=soft,
        hard={**existing_hard, **hard},
        origins={**dict(getattr(base, "origins", None) or {}), **origins},
    )
    return replace(ctx, constraints=merged)


def _offer_variants(pairs, ctx, *, backend="iminuit", minos=False, executor=None,
                    profile=None):
    """Offer every catalogue constrained variant whose predicate fires on a converged one-mode
    fit, as ADDITIONAL candidates (2a ruling 2 — the variant never replaces its twin).

    Each variant is fitted under a DERIVED context carrying its pins, seeded from the
    unconstrained fit, and returned as ``(result, hypothesis, derived_context, x0)`` (``x0``: the
    parent's parameters it was seeded from) for the single
    :func:`~isar.analyze.select.select_models` pass. Constraints are outside the data-likelihood
    fingerprint, so the pinned twin and its parent share one evidence arena legitimately and the
    posterior prices the extra physics against the parameter it costs.

    One-mode candidates only: an N-mode variant needs cross-component ties that do not exist yet
    (deferred, C2)."""
    from isar.analyze import catalogue

    profile = profile or catalogue.DEFAULT_PROFILE
    tasks, meta = [], []
    for result, hyp in pairs:
        if not result.optimizer_converged or not result.modes:
            continue                                   # nothing fitted, or a phonon-less null
        prefixes = [n.rsplit(".", 1)[0] for n in result.params if n.endswith(".Omega_d")]
        if len(prefixes) != 1:
            continue
        # Variants come from the entry that OWNS the candidate's identity: offering another
        # entry's provision would price physics the catalogue never declared there (an `@optic`
        # twin carrying the acoustic near-degenerate pins). An entry declaring none offers none.
        entry = catalogue.identity_entry(hyp.identity, profile)
        if entry is None:
            # Identity-agnostic — every production candidate today, since no entry declares a
            # prior yet. The acoustic entry stays the source deliberately: the near-degenerate
            # provision is acoustic physics offered generically until identity splitting
            # activates. Production behaviour is unchanged by the lookup above.
            entry = catalogue.entry("acoustic", profile)
        for variant in entry.constrained_variants:
            if not variant.when(ctx, result.omega0):
                continue
            hard, origins = variant.pins(ctx, prefixes[0])
            derived = _variant_context(ctx, hard, origins,
                                       variant=variant.name, label=hyp.label)
            if derived is None:
                continue
            twin = replace(hyp, label=hyp.label + variant.label_suffix, context=derived)
            tasks.append((twin, derived, backend, minos, dict(result.params)))
            meta.append((twin, derived, variant.name, dict(result.params)))
    if not tasks:
        return []
    fitted = _fit_hypothesis_batch(tasks, executor, phase="catalogue-variant candidate")
    offered = []
    for result, (twin, derived, name, x0) in zip(fitted, meta):
        result.variant = name
        offered.append((result, twin, derived, x0))
    return offered


def _enumerate_and_fit(ctx, *, backend="iminuit", minos=False, peaks=None, executor=None,
                       route_out=None, rung1_out=None, mode_ceiling=None, structure_hint=None):
    """Rung 1 of the count ladder (:func:`_rung1_ladder`; ``peaks`` are the declared seeds), then
    the catalogue's constrained variants of the one-mode representatives. Returns
    ``[(result, hypothesis, context), ...]``: every count's representative, then the variants. The
    context is ``ctx`` for ordinary candidates and the variant's derived context for a variant —
    carry it, do not re-derive it. Split out of :func:`fit_structures_two_stage` so the
    hinted fit (:func:`fit_structures_two_stage_hinted`) runs the same ladder.

    ``route_out``: an optional dict filled with ``{"multimode_route": "count_ladder"}``.
    ``rung1_out``: an optional dict filled with ``{"rung1": Rung1Record}`` (variants included)."""
    record = _rung1_ladder(ctx, backend=backend, minos=minos, peaks=peaks, executor=executor,
                           mode_ceiling=mode_ceiling, structure_hint=structure_hint)
    triples = [(model.representative, model.hypothesis, ctx) for model in record.models]
    one_pairs = [(model.representative, model.hypothesis)
                 for model in record.models if model.count == 1]
    for result, twin, derived, x0 in _offer_variants(one_pairs, ctx, backend=backend, minos=minos,
                                                     executor=executor):
        triples.append((result, twin, derived))
        record.models.append(Rung1Model(
            label=str(twin.label), count=1, elastic="elastic" in str(twin.label).split("+"),
            quasielastic="qe" in str(twin.label).split("+"), variant=result.variant,
            hypothesis=twin, starts=[_start("variant", {}, result, x0)], representative_start=0,
            at_upper_bound=_at_upper_bound(result, derived)))
    if route_out is not None:
        route_out["multimode_route"] = "count_ladder"
    if rung1_out is not None:
        rung1_out["rung1"] = record
    return triples


def fit_structures_two_stage(ctx, *, backend="iminuit", minos=False, peaks=None, executor=None,
                             hypotheses_out=None, route_out=None, rung1_out=None,
                             mode_ceiling=None) -> list[StructureFitResult]:
    """The one structure fit: rung 1 of the count ladder (:func:`_rung1_ladder`) — every count
    0..``mode_ceiling`` with and without the elastic line, several starts each, the
    highest-likelihood start per model — plus the catalogue's constrained variants, returned as
    one list for a single :func:`~isar.analyze.select.select_models` pass, whose evidence
    posterior (equal prior weight per candidate) weighs the counts. ``peaks`` (the card's declared
    ``[fitting]`` peaks) seed every count; this function never imports session/guideline code
    beyond the ceiling's environment default (:func:`~isar.analyze.phonon.default_mode_ceiling`).
    Expensive errors are not attached here: the delivery attaches them to the selected model.

    ``hypotheses_out``: an optional dict filled with ``{label: hypothesis}`` for every candidate
    actually offered. A caller that re-enumerates the hypothesis set by hand cannot see the
    catalogue variants (they exist only because a fit came out a certain way), and a variant
    carries the derived context its evidence must be resolved under — so any caller that needs
    label → hypothesis must take it from here rather than rebuild it.

    ``route_out`` / ``rung1_out``: see :func:`_enumerate_and_fit`."""
    triples = _enumerate_and_fit(
        ctx, backend=backend, minos=minos, peaks=peaks, executor=executor, route_out=route_out,
        rung1_out=rung1_out, mode_ceiling=mode_ceiling)
    if hypotheses_out is not None:
        hypotheses_out.update({h.label: h for _, h, _ in triples})
    return [r for r, _, _ in triples]


def fit_structures_two_stage_hinted(ctx, *, backend="iminuit", minos=False, peaks=None,
                                    structure_hint=None, executor=None,
                                    hypotheses_out=None, route_out=None, rung1_out=None,
                                    mode_ceiling=None) -> tuple[list[StructureFitResult], bool]:
    """:func:`fit_structures_two_stage` with an optional ``structure_hint`` (a master refit's
    previous winning label, card policy ``[fitting].reuse_master_structure``). Returns
    ``(results, structure_skipped)``; ``structure_skipped`` is always ``False``, kept so the
    delivery's provenance fields keep their shape.

    The hint is a seed source, like the declared peaks: the hinted structure gets one extra start
    on its own count and variant. The ladder always runs every count, because a hint that
    short-circuited it could never find a new peak in a refitted master. With no hint (every
    non-master path) this is bit-for-bit :func:`fit_structures_two_stage`. ``hypotheses_out``,
    ``route_out`` and ``rung1_out`` behave as there."""
    triples = _enumerate_and_fit(
        ctx, backend=backend, minos=minos, peaks=peaks, executor=executor, route_out=route_out,
        rung1_out=rung1_out, mode_ceiling=mode_ceiling, structure_hint=structure_hint)
    if hypotheses_out is not None:
        hypotheses_out.update({h.label: h for _, h, _ in triples})
    return [r for r, _, _ in triples], False
