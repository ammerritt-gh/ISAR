from __future__ import annotations

import copy
import json
import math
from types import SimpleNamespace

import pytest

from isar.analyze.shadow_selection import (
    SHADOW_REASON_INELIGIBLE,
    SHADOW_REASON_CANDIDATE_FIT_FAILED,
    SHADOW_REASON_CONTEXT_MISMATCH,
    SHADOW_REASON_MALFORMED_SEMANTICS,
    SHADOW_REASON_MISSING_LEGACY_FINGERPRINT,
    SHADOW_REASON_MISSING_ATTEMPT,
    SHADOW_REASON_MISSING_SEMANTICS,
    SHADOW_REASON_UNAPPROVED_ESTIMATOR,
    compare_shadow_selection,
    shadow_model_identity,
    unavailable_shadow_selection,
)
from isar.schema.statistics import (
    FIT_SELECTION_SCHEMA,
    MODEL_SELECTION_CONTRACT,
    SHADOW_MODEL_SELECTION_SCHEMA,
    SHADOW_MODEL_SELECTION_SCHEMA_V1,
    SemanticsConstructionError,
    authority_scopes,
    semantics_fingerprint,
    shadow_selection_fingerprint,
    read_optional,
    validate_shadow_model_selection,
    validate_for_write,
)


def _fit_semantics(
    label: str,
    *,
    data_fingerprint: str | None = None,
    estimator_kind: str = "data_likelihood_mle",
    selection_eligibility: str = "eligible",
) -> dict:
    data_fingerprint = data_fingerprint or semantics_fingerprint(
        "test.shadow.data", {"counts": [4.0, 7.0], "mask": [False, False]}
    )
    model_fingerprint = semantics_fingerprint(
        "test.shadow.model", {"label": label, "parameters": ["a"]}
    )
    constraint_fingerprint = semantics_fingerprint(
        "test.shadow.constraints", {"constraints": []}
    )
    comparison_fingerprint = semantics_fingerprint(
        "test.shadow.comparison",
        {
            "data_likelihood_fingerprint": data_fingerprint,
            "model_parameterization_fingerprint": model_fingerprint,
            "constraint_fingerprint": constraint_fingerprint,
            "estimator_kind": estimator_kind,
        },
    )
    return {
        "schema": FIT_SELECTION_SCHEMA,
        "estimator_kind": estimator_kind,
        "selection_eligibility": selection_eligibility,
        "effective_constraints": [],
        "data_likelihood_fingerprint": data_fingerprint,
        "model_parameterization_fingerprint": model_fingerprint,
        "constraint_fingerprint": constraint_fingerprint,
        "comparison_context_fingerprint": comparison_fingerprint,
        "conditioning_fingerprint": comparison_fingerprint,
        "authority_scopes": authority_scopes(user_diagnostic="active"),
    }


def _candidate(
    label: str,
    cost: float,
    k: int,
    *,
    n: int = 20,
    converged: bool = True,
    semantics: dict | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        label=label,
        cost=cost,
        k=k,
        ndof=n - k,
        optimizer_converged=converged,
        params={f"{label}.a": 1.0},
        fit_semantics=semantics if semantics is not None else _fit_semantics(label),
    )


def _identity(candidate) -> dict:
    return shadow_model_identity(
        candidate.label,
        candidate.fit_semantics["model_parameterization_fingerprint"],
    )


