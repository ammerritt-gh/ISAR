"""Producers of the two published predictive side artifacts (Qt-free, session-owned).

Split out of :mod:`isar.session.driver` by ADR-019 so the science and the predictive publication
have separate failure modes. Two artifacts, deliberately separate:

* ``isar.master_predictive/2`` (:func:`build_master_predictive`) is **physical only** — model
  manifests, named parameter draws, expected counts on the fitted grid, the fit-local
  ``omega0_draws`` the outer action scorer consumes, and the per-draw ``mode_energies`` a later
  projection selects any mode from without a refit. It carries no target state, because at the
  moment a fit finishes the target is not yet known: the ``signal_unmodeled`` conversion, branch
  triage's power to withhold a delivery, and the connected tracker's reassignment all still lie
  ahead.
* ``isar.target_projection/2`` (:func:`build_target_projection`) is resolved AFTER those three, and
  is bound to the authority basis that decided it (master revision, track revision, candidate
  identity and state, calibration/policy fingerprints). A consumer that finds the authority basis
  stale must re-resolve the projection — never re-use it, and never re-fit for it.

Producer isolation is the other half of ADR-019: the caller invokes these inside their own
``try``/``except`` and records a structured ``predictive_unavailable`` reason. A predictive
construction failure must leave a valid scientific fit, never a ``non_result``.
"""
from __future__ import annotations

import logging
import math
from collections.abc import Mapping

import numpy as np

from isar.analyze import model_manifest as manifest_mod
from isar.predictive import (PREDICTIVE_ARTIFACT_SCHEMA, PREDICTIVE_CONTRACT,
                             PREDICTIVE_MAX_DROPPED_DRAW_MASS,
                             PREDICTIVE_MAX_PRUNED_MASS, PREDICTIVE_MODEL_WEIGHT_FLOOR,
                             TARGET_PROJECTION_SCHEMA, TARGET_PROJECTION_VERSION, TARGET_QOI,
                             predictive_basis_fingerprint,
                             target_projection_set_fingerprint as _target_projection_set_fingerprint)
from isar.predictive import predictive_score as _predictive_score
from isar.schema.statistics import (
    PREDICTIVE_SCHEMA,
    SemanticsConstructionError,
    authority_scopes,
    semantics_fingerprint,
    validate_for_write,
)

log = logging.getLogger("isar.session.predictive_artifact")

_PREDICTIVE_SCORE_SEED = 940_301

# ΣM_max/ΣM_min − 1 below this is one exposure (EIG-0 §6). It gates only the SCALAR fitted-grid
# parity claim; forward evaluation of a new uniform-exposure candidate is valid on every pool.
EXPOSURE_HOMOGENEITY_TOLERANCE = 1e-12

#: Absolute FLOOR for the equidistance tolerance below. It is not the tolerance itself: an absolute
#: 1e-9 meV means "these two modes are the same distance away" is decided at a precision no
#: measurement has, so a genuine numerical tie at a 0.4 meV resolution was resolved by a coin flip.
MODE_AMBIGUITY_TOLERANCE_MEV = 1e-9

#: The equidistance tolerance as a fraction of the artifact's Gaussian resolution FWHM. Small enough
#: that two modes it merges are indistinguishable by any float bookkeeping the fit does (4e-7 meV at
#: a 0.4 meV FWHM), never small enough to be a bare double-precision comparison.
MODE_AMBIGUITY_FWHM_FRACTION = 1e-6

#: How far the authoritative reference energy may sit from the mode a draw offers before that draw is
#: held to carry no target mode at all, in multiples of the artifact's Gaussian resolution FWHM.
#:
#: Nearest-energy selection with no distance gate is *unconditional*: it returns the closest mode
#: however far away it is, so an authoritative ω₀ of 99.0 meV projected onto modes at {2.0, 5.0} was
#: published as ``complete=true`` naming the 5.0 mode — a confident answer about a target this model
#: does not contain. Three FWHM is the same "outside the resolution function" line the fitting side
#: uses to call two features separate: inside it the reference and the mode are one feature the
#: instrument cannot split, outside it they are different physics and the projection must say so.
MODE_SUPPORT_FWHM_MULTIPLE = 3.0

#: Structured reasons a target projection is incomplete. A consumer routes on these, never on text.
PROJECTION_DESCRIPTOR_ONLY = "descriptor_only"
PROJECTION_DELIVERY_WITHHELD = "withheld_delivery"
PROJECTION_AUXILIARY_ONLY = "auxiliary_only_selection"
PROJECTION_AMBIGUOUS_TRACK = "ambiguous_track"
PROJECTION_NO_REFERENCE = "no_reference_target_energy"
PROJECTION_NO_UNIQUE_MODE = "no_unique_mode"
PROJECTION_NO_TARGET_MODE = "no_retained_model_carries_the_target_mode"
PROJECTION_REFERENCE_BEYOND_SUPPORT = "reference_beyond_mode_support"
#: The artifact declares no usable resolution width, so the support distance below has no scale to be
#: measured in. Fail CLOSED: a published ``/2`` always carries a positive ``gaussian_fwhm_meV`` (the
#: strict validator refuses otherwise), so this state means the basis is not a valid one — and the
#: alternative, disabling the gate, would silently restore the unconditional nearest-mode rule for
#: exactly the artifacts least entitled to it.
PROJECTION_NO_RESOLUTION_BASIS = "no_resolution_basis_for_mode_support"
#: The projected model set could not be fingerprinted (a malformed or legacy record: no label, no
#: finite positive weight, no draw list). The projection is then incomplete like any other blocked
#: one — the alternative, a record carrying ``complete=true`` with no fingerprint and no semantics,
#: reaches a consumer as a *corrupt* record rather than an honest non-result.
PROJECTION_FINGERPRINT_FAILED = "target_projection_set_fingerprint_failed"

