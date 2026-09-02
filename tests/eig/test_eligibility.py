"""The public EIG eligibility seam, independent of census persistence."""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from isar.drive.acquisition.inventory import scientific_fingerprint_from_mapping
from isar.analyze.shadow_selection import (
    compare_shadow_selection, shadow_model_identity, unavailable_shadow_selection)
from isar.eig import EligibilityInput, QoIProjectionEvidence, evaluate_eig_eligibility
from isar.eig.qoi import OMEGA0_ADAPTER_REVISION, QOI_EVIDENCE_CONTRACT
from isar.schema.statistics import FIT_SELECTION_SCHEMA, authority_scopes, semantics_fingerprint, validate_for_write
from isar.session import driver, scan_delivery


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
                              weight=0.75 - 0.5 * index, akaike_weight=0.75 - 0.5 * index,
                              posterior_probability=0.75 - 0.5 * index,
                              candidate_prior=0.5, log_evidence=-5.0 - index,
                              log_evidence_err=None, evidence_method="laplace",
                              evidence_validity={}, survives=True,
                              fit_semantics=semantics[label])
              for index, label in enumerate(labels)]
    route = {"route": "full_two_stage", "supplied_hint": None, "hint_accepted": False,
             "guideline_structure_filter": [], "guideline_changed_model_set": False,
             "labels_before_guideline": list(labels), "multimode_route": "not_offered",
             "offered_labels": list(labels), "scored_labels": list(labels)}
    route.update(route_updates)
    return scan_delivery._build_selection_semantics(
        results,
        SimpleNamespace(
            scores=scores, pooled=object(), selection_fallback=None, best="one",
            evidence=SimpleNamespace(
                arena_fingerprint=semantics["one"]["data_likelihood_fingerprint"]),
        ),
        route,
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


def _complete_shadow_disagreeing() -> dict:
    """A ``complete`` shadow comparison whose AIC winner differs from the legacy label."""
    labels = ("one", "two")
    semantics = {label: _fit_semantics(label) for label in labels}
    candidates = [SimpleNamespace(label=label, cost=10.0 + index, k=2, ndof=18,
                                  optimizer_converged=True, fit_semantics=semantics[label])
                 for index, label in enumerate(labels)]
    offered = [shadow_model_identity(label, semantics[label]["model_parameterization_fingerprint"])
              for label in labels]
    return compare_shadow_selection(
        candidates,
        offered_model_identities=offered,
        candidate_generation_route={"route_kind": "standard_two_stage", "offered_labels": list(labels)},
        selection_context_fingerprint=semantics["one"]["data_likelihood_fingerprint"],
        legacy_model_selection_fingerprint=semantics_fingerprint("tests.eig.legacy", {"winner": "legacy"}),
        legacy_selected_label="legacy", legacy_model_weights={"legacy": 1.0},
        shadow_execution_kind="fresh", reused_model_identities=[],
        freshly_fitted_model_identities=offered)


def test_shadow_disagreement_no_longer_vetoes_eligibility():
    """2026-09-02: since H4 the delivered selection is posterior-driven and this AIC-ranked
    shadow comparison is a diagnostic only, so an AIC/posterior disagreement is expected and
    must not veto an otherwise-eligible target."""
    shadow = _complete_shadow_disagreeing()
    assert shadow["selection_status"] == "complete"
    assert shadow["selection_changed"] is True
    result = evaluate_eig_eligibility(
        _input(selection_record=_clean_selection(), shadow_record=shadow))
    assert result.status == "eligible"
    assert "shadow_disagrees" not in result.scientific_codes


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


def test_aic_fallback_selection_is_ineligible_fail_closed():
    """/2 gate: only the evidence lane may steer acquisition. A recorded AIC-fallback
    selection (evidence degraded or absent) reads as scientifically ineligible."""
    record = dict(_clean_selection())
    record.pop("evidence_arena_fingerprint", None)
    record.pop("evidence_revision", None)
    record.update(selection_method="aic_fallback",
                  selection_fallback="aic_no_evidence_inputs",
                  model_weight_kind="akaike_relative_weight")
    result = evaluate_eig_eligibility(
        _input(selection_record=record, shadow_record=_neutral_shadow()))
    assert "selection_ineligible" in result.scientific_codes
    assert result.status != "eligible"


def test_h4c_selection_eligibility_grants_no_acquisition_authority():
    """H4(c) made a prespecified soft-prior MAP fit SELECTION-eligible. It must not thereby
    become able to steer beam time.

    Letting soft-prior fits drive acquisition is a new acquisition authority — an owner-named
    milestone needing its own ruling — so this pins the boundary: the candidate arrives with
    ``selection_eligibility="eligible"`` (the H4(c) change) and is still refused, because it is
    a MAP fit carrying an effective constraint. If this test ever goes green on ``eligible``,
    beam-time authority moved without a ruling."""
    semantics = dict(_fit_semantics("one"))
    semantics.update(
        estimator_kind="soft_penalized_likelihood",
        fit_kind="soft_prior",
        selection_eligibility="eligible",          # H4(c): comparable for selection…
        aggregate_constraint_origin="prior_predictive",
        effective_constraints=[{
            "parameter": "phonon.Gamma", "constraint_kind": "soft",
            "mean": 0.3, "sigma": 0.1, "origin": "prior_predictive",
        }],
    )
    record = dict(_clean_selection())
    record["candidate_records"] = [{**item, "fit_semantics": semantics}
                                   for item in record["candidate_records"]]

    result = evaluate_eig_eligibility(
        _input(selection_record=record, shadow_record=_neutral_shadow()))

    # …and still not authorised to acquire: the estimator is not an MLE and a constraint is live.
    assert "selection_ineligible" in result.scientific_codes
    assert "selection_constrained" in result.scientific_codes
    assert result.status != "eligible"


def _catalogue_variant_fit_semantics(label: str) -> dict:
    """A hard-constrained candidate whose sole constraint origin is ``catalogue_variant``."""
    semantics = dict(_fit_semantics(label))
    semantics.update(
        estimator_kind="hard_conditioned_likelihood",
        fit_kind="constrained",
        selection_eligibility="ineligible_unapproved_conditioning",
        aggregate_constraint_origin="catalogue_variant",
        effective_constraints=[{
            "parameter": "phonon.Omega_d", "constraint_kind": "hard",
            "value": 0.001, "origin": "catalogue_variant",
        }],
        selection_ineligibility_reasons=["catalogue_variant_pin"],
    )
    return validate_for_write(semantics, FIT_SELECTION_SCHEMA)


def test_nonwinning_catalogue_variant_no_longer_disqualifies_the_scan():
    """Owner ruling 2026-08-09: a max-posterior unconstrained winner alongside a non-winning
    offered catalogue variant is ELIGIBLE. Before the ruling, mere presence of the variant
    poisoned the scan; the flip to ELIGIBLE here is the point of this test."""
    record = dict(_clean_selection())
    labels = [item["label"] for item in record["candidate_records"]]
    record["candidate_records"] = [
        {**item, "fit_semantics": _catalogue_variant_fit_semantics(item["label"])}
        if item["label"] != labels[0] else item
        for item in record["candidate_records"]
    ]

    result = evaluate_eig_eligibility(
        _input(selection_record=record, shadow_record=_neutral_shadow()))

    assert result.status == "eligible"
    assert result.scientific_codes == ()


def test_winning_catalogue_variant_still_disqualifies_the_scan():
    """Owner ruling 2026-08-09: the exemption is presence-only. When the catalogue variant
    carries the max posterior, it produces both selection_ineligible and selection_constrained."""
    record = dict(_clean_selection())
    winner_label = max(
        record["candidate_records"],
        key=lambda item: item.get("posterior_probability") or 0.0,
    )["label"]
    record["candidate_records"] = [
        {**item, "fit_semantics": _catalogue_variant_fit_semantics(item["label"])}
        if item["label"] == winner_label else item
        for item in record["candidate_records"]
    ]

    result = evaluate_eig_eligibility(
        _input(selection_record=record, shadow_record=_neutral_shadow()))

    assert "selection_ineligible" in result.scientific_codes
    assert "selection_constrained" in result.scientific_codes
    assert result.status != "eligible"


def test_nonwinning_non_variant_hard_constraint_still_disqualifies():
    """Owner ruling 2026-08-09: the exemption is variant-only. A loser hard-constrained under a
    different origin (e.g. ``prespecified_scientific``) still disqualifies the scan."""
    record = dict(_clean_selection())
    labels = [item["label"] for item in record["candidate_records"]]
    other_semantics = dict(_fit_semantics(labels[1]))
    other_semantics.update(
        estimator_kind="hard_conditioned_likelihood",
        fit_kind="constrained",
        selection_eligibility="ineligible_unapproved_conditioning",
        aggregate_constraint_origin="prespecified_scientific",
        effective_constraints=[{
            "parameter": "phonon.Omega_d", "constraint_kind": "hard",
            "value": 0.001, "origin": "prespecified_scientific",
        }],
        selection_ineligibility_reasons=["prespecified_scientific_pin"],
    )
    other_semantics = validate_for_write(other_semantics, FIT_SELECTION_SCHEMA)
    record["candidate_records"] = [
        {**item, "fit_semantics": other_semantics}
        if item["label"] == labels[1] else item
        for item in record["candidate_records"]
    ]

    result = evaluate_eig_eligibility(
        _input(selection_record=record, shadow_record=_neutral_shadow()))

    assert "selection_ineligible" in result.scientific_codes
    assert "selection_constrained" in result.scientific_codes
    assert result.status != "eligible"


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
    {"multimode_route": "declaration_relaxed"},
    {"supplied_hint": "two modes", "hint_accepted": True},
    {"guideline_changed_model_set": True},
])
def test_declaration_relaxed_and_accepted_hints_are_incomplete_comparisons(route_updates):
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(**route_updates), shadow_record=_neutral_shadow()))
    assert "selection_declaration_relaxed" in result.scientific_codes