def _compare(candidates, *, offered=None, route=None, **overrides):
    offered = offered if offered is not None else [_identity(item) for item in candidates]
    candidate_labels = {item.label for item in candidates}
    freshly_fitted = [
        identity for identity in offered if identity["label"] in candidate_labels
    ]
    first_semantics = next(
        (
            item.fit_semantics for item in candidates
            if isinstance(item.fit_semantics, dict)
        ),
        None,
    )
    kwargs = {
        "offered_model_identities": offered,
        "candidate_generation_route": route
        or {
            "route_kind": "standard_two_stage",
            "offered_labels": [item["label"] for item in offered],
            "guideline_structure_filter": ["z", "a"],
        },
        "selection_context_fingerprint": first_semantics[
            "data_likelihood_fingerprint"
        ],
        "legacy_model_selection_fingerprint": semantics_fingerprint(
            "test.shadow.legacy", {"winner": "legacy"}
        ),
        "legacy_selected_label": "legacy",
        "legacy_model_weights": {"legacy": 1.0},
        "shadow_execution_kind": "fresh",
        "reused_model_identities": [],
        "freshly_fitted_model_identities": freshly_fitted,
        "delivery_effective_constraint_origins": ["prior_predictive"],
        "runtime_increment_seconds": 0.25,
    }
    kwargs.update(overrides)
    return compare_shadow_selection(candidates, **kwargs)


def _records_by_label(record):
    return {item["label"]: item for item in record["standard_candidate_records"]}


def test_shadow_formulas_and_all_identities_are_order_independent():
    one = _candidate("one", 10.0, 2)
    two = _candidate("two", 8.0, 4)
    left = _compare([one, two])
    right = _compare(
        [two, one],
        offered=[_identity(two), _identity(one)],
        route={
            "guideline_structure_filter": ["a", "z"],
            "offered_labels": ["two", "one"],
            "route_kind": "standard_two_stage",
        },
    )

    assert left == right
    assert left["schema"] == SHADOW_MODEL_SELECTION_SCHEMA
    assert left["selection_contract_revision"] == MODEL_SELECTION_CONTRACT
    assert left["selection_status"] == "complete"
    assert left["standard_selected_label"] == "one"
    records = _records_by_label(left)
    assert records["one"]["aic"] == 14.0
    assert records["one"]["aicc"] == pytest.approx(14.0 + 12.0 / 17.0)
    assert records["one"]["bic"] == pytest.approx(10.0 + 2.0 * math.log(20.0))
    assert records["two"]["aic"] == 16.0
    assert records["one"]["weight"] == pytest.approx(1.0 / (1.0 + math.exp(-1.0)))
    assert validate_shadow_model_selection(left) == left
    json.dumps(left, allow_nan=False)


def test_exact_tie_uses_stable_model_identity_not_input_order():
    one = _candidate("one", 10.0, 2)
    two = _candidate("two", 8.0, 3)
    expected = min((_identity(one), _identity(two)), key=lambda item: item["model_identity"])
    assert _compare([one, two])["standard_selected_label"] == expected["label"]
    assert _compare([two, one])["standard_selected_label"] == expected["label"]


def test_adding_worse_model_only_changes_weight_normalization():
    one = _candidate("one", 10.0, 2)
    two = _candidate("two", 8.0, 4)
    worse = _candidate("worse", 30.0, 2)
    baseline = _compare([one, two])
    expanded = _compare([worse, two, one])

    baseline_records = _records_by_label(baseline)
    expanded_records = _records_by_label(expanded)
    for label in ("one", "two"):
        for key in ("cost", "k", "n", "aic", "aicc", "bic", "delta_aic"):
            assert expanded_records[label][key] == baseline_records[label][key]
        assert expanded_records[label]["weight"] < baseline_records[label]["weight"]
    assert (
        expanded["standard_selection_fingerprint"]
        != baseline["standard_selection_fingerprint"]
    )


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda candidate: setattr(candidate, "fit_semantics", None), SHADOW_REASON_MISSING_SEMANTICS),
        (
            lambda candidate: candidate.fit_semantics.update({"schema": "unknown"}),
            SHADOW_REASON_MALFORMED_SEMANTICS,
        ),
        (
            lambda candidate: candidate.fit_semantics.update(
                {"selection_eligibility": "ineligible_unapproved_conditioning"}
            ),
            SHADOW_REASON_INELIGIBLE,
        ),
        (
            lambda candidate: candidate.fit_semantics.update(
                {
                    "estimator_kind": "soft_penalized_likelihood",
                    "selection_eligibility": "ineligible_soft_penalized",
                }
            ),
            SHADOW_REASON_UNAPPROVED_ESTIMATOR,
        ),
    ],
)
def test_required_candidate_semantics_defects_refuse_without_legacy_fallback(
    mutate, reason
):
    one = _candidate("one", 10.0, 2)
    two = _candidate("two", 8.0, 4)
    offered = [_identity(one), _identity(two)]
    mutate(two)
    record = _compare([one, two], offered=offered)

    assert record["selection_status"] == "unavailable"
    assert record["unavailable_reason_code"] == reason
    assert record["standard_selected_label"] is None
    assert record["standard_model_weights"] == []
    assert record["selection_changed"] is None
    assert validate_shadow_model_selection(record) == record