#: Structured reasons the predictive side produced nothing for a master that otherwise fitted fine.
PREDICTIVE_UNAVAILABLE_CONSTRUCTION_FAILED = "predictive_construction_failed"
PREDICTIVE_UNAVAILABLE_NO_RETAINED_MODELS = "no_retained_models"
PROJECTION_UNAVAILABLE_CONSTRUCTION_FAILED = "target_projection_construction_failed"

#: Withholding reasons the driver may publish that this artifact republishes verbatim. Anything
#: outside it becomes the generic ``withheld_delivery`` rather than silently widening this
#: vocabulary with a string a consumer has no rule for.
_WITHHOLDING_REASONS = frozenset({PROJECTION_AUXILIARY_ONLY})


# ---------------------------------------------------------------------------
# Fit inspection
# ---------------------------------------------------------------------------
def model_manifest_of(best, by_hyp) -> dict | None:
    """Declarative ``isar.model_manifest/1`` payload for the delivered structure.

    The manifest intentionally names only component kinds owned by ISAR.  It is sufficient for a
    Qt-free reader to reconstruct multi-mode curves without importing a hypothesis class or solving
    amplitudes again.  An unknown future component is omitted by returning ``None`` rather than
    publishing a plausible but incomplete model.
    """
    if best is None:
        return None
    hyp = by_hyp.get(best.label)
    if hyp is None:
        return None
    from isar.analyze.components import (ConstantBackground, Elastic, PhononDHO,
                                         QuasiElastic)
    kinds = {PhononDHO: "phonon_dho", Elastic: "elastic",
             QuasiElastic: "quasielastic", ConstantBackground: "constant_background"}
    components = []
    composite = hyp.build()
    for component in composite.components:
        kind = kinds.get(type(component))
        if kind is None:
            return None
        item = {"type": kind, "prefix": str(component.prefix)}
        if isinstance(component, PhononDHO):
            item["peak"] = str(component.peak)
        components.append(item)
    parameters = {name: float(best.params[name]) for name in composite.param_names()
                  if name in best.params}
    if set(parameters) != set(composite.param_names()):
        return None
    return {"schema": "isar.model_manifest/1", "components": components,
            "parameters": parameters}


# Every draw basis ISAR produces is an EQUAL-WEIGHT particle set, verified at each source:
#   nested_sampling      -- dynesty's importance weights are consumed by ``resample_equal`` before
#                           the samples are stored (isar/analyze/errors/bayes.py:170-190);
#   parametric_bootstrap -- one replicate = one equal-weight particle
#                           (isar/analyze/errors/bootstrap.py:276-289);
#   laplace_hesse        -- i.i.d. ``multivariate_normal`` draws (below).
# ADR-014 requires the measure to be declared rather than assumed, so each model publishes this
# declaration; a future weighted basis publishes explicit ``draw_weights`` instead.
DRAW_WEIGHTING = {
    "nested_sampling": "equal_weight_resampled",
    "parametric_bootstrap": "equal_weight_resampled",
    "laplace_hesse": "equal_weight_resampled",
}

_PARTICLE_KIND_BY_BASIS = {
    "nested_sampling": "optimizer_bound_nested_parameter_distribution",
    "parametric_bootstrap": "parametric_bootstrap_sampling_distribution",
    "laplace_hesse": "laplace_hesse_parameter_approximation",
}


def _bootstrap_kind(estimator_kind: str) -> str:
    return {
        "data_likelihood_mle": "unconstrained_parametric_bootstrap",
        "soft_penalized_likelihood": "soft_penalized_parametric_bootstrap",
        "hard_conditioned_likelihood": "hard_conditioned_parametric_bootstrap",
    }[estimator_kind]


