from __future__ import annotations

import hashlib
import json
import runpy
import shutil
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


def _split_acquisition_provenance() -> dict:
    shared = {
        "acquisition_id": "acq-1", "job_id": "job-1",
        "scan_design_realized_comparison_fingerprint": "comparison",
        "proposal_fingerprint": "request",
        "scan_design_realized_selected_request_fingerprint": "request",
        "scan_design_realized_selected_grid_fingerprint": "grid",
    }
    return {
        "legacy_acquisition_provenance_exact": True,
        "realized_comparison_fingerprint": "comparison",
        "scientific_action_fingerprint": "action",
        "legacy_acquisition_provenance": dict(shared),
        "legacy_physical_submission": {
            **shared, "scientific_action_fingerprint": "action"},
    }


def test_case1_split_acquisition_provenance_joins_action_through_physical_event():
    assert gate._strong_provenance_join_valid(_split_acquisition_provenance())


@pytest.mark.parametrize("record,field", [
    ("legacy_acquisition_provenance", "acquisition_id"),
    ("legacy_acquisition_provenance", "job_id"),
    ("legacy_acquisition_provenance", "scan_design_realized_comparison_fingerprint"),
    ("legacy_acquisition_provenance", "proposal_fingerprint"),
    ("legacy_acquisition_provenance", "scan_design_realized_selected_grid_fingerprint"),
    ("legacy_physical_submission", "scientific_action_fingerprint"),
])
def test_case1_split_acquisition_provenance_rejects_each_broken_join(record, field):
    fork = _split_acquisition_provenance()
    fork[record][field] = "wrong"

    assert not gate._strong_provenance_join_valid(fork)
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


def test_repair_manifest_is_additive_and_preserves_original_attempt_identity():
    manifest = gate.build_repair_gate_manifest("a" * 40)

    assert gate.validate_repair_gate_manifest(manifest) is manifest
    assert manifest["gate_revision"] == "o36.8.1"
    assert manifest["runtime_scoring_configuration"]["simulation_count"] == 128
    assert manifest["runtime_scoring_configuration"]["block_count"] == 8
    assert manifest["evidence_dependency_order"] == [
        "verification-evidence.json", "run-manifest.json",
        "case-results.json", "gate-result.json"]
    assert manifest["original_attempt"]["status"] == "invalid_superseded"


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


def test_repair_eligibility_source_round_trips_without_changing_o368():
    source = gate.serialize_repair_eligibility_input(_eligible_input())
    restored, summary = gate.deserialize_repair_eligibility_input(source)

    assert source["contract"] == gate.REPAIR_ELIGIBILITY_SOURCE_CONTRACT
    assert summary["status"] == "eligible"
    assert gate.serialize_repair_eligibility_input(restored) == source


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


def test_strong_case_requires_lower_total_information():
    assert gate._strong_total_condition({
        "legacy_total_information_nats": 1.0,
        "target_core_total_information_nats": 0.9,
    }) is True
    assert gate._strong_total_condition({
        "legacy_total_information_nats": 1.0,
        "target_core_total_information_nats": 1.0,
    }) is False


def test_repair_top_level_reports_missing_retained_bundle_as_invalid(tmp_path):
    result = gate.evaluate_repair_bundle(tmp_path)

    assert result["overall_disposition"] == "invalid"
    assert result["invalid_stage"] == "gate_manifest"
    assert result["invalid_reason_code"] == "manifest_or_anchor_mismatch"
    assert result["validated_fingerprints_so_far"] == []


def test_false_repair_verification_record_is_bundle_invalid():
    manifest = gate.build_repair_gate_manifest("a" * 40)
    records = {
        name: {"status": "passed", "command": f"verify {name}", "returncode": 0}
        for name in (
            "parse_import", "focused", "normative_predictive", "non_slow", "full",
            "copied_bundle", "documentation", "source_closure")
    }
    records["full"]["returncode"] = 1
    value = {
        "contract": gate.REPAIR_VERIFICATION_CONTRACT,
        "implementation_anchor": "a" * 40,
        "records": records,
        "verification_evidence_fingerprint": "0" * 64,
    }

    with pytest.raises(gate.BundleInvalid) as error:
        gate._validate_repair_verification(value, manifest, [])
    assert error.value.reason_code == "verification_record_invalid"


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


