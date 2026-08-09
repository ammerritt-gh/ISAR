from __future__ import annotations

import numpy as np
import pytest

from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    MODEL_SELECTION_CONTRACT,
    PROBABILITY_SCHEMA,
    SHADOW_MODEL_SELECTION_SCHEMA,
    SHADOW_MODEL_SELECTION_SCHEMA_V1,
    SemanticsConstructionError,
    authority_scopes,
    canonical_shadow_model_identities,
    compact_for_csv,
    read_optional,
    read_optional_shadow_selection,
    semantics_fingerprint,
    shadow_model_set_fingerprint,
    validate_for_write,
)


def _fit_record(**overrides):
    record = {
        "schema": FIT_SELECTION_SCHEMA,
        "estimator_kind": "data_likelihood_mle",
        "selection_eligibility": "eligible",
        "effective_constraints": [],
        "authority_scopes": authority_scopes(single_fit_delivery="active"),
    }
    record.update(overrides)
    data_fingerprint = semantics_fingerprint(
        "test.data_likelihood", {"energy": [0.0], "counts": [1.0], "mask": [False]}
    )
    model_fingerprint = semantics_fingerprint(
        "test.model_parameterization", {"label": "one", "parameters": ["a"]}
    )
    constraint_fingerprint = semantics_fingerprint(
        "test.constraints",
        {"effective_constraints": record["effective_constraints"]},
    )
    comparison_fingerprint = semantics_fingerprint(
        "test.comparison",
        {
            "data_likelihood_fingerprint": data_fingerprint,
            "model_parameterization_fingerprint": model_fingerprint,
            "constraint_fingerprint": constraint_fingerprint,
            "estimator_kind": record["estimator_kind"],
        },
    )
    record.setdefault("data_likelihood_fingerprint", data_fingerprint)
    record.setdefault("model_parameterization_fingerprint", model_fingerprint)
    record.setdefault("constraint_fingerprint", constraint_fingerprint)
    record.setdefault("comparison_context_fingerprint", comparison_fingerprint)
    record.setdefault("conditioning_fingerprint", comparison_fingerprint)
    return record


def test_fingerprint_is_canonical_and_namespace_separated():
    left = semantics_fingerprint("test/a", {"b": [2, 3], "a": 1})
    right = semantics_fingerprint("test/a", {"a": 1, "b": [2, 3]})
    assert left == right
    assert left == semantics_fingerprint(
        "test/a", {"a": np.int64(1), "b": [np.int64(2), np.int64(3)]}
    )
    assert semantics_fingerprint("test/n", {"x": 3.0}) == semantics_fingerprint(
        "test/n", {"x": np.float64(3.0)}
    )
    assert left != semantics_fingerprint("test/b", {"a": 1, "b": [2, 3]})
    assert len(left) == 64
    assert left == left.lower()


@pytest.mark.parametrize("payload", [None, {"x": float("nan")}, {"x": {1, 2}}])
def test_fingerprint_rejects_missing_nonfinite_or_noncanonical_inputs(payload):
    with pytest.raises(SemanticsConstructionError):
        semantics_fingerprint("test", payload)


def test_writer_is_strict_but_legacy_reader_is_tolerant():
    valid = validate_for_write(_fit_record(), FIT_SELECTION_SCHEMA)
    assert read_optional(valid, FIT_SELECTION_SCHEMA) == valid
    assert read_optional(None, FIT_SELECTION_SCHEMA) is None
    assert read_optional({"schema": "isar.fit_selection_semantics/0"}, FIT_SELECTION_SCHEMA) is None

    with pytest.raises(SemanticsConstructionError, match="writer_unknown_legacy"):
        validate_for_write(
            _fit_record(estimator_kind="unknown_legacy"),
            FIT_SELECTION_SCHEMA,
        )
    malformed = _fit_record(estimator_kind=["not", "hashable"])
    with pytest.raises(SemanticsConstructionError, match="malformed_semantics_record"):
        validate_for_write(malformed, FIT_SELECTION_SCHEMA)
    assert read_optional(malformed, FIT_SELECTION_SCHEMA) is None


def test_eligible_record_cannot_carry_effective_constraint():
    constrained = _fit_record(
        effective_constraints=[
            {
                "parameter": "phonon.Gamma",
                "constraint_kind": "hard",
                "value": 0.001,
                "origin": "prior_predictive",
            }
        ]
    )
    with pytest.raises(SemanticsConstructionError, match="eligible_with_effective_constraints"):
        validate_for_write(constrained, FIT_SELECTION_SCHEMA)


@pytest.mark.parametrize(
    ("constraint", "reason"),
    [
        (
            {
                "parameter": "phonon.Gamma",
                "constraint_kind": "hard",
                "value": True,
                "origin": "prior_predictive",
            },
            "invalid_constraint_value",
        ),
        (
            {
                "parameter": "phonon.Gamma",
                "constraint_kind": "soft",
                "mean": "not-a-number",
                "sigma": 0.1,
                "origin": "prior_predictive",
            },
            "invalid_constraint_mean",
        ),
        (
            {
                "parameter": "phonon.Gamma",
                "constraint_kind": "soft",
                "mean": 0.2,
                "sigma": "not-a-number",
                "origin": "prior_predictive",
            },
            "invalid_constraint_sigma",
        ),
    ],
)
def test_constraint_numbers_fail_with_stable_semantics_reason(constraint, reason):
    record = _fit_record(
        selection_eligibility="ineligible_unapproved_conditioning",
        effective_constraints=[constraint],
    )
    with pytest.raises(SemanticsConstructionError, match=reason):
        validate_for_write(record, FIT_SELECTION_SCHEMA)
    assert read_optional(record, FIT_SELECTION_SCHEMA) is None


