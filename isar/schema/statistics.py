"""Pure statistical-semantics schemas and canonical fingerprint helpers.

This module owns vocabulary and validation only.  It deliberately does not import fitting,
selection, interval, tracking, or predictive producer types; those producers extract their own
domain state and submit plain records here for validation.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from numbers import Integral, Real
from typing import Any


FIT_SELECTION_SCHEMA = "isar.fit_selection_semantics/1"
INTERVAL_SCHEMA = "isar.interval_semantics/1"
PROBABILITY_SCHEMA = "isar.probability_semantics/1"
PREDICTIVE_SCHEMA = "isar.predictive_semantics/1"
SUPPORTED_SCHEMAS = frozenset(
    {FIT_SELECTION_SCHEMA, INTERVAL_SCHEMA, PROBABILITY_SCHEMA, PREDICTIVE_SCHEMA}
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
        "data_dependent_rescue",
        "post_selection",
        "mixed",
    }
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
MODEL_WEIGHT_KINDS = frozenset({"akaike_relative_weight", "none"})
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
        "model_records",
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
            if item.get("selection_eligibility") == "eligible" and constraints:
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
        _validate_semantics(record, expected_schema, for_write=True)
        _validate_predictive_self_fingerprint(record, required=False)
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


def read_optional(record: Any, expected_schema: str) -> dict[str, Any] | None:
    """Read supported metadata, returning ``None`` for absent or unsupported legacy records."""
    if not isinstance(record, Mapping) or record.get("schema") != expected_schema:
        return None
    try:
        validators = {
            FIT_SELECTION_SCHEMA: validate_fit_selection_semantics,
            INTERVAL_SCHEMA: validate_interval_semantics,
            PROBABILITY_SCHEMA: validate_probability_semantics,
            PREDICTIVE_SCHEMA: validate_predictive_semantics,
        }
        return validators[expected_schema](record)
    except (SemanticsConstructionError, KeyError, TypeError, ValueError, OverflowError):
        return None


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