def test_candidate_context_must_match_declared_selection_context():
    one = _candidate("one", 10.0, 2)
    two = _candidate(
        "two",
        8.0,
        4,
        semantics=_fit_semantics(
            "two",
            data_fingerprint=semantics_fingerprint(
                "test.shadow.other_data", {"counts": [99.0]}
            ),
        ),
    )
    record = _compare([one, two])
    assert record["selection_status"] == "unavailable"
    assert record["unavailable_reason_code"] == SHADOW_REASON_CONTEXT_MISMATCH
    assert record["affected_candidate_identities"] == [_identity(two)["model_identity"]]


def test_missing_required_attempt_refuses_instead_of_normalizing_subset():
    one = _candidate("one", 10.0, 2)
    two = _candidate("two", 8.0, 4)
    record = _compare([one], offered=[_identity(one), _identity(two)])
    assert record["selection_status"] == "unavailable"
    assert record["selection_complete"] is False
    assert record["unavailable_reason_code"] == SHADOW_REASON_MISSING_ATTEMPT
    assert record["standard_model_weights"] == []


def test_one_eligible_converged_denominator_member_is_degenerate():
    one = _candidate("one", 10.0, 2)
    record = _compare([one])
    assert record["selection_complete"] is True
    assert record["selection_status"] == "degenerate_single_candidate"
    assert record["standard_selected_label"] == "one"
    assert len(record["denominator_model_identities"]) == 1
    assert record["failed_model_identities"] == []


def test_nonconverged_required_attempt_refuses_but_remains_visible():
    one = _candidate("one", 10.0, 2, converged=False)
    two = _candidate("two", 8.0, 4)
    record = _compare([one, two])
    assert record["selection_complete"] is False
    assert record["selection_status"] == "unavailable"
    assert record["unavailable_reason_code"] == SHADOW_REASON_CANDIDATE_FIT_FAILED
    assert record["standard_model_weights"] == []
    assert record["failed_model_identities"] == [_identity(one)]


def test_schema_authenticates_criteria_and_standard_fingerprint():
    record = _compare([_candidate("one", 10.0, 2), _candidate("two", 8.0, 4)])
    criterion_tamper = copy.deepcopy(record)
    criterion_tamper["standard_candidate_records"][0]["aic"] += 1.0
    with pytest.raises(SemanticsConstructionError, match="criterion_mismatch"):
        validate_shadow_model_selection(criterion_tamper)

    fingerprint_tamper = copy.deepcopy(record)
    fingerprint_tamper["candidate_generation_route"]["route_kind"] = "tampered"
    with pytest.raises(
        SemanticsConstructionError, match="standard_selection_fingerprint_mismatch"
    ):
        validate_shadow_model_selection(fingerprint_tamper)

    denominator_tamper = copy.deepcopy(record)
    denominator_tamper["denominator_model_identities"] = denominator_tamper[
        "denominator_model_identities"
    ][:1]
    denominator_tamper["denominator_model_set_fingerprint"] = semantics_fingerprint(
        "isar.shadow_denominator_model_set/1",
        denominator_tamper["denominator_model_identities"],
    )
    with pytest.raises(
        SemanticsConstructionError, match="shadow_denominator_not_equal_converged"
    ):
        validate_shadow_model_selection(denominator_tamper)

    context_tamper = copy.deepcopy(record)
    context_tamper["selection_context_fingerprint"] = semantics_fingerprint(
        "test.shadow.other_declared_context", {}
    )
    context_tamper["standard_selection_fingerprint"] = (
        shadow_selection_fingerprint(context_tamper)
    )
    with pytest.raises(SemanticsConstructionError, match=SHADOW_REASON_CONTEXT_MISMATCH):
        validate_shadow_model_selection(context_tamper)


