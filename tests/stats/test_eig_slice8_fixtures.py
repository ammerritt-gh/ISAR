import json
from pathlib import Path

import pytest

from isar.drive.acquisition.realized_comparison import load_scan_design_realized_comparison
from isar.drive.acquisition.scan_design import load_scan_design_inventory
from tools.stats import eig_slice8_fixtures as fixtures
from tools.stats import eig_slice8_gate as gate


PARAMETERS = Path("tools/stats/eig_slice8_fixture_parameters.json")


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


def test_repair_prefreeze_rejects_contrary_128_by_8_regime(tmp_path):
    parameters = json.loads(PARAMETERS.read_text(encoding="utf-8"))
    with pytest.raises(gate.GateEvidenceError, match=(
            r'"case_1":\{"delta_positive":true,"lower_margin_positive":true\}.*'
            r'"case_2":\{"delta_positive":true,"lower_margin_positive":true\}')):
        fixtures.build_repair_development_inputs(
            tmp_path, parameters, execute_live=False)
