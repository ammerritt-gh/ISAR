from __future__ import annotations

import hashlib
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from isar.eig.eligibility import EligibilityInput
from isar.eig.shadow_inventory import score_shadow_inventory
from isar.schema.eig_policy import (
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    ScanWindowShadowScoringConfig,
)
from tools.stats import eig_slice8_gate as gate
from tools.stats import eig_slice8_cases as cases


def _eligibility_helpers() -> dict:
    return runpy.run_path(str(Path(__file__).parents[1] / "eig" / "test_eligibility.py"))


def _eligible_input() -> EligibilityInput:
    helpers = _eligibility_helpers()
    return helpers["_input"](
        selection_record=helpers["_clean_selection"](),
        shadow_record=helpers["_neutral_shadow"](),
    )


def _case_evidence(definition: dict, source: EligibilityInput, **extra) -> dict:
    value = {
        "contract": gate.CASE_EVIDENCE_CONTRACT,
        "case_id": definition["case_id"],
        "case_generation_seed": definition["case_generation_seed"],
        "classification": definition["classification"],
        "fixture_definition_fingerprint": gate.fixture_definition_fingerprint(
            definition["fixture_definition"]),
        "eligibility_source": gate.serialize_eligibility_input(source),
        **extra,
        "case_evidence_fingerprint": "",
    }
    value["case_evidence_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_case_evidence_fingerprint/o36.8",
        value,
        "case_evidence_fingerprint",
    )
    return value


def test_frozen_manifest_uses_one_two_offset_runtime_variant():
    manifest = gate.build_gate_manifest("a" * 40)
    assert gate.validate_gate_manifest(manifest) is manifest
    assert manifest["primary_policy"]["parameters"] == {
        "origin": 0.0, "bin_width": 0.5, "offsets": [0.0, 0.25]}
    assert manifest["runtime_scoring_configuration"]["simulation_count"] == 128
    assert manifest["runtime_scoring_configuration"]["block_count"] == 8
    assert "2048" not in gate.canonical_json(manifest)


def _fixture_definitions():
    value = {
        "contract": gate.FIXTURE_DEFINITIONS_CONTRACT,
        "cases": [
            {
                "case_id": item["case_id"],
                "source_kind": (
                    "deterministic_tavi" if index == 1 else "deterministic_synthetic"),
                "generation_recipe": {"helper": f"fixture-{index}"},
                "physical_parameters": {"mode_energy_meV": float(index)},
                "ordinary_analysis_acceptance": {"required_verdict": "review"},
                "fixture_rationale": "fixed before final EIG scoring",
                "truth_use": "post_run_evaluation_only",
            }
            for index, item in enumerate(gate.CASE_DEFINITIONS, start=1)
        ],
        "fixture_definitions_fingerprint": "",
    }
    value["fixture_definitions_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fixture_definitions_fingerprint/o36.8",
        value,
        "fixture_definitions_fingerprint",
    )
    return value


def _frozen_definition(case_id: str) -> dict:
    manifest = gate.build_case_manifest(
        gate.build_gate_manifest("a" * 40), _fixture_definitions())
    return next(item for item in manifest["cases"] if item["case_id"] == case_id)


def test_case_manifest_embeds_exact_prefrozen_fixture_physics():
    manifest = gate.build_gate_manifest("a" * 40)
    fixtures = _fixture_definitions()

    cases = gate.build_case_manifest(manifest, fixtures)

    assert gate.validate_case_manifest(cases, manifest) == cases
    assert cases["fixture_definitions_fingerprint"] == (
        fixtures["fixture_definitions_fingerprint"])
    assert cases["cases"][0]["fixture_definition"]["physical_parameters"] == {
        "mode_energy_meV": 1.0,
    }


def test_fixture_definitions_reject_ordinary_analysis_placeholders():
    fixtures = _fixture_definitions()
    fixtures["cases"][0]["ordinary_analysis_acceptance"] = {}
    fixtures["fixture_definitions_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fixture_definitions_fingerprint/o36.8",
        fixtures,
        "fixture_definitions_fingerprint",
    )

    with pytest.raises(gate.GateEvidenceError, match="ordinary_analysis_acceptance"):
        gate.validate_fixture_definitions(fixtures)