def test_retained_file_identity_is_eol_tolerant_but_content_sensitive():
    lf = b"alpha\nbeta\n"
    crlf = b"alpha\r\nbeta\r\n"
    cr = b"alpha\rbeta\r"

    recorded = hashlib.sha256(crlf).hexdigest()

    for payload in (lf, crlf, cr):
        assert recorded in gate._eol_variant_hashes(payload, text=True)
    assert recorded not in gate._eol_variant_hashes(b"alpha\r\ngamma\r\n", text=True)
    # Binary identity stays raw-byte: no EOL variant is tolerated for non-text content.
    assert gate._eol_variant_hashes(crlf, text=False) == {recorded}
    assert recorded not in gate._eol_variant_hashes(lf, text=False)


def test_repaired_slice8_bundle_fails_closed_under_the_v2_selection_contract(tmp_path):
    """Re-baselined under ``isar.fit_selection_semantics/2`` (H1/H4).

    The retained bundle is verbatim /1-era campaign evidence; its eligibility sources no
    longer reproduce their summaries under the current selection contract, and the gate must
    say so with a structured refusal — never a silent pass and never a crash. The byte-exact
    reproduction this test pinned before /2 was a property of contract-stable replay; H1 rules
    out judging current code against retired-serialization compatibility, so the live pin is
    now the refusal (same discipline as the r1e-o41 replay). The frozen ``gate-result.json``
    stays untouched historical evidence of what the 2026-08-04 gate concluded.
    """
    source = Path(
        "docs/campaigns/2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V3")
    bundle = tmp_path / "isolated-repaired-bundle"
    shutil.copytree(source, bundle)
    frozen = (bundle / "gate-result.json").read_bytes()

    reproduced = gate.evaluate_bundle(bundle)

    assert reproduced["overall_disposition"] == "invalid"
    assert reproduced["invalid_reason_code"] == "case_contract_invalid"
    assert reproduced["invalid_stage"].startswith("case:")
    # The frozen record of the 2026-08-04 evaluation is still in the bundle, unrewritten — byte
    # for byte, not merely non-empty: a rewritten verdict would also be non-empty.
    assert (bundle / "gate-result.json").read_bytes() == frozen


def _resign_replacement_bundle(bundle: Path) -> None:
    """Re-sign every internal fingerprint of a mutated o36.8.2 bundle.

    This is what an adversary with write access to the retained bytes can do; nothing here
    forges an identity the gate does not derive from the bundle itself.
    """
    def load(name):
        return json.loads((bundle / name).read_text(encoding="utf-8"))

    def dump(name, value):
        (bundle / name).write_bytes(gate.canonical_json(value).encode("utf-8"))

    manifest = load("case-manifest.json")
    manifest["case_manifest_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_case_manifest_fingerprint/o36.8.2", manifest,
        "case_manifest_fingerprint")
    dump("case-manifest.json", manifest)
    fixtures = {item["case_id"]: item["fixture_definition"]
                for item in manifest["cases"]}

    results = load("case-results.json")
    for case in results["cases"]:
        case["fixture_definition_fingerprint"] = gate.fixture_definition_fingerprint(
            fixtures[case["case_id"]])
        case["case_evidence_fingerprint"] = gate._fingerprint(
            "isar.eig4_slice8_case_evidence_fingerprint/o36.8.1", case,
            "case_evidence_fingerprint")
    dump("case-results.json", results)

    run = load("run-manifest.json")
    for row in run["retained_files"]:
        row["sha256"] = hashlib.sha256(
            (bundle / row["path"]).read_bytes()).hexdigest()
    run["case_manifest_fingerprint"] = manifest["case_manifest_fingerprint"]
    run["run_manifest_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_run_manifest_fingerprint/o36.8.1", run,
        "run_manifest_fingerprint")
    dump("run-manifest.json", run)

    results = load("case-results.json")
    results["case_manifest_fingerprint"] = manifest["case_manifest_fingerprint"]
    results["run_manifest_fingerprint"] = run["run_manifest_fingerprint"]
    results["case_results_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_case_results_fingerprint/o36.8.1", results,
        "case_results_fingerprint")
    dump("case-results.json", results)