def _predictive_statistical_semantics(models, fits, fit_selection_semantics, *, complete: bool):
    """Build additive ensemble metadata; return ``(record, by_label)`` or ``(None, {})``."""
    try:
        if fit_selection_semantics is None:
            raise SemanticsConstructionError("missing_fit_selection_semantics")
        required_fingerprints = (
            "model_selection_fingerprint",
            "offered_model_set_fingerprint",
            "scored_model_set_fingerprint",
            "weight_denominator_fingerprint",
        )
        missing = [name for name in required_fingerprints if not fit_selection_semantics.get(name)]
        if missing:
            raise SemanticsConstructionError(
                "missing_selection_fingerprint", ",".join(missing)
            )
        model_weight_kind = fit_selection_semantics.get("model_weight_kind")
        if model_weight_kind is None:
            raise SemanticsConstructionError("missing_model_weight_kind")
        retained_payload = {
            "retained_source_weight_total": float(
                sum(float(model["source_weight"]) for model in models)
            ),
            "published_models": sorted([
                {
                    "label": str(model["label"]),
                    "source_weight": float(model["source_weight"]),
                    "published_weight": float(model["weight"]),
                }
                for model in models
            ], key=lambda item: item["label"]),
        }
        retained_fingerprint = semantics_fingerprint(
            "isar.predictive_retained_model_set/1", retained_payload
        )
        per_model = {}
        particle_kinds = []
        for model in models:
            label = model["label"]
            result = fits.get(label)
            fit_semantics = getattr(result, "fit_semantics", None)
            if fit_semantics is None:
                raise SemanticsConstructionError("missing_candidate_fit_semantics", str(label))
            basis = str(model["draw_basis"])
            particle_kind = _PARTICLE_KIND_BY_BASIS[basis]
            particle_kinds.append(particle_kind)
            item = {
                "label": str(label),
                "source_model_selection_fingerprint": fit_selection_semantics[
                    "model_selection_fingerprint"
                ],
                "offered_model_set_fingerprint": fit_selection_semantics[
                    "offered_model_set_fingerprint"
                ],
                "scored_model_set_fingerprint": fit_selection_semantics[
                    "scored_model_set_fingerprint"
                ],
                "weight_denominator_fingerprint": fit_selection_semantics[
                    "weight_denominator_fingerprint"
                ],
                "fit_semantics": fit_semantics,
                "selection_eligibility": fit_semantics["selection_eligibility"],
                "conditioning_fingerprint": fit_semantics["conditioning_fingerprint"],
                "model_weight_kind": model_weight_kind,
                "particle_measure_kind": particle_kind,
                "draw_measure_kind": "equal_weight_empirical_particles",
                "draw_basis": basis,
            }
            if basis == "nested_sampling":
                prior_fingerprint = (
                    (getattr(result, "within_model", None) or {}).get(
                        "bayes_prior_fingerprint"
                    )
                )
                if prior_fingerprint is None:
                    raise SemanticsConstructionError(
                        "missing_bayesian_prior_fingerprint", str(label)
                    )
                item.update(
                    {
                        "inference_paradigm": "bayesian_posterior",
                        "prior_kind": "optimizer_bound_data_dependent",
                        "prior_data_dependent": True,
                        "prior_fingerprint": prior_fingerprint,
                        "posterior_measure_kind": "posterior_resample",
                        "evidence_comparable": False,
                        "bayesian_model_average": False,
                    }
                )
            elif basis == "parametric_bootstrap":
                item.update(
                    {
                        "bootstrap_measure_kind": _bootstrap_kind(
                            fit_semantics["estimator_kind"]
                        ),
                        "inference_paradigm": "frequentist_sampling_distribution",
                        "prior_kind": "none",
                        "prior_data_dependent": False,
                        "posterior_measure_kind": None,
                        "evidence_comparable": False,
                        "bayesian_model_average": False,
                    }
                )
            else:
                item.update(
                    {
                        "inference_paradigm": "frequentist_asymptotic",
                        "prior_kind": "none",
                        "prior_data_dependent": False,
                        "posterior_measure_kind": None,
                        "evidence_comparable": False,
                        "bayesian_model_average": False,
                    }
                )
            per_model[str(label)] = item
        distinct_particles = sorted(set(particle_kinds))
        ensemble_particle_kind = (
            distinct_particles[0]
            if len(distinct_particles) == 1
            else "heterogeneous_model_dependent"
        )
        ensemble_inference_paradigm = (
            "mixed" if len(distinct_particles) > 1 else
            next(iter(per_model.values()))["inference_paradigm"]
        )
        record = {
            "schema": PREDICTIVE_SCHEMA,
            "artifact_kind": "master_predictive",
            "source_model_selection_fingerprint": fit_selection_semantics[
                "model_selection_fingerprint"
            ],
            "offered_model_set_fingerprint": fit_selection_semantics[
                "offered_model_set_fingerprint"
            ],
            "scored_model_set_fingerprint": fit_selection_semantics[
                "scored_model_set_fingerprint"
            ],
            "weight_denominator_fingerprint": fit_selection_semantics[
                "weight_denominator_fingerprint"
            ],
            "predictive_retained_set_fingerprint": retained_fingerprint,
            "model_weight_kind": model_weight_kind,
            "particle_measure_kind": ensemble_particle_kind,
            "actual_particle_measure_kind": ensemble_particle_kind,
            "draw_measure_kind": "equal_weight_empirical_particles",
            "actual_particle_basis_homogeneous": len(distinct_particles) == 1,
            "contract_permits_heterogeneous_fallback": True,
            "inference_paradigm": ensemble_inference_paradigm,
            "formal_bayesian_model": False,
            "construction_complete": bool(complete),
            "homogeneous_design_eligible": False,
            "homogeneous_design_ineligibility_reasons": [
                "predictive_contract_permits_heterogeneous_model_dependent_fallback"
            ],
            "quantities": {
                "model_interval": {
                    "interval_kind": "conditional_target_present_mixed_particle_quantile",
                    "particle_measure_kind": ensemble_particle_kind,
                    "actual_particle_measure_kind": ensemble_particle_kind,
                    "model_weight_kind": model_weight_kind,
                    "inference_paradigm": ensemble_inference_paradigm,
                    "contract_permits_heterogeneous_fallback": True,
                    "formal_bayesian_model": False,
                    "conditioning": "target_present_models_only",
                    "authority_scopes": authority_scopes(
                        legacy_master_certification="active",
                        stopping="active",
                        user_diagnostic="active",
                        eig_shadow="ineligible",
                        eig_authoritative="ineligible",
                        acquisition_selection="ineligible",
                    ),
                }
            },
            "models": [per_model[str(model["label"])] for model in models],
            "authority_scopes": authority_scopes(
                legacy_master_certification="active",
                stopping="active",
                eig_shadow="ineligible",
                eig_authoritative="ineligible",
                acquisition_selection="ineligible",
                user_diagnostic="active",
            ),
        }
        validated = validate_for_write(record, PREDICTIVE_SCHEMA)
        validated["statistical_semantics_fingerprint"] = semantics_fingerprint(
            "isar.predictive_statistical_semantics/1", validated
        )
        validated = validate_for_write(validated, PREDICTIVE_SCHEMA)
        return validated, per_model
    except (SemanticsConstructionError, KeyError) as exc:
        reason_code = (
            exc.reason_code if isinstance(exc, SemanticsConstructionError)
            else "missing_predictive_semantics_field"
        )
        log.warning(
            "semantics_construction_failed producer=isar.session.predictive_artifact "
            "quantity=master_predictive reason_code=%s",
            reason_code,
        )
        return None, {}