def test_case_three_is_a_selection_time_eligible_legacy_case():
    definition = next(item for item in gate.CASE_DEFINITIONS
                      if item["case_id"] == "s8-c03-near-elastic-selection-time")
    assert definition["expected_eligibility"] == "eligible"
    assert definition["required_eligibility_code"] is None
    assert definition["expected_outcome"] == gate.OUTCOME_LEGACY


def test_full_eligibility_source_round_trips_and_reproduces_summary():
    source = gate.serialize_eligibility_input(_eligible_input())
    restored, summary = gate.deserialize_eligibility_input(source)
    assert summary["status"] == "eligible"
    assert gate.serialize_eligibility_input(restored) == source


def test_ineligible_case_requires_no_realized_comparison(tmp_path):
    base = _eligible_input()
    target = dict(base.target_outcome)
    target["modality"] = "fork"
    forked = EligibilityInput.from_current_contract(
        action_intent=base.action_intent,
        action_provenance=base.action_provenance,
        target_outcome=target,
        selection_record=base.selection_record,
        qoi_evidence=base.qoi_evidence,
        shadow_record=base.shadow_record,
    )
    definition = _frozen_definition("s8-c04-forked-target")
    case = _case_evidence(definition, forked)

    result = gate.evaluate_case(case, definition, tmp_path,
                                gate.build_gate_manifest("a" * 40))

    assert result["outcome"] == gate.OUTCOME_NON_EIG
    assert result["matches_expected"] is True
    assert result["diagnostics"]["scientific_codes"] == ["modality_fork"]


def test_eligible_case_with_missing_branch_evidence_is_unavailable(tmp_path):
    definition = _frozen_definition("s8-c03-near-elastic-selection-time")
    case = _case_evidence(definition, _eligible_input())

    result = gate.evaluate_case(case, definition, tmp_path,
                                gate.build_gate_manifest("a" * 40))

    assert result["outcome"] == gate.OUTCOME_UNAVAILABLE
    assert result["matches_expected"] is False


def test_paired_margin_uses_block_mean_standard_error():
    delta, standard_error, lower = gate._paired_summary((0.2, 0.4, 0.6, 0.8))
    assert delta == 0.5
    assert standard_error > 0.0
    assert lower == delta - 2.0 * standard_error


def test_frozen_grid_fault_requires_fail_open_shadow_continuation():
    definition = gate.FAULT_DEFINITIONS[0]
    value = {
        "contract": gate.FAULT_EVIDENCE_CONTRACT,
        "fault_id": definition["fault_id"],
        "source_case_id": definition["source_case_id"],
        "executed_grid_status": "mismatch",
        "reason_code": "executed_grid_mismatch",
        "ordinary_shadow_analysis_continued": True,
        "fault_evidence_fingerprint": "",
    }
    value["fault_evidence_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_fault_evidence_fingerprint/o36.8",
        value,
        "fault_evidence_fingerprint",
    )
    result = gate.evaluate_fault(value, definition)
    assert result["outcome"] == gate.OUTCOME_UNAVAILABLE
    assert result["matches_expected"] is True