@pytest.mark.parametrize("multimode_route", ["residual_triggered", "not_offered"])
def test_ordinary_multimode_routes_raise_no_relaxation_flag(multimode_route):
    """A route the global gate triggered on its own (or never offered) is NOT relaxation-driven —
    ``selection_declaration_relaxed`` is keyed on the gate outcome, not on multimode presence."""
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(multimode_route=multimode_route),
        shadow_record=_neutral_shadow()))
    assert "selection_declaration_relaxed" not in result.scientific_codes


def test_a_scored_label_the_route_never_offered_is_an_unaccountable_denominator():
    """The weight denominator may not hold a model the generation route cannot account for."""
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(offered_labels=["one"]),
        shadow_record=_neutral_shadow()))
    assert "selection_scored_not_offered" in result.scientific_codes


def test_a_route_that_scored_fewer_models_than_it_offered_is_still_complete():
    """``score_models`` scores only converged fits, so scored is routinely a strict subset.

    Requiring equality required every offered structure to converge on every master, which
    rejected two thirds of the real fits in the R3 cold-start campaign.  Whether the surviving
    normalisation is sound is the producer's own ``selection_eligibility`` judgement, read
    separately; the route check only owes the containment.
    """
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(scored_labels=["one"]),
        shadow_record=_neutral_shadow()))
    assert result.scientific_codes == ()
    assert result.status == "eligible"


def test_a_route_that_scored_nothing_is_not_a_comparison():
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(scored_labels=[]),
        shadow_record=_neutral_shadow()))
    assert "selection_scored_not_offered" in result.scientific_codes


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
    assert "selection_declaration_relaxed" not in result.scientific_codes


def test_pre_guideline_model_set_must_survive_into_the_offered_set():
    result = evaluate_eig_eligibility(_input(
        selection_record=_clean_selection(labels_before_guideline=["one", "two", "three"]),
        shadow_record=_neutral_shadow()))
    assert "selection_labels_shifted_before_guideline" in result.scientific_codes


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