def bounded_parameter_draws(result, *, seed: int, maximum: int = 64):
    """Strongest valid full-parameter draw basis for one fitted candidate.

    Returns ``(names, samples, basis, diagnostics)``. ``diagnostics`` is the ADR-014 draw-level
    completeness record: how many draws the source produced, which were dropped and why, and the
    retained probability mass. Thinning an equal-weight ensemble to the draw cap is a deterministic
    representative subsample, not lost mass, so it is reported separately from ``dropped_draws``.
    """
    within = result.within_model or {}
    if within.get("bayes_ok") and within.get("bayes_parameter_samples") is not None:
        names = tuple(within.get("bayes_parameter_names") or ())
        samples = np.asarray(within["bayes_parameter_samples"], dtype=float)
        basis = "nested_sampling"
    elif within.get("parameter_samples") is not None:
        names = tuple(within.get("parameter_names") or ())
        samples = np.asarray(within["parameter_samples"], dtype=float)
        basis = "parametric_bootstrap"
    elif result.covariance_ok and result.parameter_covariance is not None:
        names = tuple(result.params)
        mean = np.asarray([result.params[name] for name in names], dtype=float)
        covariance = np.asarray(result.parameter_covariance, dtype=float)
        try:
            samples = np.random.default_rng(int(seed)).multivariate_normal(
                mean, covariance, size=int(maximum), check_valid="raise")
        except (ValueError, np.linalg.LinAlgError):
            return None
        basis = "laplace_hesse"
    else:
        return None
    if samples.ndim != 2 or samples.shape[1] != len(names) or samples.shape[0] == 0:
        return None
    source_count = int(samples.shape[0])
    finite = np.all(np.isfinite(samples), axis=1)
    dropped = [{"id": f"draw-{index}", "reason": "nonfinite_parameter_draw"}
               for index in range(source_count) if not bool(finite[index])]
    source_ids = [f"draw-{index}" for index in range(source_count) if bool(finite[index])]
    samples = samples[finite]
    if samples.shape[0] == 0:
        return None
    thinned_from = int(samples.shape[0])
    if samples.shape[0] > maximum:
        keep = np.linspace(0, samples.shape[0] - 1, maximum, dtype=int)
        samples = samples[keep]
        source_ids = [source_ids[index] for index in keep]
    for column, name in enumerate(names):
        if name.endswith((".Omega_d", ".Gamma", ".A", ".E", ".QE", ".BG")):
            samples[:, column] = np.maximum(samples[:, column], 0.0)
    diagnostics = {
        "draw_weighting": DRAW_WEIGHTING[basis],
        "draw_weights": None,
        "draw_ids": source_ids,
        "source_draw_count": source_count,
        "retained_draw_count": int(samples.shape[0]),
        "dropped_draws": dropped,
        "draw_mass_retained": float(thinned_from) / float(source_count),
        "draw_cap": int(maximum),
        "thinned_from_draw_count": thinned_from,
    }
    return names, samples, basis, diagnostics


# ---------------------------------------------------------------------------
# isar.master_predictive/2 (physical only)
# ---------------------------------------------------------------------------
def exposure_basis(ctx, n_members: int) -> tuple[dict, np.ndarray]:
    """``(exposure_basis, stored_exposure_ratio)`` for one pooled master's fit context.

    The basis is read from the fit's OWN context — ``exposure_reference`` is the value
    :func:`~isar.analyze.context.normalized_exposure` returned for this fit and
    ``exposure_scale`` the per-bin relative exposure it fitted with — never recomputed from the
    pooled spectrum (EIG-0 §4.5: the median reference is a rank statistic and moves with the kept
    set). ``per_energy_monitor`` is ΣM aligned to the fitted grid.

    ``stored_exposure_ratio`` is the exact multiplier used for the stored ``expected_counts``: the
    per-member incremental exposure ΣM_i/(n_members · E_ref), which keeps the /1 meaning of the
    stored matrices ("the counts one further member-equivalent scan would see"). A master with no
    usable monitor has no exposure basis at all (EIG-0 §4.6) and fits on the uniform path; it
    publishes null exposure fields and the evaluator refuses it with ``missing_exposure_basis``.
    """
    grid = np.asarray(ctx.x_used, dtype=float)
    scale = ctx.exposure_used if getattr(ctx, "exposure_scale", None) is not None else None
    reference = getattr(ctx, "exposure_reference", None)
    basis = {
        "unit": "monitor_counts",
        "request_unit": "number_neutrons",
        # TAVI's realized per-point monitor is identically the requested number_neutrons (EIG-0
        # §4.1/§5). The seam is declared even though the map is the identity today, and the
        # evaluator applies it rather than assuming it (ADR-020).
        "request_to_exposure_factor": 1.0,
        "n_members": int(n_members),
        "kept_point_count": int(grid.size),
    }
    if scale is None or reference is None or not math.isfinite(float(reference)) \
            or float(reference) <= 0.0:
        basis.update({
            "fit_reference_exposure": None, "reference_incremental_exposure": None,
            "per_energy_monitor": None, "scalar_exposure_eligible": False,
            "exposure_homogeneous": True, "full_member_support": None,
            "exposure_ratio_span": None,
        })
        return basis, np.full(grid.shape, 1.0 / float(n_members))
    reference = float(reference)
    scale = np.asarray(scale, dtype=float)
    monitor = scale * reference
    span = (float(np.max(monitor) / np.min(monitor))
            if monitor.size and float(np.min(monitor)) > 0.0 else None)
    homogeneous = span is not None and (span - 1.0) <= EXPOSURE_HOMOGENEITY_TOLERANCE
    # Per-bin member support is not carried on the pooled spectrum, so it is reported only where it
    # is known (a single-member pool supports every kept bin). The operative eligibility predicate
    # is the ΣM profile itself (EIG-0 §6), which separates the partial-support cases as well.
    full_support = True if int(n_members) == 1 else None
    basis.update({
        "fit_reference_exposure": reference,
        # A MEAN over members, not any member's request (EIG-0 §5): a consumer must not read it as
        # "the exposure of one scan". Its contractual role is the scalar fitted-grid parity check.
        "reference_incremental_exposure": reference / float(n_members),
        "per_energy_monitor": monitor.tolist(),
        "scalar_exposure_eligible": bool(homogeneous and full_support is not False),
        "exposure_homogeneous": bool(homogeneous),
        "full_member_support": full_support,
        "exposure_ratio_span": span,
    })
    return basis, scale / float(n_members)