def test_safe_unavailable_constructor_covers_soft_deadline_skip():
    record = unavailable_shadow_selection(
        "shadow_skipped_soft_deadline",
        stage="execution_policy",
        selection_context_fingerprint=semantics_fingerprint(
            "test.shadow.context", {"scan": 1}
        ),
        candidate_generation_route={"route_kind": "standard_two_stage"},
        legacy_model_selection_fingerprint=None,
        legacy_selected_label="one",
        legacy_model_weights={"one": 1.0},
    )
    assert record["selection_status"] == "unavailable"
    assert record["runtime_increment_seconds"] == 0.0
    assert record["shadow_execution_kind"] == "skipped"
    assert record["reused_model_identities"] == []
    assert record["freshly_fitted_model_identities"] == []
    assert record["selection_changed"] is None
    assert validate_shadow_model_selection(record) == record


def test_missing_legacy_fingerprint_makes_comparator_structurally_unavailable():
    record = _compare(
        [_candidate("one", 10.0, 2)],
        legacy_model_selection_fingerprint=None,
    )
    assert record["selection_status"] == "unavailable"
    assert record["unavailable_stage"] == "legacy_projection"
    assert (
        record["unavailable_reason_code"]
        == SHADOW_REASON_MISSING_LEGACY_FINGERPRINT
    )
    assert record["legacy_selected_label"] == "legacy"
    assert record["legacy_model_weights"] == [{"label": "legacy", "weight": 1.0}]


def test_execution_provenance_is_strict_but_outside_standard_fingerprint():
    one = _candidate("one", 10.0, 2)
    two = _candidate("two", 8.0, 4)
    identities = [_identity(one), _identity(two)]
    fresh = _compare([one, two])
    exact = _compare(
        [one, two],
        shadow_execution_kind="exact",
        reused_model_identities=identities,
        freshly_fitted_model_identities=[],
    )
    partial = _compare(
        [one, two],
        shadow_execution_kind="partial",
        reused_model_identities=[identities[0]],
        freshly_fitted_model_identities=[identities[1]],
    )

    assert fresh["shadow_execution_kind"] == "fresh"
    assert fresh["freshly_fitted_model_identities"] == sorted(
        identities, key=lambda item: (item["model_identity"], item["label"])
    )
    assert exact["standard_selection_fingerprint"] == fresh[
        "standard_selection_fingerprint"
    ]
    assert partial["standard_selection_fingerprint"] == fresh[
        "standard_selection_fingerprint"
    ]

    malformed = copy.deepcopy(partial)
    malformed["shadow_execution_kind"] = "exact"
    with pytest.raises(
        SemanticsConstructionError, match="inconsistent_shadow_execution_provenance"
    ):
        validate_shadow_model_selection(malformed)


def test_v1_record_remains_readable_while_current_writer_publishes_v2():
    current = _compare([_candidate("one", 10.0, 2)])
    legacy = copy.deepcopy(current)
    legacy["schema"] = SHADOW_MODEL_SELECTION_SCHEMA_V1
    legacy.pop("shadow_execution_kind")
    legacy.pop("reused_model_identities")
    legacy.pop("freshly_fitted_model_identities")

    assert current["schema"] == SHADOW_MODEL_SELECTION_SCHEMA
    assert validate_shadow_model_selection(legacy) == legacy
    assert read_optional(legacy, SHADOW_MODEL_SELECTION_SCHEMA) == legacy
    with pytest.raises(
        SemanticsConstructionError, match="legacy_shadow_schema_is_read_only"
    ):
        validate_for_write(legacy, SHADOW_MODEL_SELECTION_SCHEMA_V1)