def test_eligible_gate_uses_existing_strict_inventory_and_realized_loaders(
        monkeypatch, tmp_path):
    shadow = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_shadow_inventory.py"))
    realized = runpy.run_path(str(
        Path(__file__).parents[1] / "drive" / "test_realized_comparison.py"))
    predictive, eligibility, inventory = shadow["_producer_bundle"](monkeypatch)
    scope = shadow["_fp"]("slice8-campaign")
    variant = QoIDistinctionVariant(gate.PRIMARY_VARIANT_ID, gate.PRIMARY_PARAMETERS)
    policy = QoIDistinctionPolicy(
        gate.PRIMARY_POLICY_ID,
        gate.PRIMARY_POLICY_KIND,
        {"id": "excitation.energy", "unit": "meV"},
        {"kind": "campaign_configuration", "fingerprint": scope,
         "annotation": {"campaign_name": "slice8", "experiment_description": "gate test",
                        "date": "2026-08-04"}},
        {"kind": "explicit_user_analysis/1", "source_id": "owner",
         "rationale": "frozen Slice 8 primary partition"},
        (variant,),
    )
    policies = QoIDistinctionPolicySet((policy,))
    scoring = score_shadow_inventory(
        inventory,
        predictive_artifact=predictive,
        eligibility_input=eligibility,
        policy_set=policies,
        scoring_config=ScanWindowShadowScoringConfig(),
        scope_kind="campaign_configuration",
        scope_fingerprint=scope,
    )
    assert scoring.status == "complete"
    inventory = inventory.with_shadow_scoring(scoring)
    proposal = realized["_proposal"](inventory)

    def finalize(target, *_args, **_kwargs):
        target.meta["proposal_fingerprint"] = "e" * 64

    pair = realized["construct_realized_pair"](
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=finalize)
    inventory_ref = realized["_inventory_reference"](tmp_path, inventory)
    comparison = realized["build_realized_comparison"](
        inventory, inventory_ref, pair, remaining_seconds=5.0, rescorer=None)
    comparison_ref = realized["persist_realized_comparison"](tmp_path, comparison)
    primary = next(item for item in comparison.variants
                   if item.variant_id == gate.PRIMARY_VARIANT_ID)
    score_set, legacy_id, target_id = gate._score_set_payload(comparison, inventory, primary)
    blocks = list(gate._paired_blocks(score_set, legacy_id, target_id))
    case = {
        "artifact_root": ".",
        "realized_comparison_ref": comparison_ref,
        "applicability": "applicable",
        "executed_grid_status": "matched",
        "submission_provenance": {
            "scan_design_realized_comparison_fingerprint": (
                comparison.realized_comparison_fingerprint),
            "scan_design_realized_selected_request_fingerprint": (
                comparison.selected_request_fingerprint),
            "scan_design_realized_selected_grid_fingerprint": (
                comparison.selected_grid_fingerprint),
        },
        "paired_block_deltas_per_executed_point_nats": blocks,
        "analysis_outcome": {},
    }

    outcome, diagnostics = gate._classify_eligible(
        case, tmp_path, gate.build_gate_manifest("a" * 40))

    assert outcome in {gate.OUTCOME_TARGET, gate.OUTCOME_LEGACY}
    assert diagnostics["target_core_executed_point_count"] < (
        diagnostics["legacy_executed_point_count"])
    target = pair.target_core
    legacy = pair.legacy
    target_wire = sorted(gate.TAVI_GRID_ADAPTER.wire_window(
        target.e_lo, target.e_hi, target.e_step)[:2])
    legacy_wire = sorted(gate.TAVI_GRID_ADAPTER.wire_window(
        legacy.e_lo, legacy.e_hi, legacy.e_step)[:2])
    expected_structural = {
        "wire_request_contained": (
            target_wire[0] >= legacy_wire[0] and target_wire[1] <= legacy_wire[1]),
        "executed_support_contained": (
            min(target.executed_energies) >= min(legacy.executed_energies)
            and max(target.executed_energies) <= max(legacy.executed_energies)),
        "executed_span_strictly_smaller": (
            max(target.executed_energies) - min(target.executed_energies)
            < max(legacy.executed_energies) - min(legacy.executed_energies)),
        "executed_points_strictly_fewer": (
            target.executed_point_count < legacy.executed_point_count),
        "per_point_ncount_equal": target.ncount == legacy.ncount,
    }
    assert diagnostics["structural_conditions"] == expected_structural
    assert outcome == (
        gate.OUTCOME_TARGET if all(expected_structural.values())
        and diagnostics["lower_margin_nats"] > 0.0 else gate.OUTCOME_LEGACY)


def test_capture_copy_accepts_a_relative_bundle_root(tmp_path, monkeypatch):
    source_root = tmp_path / "source"
    source = source_root / "report" / "artifact.json"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"slice-8")
    monkeypatch.chdir(tmp_path)
    reference = {
        "path": "report/artifact.json",
        "sha256": hashlib.sha256(b"slice-8").hexdigest(),
    }

    retained = cases._copy_artifact(source_root, Path("bundle"), reference)

    assert retained == reference
    assert (tmp_path / "bundle" / "report" / "artifact.json").read_bytes() == b"slice-8"


def test_retained_slice8_bundle_is_self_contained_and_reproduces_exact_bytes():
    bundle = Path("docs/campaigns/2026-08-04_EIG4_SLICE8_SHADOW_GATE")

    reproduced = gate.evaluate_bundle(bundle)

    assert gate._canonical_bytes(reproduced) == (bundle / "gate-result.json").read_bytes()
    assert reproduced["overall_disposition"] == "passed"