def build_master_predictive(best, scores, fits, by_hyp, ctx, *, seed: int,
                            n_members: int, fit_selection_semantics: dict | None = None
                            ) -> dict | None:
    """Bounded model-weighted full-parameter ensemble retained with a master revision.

    Publishes ``isar.master_predictive/2``: the same draws the fit produced, plus everything needed
    to re-evaluate them away from the fitted grid (the grid itself, the evaluation context, the
    exposure basis) and everything needed to know whether the published probability measure is
    complete enough to score (ADR-014). Every stored matrix is produced by the shared evaluation
    path, so a stored prediction and an arbitrary-grid prediction cannot disagree.

    ``omega0_draws`` is unchanged from /1: the fitted mode nearest the fit's own delivered ω₀, which
    is what the outer action scorer :func:`isar.predictive.predictive_score` ranks on. It is a
    fit-local descriptor, NOT the campaign target — the authoritative target projection is
    :func:`build_target_projection`, resolved later against ``mode_energies``.
    """
    grid = np.asarray(ctx.x_used, dtype=float)
    evaluation_context = {
        "center_meV": float(ctx.center),
        "gaussian_fwhm_meV": float(ctx.gauss_fwhm),
        "lorentzian_fwhm_meV": float(ctx.lorentz_fwhm),
        "temperature_K": float(ctx.T),
        "quasielastic_fwhm_meV": (None if getattr(ctx, "quasielastic_fwhm", None) is None
                                  else float(ctx.quasielastic_fwhm)),
    }
    basis_values, stored_ratio = exposure_basis(ctx, max(int(n_members), 1))
    basis_values["stored_exposure_ratio"] = [float(value) for value in stored_ratio]
    eval_ctx = manifest_mod.EvaluationContext(
        center=evaluation_context["center_meV"],
        gauss_fwhm=evaluation_context["gaussian_fwhm_meV"],
        lorentz_fwhm=evaluation_context["lorentzian_fwhm_meV"],
        T=evaluation_context["temperature_K"],
        quasielastic_fwhm=evaluation_context["quasielastic_fwhm_meV"],
        exposure_ratio=stored_ratio)
    try:
        point_omega = float(best.omega0) if best is not None else float("nan")
    except (TypeError, ValueError):
        point_omega = float("nan")
    local_reference = point_omega if math.isfinite(point_omega) else None

    models = []
    dropped_models = []
    weights = {score.label: float(score.weight) for score in scores}
    source_weight_total = sum(weight for weight in weights.values() if weight > 0.0)
    for model_index, label in enumerate(sorted(weights)):
        result = fits.get(label)
        weight = weights[label]
        if result is None or by_hyp.get(label) is None or weight <= 0.0:
            if weight > 0.0:
                dropped_models.append({"label": label, "original_weight": weight,
                                       "reason": "no_fitted_candidate"})
            continue
        draws = bounded_parameter_draws(result, seed=seed + 7919 * (model_index + 1))
        manifest = model_manifest_of(result, by_hyp)
        if draws is None:
            dropped_models.append({"label": label, "original_weight": weight,
                                   "reason": "no_usable_parameter_draws"})
            continue
        if manifest is None:
            dropped_models.append({"label": label, "original_weight": weight,
                                   "reason": "unsupported_model_manifest"})
            continue
        names, samples, draw_basis, draw_diagnostics = draws
        try:
            composite = manifest_mod.build_composite(
                manifest, evaluation_context["quasielastic_fwhm_meV"])
            phonon_indices = composite.phonon_indices()
            expected, omega0_draws, mode_energies = [], [], []
            for sample in samples:
                theta = manifest_mod.draw_theta(composite, names, sample)
                expected.append(manifest_mod.expected_counts(
                    manifest, names, sample, eval_ctx, grid).tolist())
                candidates = [math.sqrt(theta[i] ** 2 + theta[i + 1] ** 2)
                              for i in phonon_indices]
                # Every fitted mode of this draw, in composite order: the complete physical menu a
                # later projection selects from without re-running the fit.
                mode_energies.append([float(value) for value in candidates])
                if candidates and local_reference is not None:
                    omega0_draws.append(
                        min(candidates, key=lambda value: abs(value - local_reference)))
        except manifest_mod.ModelManifestError as error:
            # Fail closed: a model whose declared draws cannot be mapped onto its own manifest is
            # dropped with its reason recorded, never evaluated positionally as a guess.
            log.warning("predictive model %s refused (%s): %s", label, error.reason_code, error)
            dropped_models.append({"label": label, "original_weight": weight,
                                   "reason": error.reason_code})
            continue
        models.append({
            "label": label,
            # ADR-009: an additive seam for the parallel physics workstream. The fit-model label is
            # never the rewarded identity, so this defaults to the label and earns no reward until a
            # real per-particle identity projection exists.
            "identity_label": label,
            "source_weight": weight, "weight": weight, "draw_basis": draw_basis,
            "draw_weighting": draw_diagnostics["draw_weighting"],
            "draw_weights": draw_diagnostics["draw_weights"],
            "draw_ids": draw_diagnostics["draw_ids"],
            "parameter_names": list(names), "parameter_draws": samples.tolist(),
            "model_manifest": manifest, "omega0_draws": omega0_draws,
            "mode_energies": mode_energies,
            "expected_counts": expected,
            "source_draw_count": draw_diagnostics["source_draw_count"],
            "retained_draw_count": draw_diagnostics["retained_draw_count"],
            "dropped_draws": draw_diagnostics["dropped_draws"],
            "draw_mass_retained": draw_diagnostics["draw_mass_retained"],
            "draw_cap": draw_diagnostics["draw_cap"],
            "thinned_from_draw_count": draw_diagnostics["thinned_from_draw_count"],
        })
    if not models:
        return None
    retained_weight_total = sum(model["source_weight"] for model in models)
    for model in models:
        model["weight"] = model["source_weight"] / retained_weight_total
    pruned_mass = max(source_weight_total - retained_weight_total, 0.0)
    complete = (
        all(item["original_weight"] < PREDICTIVE_MODEL_WEIGHT_FLOOR for item in dropped_models)
        and pruned_mass <= PREDICTIVE_MAX_PRUNED_MASS
        and all(1.0 - model["draw_mass_retained"] <= PREDICTIVE_MAX_DROPPED_DRAW_MASS
                for model in models))
    completeness = {
        "source_model_weight_total": float(source_weight_total),
        "retained_model_weight_total": float(retained_weight_total),
        "pruned_model_weight": float(pruned_mass),
        "dropped_models": dropped_models,
        "model_weight_floor": PREDICTIVE_MODEL_WEIGHT_FLOOR,
        "max_pruned_mass": PREDICTIVE_MAX_PRUNED_MASS,
        "max_dropped_draw_mass": PREDICTIVE_MAX_DROPPED_DRAW_MASS,
        "predictive_complete_for_eig": bool(complete),
    }
    omega_values, omega_weights = [], []
    for model in models:
        draws = [float(value) for value in model["omega0_draws"] if math.isfinite(float(value))]
        if not draws:
            continue
        omega_values.extend(draws)
        omega_weights.extend([float(model["weight"]) / len(draws)] * len(draws))
    omega_interval = None
    if omega_values:
        order = np.argsort(np.asarray(omega_values, dtype=float))
        values = np.asarray(omega_values, dtype=float)[order]
        weights_array = np.asarray(omega_weights, dtype=float)[order]
        cumulative = np.cumsum(weights_array)
        cumulative /= cumulative[-1]
        omega_interval = [float(np.interp(q, cumulative, values)) for q in (0.16, 0.5, 0.84)]
    model_entropy = -sum(float(model["weight"]) * math.log(max(float(model["weight"]), 1e-300))
                         for model in models)
    diagnostics = _predictive_score(models, seed=_PREDICTIVE_SCORE_SEED, n_simulations=128)
    statistical_semantics, per_model_semantics = _predictive_statistical_semantics(
        models,
        fits,
        fit_selection_semantics,
        complete=bool(complete),
    )
    for model in models:
        model["statistical_semantics"] = per_model_semantics.get(str(model["label"]))
    return {"schema": PREDICTIVE_ARTIFACT_SCHEMA,
            # Self-description of the payload SHAPE under that schema label; it is hashed into the
            # basis fingerprint, so the two can never drift apart.
            "contract": PREDICTIVE_CONTRACT,
            "models": models,
            "evaluator_version": manifest_mod.EVALUATOR_VERSION,
            "negative_prediction_eps": manifest_mod.NEGATIVE_PREDICTION_EPS,
            "negative_prediction_eps_rule": manifest_mod.NEGATIVE_PREDICTION_EPS_RULE,
            "energy_meV": grid.tolist(),
            "evaluation_context": evaluation_context,
            "exposure_basis": basis_values,
            "completeness": completeness,
            "construction_complete": bool(complete),
            "homogeneous_design_eligible": False,
            "homogeneous_design_ineligibility_reasons": [
                "predictive_contract_permits_heterogeneous_model_dependent_fallback"
            ],
            "predictive_retained_set_fingerprint": (
                statistical_semantics.get("predictive_retained_set_fingerprint")
                if statistical_semantics is not None else None
            ),
            "statistical_semantics": statistical_semantics,
            "statistical_semantics_fingerprint": (
                statistical_semantics.get("statistical_semantics_fingerprint")
                if statistical_semantics is not None else None
            ),
            "predictive_basis_fingerprint": predictive_basis_fingerprint(
                models=models, energy_meV=grid.tolist(),
                evaluation_context=evaluation_context, exposure_basis=basis_values),
            "uncertainty_provenance": {
                "draw_basis": sorted(set(model["draw_basis"] for model in models)),
                "deterministic": True, "seed": int(seed)},
            "seed": int(seed), "omega_interval": omega_interval,
            "model_entropy_nats": float(model_entropy),
            # The ensemble itself is master-seeded; the simulated future-count stream deliberately
            # uses one campaign-independent seed so candidates share random numbers during ranking.
            "diagnostics": diagnostics}


