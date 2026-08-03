"""The public EIG eligibility seam, independent of census persistence."""
from __future__ import annotations

from types import SimpleNamespace

from isar.analyze.shadow_selection import unavailable_shadow_selection
from isar.eig import EligibilityInput, QoIProjectionEvidence, evaluate_eig_eligibility
from isar.schema.statistics import FIT_SELECTION_SCHEMA, authority_scopes, semantics_fingerprint, validate_for_write
from isar.session import driver


def _evidence(**overrides):
    values = {
        "contract": "isar.eig_qoi_projection_evidence/o36.1",
        "adapter_revision": "isar.target_projection_omega0_adapter/1",
        "qoi_id": "excitation.energy",
        "qoi_unit": "meV",
        "source_qoi": "omega0",
        "status": "valid",
        "authority_scopes": (
            ("acquisition_selection", "ineligible"),
            ("eig_authoritative", "ineligible"),
            ("eig_shadow", "candidate"),
        ),
    }
    values.update(overrides)
    return QoIProjectionEvidence(**values)


def _input(action_class="production", **overrides):
    target = {
        "hkl": [2.0, 0.0, 0.0], "pooled": True, "omega0": 2.0,
        "target_delivery_eligible": True, "track_state": "confirmed",
        "candidate_state": "confirmed", "candidate_id": "target",
        "selected_candidate_index": 0, "pool_consistent": True,
        "modality": "fold", "master_revision": "revision-1",
    }
    provenance = {
        "action_class": action_class, "scientific_action_fingerprint": "action-fp",
        "acquisition_action_role": "production", "evidence_sink": "canonical_master",
        "driver_inserted": False, "insertion_purpose": None,
        "authoritative_evidence_revision": "revision-1",
    }
    values = {
        "action_intent": {"action_class": action_class, "hkl": [2.0, 0.0, 0.0],
                          "evidence_sink": "canonical_master"},
        "action_provenance": provenance,
        "target_outcome": target,
        "selection_record": None,
        "qoi_evidence": _evidence(),
        "shadow_record": None,
    }
    values.update(overrides)
    return EligibilityInput.from_current_contract(**values)


def _fit_semantics(label: str) -> dict:
    data = semantics_fingerprint("tests.eig.data", {"counts": [2.0, 3.0]})
    model = semantics_fingerprint("tests.eig.model", {"label": label, "parameters": ["a"]})
    constraints = semantics_fingerprint("tests.eig.constraints", {"constraints": []})
    comparison = semantics_fingerprint("tests.eig.comparison", {
        "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model,
        "constraint_fingerprint": constraints,
        "estimator_kind": "data_likelihood_mle",
    })
    return validate_for_write({
        "schema": FIT_SELECTION_SCHEMA,
        "estimator_kind": "data_likelihood_mle",
        "fit_kind": "unconstrained",
        "effective_constraints": [],
        "aggregate_constraint_origin": "none",
        "selection_eligibility": "eligible",
        "selection_ineligibility_reasons": [],
        "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model,
        "constraint_fingerprint": constraints,
        "comparison_context_fingerprint": comparison,
        "conditioning_fingerprint": comparison,
        "authority_scopes": authority_scopes(single_fit_delivery="active"),
    }, FIT_SELECTION_SCHEMA)


def _clean_selection() -> dict:
    labels = ("one", "two")
    semantics = {label: _fit_semantics(label) for label in labels}
    results = [SimpleNamespace(label=label, fit_semantics=semantics[label],
                               within_model={"omega0_samples": [1.0, 1.1]}) for label in labels]
    scores = [SimpleNamespace(label=label, cost=10.0 + index, k=2, aic=14.0 + index,
                              weight=0.75 - 0.5 * index, survives=True,
                              fit_semantics=semantics[label])
              for index, label in enumerate(labels)]
    return driver._build_selection_semantics(
        results, SimpleNamespace(scores=scores, pooled=object()),
        {"route": "full_two_stage", "labels_before_guideline": list(labels),
         "guideline_structure_filter": list(labels), "offered_labels": list(labels),
         "scored_labels": list(labels)},
        error_attachment_labels=list(labels))


def _neutral_shadow() -> dict:
    return unavailable_shadow_selection(
        "shadow_skipped_soft_deadline", stage="execution_policy",
        selection_context_fingerprint=semantics_fingerprint("tests.eig.shadow", {}),
        candidate_generation_route={"route_kind": "standard_two_stage"},
        legacy_model_selection_fingerprint=None, legacy_selected_label="one",
        legacy_model_weights={"one": 1.0})


def test_clean_current_contract_is_eligible_and_deterministic():
    value = _input(selection_record=_clean_selection(), shadow_record=_neutral_shadow())
    first = evaluate_eig_eligibility(value)
    second = evaluate_eig_eligibility(value)
    assert first.observability_codes == ()
    assert first.scientific_codes == ()
    assert first.status == "eligible"
    assert first.scientific_codes == ()
    assert first.observability_codes == ()
    assert first.to_dict() == second.to_dict()


def test_production_and_confirm_pass_the_action_gate():
    for action_class in ("production", "confirm"):
        result = evaluate_eig_eligibility(_input(action_class))
        assert "action_not_canonical_target" not in result.scientific_codes
        assert result.primary_scientific_code is None


def test_quicklook_and_auxiliary_actions_fail_the_action_gate():
    for action_class in ("quicklook", "differentiation_probe", "identity_rival_probe"):
        result = evaluate_eig_eligibility(_input(action_class))
        assert "action_not_canonical_target" in result.scientific_codes


def test_action_gate_rejects_nonproduction_role_auxiliary_sink_and_insertion():
    for changes in (
        {"acquisition_action_role": "discovery"},
        {"evidence_sink": "auxiliary_track_node"},
        {"driver_inserted": True},
        {"insertion_purpose": "differentiate"},
    ):
        record = _input()
        provenance = dict(record.action_provenance)
        provenance.update(changes)
        intent = dict(record.action_intent)
        if "evidence_sink" in changes:
            intent["evidence_sink"] = changes["evidence_sink"]
        result = evaluate_eig_eligibility(EligibilityInput.from_current_contract(
            action_intent=intent,
            action_provenance=provenance,
            target_outcome=record.target_outcome,
            selection_record=None,
            qoi_evidence=record.qoi_evidence,
            shadow_record=None,
        ))
        assert "action_not_canonical_target" in result.scientific_codes


def test_projection_fit_role_is_not_an_acquisition_intent_field():
    first = evaluate_eig_eligibility(_input(
        action_provenance={**dict(_input().action_provenance), "projection_action_role": "master"},
    ))
    second = evaluate_eig_eligibility(_input(
        action_provenance={**dict(_input().action_provenance), "projection_action_role": "certification"},
    ))
    assert first.scientific_codes == second.scientific_codes
    assert "action_not_canonical_target" not in first.scientific_codes


def test_missing_contemporaneous_action_is_unassessable_not_reconstructed():
    result = evaluate_eig_eligibility(_input(action_intent=None, action_provenance=None))
    assert result.status == "unassessable"
    assert result.primary_observability_code == "action_context_unknown"


def test_scientific_exclusions_outrank_observability_gaps():
    result = evaluate_eig_eligibility(_input(
        action_class="quicklook",
        qoi_evidence=_evidence(observability_codes=("projection_record_missing",)),
    ))
    assert result.status == "ineligible"
    assert result.primary_scientific_code == "action_not_canonical_target"
    assert result.primary_observability_code == "selection_record_missing"
