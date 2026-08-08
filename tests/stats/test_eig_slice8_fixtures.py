import json
import shutil
from pathlib import Path

import pytest

from isar.drive.acquisition.realized_comparison import load_scan_design_realized_comparison
from isar.drive.acquisition.scan_design import load_scan_design_inventory
from tools.stats import eig_slice8_fixtures as fixtures
from tools.stats import eig_slice8_gate as gate


PARAMETERS = Path("tools/stats/eig_slice8_fixture_parameters.json")
CASE2_PROTOCOL = Path(
    "tools/stats/eig_slice8_case2_amplitude_protocol_o36_8_2.json")
CASE2_TABLE = Path(
    "docs/campaigns/2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V3/"
    "case2-amplitude-calibration-table.json")
CASE2_REPRODUCIBILITY = Path(
    "docs/campaigns/2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V3/"
    "case2-amplitude-reproducibility.json")
REPLACEMENT_PARAMETERS = Path(
    "tools/stats/eig_slice8_fixture_parameters_o36_8_2.json")


def test_fixture_builder_uses_current_contracts_and_declared_regimes(tmp_path):
    parameters = json.loads(PARAMETERS.read_text(encoding="utf-8"))
    development = fixtures.build_development_inputs(tmp_path, parameters)
    definitions = gate.validate_fixture_definitions(development["fixture_definitions"])
    by_case = {item["case_id"]: item for item in development["cases"]}

    for case_id in (
            "s8-c01-strong-confirmed", "s8-c02-weak-unresolved-confirmed",
            "s8-c03-near-elastic-selection-time"):
        raw = by_case[case_id]
        source = Path(raw["source_workspace"])
        comparison = load_scan_design_realized_comparison(raw["realized_comparison_ref"], source)
        inventory = load_scan_design_inventory(comparison.source_inventory_ref, source)
        assert inventory.shadow_scoring is not None
        _source, summary = gate.deserialize_eligibility_input(raw["eligibility_source"])
        assert summary["status"] == "eligible"

    strong = development["diagnostics"]["s8-c01-strong-confirmed"]
    assert strong["target_points"] < strong["legacy_points"]
    assert all(value > 0.0 for value in strong["blocks"])

    weak = development["diagnostics"]["s8-c02-weak-unresolved-confirmed"]
    delta, standard_error, lower = gate._paired_summary(weak["blocks"])
    assert delta > 0.0
    assert standard_error > 0.0
    assert lower <= 0.0

    near_elastic = development["diagnostics"]["s8-c03-near-elastic-selection-time"]
    assert near_elastic["blocks"] == [-0.0] * 8

    expected_codes = {
        "s8-c04-forked-target": "modality_fork",
        "s8-c05-projection-join-mismatch": "projection_join_mismatch",
        "s8-c06-constrained-selection": "selection_constrained",
    }
    for case_id, reason in expected_codes.items():
        _source, summary = gate.deserialize_eligibility_input(by_case[case_id]["eligibility_source"])
        assert summary["status"] == "ineligible"
        assert reason in summary["scientific_codes"]

    source_kind = {
        item["case_id"]: item["source_kind"] for item in definitions["cases"]}
    assert source_kind["s8-c01-strong-confirmed"] == (
        "deterministic_tavi_with_synthetic_predictive_fixture")


def test_isolated_tavi_workspace_seeds_only_ordinary_campaign_state(tmp_path):
    workspace = fixtures._init_tavi_workspace(tmp_path / "workspace")
    state = json.loads((workspace / "card.state.json").read_text(encoding="utf-8"))
    assert len(state["dispersion_prior"]) == 3
    assert {item["branch_id"] for item in state["dispersion_prior"]} == {"acoustic"}
    assert state["branch_belief_models"]
    assert state["campaign_target"] == {}
    assert state["acquisition_provenance"] == []