# ---------------------------------------------------------------------------
# isar.target_projection/2 (authority-bound, resolved after triage + tracker)
# ---------------------------------------------------------------------------
def _authoritative_reference(outcome) -> float | None:
    """The delivered target energy this row currently stands behind, or ``None``.

    This is the post-triage, post-tracker scalar — the tracker overwrites it with the selected
    candidate's energy, triage nulls it when it withholds the delivery, and the
    ``signal_unmodeled`` conversion nulls it before either. The fit's own local ω₀ is deliberately
    NOT consulted: projecting onto it is exactly the defect ADR-019 repairs.
    """
    try:
        value = float(outcome.get("omega0"))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _incompleteness_reason(outcome) -> str | None:
    """Why this row cannot carry a complete target projection, or ``None`` when it can."""
    if outcome.get("fit_kind") == "descriptor_only" or outcome.get("verdict") == "signal_unmodeled":
        # The descriptor localised real signal the phonon model cannot fit. There is no ω₀
        # measurement here by machine contract, so there is no target mode to project onto.
        return PROJECTION_DESCRIPTOR_ONLY
    if outcome.get("target_delivery_eligible") is False:
        withheld = str(outcome.get("delivery_withheld_reason") or "")
        return withheld if withheld in _WITHHOLDING_REASONS else PROJECTION_DELIVERY_WITHHELD
    if outcome.get("verdict") == "branch_continuation":
        # Branch triage declined to deliver: the row's modes exist but none of them is the target.
        return PROJECTION_DELIVERY_WITHHELD
    if outcome.get("track_revision") is not None \
            and outcome.get("candidate_state") not in ("confirmed", "individual"):
        return PROJECTION_AMBIGUOUS_TRACK
    if outcome.get("track_revision") is not None and outcome.get("selected_candidate_index") is None:
        return PROJECTION_AMBIGUOUS_TRACK
    return None


