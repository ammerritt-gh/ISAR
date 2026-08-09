"""Pure statistical-semantics schemas and canonical fingerprint helpers.

This module owns vocabulary and validation only.  It deliberately does not import fitting,
selection, interval, tracking, or predictive producer types; those producers extract their own
domain state and submit plain records here for validation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any


logger = logging.getLogger(__name__)

FIT_SELECTION_SCHEMA = "isar.fit_selection_semantics/2"
INTERVAL_SCHEMA = "isar.interval_semantics/1"
PROBABILITY_SCHEMA = "isar.probability_semantics/1"
PREDICTIVE_SCHEMA = "isar.predictive_semantics/1"
SHADOW_MODEL_SELECTION_SCHEMA_V1 = "isar.shadow_model_selection/1"
SHADOW_MODEL_SELECTION_SCHEMA = "isar.shadow_model_selection/2"
SHADOW_MODEL_SELECTION_SCHEMAS = frozenset(
    {SHADOW_MODEL_SELECTION_SCHEMA_V1, SHADOW_MODEL_SELECTION_SCHEMA}
)
MODEL_SELECTION_CONTRACT = "isar.model_selection/2-shadow"
SUPPORTED_SCHEMAS = frozenset(
    {
        FIT_SELECTION_SCHEMA,
        INTERVAL_SCHEMA,
        PROBABILITY_SCHEMA,
        PREDICTIVE_SCHEMA,
        *SHADOW_MODEL_SELECTION_SCHEMAS,
    }
)

SHADOW_SELECTION_STATUSES = frozenset(
    {"complete", "degenerate_single_candidate", "unavailable"}
)
SHADOW_EXECUTION_KINDS = frozenset(
    {"exact", "partial", "fresh", "skipped", "unavailable"}
)

ESTIMATOR_KINDS = frozenset(
    {
        "data_likelihood_mle",
        "soft_penalized_likelihood",
        "hard_conditioned_likelihood",
    }
)
CONSTRAINT_ORIGINS = frozenset(
    {
        "none",
        "prespecified_scientific",
        "prior_predictive",
        "user_declared",
        "catalogue_variant",
        "data_dependent_rescue",
        "post_selection",
        "mixed",
    }
)
#: Soft-constraint origins H4(c) recast as evidence-comparable: prespecified relative to the scan
#: being fit, so the prior IS its parameter's prior and enters lnZ exactly once.
#: ``user_declared`` (SCENE-1 PR-c) is the scientist's pre-fit declaration — "ω₀ ≈ 5 ± 0.5 meV"
#: as a soft Gaussian — priced identically: declared before the fit sees this scan's counts, it
#: is prespecified by construction. (Eyeballed *seeding* needs no origin: initial conditions
#: never touch ``ParamConstraints`` and carry no evidence weight.)
#: ``catalogue_variant`` (SCENE-1 PR-c) is deliberately NOT here: a constrained variant pins a
#: parameter HARD (the near-degenerate Γ floor), and a hard-conditioned fit is never an
#: unconditioned data-likelihood estimate however prespecified the physics behind it is. The
#: variant competes as its own candidate, priced by evidence.
#: ``data_dependent_rescue`` / ``post_selection`` read this scan's own counts and stay ineligible;
#: an unrecognised origin is not prespecified. The producer (``isar.analyze.loop``) reads this, so
#: the vocabulary and the rule that uses it stay in one place.
PRESPECIFIED_SOFT_ORIGINS = frozenset(
    {"prespecified_scientific", "prior_predictive", "user_declared"}
)
SELECTION_ELIGIBILITIES = frozenset(
    {
        "eligible",
        "ineligible_soft_penalized",
        "ineligible_data_dependent_conditioning",
        "ineligible_unapproved_conditioning",
        "ineligible_incomparable_context",
    }
)
MODEL_WEIGHT_KINDS = frozenset(
    {"posterior_probability", "akaike_relative_weight", "none"}
)
SELECTION_METHODS = frozenset({"laplace_evidence_selection", "aic_fallback", "none"})
SELECTION_FALLBACKS = frozenset(
    {"aic_no_evidence_inputs", "aic_all_evidence_invalid", "evidence_budget_exhausted"}
)
EVIDENCE_METHODS = frozenset(
    {"laplace", "laplace_truncated", "laplace_map", "dynesty", "deferred_escalation",
     "dominated", "unresolved", "invalid"}
)
BOOTSTRAP_MEASURE_KINDS = frozenset(
    {
        "unconstrained_parametric_bootstrap",
        "soft_penalized_parametric_bootstrap",
        "hard_conditioned_parametric_bootstrap",
    }
)
PARTICLE_MEASURE_KINDS = frozenset(
    {
        "parametric_bootstrap_sampling_distribution",
        "optimizer_bound_nested_parameter_distribution",
        "laplace_hesse_parameter_approximation",
        "heterogeneous_model_dependent",
    }
)
DRAW_MEASURE_KINDS = frozenset(
    {"equal_weight_empirical_particles", "explicit_weight_empirical_particles"}
)
QUANTITY_KINDS = frozenset(
    {"distribution", "marginal_weight", "entropy", "heuristic_score", "eligibility_bit"}
)
SOURCE_MEASURE_SEMANTICS = frozenset(
    {
        "normalized_assignment_weight",
        "normalized_factor_graph_score",
        "akaike_relative_weight",
        "posterior_probability",
        "heuristic_triage_score",
    }
)
CONSTRAINT_KINDS = frozenset({"soft", "hard"})
INTERVAL_KINDS = frozenset(
    {
        "parametric_bootstrap_percentile",
        "akaike_weighted_parametric_bootstrap_pool",
        "likelihood_profile",
        "penalized_profile",
        "conditional_profile",
        "hesse_delta",
        "optimizer_bound_nested_credible",
        "conditional_target_present_mixed_particle_quantile",
    }
)
INFERENCE_PARADIGMS = frozenset(
    {
        "frequentist_sampling_distribution",
        "frequentist_profile",
        "frequentist_asymptotic",
        "frequentist_empirical_model_mixture",
        "bayesian_posterior",
        "mixed",
        "information_criterion_weight_distribution",
        "normalized_heuristic",
        "normalized_score_distribution",
        "normalized_factor_graph_distribution",
    }
)
PRIOR_KINDS = frozenset({"none", "optimizer_bound_data_dependent"})
POSTERIOR_MEASURE_KINDS = frozenset({"posterior_resample"})
PREDICTIVE_ARTIFACT_KINDS = frozenset({"master_predictive", "target_projection"})

AUTHORITY_SCOPE_NAMES = (
    "single_fit_delivery",
    "legacy_master_certification",
    "stopping",
    "eig_shadow",
    "eig_authoritative",
    "acquisition_selection",
    "user_diagnostic",
)
AUTHORITY_STATUSES = frozenset(
    {"active", "candidate", "diagnostic", "shadow_only", "inactive", "ineligible"}
)
UNKNOWN_LEGACY = "unknown_legacy"

_VOCABULARIES = {
    "estimator_kind": ESTIMATOR_KINDS,
    "constraint_origin": CONSTRAINT_ORIGINS,
    "aggregate_constraint_origin": CONSTRAINT_ORIGINS,
    "selection_eligibility": SELECTION_ELIGIBILITIES,
    "model_weight_kind": MODEL_WEIGHT_KINDS,
    "selection_method": SELECTION_METHODS,
    "selection_fallback": SELECTION_FALLBACKS,
    "evidence_method": EVIDENCE_METHODS,
    "bootstrap_measure_kind": BOOTSTRAP_MEASURE_KINDS,
    "particle_measure_kind": PARTICLE_MEASURE_KINDS,
    "actual_particle_measure_kind": PARTICLE_MEASURE_KINDS,
    "draw_measure_kind": DRAW_MEASURE_KINDS,
    "quantity_kind": QUANTITY_KINDS,
    "source_measure_semantics": SOURCE_MEASURE_SEMANTICS,
    "constraint_kind": CONSTRAINT_KINDS,
    "interval_kind": INTERVAL_KINDS,
    "inference_paradigm": INFERENCE_PARADIGMS,
    "prior_kind": PRIOR_KINDS,
    "posterior_measure_kind": POSTERIOR_MEASURE_KINDS,
    "artifact_kind": PREDICTIVE_ARTIFACT_KINDS,
}

_VERBOSE_CSV_KEYS = frozenset(
    {
        "effective_constraints",
        "constraint_records",
        "offered_models",
        "scored_models",
        "labels_before_guideline",
        "offered_labels",
        "scored_labels",
        "candidate_records",
        "standard_candidate_records",
        "model_records",
        "offered_model_identities",
        "attempted_model_identities",
        "converged_model_identities",
        "denominator_model_identities",
        "failed_model_identities",
        # Runtime is execution telemetry, not compact scientific provenance. Keeping it in
        # results.csv would make serial and parallel runs byte-different solely by wall clock.
        "runtime_increment_seconds",
        "prior_specification",
        "prior_bounds",
        "prior_payload",
    }
)


class SemanticsConstructionError(ValueError):
    """A stable, machine-readable failure to construct statistical semantics."""

    def __init__(self, reason_code: str, detail: str | None = None):
        self.reason_code = str(reason_code)
        self.detail = None if detail is None else str(detail)
        message = self.reason_code if self.detail is None else f"{self.reason_code}: {self.detail}"
        super().__init__(message)


@dataclass(frozen=True)
class EffectiveConstraint:
    """One effective fit constraint with producer-owned provenance."""

    parameter: str
    constraint_kind: str
    origin: str
    value: float | None = None
    mean: float | None = None
    sigma: float | None = None

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "parameter": self.parameter,
            "constraint_kind": self.constraint_kind,
            "origin": self.origin,
        }
        if self.constraint_kind == "hard":
            record["value"] = self.value
        elif self.constraint_kind == "soft":
            record["mean"] = self.mean
            record["sigma"] = self.sigma
        return record


def authority_scopes(**overrides: str) -> dict[str, str]:
    """Return a complete inactive scope map with validated caller overrides."""
    unknown = sorted(set(overrides) - set(AUTHORITY_SCOPE_NAMES))
    if unknown:
        raise SemanticsConstructionError("malformed_authority_scopes", ",".join(unknown))
    scopes = {name: "inactive" for name in AUTHORITY_SCOPE_NAMES}
    scopes.update(overrides)
    _validate_authority_scopes(scopes, for_write=True)
    return scopes


def _canonical_value(value: Any, path: str = "$") -> Any:
    if value is None or isinstance(value, (str, bool)):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, Real):
        numeric = float(value)
        if not math.isfinite(numeric):
            raise SemanticsConstructionError("nonfinite_fingerprint_input", path)
        return numeric
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise SemanticsConstructionError("noncanonical_fingerprint_input", f"{path}: non-string key")
            result[key] = _canonical_value(item, f"{path}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_canonical_value(item, f"{path}[{index}]") for index, item in enumerate(value)]
    raise SemanticsConstructionError(
        "noncanonical_fingerprint_input", f"{path}: {type(value).__name__}"
    )


def _canonical_json(payload: Any) -> str:
    canonical = _canonical_value(payload)
    return json.dumps(
        canonical,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def semantics_fingerprint(namespace: str, payload: Any) -> str:
    """Hash a namespace and finite canonical JSON payload with lowercase SHA-256."""
    if not isinstance(namespace, str) or not namespace.strip():
        raise SemanticsConstructionError("missing_fingerprint_namespace")
    if payload is None:
        raise SemanticsConstructionError("missing_fingerprint_payload")
    encoded = _canonical_json({"namespace": namespace, "payload": payload}).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_one_fingerprint(value: Any, field: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value.lower() != value
        or any(ch not in "0123456789abcdef" for ch in value)
    ):
        raise SemanticsConstructionError("malformed_fingerprint", field)


def canonical_shadow_model_identities(identities: Any) -> list[dict[str, str]]:
    """Validate and sort shadow model identities independently of producer order."""
    if not isinstance(identities, Sequence) or isinstance(
        identities, (str, bytes, bytearray)
    ):
        raise SemanticsConstructionError("malformed_shadow_model_identity_set")
    canonical = []
    seen = set()
    seen_labels = set()
    for identity in identities:
        if not isinstance(identity, Mapping):
            raise SemanticsConstructionError("malformed_shadow_model_identity")
        if set(identity) != {
            "label",
            "model_parameterization_fingerprint",
            "model_identity",
        }:
            raise SemanticsConstructionError("malformed_shadow_model_identity")
        label = identity.get("label")
        parameterization = identity.get("model_parameterization_fingerprint")
        model_identity = identity.get("model_identity")
        if not isinstance(label, str) or not label:
            raise SemanticsConstructionError("malformed_shadow_model_identity")
        _validate_one_fingerprint(parameterization, "model_parameterization_fingerprint")
        _validate_one_fingerprint(model_identity, "model_identity")
        expected = semantics_fingerprint(
            "isar.shadow_model_identity/1",
            {
                "label": label,
                "model_parameterization_fingerprint": parameterization,
            },
        )
        if model_identity != expected:
            raise SemanticsConstructionError("shadow_model_identity_mismatch", label)
        if model_identity in seen:
            raise SemanticsConstructionError("duplicate_shadow_model_identity", label)
        if label in seen_labels:
            raise SemanticsConstructionError("duplicate_shadow_model_label", label)
        seen.add(model_identity)
        seen_labels.add(label)
        canonical.append(
            {
                "label": label,
                "model_parameterization_fingerprint": parameterization,
                "model_identity": model_identity,
            }
        )
    return sorted(canonical, key=lambda item: (item["model_identity"], item["label"]))


def shadow_model_set_fingerprint(kind: str, identities: Any) -> str:
    """Fingerprint one canonical shadow model-identity set."""
    if not isinstance(kind, str) or not kind:
        raise SemanticsConstructionError("missing_shadow_model_set_kind")
    canonical = canonical_shadow_model_identities(identities)
    return semantics_fingerprint(f"isar.shadow_{kind}_model_set/1", canonical)


_SHADOW_FINGERPRINT_FIELDS = (
    "selection_contract_revision",
    "selection_context_fingerprint",
    "candidate_generation_route",
    "offered_model_identities",
    "attempted_model_identities",
    "converged_model_identities",
    "denominator_model_identities",
    "failed_model_identities",
    "offered_model_set_fingerprint",
    "attempted_model_set_fingerprint",
    "converged_model_set_fingerprint",
    "denominator_model_set_fingerprint",
    "failed_model_set_fingerprint",
    "standard_candidate_records",
    "standard_selected_label",
    "standard_model_weights",
    "selection_complete",
    "selection_status",
    "unavailable_stage",
    "unavailable_reason_code",
    "affected_candidate_identities",
)


def shadow_selection_fingerprint(record: Mapping[str, Any]) -> str:
    """Compute the self-authenticating standard-selection fingerprint."""
    if not isinstance(record, Mapping):
        raise SemanticsConstructionError("record_not_mapping")
    missing = [name for name in _SHADOW_FINGERPRINT_FIELDS if name not in record]
    if missing:
        raise SemanticsConstructionError(
            "missing_shadow_selection_field", ",".join(missing)
        )
    payload = {name: record[name] for name in _SHADOW_FINGERPRINT_FIELDS}
    return semantics_fingerprint("isar.standard_shadow_selection/1", payload)


def _walk(value: Any, path: str = "$"):
    yield path, value
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            yield from _walk(item, f"{path}[{index}]")


def _validate_authority_scopes(scopes: Any, *, for_write: bool) -> None:
    if not isinstance(scopes, Mapping) or set(scopes) != set(AUTHORITY_SCOPE_NAMES):
        raise SemanticsConstructionError("malformed_authority_scopes")
    allowed = AUTHORITY_STATUSES if for_write else AUTHORITY_STATUSES | {UNKNOWN_LEGACY}
    if any(not isinstance(status, str) or status not in allowed for status in scopes.values()):
        raise SemanticsConstructionError("malformed_authority_scopes")


def _validate_constraint(constraint: Any) -> None:
    if not isinstance(constraint, Mapping):
        raise SemanticsConstructionError("malformed_effective_constraint")
    kind = constraint.get("constraint_kind")
    if kind not in {"soft", "hard"}:
        raise SemanticsConstructionError("malformed_effective_constraint")
    if not isinstance(constraint.get("parameter"), str) or not constraint["parameter"]:
        raise SemanticsConstructionError("missing_constraint_parameter")
    origin = constraint.get("origin")
    if (
        not isinstance(origin, str)
        or origin not in CONSTRAINT_ORIGINS - {"none", "mixed"}
    ):
        raise SemanticsConstructionError("missing_constraint_origin")
    if kind == "hard":
        if "value" not in constraint:
            raise SemanticsConstructionError("missing_constraint_value")
        value = constraint["value"]
        if isinstance(value, bool) or not isinstance(value, Real):
            raise SemanticsConstructionError("invalid_constraint_value")
        if not math.isfinite(float(value)):
            raise SemanticsConstructionError("invalid_constraint_value")
    else:
        if "mean" not in constraint or "sigma" not in constraint:
            raise SemanticsConstructionError("missing_constraint_distribution")
        mean = constraint["mean"]
        sigma = constraint["sigma"]
        if isinstance(mean, bool) or not isinstance(mean, Real):
            raise SemanticsConstructionError("invalid_constraint_mean")
        if not math.isfinite(float(mean)):
            raise SemanticsConstructionError("invalid_constraint_mean")
        if (
            isinstance(sigma, bool)
            or not isinstance(sigma, Real)
            or not math.isfinite(float(sigma))
            or float(sigma) <= 0
        ):
            raise SemanticsConstructionError("invalid_constraint_sigma")


def _validate_vocabularies(record: Mapping[str, Any]) -> None:
    for path, value in _walk(record):
        key = path.rsplit(".", 1)[-1]
        allowed = _VOCABULARIES.get(key)
        if allowed is not None and value is not None and value not in allowed:
            raise SemanticsConstructionError("invalid_vocabulary_value", f"{path}={value!r}")


def _validate_fingerprints(record: Mapping[str, Any]) -> None:
    for path, value in _walk(record):
        key = path.rsplit(".", 1)[-1]
        if key.endswith("_fingerprint"):
            if (
                value is None
                and record.get("schema") in SHADOW_MODEL_SELECTION_SCHEMAS
                and key == "legacy_model_selection_fingerprint"
            ):
                continue
            if not isinstance(value, str) or len(value) != 64:
                raise SemanticsConstructionError("malformed_fingerprint", path)
            if value.lower() != value or any(ch not in "0123456789abcdef" for ch in value):
                raise SemanticsConstructionError("malformed_fingerprint", path)


_PREDICTIVE_FINGERPRINT_NAMESPACES = {
    "master_predictive": "isar.predictive_statistical_semantics/1",
    "target_projection": "isar.target_projection_statistical_semantics/1",
}


def _validate_predictive_self_fingerprint(
    record: Mapping[str, Any],
    *,
    required: bool,
) -> None:
    if record.get("schema") != PREDICTIVE_SCHEMA:
        return
    stored = record.get("statistical_semantics_fingerprint")
    if stored is None:
        if required:
            raise SemanticsConstructionError(
                "missing_statistical_semantics_fingerprint"
            )
        return
    namespace = _PREDICTIVE_FINGERPRINT_NAMESPACES.get(record.get("artifact_kind"))
    if namespace is None:
        raise SemanticsConstructionError(
            "unsupported_predictive_semantics_artifact_kind"
        )
    payload = dict(record)
    payload.pop("statistical_semantics_fingerprint", None)
    recomputed = semantics_fingerprint(namespace, payload)
    if recomputed != stored:
        raise SemanticsConstructionError(
            "statistical_semantics_fingerprint_mismatch"
        )


def _require_finite_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, Real)
        or not math.isfinite(float(value))
    ):
        raise SemanticsConstructionError("invalid_shadow_selection_number", field)
    return float(value)


def _same_number(left: Any, right: float) -> bool:
    return (
        isinstance(left, Real)
        and not isinstance(left, bool)
        and math.isfinite(float(left))
        and math.isclose(float(left), float(right), rel_tol=1e-12, abs_tol=1e-12)
    )


def _validate_shadow_model_selection(record: Mapping[str, Any]) -> None:
    schema = record.get("schema")
    if schema not in SHADOW_MODEL_SELECTION_SCHEMAS:
        return
    is_v2 = schema == SHADOW_MODEL_SELECTION_SCHEMA
    required = {
        "selection_contract_revision",
        "legacy_model_selection_fingerprint",
        "standard_selection_fingerprint",
        "selection_context_fingerprint",
        "candidate_generation_route",
        "legacy_selected_label",
        "standard_selected_label",
        "selection_changed",
        "legacy_model_weights",
        "standard_model_weights",
        "standard_candidate_records",
        "delivery_effective_constraint_origins",
        "offered_model_identities",
        "attempted_model_identities",
        "converged_model_identities",
        "denominator_model_identities",
        "failed_model_identities",
        "offered_model_set_fingerprint",
        "attempted_model_set_fingerprint",
        "converged_model_set_fingerprint",
        "denominator_model_set_fingerprint",
        "failed_model_set_fingerprint",
        "selection_complete",
        "selection_status",
        "runtime_increment_seconds",
        "unavailable_stage",
        "unavailable_reason_code",
        "affected_candidate_identities",
    }
    if is_v2:
        required.update(
            {
                "shadow_execution_kind",
                "reused_model_identities",
                "freshly_fitted_model_identities",
            }
        )
    missing = sorted(required - set(record))
    if missing:
        raise SemanticsConstructionError(
            "missing_shadow_selection_field", ",".join(missing)
        )
    allowed = required | {
        "schema",
        "standard_selection_fingerprint",
        "authority_scopes",
    }
    extra = sorted(set(record) - allowed)
    if extra:
        raise SemanticsConstructionError(
            "unexpected_shadow_selection_field", ",".join(extra)
        )
    if record["selection_contract_revision"] != MODEL_SELECTION_CONTRACT:
        raise SemanticsConstructionError("shadow_selection_contract_mismatch")
    legacy_fingerprint = record["legacy_model_selection_fingerprint"]
    if legacy_fingerprint is None:
        if record.get("selection_status") != "unavailable":
            raise SemanticsConstructionError(
                "missing_legacy_model_selection_fingerprint"
            )
    else:
        _validate_one_fingerprint(
            legacy_fingerprint,
            "legacy_model_selection_fingerprint",
        )
    _validate_one_fingerprint(
        record["selection_context_fingerprint"],
        "selection_context_fingerprint",
    )
    _validate_one_fingerprint(
        record["standard_selection_fingerprint"],
        "standard_selection_fingerprint",
    )
    if not isinstance(record["candidate_generation_route"], Mapping):
        raise SemanticsConstructionError("malformed_candidate_generation_route")

    identity_fields = {
        "offered": "offered_model_identities",
        "attempted": "attempted_model_identities",
        "converged": "converged_model_identities",
        "denominator": "denominator_model_identities",
        "failed": "failed_model_identities",
    }
    canonical_sets = {}
    for kind, field in identity_fields.items():
        canonical = canonical_shadow_model_identities(record[field])
        if canonical != record[field]:
            raise SemanticsConstructionError(
                "noncanonical_shadow_model_identity_set", field
            )
        canonical_sets[kind] = canonical
        fingerprint_field = f"{kind}_model_set_fingerprint"
        expected = shadow_model_set_fingerprint(kind, canonical)
        if record[fingerprint_field] != expected:
            raise SemanticsConstructionError(
                "shadow_model_set_fingerprint_mismatch", field
            )

    offered_ids = {item["model_identity"] for item in canonical_sets["offered"]}
    attempted_ids = {item["model_identity"] for item in canonical_sets["attempted"]}
    converged_ids = {item["model_identity"] for item in canonical_sets["converged"]}
    denominator_ids = {
        item["model_identity"] for item in canonical_sets["denominator"]
    }
    failed_ids = {item["model_identity"] for item in canonical_sets["failed"]}
    reused_ids: set[str] = set()
    freshly_fitted_ids: set[str] = set()
    if is_v2:
        execution_sets = {}
        for field in (
            "reused_model_identities",
            "freshly_fitted_model_identities",
        ):
            canonical = canonical_shadow_model_identities(record[field])
            if canonical != record[field]:
                raise SemanticsConstructionError(
                    "noncanonical_shadow_execution_identity_set", field
                )
            execution_sets[field] = {
                item["model_identity"] for item in canonical
            }
        reused_ids = execution_sets["reused_model_identities"]
        freshly_fitted_ids = execution_sets["freshly_fitted_model_identities"]
        if not reused_ids <= attempted_ids or not freshly_fitted_ids <= attempted_ids:
            raise SemanticsConstructionError(
                "shadow_execution_identity_not_attempted"
            )
        if reused_ids & freshly_fitted_ids:
            raise SemanticsConstructionError(
                "shadow_execution_identity_sets_overlap"
            )
    if not attempted_ids <= offered_ids:
        raise SemanticsConstructionError("shadow_attempted_set_not_offered")
    if not converged_ids <= attempted_ids or not failed_ids <= attempted_ids:
        raise SemanticsConstructionError("shadow_status_set_not_attempted")
    if converged_ids & failed_ids or converged_ids | failed_ids != attempted_ids:
        raise SemanticsConstructionError("shadow_attempt_status_partition_mismatch")
    if not denominator_ids <= converged_ids:
        raise SemanticsConstructionError("shadow_denominator_not_converged")
    if (
        record.get("selection_status") != "unavailable"
        and denominator_ids != converged_ids
    ):
        raise SemanticsConstructionError("shadow_denominator_not_equal_converged")

    if not isinstance(record["selection_complete"], bool):
        raise SemanticsConstructionError("invalid_boolean_field", "selection_complete")
    complete_attempts = attempted_ids == offered_ids
    status = record["selection_status"]
    if status not in SHADOW_SELECTION_STATUSES:
        raise SemanticsConstructionError("invalid_shadow_selection_status")
    reason = record["unavailable_reason_code"]
    unavailable_stage = record["unavailable_stage"]
    selected = record["standard_selected_label"]
    changed = record["selection_changed"]
    if status == "complete":
        if (
            not record["selection_complete"]
            or not complete_attempts
            or len(denominator_ids) < 2
            or reason is not None
            or unavailable_stage is not None
            or selected is None
        ):
            raise SemanticsConstructionError("inconsistent_shadow_selection_status")
    elif status == "degenerate_single_candidate":
        if (
            not record["selection_complete"]
            or not complete_attempts
            or len(denominator_ids) != 1
            or reason is not None
            or unavailable_stage is not None
            or selected is None
        ):
            raise SemanticsConstructionError("inconsistent_shadow_selection_status")
    else:
        if (
            not isinstance(reason, str)
            or not reason
            or not isinstance(unavailable_stage, str)
            or not unavailable_stage
            or record["selection_complete"]
            or denominator_ids
            or selected is not None
            or changed is not None
        ):
            raise SemanticsConstructionError("inconsistent_shadow_selection_status")
    if record["selection_complete"] and not complete_attempts:
        raise SemanticsConstructionError("shadow_selection_completeness_mismatch")

    affected = record["affected_candidate_identities"]
    if (
        not isinstance(affected, list)
        or affected != sorted(set(affected))
        or any(item not in offered_ids for item in affected)
    ):
        raise SemanticsConstructionError("malformed_affected_candidate_identities")
    if status != "unavailable" and affected:
        raise SemanticsConstructionError("unexpected_affected_candidate_identities")

    origins = record["delivery_effective_constraint_origins"]
    if (
        not isinstance(origins, list)
        or origins != sorted(set(origins))
        or any(origin not in CONSTRAINT_ORIGINS for origin in origins)
    ):
        raise SemanticsConstructionError(
            "malformed_delivery_effective_constraint_origins"
        )
    runtime = _require_finite_number(
        record["runtime_increment_seconds"], "runtime_increment_seconds"
    )
    if runtime < 0:
        raise SemanticsConstructionError(
            "invalid_shadow_selection_number", "runtime_increment_seconds"
        )
    if is_v2:
        execution_kind = record["shadow_execution_kind"]
        if execution_kind not in SHADOW_EXECUTION_KINDS:
            raise SemanticsConstructionError("invalid_shadow_execution_kind")
        execution_union = reused_ids | freshly_fitted_ids
        if execution_kind == "exact":
            valid_execution = (
                bool(attempted_ids)
                and reused_ids == attempted_ids
                and not freshly_fitted_ids
            )
        elif execution_kind == "partial":
            valid_execution = (
                bool(reused_ids)
                and bool(freshly_fitted_ids)
                and execution_union == attempted_ids
            )
        elif execution_kind == "fresh":
            valid_execution = (
                bool(attempted_ids)
                and freshly_fitted_ids == attempted_ids
                and not reused_ids
            )
        elif execution_kind == "skipped":
            valid_execution = (
                status == "unavailable"
                and not attempted_ids
                and not reused_ids
                and not freshly_fitted_ids
                and reason == "shadow_skipped_soft_deadline"
                and runtime == 0.0
            )
        else:
            valid_execution = (
                status == "unavailable"
                and not attempted_ids
                and not reused_ids
                and not freshly_fitted_ids
            )
        if not valid_execution:
            raise SemanticsConstructionError(
                "inconsistent_shadow_execution_provenance"
            )
        if (
            status != "unavailable"
            and execution_kind in {"skipped", "unavailable"}
        ):
            raise SemanticsConstructionError(
                "inconsistent_shadow_execution_provenance"
            )

    legacy_selected = record["legacy_selected_label"]
    if legacy_selected is not None and (
        not isinstance(legacy_selected, str) or not legacy_selected
    ):
        raise SemanticsConstructionError("malformed_legacy_selected_label")
    legacy_weights = record["legacy_model_weights"]
    if not isinstance(legacy_weights, list):
        raise SemanticsConstructionError("malformed_legacy_model_weights")
    legacy_labels = []
    for item in legacy_weights:
        if (
            not isinstance(item, Mapping)
            or set(item) != {"label", "weight"}
            or not isinstance(item["label"], str)
            or not item["label"]
        ):
            raise SemanticsConstructionError("malformed_legacy_model_weights")
        weight = _require_finite_number(item["weight"], "legacy_model_weights.weight")
        if weight < 0:
            raise SemanticsConstructionError("malformed_legacy_model_weights")
        legacy_labels.append(item["label"])
    if legacy_labels != sorted(set(legacy_labels)):
        raise SemanticsConstructionError("malformed_legacy_model_weights")

    candidates = record["standard_candidate_records"]
    if not isinstance(candidates, list):
        raise SemanticsConstructionError("malformed_standard_candidate_records")
    by_identity = {}
    for candidate in candidates:
        expected_keys = {
            "label",
            "model_identity",
            "cost",
            "k",
            "n",
            "optimizer_converged",
            "candidate_validation_status",
            "candidate_validation_reason_code",
            "aic",
            "aicc",
            "bic",
            "delta_aic",
            "weight",
            "fit_semantics",
        }
        if not isinstance(candidate, Mapping) or set(candidate) != expected_keys:
            raise SemanticsConstructionError("malformed_standard_candidate_record")
        identity = candidate["model_identity"]
        if identity in by_identity or identity not in attempted_ids:
            raise SemanticsConstructionError("malformed_standard_candidate_record")
        label = candidate["label"]
        matching_identity = next(
            item for item in canonical_sets["attempted"]
            if item["model_identity"] == identity
        )
        if label != matching_identity["label"]:
            raise SemanticsConstructionError("shadow_candidate_identity_join_failed", label)
        validation_status = candidate["candidate_validation_status"]
        validation_reason = candidate["candidate_validation_reason_code"]
        if validation_status not in {"valid", "invalid"}:
            raise SemanticsConstructionError("malformed_standard_candidate_record", label)
        if validation_status == "valid" and validation_reason is not None:
            raise SemanticsConstructionError("malformed_standard_candidate_record", label)
        if validation_status == "invalid" and (
            status != "unavailable"
            or not isinstance(validation_reason, str)
            or not validation_reason
            or identity not in affected
        ):
            raise SemanticsConstructionError("malformed_standard_candidate_record", label)
        semantics = None
        if candidate["fit_semantics"] is not None:
            semantics = validate_fit_selection_semantics(candidate["fit_semantics"])
            if (
                semantics["model_parameterization_fingerprint"]
                != matching_identity["model_parameterization_fingerprint"]
            ):
                raise SemanticsConstructionError(
                    "shadow_candidate_identity_join_failed", label
                )
        context_mismatch_reason = (
            "shadow_candidate_context_does_not_match_declared_selection_context"
        )
        if (
            is_v2
            and validation_status == "valid"
            and semantics is not None
            and semantics.get("data_likelihood_fingerprint")
            != record["selection_context_fingerprint"]
        ):
            raise SemanticsConstructionError(context_mismatch_reason, label)
        if (
            is_v2
            and validation_reason == context_mismatch_reason
            and (
                semantics is None
                or semantics.get("data_likelihood_fingerprint")
                == record["selection_context_fingerprint"]
            )
        ):
            raise SemanticsConstructionError(
                "shadow_candidate_context_mismatch_reason_inconsistent", label
            )
        if validation_status == "valid":
            if semantics is None:
                raise SemanticsConstructionError("malformed_standard_candidate_record", label)
            if (
                semantics.get("estimator_kind") != "data_likelihood_mle"
                or semantics.get("selection_eligibility") != "eligible"
            ):
                raise SemanticsConstructionError("ineligible_shadow_candidate", label)
            cost = _require_finite_number(candidate["cost"], f"{label}.cost")
            k = candidate["k"]
            n = candidate["n"]
            if (
                isinstance(k, bool)
                or not isinstance(k, Integral)
                or int(k) < 0
                or isinstance(n, bool)
                or not isinstance(n, Integral)
                or int(n) <= 0
                or int(k) > int(n)
            ):
                raise SemanticsConstructionError(
                    "malformed_standard_candidate_record", label
                )
        else:
            cost = candidate["cost"]
            k = candidate["k"]
            n = candidate["n"]
            if cost is not None:
                _require_finite_number(cost, f"{label}.cost")
            if k is not None and (
                isinstance(k, bool) or not isinstance(k, Integral) or int(k) < 0
            ):
                raise SemanticsConstructionError(
                    "malformed_standard_candidate_record", label
                )
            if n is not None and (
                isinstance(n, bool) or not isinstance(n, Integral) or int(n) <= 0
            ):
                raise SemanticsConstructionError(
                    "malformed_standard_candidate_record", label
                )
        if not isinstance(candidate["optimizer_converged"], bool):
            raise SemanticsConstructionError("malformed_standard_candidate_record", label)
        if candidate["optimizer_converged"] != (identity in converged_ids):
            raise SemanticsConstructionError("shadow_candidate_convergence_mismatch", label)
        if identity in denominator_ids:
            if validation_status != "valid":
                raise SemanticsConstructionError("invalid_candidate_in_denominator", label)
            aic = cost + 2.0 * int(k)
            aicc_denom = int(n) - int(k) - 1
            aicc = (
                aic + 2.0 * int(k) * (int(k) + 1) / aicc_denom
                if aicc_denom > 0
                else None
            )
            bic = cost + int(k) * math.log(int(n))
            if (
                not _same_number(candidate["aic"], aic)
                or (
                    aicc is None
                    and candidate["aicc"] is not None
                    or aicc is not None
                    and not _same_number(candidate["aicc"], aicc)
                )
                or not _same_number(candidate["bic"], bic)
            ):
                raise SemanticsConstructionError("shadow_selection_criterion_mismatch", label)
        elif any(
            candidate[name] is not None
            for name in ("aic", "aicc", "bic", "delta_aic", "weight")
        ):
            raise SemanticsConstructionError("ineligible_candidate_carries_criteria", label)
        by_identity[identity] = candidate
    if set(by_identity) != attempted_ids:
        raise SemanticsConstructionError("shadow_candidate_record_set_mismatch")
    if candidates != sorted(
        candidates, key=lambda item: (item["model_identity"], item["label"])
    ):
        raise SemanticsConstructionError("noncanonical_standard_candidate_records")

    standard_weights = record["standard_model_weights"]
    if not isinstance(standard_weights, list):
        raise SemanticsConstructionError("malformed_standard_model_weights")
    if denominator_ids:
        denominator = [by_identity[identity] for identity in denominator_ids]
        aic_min = min(float(item["aic"]) for item in denominator)
        rel = {
            item["model_identity"]: math.exp(-0.5 * (float(item["aic"]) - aic_min))
            for item in denominator
        }
        normalizer = sum(rel.values())
        expected_weights = {
            identity: value / normalizer for identity, value in rel.items()
        }
        expected_best = min(
            denominator,
            key=lambda item: (float(item["aic"]), item["model_identity"]),
        )
        if status != "unavailable" and selected != expected_best["label"]:
            raise SemanticsConstructionError("shadow_selected_label_mismatch")
        for identity, expected_weight in expected_weights.items():
            candidate = by_identity[identity]
            if (
                not _same_number(candidate["delta_aic"], float(candidate["aic"]) - aic_min)
                or not _same_number(candidate["weight"], expected_weight)
            ):
                raise SemanticsConstructionError(
                    "shadow_selection_weight_mismatch", candidate["label"]
                )
        expected_weight_records = sorted(
            (
                {
                    "label": by_identity[identity]["label"],
                    "model_identity": identity,
                    "weight": expected_weights[identity],
                }
                for identity in denominator_ids
            ),
            key=lambda item: (item["model_identity"], item["label"]),
        )
    else:
        expected_weight_records = []
    if len(standard_weights) != len(expected_weight_records):
        raise SemanticsConstructionError("shadow_selection_weight_mismatch")
    for actual, expected in zip(standard_weights, expected_weight_records):
        if (
            not isinstance(actual, Mapping)
            or set(actual) != set(expected)
            or actual["label"] != expected["label"]
            or actual["model_identity"] != expected["model_identity"]
            or not _same_number(actual["weight"], expected["weight"])
        ):
            raise SemanticsConstructionError("shadow_selection_weight_mismatch")

    expected_changed = (
        None
        if status == "unavailable"
        else bool(legacy_selected != record["standard_selected_label"])
    )
    if changed is not expected_changed:
        raise SemanticsConstructionError("shadow_selection_changed_mismatch")
    expected_fingerprint = shadow_selection_fingerprint(record)
    if record["standard_selection_fingerprint"] != expected_fingerprint:
        raise SemanticsConstructionError("standard_selection_fingerprint_mismatch")


def _validate_semantics(record: Mapping[str, Any], expected_schema: str, *, for_write: bool) -> None:
    if not isinstance(expected_schema, str) or expected_schema not in SUPPORTED_SCHEMAS:
        raise SemanticsConstructionError("unsupported_expected_schema", expected_schema)
    if not isinstance(record, Mapping):
        raise SemanticsConstructionError("record_not_mapping")
    if record.get("schema") != expected_schema:
        raise SemanticsConstructionError("schema_mismatch")
    _canonical_value(record)
    if "authority_scopes" not in record:
        raise SemanticsConstructionError("missing_authority_scopes")
    if for_write and any(value == UNKNOWN_LEGACY for _, value in _walk(record)):
        raise SemanticsConstructionError("writer_unknown_legacy")
    if any(path.rsplit(".", 1)[-1] == "formal_bayesian" for path, _ in _walk(record)):
        raise SemanticsConstructionError("deprecated_formal_bayesian_flag")
    _validate_vocabularies(record)
    _validate_fingerprints(record)
    if expected_schema == FIT_SELECTION_SCHEMA and "estimator_kind" in record:
        required = (
            "data_likelihood_fingerprint",
            "model_parameterization_fingerprint",
            "constraint_fingerprint",
            "comparison_context_fingerprint",
            "conditioning_fingerprint",
        )
        missing = [name for name in required if not record.get(name)]
        if missing:
            raise SemanticsConstructionError(
                "missing_comparison_context_fingerprint", ",".join(missing)
            )
        if record["conditioning_fingerprint"] != record["comparison_context_fingerprint"]:
            raise SemanticsConstructionError("conditioning_fingerprint_alias_mismatch")
    if expected_schema == FIT_SELECTION_SCHEMA and "model_selection_fingerprint" in record:
        if not record.get("selection_eligibility"):
            raise SemanticsConstructionError("missing_aggregate_selection_eligibility")
        if "selection_ineligibility_reasons" not in record:
            raise SemanticsConstructionError("missing_aggregate_selection_reasons")
        if "selection_method" not in record or "selection_fallback" not in record:
            raise SemanticsConstructionError("missing_selection_lane_fields")
        method = record["selection_method"]
        fallback = record["selection_fallback"]
        weight_kind = record.get("model_weight_kind")
        has_arena = "evidence_arena_fingerprint" in record
        if method == "laplace_evidence_selection" and not has_arena:
            raise SemanticsConstructionError("missing_evidence_arena_fingerprint")
        lane_coherent = (
            (
                method == "laplace_evidence_selection"
                and weight_kind == "posterior_probability"
                and fallback is None
            )
            or (
                method == "aic_fallback"
                and weight_kind == "akaike_relative_weight"
                and fallback in SELECTION_FALLBACKS
            )
            or (
                method == "none"
                and weight_kind == "none"
                and fallback is None
                and not has_arena
            )
        )
        if not lane_coherent:
            raise SemanticsConstructionError(
                "inconsistent_selection_lane", f"{method}/{weight_kind}/{fallback}"
            )
        if has_arena != ("evidence_revision" in record):
            raise SemanticsConstructionError(
                "inconsistent_selection_lane", "evidence_revision"
            )
        if has_arena:
            revision = record["evidence_revision"]
            if not isinstance(revision, str) or not revision:
                raise SemanticsConstructionError(
                    "inconsistent_selection_lane", "evidence_revision"
                )
            candidates = record.get("candidate_records")
            if isinstance(candidates, list):
                data_fingerprints = {
                    candidate["fit_semantics"].get("data_likelihood_fingerprint")
                    for candidate in candidates
                    if isinstance(candidate, Mapping)
                    and isinstance(candidate.get("fit_semantics"), Mapping)
                }
                data_fingerprints.discard(None)
                # The arena is a single-likelihood guarantee: EVERY candidate's data
                # fingerprint must equal it. A mixed set is exactly the cross-arena
                # comparison the evidence engine refuses (ArenaMismatchError), so the
                # persistence trust boundary refuses it too.
                if any(
                    fingerprint != record["evidence_arena_fingerprint"]
                    for fingerprint in data_fingerprints
                ):
                    raise SemanticsConstructionError("evidence_arena_mismatch")
    for path, item in _walk(record):
        if not isinstance(item, Mapping):
            continue
        if "authority_scopes" in item:
            _validate_authority_scopes(item["authority_scopes"], for_write=for_write)
        if "producer_authority_scopes" in item:
            _validate_authority_scopes(
                item["producer_authority_scopes"], for_write=for_write
            )

        constraints = item.get("effective_constraints")
        if constraints is not None:
            if not isinstance(constraints, list):
                raise SemanticsConstructionError("malformed_effective_constraints")
            for constraint in constraints:
                _validate_constraint(constraint)
            # H4(c) narrowed this invariant rather than removing it: a prespecified-origin SOFT
            # prior may be carried by an eligible record (it is the parameter's prior, counted
            # once). Anything else that constrained the fit — any hard conditioning, any
            # data-dependent or unrecognised soft origin — still may not.
            if item.get("selection_eligibility") == "eligible" and not all(
                constraint.get("constraint_kind") == "soft"
                and constraint.get("origin") in PRESPECIFIED_SOFT_ORIGINS
                for constraint in constraints
            ):
                raise SemanticsConstructionError("eligible_with_effective_constraints")

        if item.get("quantity_kind") == "entropy":
            if not item.get("source_measure_semantics"):
                raise SemanticsConstructionError("entropy_missing_source_measure")
            if not item.get("state_space") and not item.get("conditioning"):
                raise SemanticsConstructionError("entropy_missing_state_space")

        if item.get("evidence_comparable") is True and item.get("prior_data_dependent") is True:
            raise SemanticsConstructionError("data_dependent_prior_evidence_comparable")

        for key in (
            "formal_bayesian_model",
            "prior_data_dependent",
            "evidence_comparable",
            "bayesian_model_average",
            "availability",
            "producer_availability",
            "construction_complete",
            "homogeneous_design_eligible",
            "actual_particle_basis_homogeneous",
            "contract_permits_heterogeneous_fallback",
            "selected_for_delivery",
            "passed_reliability_gate",
            "selected_interval_certifiable",
        ):
            if key in item and not isinstance(item[key], bool):
                raise SemanticsConstructionError("invalid_boolean_field", f"{path}.{key}")

        if "nominal_level" in item:
            level = item["nominal_level"]
            if (
                isinstance(level, bool)
                or not isinstance(level, Real)
                or not math.isfinite(float(level))
                or not 0.0 < float(level) < 1.0
            ):
                raise SemanticsConstructionError("invalid_nominal_level", path)


def validate_for_write(record: Mapping[str, Any], expected_schema: str) -> dict[str, Any]:
    """Strictly validate producer metadata and return a detached canonical record."""
    try:
        if expected_schema == SHADOW_MODEL_SELECTION_SCHEMA_V1:
            raise SemanticsConstructionError(
                "legacy_shadow_schema_is_read_only"
            )
        _validate_semantics(record, expected_schema, for_write=True)
        _validate_predictive_self_fingerprint(record, required=False)
        _validate_shadow_model_selection(record)
        return json.loads(_canonical_json(record))
    except SemanticsConstructionError:
        raise
    except (TypeError, ValueError, OverflowError) as exc:
        raise SemanticsConstructionError(
            "malformed_semantics_record", type(exc).__name__
        ) from exc


def _validate_for_read(
    record: Mapping[str, Any],
    expected_schema: str,
    *,
    require_predictive_fingerprint: bool = False,
) -> dict[str, Any]:
    _validate_semantics(record, expected_schema, for_write=False)
    _validate_predictive_self_fingerprint(
        record,
        required=require_predictive_fingerprint,
    )
    _validate_shadow_model_selection(record)
    return json.loads(_canonical_json(record))


def validate_fit_selection_semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a recognized fit/selection record read from persistence."""
    return _validate_for_read(record, FIT_SELECTION_SCHEMA)