def test_repair_fixture_uses_one_action_and_distinct_prior_inventory(tmp_path):
    parameters = json.loads(PARAMETERS.read_text(encoding="utf-8"))
    physical = parameters["eligible_cases"]["s8-c01-strong-confirmed"]
    raw, diagnostics = fixtures._persist_repair_eligible_case(
        tmp_path / "case", case_id="s8-c01-strong-confirmed", case_seed=8040801,
        q=tuple(physical["q"]), draw_energies=physical["draw_energies_meV"],
        target_energy=physical["target_energy_meV"],
        prediction_sigma=physical["prediction_sigma_meV"],
        amplitude=physical["amplitude"], ncount=physical["ncount"],
        survey=physical["survey"])

    root = Path(raw["source_workspace"])
    current = load_scan_design_inventory(raw["current_scan_design_inventory_ref"], root)
    prior = load_scan_design_inventory(raw["prior_scan_design_inventory_ref"], root)
    source, summary = gate.deserialize_repair_eligibility_input(raw["eligibility_source"])
    action = raw["scientific_action_fingerprint"]

    assert summary["status"] == "eligible"
    assert source.action_intent["scientific_action_fingerprint"] == action
    assert source.action_provenance["scientific_action_fingerprint"] == action
    assert current.context.scientific_action_fingerprint == action
    assert raw["submission_provenance"]["scientific_action_fingerprint"] == action
    assert raw["acquisition_provenance"]["scientific_action_fingerprint"] == action
    assert prior.content_hash() != current.content_hash()
    assert prior.context.scientific_action_fingerprint != action
    assert diagnostics["predictive_seed"] == 8040801
    reloaded_proposal, reloaded_objective = fixtures._repair_proposal_from_retained(
        raw, root)
    assert reloaded_proposal.intent.scientific_action_fingerprint == action
    assert reloaded_proposal.scan_design_inventory.content_hash() == current.content_hash()
    assert reloaded_objective.scan_window_eig is not None


def test_grid_fault_provenance_selects_exact_mismatching_job_not_later_recovery():
    state = {"acquisition_provenance": [
        {"job_id": "fault-job", "acquisition_id": "logical-1",
         "scan_design_executed_grid_status": "mismatch"},
        {"job_id": "recovery-job", "acquisition_id": "logical-1",
         "scan_design_executed_grid_status": "not_applicable"},
    ]}
    emitted = {"kind": "exception", "payload": {
        "action": "executed_grid_mismatch", "job_id": "fault-job"}}

    provenance = fixtures._grid_fault_provenance(state, emitted)

    assert provenance["job_id"] == "fault-job"
    assert provenance["scan_design_executed_grid_status"] == "mismatch"


def test_case1_retention_uses_strict_production_comparison_and_authentic_records(
        tmp_path):
    retained = Path(
        "docs/campaigns/2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V2/"
        "artifacts/s8-c01-strong-confirmed")
    source = tmp_path / "source"
    production = tmp_path / "production"
    shutil.copytree(retained, source)
    shutil.copytree(retained, production)
    comparison_ref = {
        "kind": "scan_design_realized_comparison",
        "path": (
            "report/scan-design-realizations/"
            "6052e0b1f6ab4ce512f79a6373d159eb765868a8c45f73667f7d1cc1679ec113.json"),
        "sha256": "6052e0b1f6ab4ce512f79a6373d159eb765868a8c45f73667f7d1cc1679ec113",
    }
    comparison = load_scan_design_realized_comparison(comparison_ref, production)
    physical = {
        "job_id": "physical-job", "acquisition_id": "current-action",
        "scan_design_realized_comparison_fingerprint": (
            comparison.realized_comparison_fingerprint),
    }
    acquisition = {"job_id": "physical-job", "acquisition_id": "current-action"}
    raw = {"realized_comparison_ref": None}

    fixtures.retain_case1_production_comparison(
        raw, source=source, production_workspace=production,
        comparison_ref=comparison_ref, physical_submission=physical,
        acquisition_provenance=acquisition)

    assert raw["realized_comparison_ref"] == comparison_ref
    assert load_scan_design_realized_comparison(
        raw["realized_comparison_ref"], source).realized_comparison_fingerprint == (
            comparison.realized_comparison_fingerprint)
    assert json.loads((source / "report/current-submission.json").read_text(
        encoding="utf-8")) == physical
    assert json.loads((source / "report/current-acquisition-provenance.json").read_text(
        encoding="utf-8")) == acquisition


SUPERSEDED_STATUS = Path("tools/stats/eig-slice8-gate.superseded-status.json")