def mode_selection_rule(artifact) -> dict:
    """The resolution-relative constants the nearest-mode rule is evaluated under, for this artifact.

    Published inside the projection because both are *derived*: a consumer that reads only the
    verdict cannot tell how far "near enough" was, and a later change to either constant must be
    visible in the record it produced. The resolution comes from the artifact's own
    ``evaluation_context`` — the width the fit was actually performed at.

    A missing or unusable width leaves ``resolution_gaussian_fwhm_meV`` and
    ``max_reference_distance_meV`` at ``None``, which the caller reads as
    :data:`PROJECTION_NO_RESOLUTION_BASIS` and refuses on.
    """
    context = artifact.get("evaluation_context")
    fwhm = None
    if isinstance(context, Mapping):
        try:
            candidate = float(context.get("gaussian_fwhm_meV"))
        except (TypeError, ValueError):
            candidate = float("nan")
        if math.isfinite(candidate) and candidate > 0.0:
            fwhm = candidate
    return {
        "resolution_gaussian_fwhm_meV": fwhm,
        "support_fwhm_multiple": MODE_SUPPORT_FWHM_MULTIPLE,
        "max_reference_distance_meV": (None if fwhm is None
                                       else MODE_SUPPORT_FWHM_MULTIPLE * fwhm),
        "ambiguity_fwhm_fraction": MODE_AMBIGUITY_FWHM_FRACTION,
        "ambiguity_tolerance_meV": (MODE_AMBIGUITY_TOLERANCE_MEV if fwhm is None else max(
            MODE_AMBIGUITY_FWHM_FRACTION * fwhm, MODE_AMBIGUITY_TOLERANCE_MEV)),
    }


def _project_model(model, reference: float, rule: dict) -> tuple[list, list, str | None]:
    """``(present_by_draw, qoi_draws, blocking_reason)`` for one model against the authoritative ω₀."""
    present, qoi = [], []
    blocked = None
    max_distance = rule["max_reference_distance_meV"]
    tolerance = rule["ambiguity_tolerance_meV"]
    for row in model.get("mode_energies") or ():
        candidates = [float(value) for value in row]
        if not candidates:
            # A null (no-phonon) model is a legitimate "target absent" state, not a failure.
            present.append(False)
            qoi.append(None)
            continue
        ordered = sorted(candidates, key=lambda value: abs(value - reference))
        if max_distance is None:
            blocked = PROJECTION_NO_RESOLUTION_BASIS
            present.append(False)
            qoi.append(None)
            continue
        if abs(ordered[0] - reference) > max_distance:
            # Checked before the tie test: "the reference is nowhere near any mode this draw offers"
            # is a different and more fundamental failure than "two of them are equally near".
            blocked = blocked or PROJECTION_REFERENCE_BEYOND_SUPPORT
            present.append(False)
            qoi.append(None)
            continue
        if len(ordered) > 1 and abs(abs(ordered[0] - reference) - abs(ordered[1] - reference)) \
                <= tolerance:
            # Two modes equidistant from the authoritative energy: a nearest-energy rule cannot
            # decide which one is the target, and guessing would publish a coin flip as a QoI.
            blocked = blocked or PROJECTION_NO_UNIQUE_MODE
            present.append(False)
            qoi.append(None)
            continue
        present.append(True)
        qoi.append(float(ordered[0]))
    return present, qoi, blocked


def _target_projection_semantics(
    artifact,
    *,
    target_projection_set_fingerprint: str,
    complete: bool,
) -> dict | None:
    try:
        source_retained = artifact.get("predictive_retained_set_fingerprint")
        source_semantics = artifact.get("statistical_semantics_fingerprint")
        source_weight_kind = (artifact.get("statistical_semantics") or {}).get(
            "model_weight_kind"
        )
        if source_retained is None or source_semantics is None or source_weight_kind is None:
            raise SemanticsConstructionError("missing_source_predictive_semantics")
        record = {
            "schema": PREDICTIVE_SCHEMA,
            "artifact_kind": "target_projection",
            "source_predictive_retained_set_fingerprint": source_retained,
            "source_predictive_semantics_fingerprint": source_semantics,
            "target_projection_set_fingerprint": target_projection_set_fingerprint,
            "construction_complete": bool(complete),
            "model_weight_kind": source_weight_kind,
            "inference_paradigm": "mixed",
            "formal_bayesian_model": False,
            "conditioning": "authoritative_target_identity_and_target_present_draws",
            "authority_scopes": authority_scopes(
                eig_shadow="ineligible",
                eig_authoritative="ineligible",
                acquisition_selection="ineligible",
                user_diagnostic="active",
            ),
        }
        validated = validate_for_write(record, PREDICTIVE_SCHEMA)
        validated["statistical_semantics_fingerprint"] = semantics_fingerprint(
            "isar.target_projection_statistical_semantics/1", validated
        )
        return validate_for_write(validated, PREDICTIVE_SCHEMA)
    except SemanticsConstructionError as exc:
        log.warning(
            "semantics_construction_failed producer=isar.session.predictive_artifact "
            "quantity=target_projection reason_code=%s",
            exc.reason_code,
        )
        return None


def model_entropy_probability_semantics(
    predictive_retained_set_fingerprint: str,
    model_weight_kind: str,
) -> dict:
    """Producer-owned declaration for ``model_entropy_nats`` / ``model_entropy``.

    ``model_weight_kind`` is whatever drove selection for the source predictive
    (``posterior_probability`` on the evidence lane, ``akaike_relative_weight`` on the
    recorded AIC fallback) — the entropy is over that distribution, so the declaration
    names it rather than assuming a lane."""
    return {
        "quantity_kind": "entropy",
        "source_measure_semantics": model_weight_kind,
        "state_space": {
            "kind": "predictive_retained_model_set",
            "fingerprint": predictive_retained_set_fingerprint,
        },
        "conditioning": "models_retained_after_draw_and_manifest_pruning",
        "model_weight_kind": model_weight_kind,
        "calibration_reference": None,
        "inference_paradigm": (
            "bayesian_posterior"
            if model_weight_kind == "posterior_probability"
            else "information_criterion_weight_distribution"
        ),
        "authority_scopes": authority_scopes(user_diagnostic="active"),
    }