def _forge_case2_amplitude(bundle: Path) -> None:
    path = bundle / "case-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    case = next(item for item in manifest["cases"]
                if item["case_id"] == "s8-c02-weak-unresolved-confirmed")
    case["fixture_definition"]["physical_parameters"]["amplitude"] = 0.9999
    path.write_bytes(gate.canonical_json(manifest).encode("utf-8"))


def _forge_case3_fixture(bundle: Path) -> None:
    path = bundle / "case-manifest.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    case = next(item for item in manifest["cases"]
                if item["case_id"] == "s8-c03-near-elastic-selection-time")
    case["fixture_definition"]["physical_parameters"]["ncount"] = 999999
    path.write_bytes(gate.canonical_json(manifest).encode("utf-8"))


def _forge_calibration_table(bundle: Path) -> None:
    path = bundle / "case2-amplitude-calibration-table.json"
    table = json.loads(path.read_text(encoding="utf-8"))
    table["selected_amplitude"] = 0.4
    table["selected_ladder_index"] = 3
    table["calibration_table_fingerprint"] = gate._fingerprint(
        "isar.eig4_slice8_case2_amplitude_calibration_table_fingerprint/o36.8.2",
        table, "calibration_table_fingerprint")
    path.write_text(
        json.dumps(table, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.mark.parametrize("forge,stage", [
    (_forge_case2_amplitude, "case_manifest"),
    (_forge_case3_fixture, "case_manifest"),
    (_forge_calibration_table, "case2_calibration"),
])
def test_replacement_bundle_with_forged_fixture_or_calibration_is_refused(
        tmp_path, forge, stage):
    source = Path("docs/campaigns/2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V3")
    bundle = tmp_path / "forged-bundle"
    shutil.copytree(source, bundle)
    forge(bundle)
    _resign_replacement_bundle(bundle)

    result = gate.evaluate_bundle(bundle)

    assert result["overall_disposition"] == "invalid"
    assert result["invalid_stage"] == stage
    assert gate.main(["evaluate", "--bundle", str(bundle),
                      "--output", str(tmp_path / "out.json")]) == 1



@pytest.mark.parametrize("bundle_name, contract, expected_status", [
    ("2026-08-04_EIG4_SLICE8_SHADOW_GATE",
     "isar.eig4_slice8_attempt_disposition_fingerprint/o36.8.1",
     "invalid_superseded"),
    ("2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_ABORTED",
     "isar.eig4_slice8_repair_attempt_disposition_fingerprint/o36.8.1",
     "aborted_pre_freeze"),
    ("2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR",
     "isar.eig4_slice8_attempt_disposition_fingerprint/o36.8.2",
     "invalid_superseded"),
    ("2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V2",
     "isar.eig4_slice8_attempt_disposition_fingerprint/o36.8.2",
     "invalid_superseded"),
])
def test_superseded_attempt_cannot_pose_as_current(bundle_name, contract, expected_status):
    disposition = json.loads(
        (Path("docs/campaigns") / bundle_name / "attempt-disposition.json").read_text(
            encoding="utf-8"))
    fingerprint = disposition.pop("attempt_disposition_fingerprint")

    assert disposition["status"] == expected_status
    assert fingerprint == gate.semantics_fingerprint(contract, disposition)


def test_replacement_gate_manifest_is_additive_and_keeps_gate_policy():
    manifest = gate.build_replacement_gate_manifest("a" * 40)

    assert gate.validate_replacement_gate_manifest(manifest) == manifest
    assert manifest["gate_revision"] == "o36.8.2"
    assert manifest["selection_margin_multiplier"] == 2.0
    assert manifest["primary_policy"]["parameters"]["offsets"] == [0.0, 0.25]
    assert manifest["runtime_scoring_configuration"]["simulation_count"] == 128
    assert manifest["runtime_scoring_configuration"]["block_count"] == 8
    assert manifest["cases"][1]["expected_outcome"] == gate.OUTCOME_LEGACY
    assert manifest["case2_calibration"]["selected_amplitude"] == 0.275
    assert manifest["superseded_attempts"][-1]["reason_code"] == (
        "case1_comparison_execution_join_mismatch")