def test_slice8_gate_is_superseded_with_frozen_artifacts_intact():
    """H4 (universal Bose factor) changed model content fingerprints, reshuffling the seeded-MC
    blocks past the 128x8 regime margin: the frozen repair/replacement narrative cannot reproduce
    under the current model, by mechanism rather than by defect. Owner ruling 2026-08-08:
    supersede with a status record (the stat2b precedent); the freeze stays authentic at its
    commit; no successor freeze (S10/H1) — the machinery deletes at SCENE-4/5."""
    status = json.loads(SUPERSEDED_STATUS.read_text(encoding="utf-8"))
    assert status["status"] == "superseded_by_h4_universal_bose_factor"
    assert status["gate_commit"] == "0285e4d79663c5323811e36d28faa15348b31380"
    assert status["frozen_artifacts_remain_authentic_at_commit"] is True
    assert status["development_regime_reverified_under_h4"] is True
    # the frozen artifacts themselves stay in the tree, untouched (S10)
    for frozen in (CASE2_PROTOCOL, CASE2_TABLE, CASE2_REPRODUCIBILITY, REPLACEMENT_PARAMETERS):
        assert frozen.exists()


def test_case2_replacement_protocol_predeclares_only_an_amplitude_ladder():
    protocol = json.loads(CASE2_PROTOCOL.read_text(encoding="utf-8"))

    fixtures.validate_case2_calibration_protocol(protocol)

    assert protocol["case_generation_seed"] == 8040802
    assert protocol["fixed_physical_parameters"] == {
        "q": [2.15, 0.0, 0.0], "draw_energies_meV": [1.9, 2.1],
        "target_energy_meV": 2.0, "prediction_sigma_meV": 0.4,
        "ncount": 1, "survey": True,
    }
    assert protocol["amplitude_ladder"][0] == 0.3
    assert protocol["selection_rule"]["order"] == "first_ladder_value"
    assert protocol["scoring_contract"]["simulation_count"] == 128
    assert protocol["scoring_contract"]["block_count"] == 8
    assert protocol["policy_contract"]["offsets_meV"] == [0.0, 0.25]


def test_case2_replacement_protocol_rejects_changes_to_fixed_parameters():
    protocol = json.loads(CASE2_PROTOCOL.read_text(encoding="utf-8"))
    protocol["fixed_physical_parameters"]["prediction_sigma_meV"] = 0.5

    with pytest.raises(gate.GateEvidenceError, match="fixed physical parameter"):
        fixtures.validate_case2_calibration_protocol(protocol)


def test_case2_calibration_retains_ladder_and_first_qualifying_value():
    protocol = json.loads(CASE2_PROTOCOL.read_text(encoding="utf-8"))
    table = json.loads(CASE2_TABLE.read_text(encoding="utf-8"))
    reproducibility = json.loads(
        CASE2_REPRODUCIBILITY.read_text(encoding="utf-8"))

    fixtures.validate_case2_calibration_table(table, protocol)
    fixtures.validate_case2_reproducibility(reproducibility, table)

    assert len(table["rows"]) == len(protocol["amplitude_ladder"])
    assert table["rows"][0]["qualifies"] is False
    assert table["selected_ladder_index"] == 1
    assert table["selected_amplitude"] == 0.275
    assert table["rows"][1]["qualifies"] is True
    assert reproducibility["scorer_seed"] == 8374860497622894989


def test_replacement_narrative_no_longer_reproduces_under_h4(tmp_path):
    """The teeth of the supersession record: if this rebuild ever reproduces the frozen
    replacement narrative again (calibration reproduced AND case-2 unresolvable at 128x8),
    the supersession was unnecessary and must be revisited — most likely an accidental
    reversion of the H4 reparameterization."""
    protocol = json.loads(CASE2_PROTOCOL.read_text(encoding="utf-8"))
    table = json.loads(CASE2_TABLE.read_text(encoding="utf-8"))
    reproducibility = json.loads(
        CASE2_REPRODUCIBILITY.read_text(encoding="utf-8"))
    parameters = json.loads(REPLACEMENT_PARAMETERS.read_text(encoding="utf-8"))

    reproduced = True
    try:
        development = fixtures.build_replacement_development_inputs(
            tmp_path, parameters, protocol, table, reproducibility,
            execute_live=False)
        assessment = development["fixture_definitions"]["regime_assessment"]
        reproduced = (
            bool(development["calibration_reproduced_at_prefreeze"])
            and assessment["case_2"] == {
                "delta_positive": True, "lower_margin_positive": False})
    except gate.GateEvidenceError:
        reproduced = False
    assert reproduced is False