def test_user_declared_soft_prior_passes_eligible_record_validation():
    """SCENE-1 PR-c: ``user_declared`` is in ``PRESPECIFIED_SOFT_ORIGINS``, so an eligible record
    may carry a soft constraint with that origin — same invariant as ``prior_predictive``."""
    record = _fit_record(
        effective_constraints=[
            {
                "parameter": "phonon.Gamma",
                "constraint_kind": "soft",
                "mean": 0.3,
                "sigma": 0.1,
                "origin": "user_declared",
            }
        ]
    )
    validate_for_write(record, FIT_SELECTION_SCHEMA)


def test_entropy_requires_source_and_declared_state_space_or_conditioning():
    record = {
        "schema": PROBABILITY_SCHEMA,
        "quantity_kind": "entropy",
        "source_measure_semantics": "normalized_assignment_weight",
        "authority_scopes": authority_scopes(user_diagnostic="active"),
    }
    with pytest.raises(SemanticsConstructionError, match="entropy_missing_state_space"):
        validate_for_write(record, PROBABILITY_SCHEMA)
    record["state_space"] = {"kind": "branch_assignment", "labels": ["a", "b"]}
    validate_for_write(record, PROBABILITY_SCHEMA)


def test_data_dependent_prior_cannot_claim_comparable_evidence():
    record = {
        "schema": PROBABILITY_SCHEMA,
        "quantity_kind": "distribution",
        "source_measure_semantics": "normalized_factor_graph_score",
        "prior_data_dependent": True,
        "evidence_comparable": True,
        "authority_scopes": authority_scopes(user_diagnostic="active"),
    }
    with pytest.raises(
        SemanticsConstructionError, match="data_dependent_prior_evidence_comparable"
    ):
        validate_for_write(record, PROBABILITY_SCHEMA)
    record["evidence_comparable"] = False
    record["formal_bayesian"] = False
    with pytest.raises(SemanticsConstructionError, match="deprecated_formal_bayesian_flag"):
        validate_for_write(record, PROBABILITY_SCHEMA)


def test_producer_authority_and_actual_particle_measure_use_controlled_vocabularies():
    record = {
        "schema": PROBABILITY_SCHEMA,
        "producer_authority_scopes": {"user_diagnostic": "active"},
        "authority_scopes": authority_scopes(user_diagnostic="active"),
    }
    with pytest.raises(SemanticsConstructionError, match="malformed_authority_scopes"):
        validate_for_write(record, PROBABILITY_SCHEMA)

    record["producer_authority_scopes"] = authority_scopes(
        user_diagnostic="active"
    )
    record["actual_particle_measure_kind"] = "made_up_particle_measure"
    with pytest.raises(SemanticsConstructionError, match="invalid_vocabulary_value"):
        validate_for_write(record, PROBABILITY_SCHEMA)


def test_compact_csv_projection_removes_verbose_payloads_only():
    record = _fit_record(
        effective_constraints=[],
        offered_models=[{"label": "elastic"}],
        prior_specification={"bounds": [0.0, 1.0]},
        offered_model_set_fingerprint=semantics_fingerprint("offered", ["elastic"]),
        reference_id="artifact.json",
    )
    compact = compact_for_csv(record)
    assert "effective_constraints" not in compact
    assert "offered_models" not in compact
    assert "prior_specification" not in compact
    assert compact["offered_model_set_fingerprint"] == record["offered_model_set_fingerprint"]
    assert compact["authority_scopes"] == record["authority_scopes"]
    assert compact["reference_id"] == "artifact.json"


def test_shadow_model_identity_and_set_fingerprint_are_order_independent():
    identities = []
    for label in ("two", "one"):
        parameterization = semantics_fingerprint(
            "test.shadow.parameterization", {"label": label}
        )
        payload = {
            "label": label,
            "model_parameterization_fingerprint": parameterization,
        }
        identities.append(
            {
                **payload,
                "model_identity": semantics_fingerprint(
                    "isar.shadow_model_identity/1", payload
                ),
            }
        )
    canonical = canonical_shadow_model_identities(identities)
    assert canonical == canonical_shadow_model_identities(list(reversed(identities)))
    assert shadow_model_set_fingerprint(
        "offered", identities
    ) == shadow_model_set_fingerprint("offered", list(reversed(identities)))

    tampered = [dict(canonical[0])]
    tampered[0]["model_identity"] = semantics_fingerprint("test.tamper", {})
    with pytest.raises(SemanticsConstructionError, match="shadow_model_identity_mismatch"):
        canonical_shadow_model_identities(tampered)

    compact = compact_for_csv(
        {
            "reused_model_identities": canonical[:1],
            "freshly_fitted_model_identities": canonical[1:],
            "offered_model_identities": canonical,
        }
    )
    assert compact["reused_model_identities"] == canonical[:1]
    assert compact["freshly_fitted_model_identities"] == canonical[1:]
    assert "offered_model_identities" not in compact


def test_shadow_contract_is_separate_supported_schema():
    assert MODEL_SELECTION_CONTRACT == "isar.model_selection/2-shadow"
    assert SHADOW_MODEL_SELECTION_SCHEMA == "isar.shadow_model_selection/2"
    assert SHADOW_MODEL_SELECTION_SCHEMA_V1 == "isar.shadow_model_selection/1"
    assert read_optional(
        {"schema": "isar.shadow_model_selection/0"},
        SHADOW_MODEL_SELECTION_SCHEMA,
    ) is None
    legacy = read_optional_shadow_selection(None)
    assert legacy["selection_status"] == "unavailable"
    assert legacy["selection_changed"] is None
    assert legacy["unavailable_reason_code"] == "legacy_artifact_not_evaluated"