def validate_interval_semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a recognized interval record read from persistence."""
    return _validate_for_read(record, INTERVAL_SCHEMA)


def validate_probability_semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a recognized probability-like record read from persistence."""
    return _validate_for_read(record, PROBABILITY_SCHEMA)


def validate_predictive_semantics(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and authenticate a recognized predictive/projection semantics record."""
    return _validate_for_read(
        record,
        PREDICTIVE_SCHEMA,
        require_predictive_fingerprint=True,
    )


def validate_shadow_model_selection(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and authenticate a recognized STAT-2 shadow-selection record."""
    schema = record.get("schema") if isinstance(record, Mapping) else None
    if schema not in SHADOW_MODEL_SELECTION_SCHEMAS:
        raise SemanticsConstructionError("schema_mismatch")
    return _validate_for_read(record, schema)


def read_optional(record: Any, expected_schema: str) -> dict[str, Any] | None:
    """Read supported metadata, returning ``None`` for absent or unsupported legacy records."""
    if not isinstance(record, Mapping):
        return None
    actual_schema = record.get("schema")
    shadow_compatible = (
        expected_schema in SHADOW_MODEL_SELECTION_SCHEMAS
        and actual_schema in SHADOW_MODEL_SELECTION_SCHEMAS
    )
    if actual_schema != expected_schema and not shadow_compatible:
        return None
    try:
        validators = {
            FIT_SELECTION_SCHEMA: validate_fit_selection_semantics,
            INTERVAL_SCHEMA: validate_interval_semantics,
            PROBABILITY_SCHEMA: validate_probability_semantics,
            PREDICTIVE_SCHEMA: validate_predictive_semantics,
            SHADOW_MODEL_SELECTION_SCHEMA_V1: validate_shadow_model_selection,
            SHADOW_MODEL_SELECTION_SCHEMA: validate_shadow_model_selection,
        }
        return validators[expected_schema](record)
    except (SemanticsConstructionError, KeyError, TypeError, ValueError, OverflowError) as exc:
        logger.warning(
            "Rejected %s record as unreadable: %s: %s",
            expected_schema,
            type(exc).__name__,
            exc,
        )
        return None


def read_optional_shadow_selection(record: Any) -> dict[str, Any]:
    """Return a validated shadow record or a structured legacy-artifact diagnostic view."""
    recognized = read_optional(record, SHADOW_MODEL_SELECTION_SCHEMA)
    if recognized is not None:
        return recognized
    return {
        "selection_status": "unavailable",
        "selection_complete": False,
        "standard_selected_label": None,
        "selection_changed": None,
        "standard_model_weights": [],
        "unavailable_stage": "artifact_read",
        "unavailable_reason_code": "legacy_artifact_not_evaluated",
    }


def compact_for_csv(record: Any) -> dict[str, Any] | None:
    """Remove verbose producer payloads while retaining classifications and references."""
    if not isinstance(record, Mapping):
        return None

    def compact(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                key: compact(item)
                for key, item in value.items()
                if key not in _VERBOSE_CSV_KEYS
            }
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return [compact(item) for item in value]
        return value

    return json.loads(_canonical_json(compact(record)))