def build_target_projection(artifact, outcome, *, master_revision, calibration_fingerprint,
                            policy_fingerprint, action_role: str = "master") -> dict:
    """One ``isar.target_projection/2`` resolved against the AUTHORITATIVE target of this row.

    ``artifact`` is the published ``isar.master_predictive/2`` payload (for its ``mode_energies``),
    ``outcome`` the pooled master's outcome dict AFTER branch triage and the connected tracker have
    run. The result is always a valid artifact: ``complete=false`` plus a structured ``reason`` is
    the honest answer whenever a projection cannot be resolved, and never an exception.
    """
    models = list(artifact.get("models") or ())
    reason = _incompleteness_reason(outcome)
    reference = _authoritative_reference(outcome)
    if reason is None and reference is None:
        reason = PROJECTION_NO_REFERENCE
    # Two passes on purpose. Blanking as we go would make the output order-dependent — the models
    # ahead of an ambiguous one would keep real draws while the ones behind it were zeroed — so the
    # completeness verdict is settled over ALL models first, then applied uniformly.
    rule = mode_selection_rule(artifact)
    selected = ([(None, None, None)] * len(models) if reason is not None
                else [_project_model(model, reference, rule) for model in models])
    if reason is None:
        # First blocking reason in model order, so the verdict is deterministic; support outranks
        # ambiguity because a draw can be both and the support failure is the informative one.
        blocking = [blocked for _present, _qoi, blocked in selected if blocked]
        for preferred in (PROJECTION_NO_RESOLUTION_BASIS, PROJECTION_REFERENCE_BEYOND_SUPPORT):
            if preferred in blocking:
                reason = preferred
                break
        else:
            reason = blocking[0] if blocking else None
    projected = []
    for model, (present, qoi, _blocked) in zip(models, selected):
        if reason is not None:
            # An incomplete projection publishes no target draws at all: a partially-populated one
            # invites a consumer to score the half it happens to like.
            present = [False] * len(model.get("mode_energies") or ())
            qoi = [None] * len(present)
        projected.append({
            "label": model.get("label"),
            "identity_label": model.get("identity_label", model.get("label")),
            "weight": model.get("weight"),
            "target_present_by_draw": present,
            "target_qoi_draws": qoi,
        })
    if reason is None and not any(any(item["target_present_by_draw"]) for item in projected):
        reason = PROJECTION_NO_TARGET_MODE
        for item in projected:
            item["target_qoi_draws"] = [None] * len(item["target_present_by_draw"])
    try:
        target_projection_set_fingerprint = _target_projection_set_fingerprint(projected)
    except (SemanticsConstructionError, TypeError, ValueError) as exc:
        # Only SemanticsConstructionError carries a reason_code; the plain ValueError/TypeError
        # raised for a malformed model record does not, and reading it unconditionally used to
        # raise AttributeError straight out of an "always returns an artifact" function.
        log.warning(
            "semantics_construction_failed producer=isar.session.predictive_artifact "
            "quantity=target_projection_set reason_code=%s detail=%s",
            getattr(exc, "reason_code", type(exc).__name__),
            exc,
        )
        target_projection_set_fingerprint = None
        reason = PROJECTION_FINGERPRINT_FAILED
        for item in projected:
            present = item.get("target_present_by_draw")
            count = len(present) if isinstance(present, (list, tuple)) else 0
            item["target_present_by_draw"] = [False] * count
            item["target_qoi_draws"] = [None] * count
    statistical_semantics = (
        _target_projection_semantics(
            artifact,
            target_projection_set_fingerprint=target_projection_set_fingerprint,
            complete=reason is None,
        )
        if target_projection_set_fingerprint is not None
        else None
    )
    return {
        "schema": TARGET_PROJECTION_SCHEMA,
        "qoi": TARGET_QOI,
        "projection_version": TARGET_PROJECTION_VERSION,
        "predictive_basis_fingerprint": artifact.get("predictive_basis_fingerprint"),
        "source_predictive_retained_set_fingerprint": artifact.get(
            "predictive_retained_set_fingerprint"
        ),
        "source_predictive_semantics_fingerprint": artifact.get(
            "statistical_semantics_fingerprint"
        ),
        "target_projection_set_fingerprint": target_projection_set_fingerprint,
        "statistical_semantics": statistical_semantics,
        "statistical_semantics_fingerprint": (
            statistical_semantics.get("statistical_semantics_fingerprint")
            if statistical_semantics is not None else None
        ),
        "reference_energy_meV": reference,
        # The resolution-relative rule the nearest-mode selection ran under, so the verdict can be
        # re-derived from the record instead of from whatever the constants say today.
        "mode_selection": rule,
        # The authority basis this projection is bound to. A consumer whose current state differs on
        # ANY of these fields holds a stale projection and must re-resolve it (EIG-3/4 eligibility).
        #
        # ``action_role`` is one of them. It was published OUTSIDE this block, which made the
        # documented staleness test — any authority field differs ⇒ stale — silently blind to a role
        # change: the same master re-resolved as a rival probe rather than the master compared equal
        # and its projection was reused.
        "authority": {
            "action_role": str(action_role),
            "master_revision": master_revision,
            "track_revision": outcome.get("track_revision"),
            "track_state": outcome.get("track_state"),
            "candidate_id": outcome.get("candidate_id"),
            "candidate_state": outcome.get("candidate_state"),
            "selected_candidate_index": outcome.get("selected_candidate_index"),
            "calibration_fingerprint": calibration_fingerprint,
            "policy_fingerprint": policy_fingerprint,
        },
        "complete": reason is None,
        "target_projection_complete": reason is None,
        "authority_scopes": authority_scopes(
            eig_shadow="ineligible",
            eig_authoritative="ineligible",
            acquisition_selection="ineligible",
            user_diagnostic="active",
        ),
        "reason": reason,
        "models": projected,
    }
