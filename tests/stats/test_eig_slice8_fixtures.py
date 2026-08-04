import json
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
    "docs/campaigns/2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR/"
    "case2-amplitude-calibration-table.json")
CASE2_REPRODUCIBILITY = Path(
    "docs/campaigns/2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR/"
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


def test_repair_prefreeze_rejects_contrary_128_by_8_regime(tmp_path):
    parameters = json.loads(PARAMETERS.read_text(encoding="utf-8"))
    with pytest.raises(gate.GateEvidenceError, match=(
            r'"case_1":\{"delta_positive":true,"lower_margin_positive":true\}.*'
            r'"case_2":\{"delta_positive":true,"lower_margin_positive":true\}')):
        fixtures.build_repair_development_inputs(
            tmp_path, parameters, execute_live=False)


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


def test_replacement_prefreeze_reproduces_calibration_and_keeps_case2_unresolved(
        tmp_path):
    protocol = json.loads(CASE2_PROTOCOL.read_text(encoding="utf-8"))
    table = json.loads(CASE2_TABLE.read_text(encoding="utf-8"))
    reproducibility = json.loads(
        CASE2_REPRODUCIBILITY.read_text(encoding="utf-8"))
    parameters = json.loads(REPLACEMENT_PARAMETERS.read_text(encoding="utf-8"))

    development = fixtures.build_replacement_development_inputs(
        tmp_path, parameters, protocol, table, reproducibility,
        execute_live=False)

    assessment = development["fixture_definitions"]["regime_assessment"]
    case_2 = development["diagnostics"]["s8-c02-weak-unresolved-confirmed"]
    delta, standard_error, lower = gate._paired_summary(case_2["blocks"])
    assert development["contract"] == fixtures.REPLACEMENT_DEVELOPMENT_SPEC_CONTRACT
    assert development["calibration_reproduced_at_prefreeze"] is True
    assert assessment["case_2"] == {
        "delta_positive": True, "lower_margin_positive": False}
    assert delta > 0.0
    assert standard_error > 0.0
    assert lower <= 0.0
    assert gate.CASE_DEFINITIONS[1]["expected_outcome"] == gate.OUTCOME_LEGACY
    gate_manifest = gate.build_replacement_gate_manifest("a" * 40)
    case_manifest = gate.build_replacement_case_manifest(
        gate_manifest, development["fixture_definitions"])
    assert gate.validate_replacement_case_manifest(
        case_manifest, gate_manifest) == case_manifest
