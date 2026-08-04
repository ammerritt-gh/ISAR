"""The public EIG eligibility seam, independent of census persistence."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from isar.drive.acquisition.inventory import scientific_fingerprint_from_mapping
from isar.analyze.shadow_selection import unavailable_shadow_selection
from isar.eig import EligibilityInput, QoIProjectionEvidence, evaluate_eig_eligibility
from isar.eig.qoi import OMEGA0_ADAPTER_REVISION, QOI_EVIDENCE_CONTRACT
from isar.schema.statistics import FIT_SELECTION_SCHEMA, authority_scopes, semantics_fingerprint, validate_for_write
from isar.session import driver


def _evidence(**overrides):
    values = {
        "contract": QOI_EVIDENCE_CONTRACT,
        "adapter_revision": OMEGA0_ADAPTER_REVISION,
        "qoi_id": "excitation.energy",
        "qoi_unit": "meV",
        "source_qoi": "omega0",
        "status": "valid",
        "authority_scopes": (
            ("acquisition_selection", "ineligible"),
            ("eig_authoritative", "ineligible"),
            ("eig_shadow", "candidate"),
        ),
        "source_authority_scopes": tuple(sorted({
            name: {
                "eig_shadow": "ineligible", "eig_authoritative": "ineligible",
                "acquisition_selection": "ineligible",
            }
            for name in ("predictive_semantics", "projection_semantics", "projection")
        }.items())),
    }
    values.update(overrides)
    if ((values.get("scientific_codes") or values.get("observability_codes"))
            and values.get("status") == "valid"):
        values["status"] = "qualified"
        values["authority_scopes"] = tuple(sorted({
            "eig_shadow": "ineligible", "eig_authoritative": "ineligible",
            "acquisition_selection": "ineligible",
        }.items()))
    return QoIProjectionEvidence(**values)


def _input(action_class="production", **overrides):
    target = {
        "hkl": [2.0, 0.0, 0.0], "pooled": True, "omega0": 2.0,
        "target_delivery_eligible": True, "track_state": "confirmed",
        "candidate_state": "confirmed", "candidate_id": "target",
        "selected_candidate_index": 0, "pool_consistent": True,
        "modality": "fold", "master_revision": "revision-1",
    }
    intent = {
        "action_class": action_class, "hkl": [2.0, 0.0, 0.0],
        "energy_lo": 1.0, "energy_hi": 3.0, "window_family": "target",
        "target_branch_role": None, "rival_branch_role": None,
        "evidence_basis_fingerprint": "basis", "hypotheses_fingerprint": "hyp",
        "campaign_slice_fingerprint": "slice", "evidence_sink": "canonical_master",
    }
    fingerprint = scientific_fingerprint_from_mapping(intent)
    provenance = {
        "action_class": action_class, "scientific_action_fingerprint": fingerprint,
        "proposal_scientific_action_fingerprint": fingerprint,
        "acquisition_action_role": "production", "evidence_sink": "canonical_master",
        "driver_inserted": False, "insertion_purpose": None,
        "authoritative_evidence_revision": "revision-1", "hkl": [2.0, 0.0, 0.0],
    }
    values = {
        "action_intent": {**intent, "scientific_action_fingerprint": fingerprint},
        "action_provenance": provenance,
        "target_outcome": target,
        "selection_record": None,
        "qoi_evidence": _evidence(),
        "shadow_record": None,
    }
    values.update(overrides)
    selection = values.get("selection_record")
    evidence = values.get("qoi_evidence")
    if (isinstance(selection, dict) and isinstance(evidence, QoIProjectionEvidence)
            and evidence.predictive_source_model_selection_fingerprint is None):
        values["qoi_evidence"] = replace(
            evidence,
            predictive_source_model_selection_fingerprint=selection["model_selection_fingerprint"],
            qoi_evidence_fingerprint=None,
        )
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


def _clean_selection(**route_updates) -> dict:
    """Build a selection record in the shape ``isar.session.driver`` actually persists.

    ``labels_before_guideline``/``offered_labels``/``scored_labels`` hold model
    labels; ``guideline_structure_filter`` holds card-supplied structure
    directives and is empty on an unguided route.
    """
    labels = ("one", "two")
    semantics = {label: _fit_semantics(label) for label in labels}
    results = [SimpleNamespace(label=label, fit_semantics=semantics[label],
                               within_model={"omega0_samples": [1.0, 1.1]}) for label in labels]
    scores = [SimpleNamespace(label=label, cost=10.0 + index, k=2, aic=14.0 + index,
                              weight=0.75 - 0.5 * index, survives=True,
                              fit_semantics=semantics[label])
              for index, label in enumerate(labels)]
    route = {"route": "full_two_stage", "supplied_hint": None, "hint_accepted": False,
             "guideline_structure_filter": [], "guideline_changed_model_set": False,
             "labels_before_guideline": list(labels), "multimode_route": "not_offered",
             "offered_labels": list(labels), "scored_labels": list(labels)}
    route.update(route_updates)
    return driver._build_selection_semantics(
        results, SimpleNamespace(scores=scores, pooled=object()), route,
        error_attachment_labels=list(labels))


def _neutral_shadow(*, runtime_increment_seconds: float = 0.0,
                    shadow_execution_kind: str | None = None) -> dict:
    return unavailable_shadow_selection(
        "shadow_skipped_soft_deadline", stage="execution_policy",
        selection_context_fingerprint=semantics_fingerprint("tests.eig.shadow", {}),
        candidate_generation_route={"route_kind": "standard_two_stage"},
        legacy_model_selection_fingerprint=None, legacy_selected_label="one",
        legacy_model_weights={"one": 1.0},
        runtime_increment_seconds=runtime_increment_seconds,
        shadow_execution_kind=shadow_execution_kind)


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


def test_current_eligibility_contract_revision_is_exact():
    result = evaluate_eig_eligibility(
        _input(selection_record=_clean_selection(), shadow_record=_neutral_shadow()))
    assert result.contract == "isar.eig_eligibility/o36.2.1"


def test_unauthenticated_qoi_is_refused_without_consuming_codes_or_join_identity(caplog):
    base = _input(selection_record=_clean_selection(), shadow_record=_neutral_shadow())
    first_qoi = replace(base.qoi_evidence)
    object.__setattr__(first_qoi, "scientific_codes", ("projection_stale",))
    object.__setattr__(first_qoi, "predictive_source_model_selection_fingerprint", "other")
    second_qoi = replace(base.qoi_evidence)
    object.__setattr__(second_qoi, "observability_codes", ("projection_record_missing",))
    object.__setattr__(second_qoi, "predictive_source_model_selection_fingerprint", "another")

    first = evaluate_eig_eligibility(replace(base, qoi_evidence=first_qoi))
    second = evaluate_eig_eligibility(replace(base, qoi_evidence=second_qoi))

    assert first.status == "unassessable"
    assert first.scientific_codes == ()
    assert first.observability_codes == ("projection_record_invalid",)
    assert "projection_stale" not in first.scientific_codes
    assert "projection_join_mismatch" not in first.scientific_codes
    assert "projection_record_missing" not in second.observability_codes
    assert first.input_fingerprint == second.input_fingerprint
    assert dict(first.diagnostics)["qoi_status"] == "invalid"
    assert "refused unauthenticated QoI evidence" in caplog.text


@pytest.mark.parametrize(("field", "value"), [
    ("contract", "isar.eig_qoi_projection_evidence/o36.2"),
    ("adapter_revision", "isar.target_projection_omega0_adapter/2"),
])
def test_old_qoi_revision_is_refused_before_eligibility_consumption(field, value):
    base = _input(selection_record=_clean_selection(), shadow_record=_neutral_shadow())
    old = replace(base.qoi_evidence)
    object.__setattr__(old, field, value)

    result = evaluate_eig_eligibility(replace(base, qoi_evidence=old))

    assert result.status == "unassessable"
    assert result.scientific_codes == ()
    assert result.observability_codes == ("projection_record_invalid",)
    assert dict(result.diagnostics)["qoi_status"] == "invalid"


def test_semantic_eligibility_fingerprint_ignores_shadow_runtime_telemetry():
    selection = _clean_selection()
    first = evaluate_eig_eligibility(_input(
        selection_record=selection, shadow_record=_neutral_shadow(
            runtime_increment_seconds=0.0, shadow_execution_kind="unavailable")))
    second = evaluate_eig_eligibility(_input(
        selection_record=selection, shadow_record=_neutral_shadow(
            runtime_increment_seconds=17.25, shadow_execution_kind="unavailable")))

    assert first.input_fingerprint == second.input_fingerprint
    assert first.result_fingerprint == second.result_fingerprint


def test_production_and_confirm_pass_the_action_gate():
    for action_class in ("production", "confirm"):
        result = evaluate_eig_eligibility(_input(action_class))
        assert "action_not_canonical_target" not in result.scientific_codes
        assert result.primary_scientific_code is None


def test_action_gate_uses_producer_q_after_empty_legacy_target_aliases():
    record = _input()
    target = dict(record.target_outcome)
    target.update({"hkl": None, "target_hkl": None, "q": [2.0, 0.0, 0.0]})
    result = evaluate_eig_eligibility(EligibilityInput.from_current_contract(
        action_intent=record.action_intent,
        action_provenance=record.action_provenance,
        target_outcome=target,
        selection_record=None,
        qoi_evidence=record.qoi_evidence,
        shadow_record=None,
    ))

    assert "action_not_canonical_target" not in result.scientific_codes
    assert "action_context_unknown" not in result.observability_codes


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


def test_selection_and_predictive_source_model_identities_must_match():
    selection = _clean_selection()
    matched = _input(selection_record=selection, shadow_record=_neutral_shadow())
    mismatched = EligibilityInput.from_current_contract(
        action_intent=matched.action_intent, action_provenance=matched.action_provenance,
        target_outcome=matched.target_outcome, selection_record=matched.selection_record,
        qoi_evidence=replace(matched.qoi_evidence,
                             predictive_source_model_selection_fingerprint="other",
                             qoi_evidence_fingerprint=None), shadow_record=matched.shadow_record)
    result = evaluate_eig_eligibility(mismatched)
    assert "projection_join_mismatch" in result.scientific_codes


@pytest.mark.parametrize("route_updates", [
    {"multimode_route": "guideline_forced"},
    {"supplied_hint": "two modes", "hint_accepted": True},
    {"guideline_changed_model_set": True},
    {"offered_labels": ["one"]},
])
def test_guideline_forced_and_accepted_hints_are_incomplete_comparisons(route_updates):
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(**route_updates), shadow_record=_neutral_shadow()))
    assert "selection_comparison_incomplete" in result.scientific_codes


def test_unguided_route_with_empty_structure_filter_is_eligible():
    """Model labels are never compared against structure directives (the R1-A bug)."""
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(), shadow_record=_neutral_shadow()))
    assert result.scientific_codes == ()
    assert result.status == "eligible"


def test_structure_filter_content_alone_does_not_block_a_complete_comparison():
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(guideline_structure_filter=["dho_single"]),
        shadow_record=_neutral_shadow()))
    assert "selection_comparison_incomplete" not in result.scientific_codes


def test_pre_guideline_model_set_must_survive_into_the_offered_set():
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(labels_before_guideline=["one", "two", "three"]),
        shadow_record=_neutral_shadow()))
    assert "selection_comparison_incomplete" in result.scientific_codes


def _input_with_target(**target_updates):
    record = _input()
    target = dict(record.target_outcome)
    target.update(target_updates)
    return _input(target_outcome=target, selection_record=_clean_selection(),
                  shadow_record=_neutral_shadow())


def test_confirmed_pool_consistent_unpooled_master_is_eligible():
    result = evaluate_eig_eligibility(_input_with_target(modality="unpooled"))
    assert result.scientific_codes == ()
    assert result.status == "eligible"
    assert dict(result.diagnostics)["target_modality"] == "unpooled"


def test_fork_modality_stays_a_scientific_rejection():
    result = evaluate_eig_eligibility(_input_with_target(modality="fork"))
    assert result.status == "ineligible"
    assert result.primary_scientific_code == "modality_fork"
    assert dict(result.diagnostics)["target_modality"] == "fork"


def test_pool_inconsistent_source_names_its_exact_blocker():
    result = evaluate_eig_eligibility(_input_with_target(pool_consistent=False))
    assert result.status == "ineligible"
    assert result.scientific_codes == ("pool_inconsistent",)
    assert result.primary_scientific_code == "pool_inconsistent"


def test_action_fingerprint_disagreement_is_an_observability_gap():
    record = _input()
    intent = dict(record.action_intent)
    intent["scientific_action_fingerprint"] = "wrong"
    result = evaluate_eig_eligibility(EligibilityInput.from_current_contract(
        action_intent=intent, action_provenance=record.action_provenance,
        target_outcome=record.target_outcome, selection_record=None,
        qoi_evidence=record.qoi_evidence, shadow_record=None))
    assert "action_context_unknown" in result.observability_codes
