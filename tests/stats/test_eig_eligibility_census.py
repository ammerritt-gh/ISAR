"""Focused contract tests for the read-only EIG-4 Slice 1 census tool."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
ENGINE_PATH = ROOT / "tools" / "stats" / "_eig_eligibility_census.py"
SPEC = importlib.util.spec_from_file_location("eig_census_test_engine", ENGINE_PATH)
assert SPEC is not None and SPEC.loader is not None
ENGINE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ENGINE
SPEC.loader.exec_module(ENGINE)


def _write_campaign(tmp_path: Path, *, outcome: dict | None = None, session: object | None = None,
                    state: dict | None = None) -> Path:
    campaign = tmp_path / "campaign"
    campaign.mkdir()
    if session is None:
        session = {
            "schema_version": "isar.session/3",
            "scans": {
                "master:one": {
                    "scan_id": "master:one", "scan_number": 1, "scan_type": "master",
                    "status": "fitted", "detectors": {"det": outcome or {
                        "pooled": True, "omega0": 2.0, "track_state": None,
                        "pool_consistent": True,
                    }}, "provenance": {},
                },
            },
        }
    (campaign / "session.json").write_text(json.dumps(session), encoding="utf-8")
    (campaign / "card.state.json").write_text(json.dumps(state or {}), encoding="utf-8")
    (campaign / "card.policy.toml").write_text("[experiment]\nname = 'synthetic'\n", encoding="utf-8")
    return campaign


def _campaign_set(tmp_path: Path, root: str = "campaign"):
    manifest = tmp_path / "campaigns.toml"
    manifest.write_text(
        "[[campaign]]\nid = 'one'\ncohort = 'ordinary'\nevidence_tier = 'primary'\n"
        f"root = '{root}'\nlineage = 'one'\n", encoding="utf-8")
    return ENGINE.load_campaign_set(manifest, tmp_path)


def test_missing_session_is_not_silently_an_empty_campaign(tmp_path: Path):
    (tmp_path / "campaign").mkdir()
    campaign_set = _campaign_set(tmp_path)
    with pytest.raises(ENGINE.NoReadableCampaignError):
        ENGINE.analyze_campaign_set(campaign_set, tmp_path)


def test_legacy_evidence_is_unassessable_not_false_zero_rate(tmp_path: Path):
    _write_campaign(tmp_path)
    result = ENGINE.analyze_campaign_set(_campaign_set(tmp_path), tmp_path)
    pooled = [row for row in result.rows if row["row_kind"] == "pooled_outcome"]
    assert len(pooled) == 1
    assert pooled[0]["current_contract_observability"] == "incomplete"
    assert pooled[0]["provisional_eligibility"] == "unassessable"
    paths = ENGINE.write_census_bundle(result, tmp_path / "output", repo_root=tmp_path)
    assert "Current-contract eligibility rate not measurable from the retained campaign population." in paths["findings.md"].read_text(encoding="utf-8")
    summary = json.loads(paths["summary.json"].read_text(encoding="utf-8"))
    assert summary["groups"]["primary_combined"]["headline_eligibility_rate"] is None


def test_explicit_science_failure_coexists_with_incomplete_observability(tmp_path: Path):
    _write_campaign(tmp_path, outcome={"pooled": True, "omega0": None, "track_state": "rejected",
                                       "pool_consistent": False})
    result = ENGINE.analyze_campaign_set(_campaign_set(tmp_path), tmp_path)
    row = next(row for row in result.rows if row["row_kind"] == "pooled_outcome")
    assert row["current_contract_observability"] == "incomplete"
    assert row["provisional_eligibility"] == "ineligible"
    assert {"target_not_finite", "track_not_confirmed", "pool_inconsistent"} <= set(row["findings"])


def test_nonpooled_master_is_anomaly_not_a_denominator_row(tmp_path: Path):
    _write_campaign(tmp_path, outcome={"pooled": False, "omega0": 2.0})
    result = ENGINE.analyze_campaign_set(_campaign_set(tmp_path), tmp_path)
    assert result.rows[0]["row_kind"] == "master_entry_without_pooled_outcome"
    paths = ENGINE.write_census_bundle(result, tmp_path / "output", repo_root=tmp_path)
    summary = json.loads(paths["summary.json"].read_text(encoding="utf-8"))
    assert summary["groups"]["primary_combined"]["pooled_outcomes"] == 0
    assert summary["row_kinds"]["master_entry_without_pooled_outcome"] == 1


def test_manifest_rejects_absolute_campaign_root(tmp_path: Path):
    manifest = tmp_path / "campaigns.toml"
    manifest.write_text("[[campaign]]\nid='one'\ncohort='ordinary'\nevidence_tier='primary'\n"
                        "root='C:/unsafe'\n", encoding="utf-8")
    with pytest.raises(ENGINE.ConfigError):
        ENGINE.load_campaign_set(manifest, tmp_path)


def test_output_is_deterministic_and_confined(tmp_path: Path):
    _write_campaign(tmp_path)
    result = ENGINE.analyze_campaign_set(_campaign_set(tmp_path), tmp_path)
    first = ENGINE.write_census_bundle(result, tmp_path / "out-one", repo_root=tmp_path)
    second = ENGINE.write_census_bundle(result, tmp_path / "out-two", repo_root=tmp_path)
    for name in ("masters.jsonl", "masters.csv", "summary.json", "findings.md"):
        assert first[name].read_bytes() == second[name].read_bytes()
    with pytest.raises(ENGINE.ConfigError):
        ENGINE.write_census_bundle(result, tmp_path / "campaign" / "nested-output", repo_root=tmp_path)
    for path in first.values():
        assert str(tmp_path) not in path.read_text(encoding="utf-8")


def test_shared_projection_payload_is_joined_per_master_not_cached_as_a_verdict():
    predictive = {
        "_record": {"predictive_basis_fingerprint": "basis-a", "predictive_retained_set_fingerprint": "retained-a",
                    "statistical_semantics_fingerprint": "semantics-a",
                    "models": [{"label": "one", "identity_label": "one", "weight": 1.0,
                                "source_weight": 1.0, "retained_draw_count": 2}]},
        "masses": ENGINE._empty_masses(),
    }
    projection_record = {"models": [{"label": "one", "identity_label": "one", "weight": 1.0,
                                      "target_present_by_draw": [True, False], "target_qoi_draws": [2.0, None]}],
                         "predictive_basis_fingerprint": "basis-a",
                         "source_predictive_retained_set_fingerprint": "retained-a",
                         "source_predictive_semantics_fingerprint": "semantics-a",
                         "authority": {}}
    projection_record["target_projection_set_fingerprint"] = ENGINE.semantics_fingerprint(
        "isar.target_projection_model_set/1", [{"label": "one", "weight": 1.0, "draw_count": 2}])
    raw = {"strict_validation": {"status": "passed", "reason_code": None}, "findings": [],
           "record": projection_record}
    first = ENGINE._projection_details(raw, predictive, {}, None, None)
    changed = {**predictive, "_record": {**predictive["_record"], "predictive_basis_fingerprint": "basis-b"}}
    second = ENGINE._projection_details(raw, changed, {}, None, None)
    assert first["joins"]["predictive_basis"] == "pass"
    assert second["joins"]["predictive_basis"] == "fail"
    assert first["particle_masses"]["target_absent_source_mass"] == 0.5


def test_acquisition_role_never_becomes_projection_authority_role():
    class Entry:
        provenance = {"target_projection_authority": {"action_role": "master", "master_revision": "r"}}
    projection = {"authority": {"action_role": "master", "master_revision": "r"}}
    outcome = {"master_revision": "r"}
    statuses = ENGINE._projection_currentness(projection, outcome, Entry(), None)
    assert statuses["action_role"] == "pass"
    assert statuses["master_revision"] == "pass"
    assert statuses["calibration_fingerprint"] == "unknown"


def _physical_master_session(*, outcome_members=None, declared_members=None):
    outcome_members = ["1"] if outcome_members is None else outcome_members
    declared_members = [["tavi", 1, "det"]] if declared_members is None else declared_members
    return {
        "schema_version": "isar.session/3",
        "scans": {
            "tavi:1:det": {
                "scan_id": "tavi:1:det", "scan_number": 1, "scan_type": "scan",
                "status": "fitted", "detectors": {"det": {"acquisition_id": "acq-1"}},
                "provenance": {},
            },
            "master:one": {
                "scan_id": "master:one", "scan_number": 1, "scan_type": "master",
                "status": "fitted", "detectors": {"det": {
                    "pooled": True, "omega0": 2.0, "track_state": "confirmed",
                    "pool_consistent": True, "members": outcome_members,
                    "evidence_sink": "canonical_master", "candidate_id": "branch-a",
                }}, "provenance": {"members": declared_members},
            },
        },
    }


def test_member_join_normalizes_bare_labels_and_uses_card_state_acquisition_provenance(tmp_path: Path):
    _write_campaign(
        tmp_path, session=_physical_master_session(),
        state={"acquisition_provenance": [{
            "acquisition_id": "acq-1", "job_id": "job-1",
            "acquisition_action_role": "production",
        }]})
    result = ENGINE.analyze_campaign_set(_campaign_set(tmp_path), tmp_path)
    row = next(row for row in result.rows if row["row_kind"] == "pooled_outcome")
    assert row["member_join"]["status"] == "pass"
    assert row["member_join"]["declared_members"] == [["tavi", "1", "det"]]
    assert row["member_join"]["outcome_members"] == [["tavi", "1", "det"]]
    assert row["member_join"]["acquisition_provenance_status"] == "pass"
    assert row["acquisition_intent"] == "production"
    assert "member_provenance_inconsistent" not in row["findings"]


def test_member_join_reports_only_a_true_logical_mismatch_as_inconsistent(tmp_path: Path):
    _write_campaign(tmp_path, session=_physical_master_session(outcome_members=["tavi:2:det"]))
    result = ENGINE.analyze_campaign_set(_campaign_set(tmp_path), tmp_path)
    row = next(row for row in result.rows if row["row_kind"] == "pooled_outcome")
    assert row["member_join"]["status"] == "fail"
    assert row["member_join"]["reason_code"] == "logical_member_mismatch"
    assert "member_provenance_inconsistent" in row["findings"]


def _write_projection(tmp_path: Path, payload: dict, name: str = "projection.json"):
    root = tmp_path / "campaign"
    directory = root / "data" / "target_projection"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return root, f"data/target_projection/{name}"


@pytest.mark.parametrize(("overrides", "status", "reason"), [
    ({"statistical_semantics": None}, "passed", None),
    ({"authority": None}, "failed", "authority_missing_or_malformed"),
    ({"models": {}}, "failed", "models_not_list"),
])
def test_projection_raw_matches_workspace_structural_and_optional_semantics_contract(
        tmp_path: Path, overrides: dict, status: str, reason: str | None):
    payload = {"schema": ENGINE.TARGET_PROJECTION_SCHEMA, "authority": {}, "models": []}
    payload.update(overrides)
    root, ref = _write_projection(tmp_path, payload)
    result = ENGINE._projection_raw(root, ref, {})
    assert result["strict_validation"] == {"status": status, "reason_code": reason}


def _classification_row(*, predictive=None, projection=None):
    return {
        "findings": [],
        "outcome": {"omega0": 2.0, "track_state": "confirmed", "pool_consistent": True},
        "member_join": {"status": "pass", "reason_code": None,
                        "acquisition_provenance_status": "pass"},
        "acquisition_intent": "production",
        "selection": {"strict_validation": {"status": "passed", "reason_code": None}},
        "selection_record": {},
        "predictive": predictive or {
            "strict_validation": {"status": "passed", "reason_code": None},
            "masses": ENGINE._empty_masses(),
        },
        "projection": projection or {
            "strict_validation": {"status": "passed", "reason_code": None},
            "complete": True, "currentness": "current", "joins": {},
            "particle_masses": {"qoi_unavailable_source_mass": 0.0,
                                "target_absent_source_mass": 0.0},
        },
        "shadow": {"strict_validation": {"status": "passed", "reason_code": None},
                   "category": "complete_agrees"},
        "scan_design": {"status": "passed", "reason_code": None, "realization_events": []},
    }


def test_row_particle_masses_do_not_mutate_or_accumulate_in_shared_predictive_view():
    shared = {"strict_validation": {"status": "passed", "reason_code": None},
              "masses": ENGINE._empty_masses()}
    first_projection = {"strict_validation": {"status": "passed", "reason_code": None},
                        "complete": True, "currentness": "current", "joins": {},
                        "particle_masses": {"qoi_unavailable_source_mass": 0.25,
                                            "target_absent_source_mass": 0.75}}
    second_projection = {"strict_validation": {"status": "passed", "reason_code": None},
                         "complete": True, "currentness": "current", "joins": {},
                         "particle_masses": {"qoi_unavailable_source_mass": 0.0,
                                             "target_absent_source_mass": 1.0}}
    first = _classification_row(predictive=shared, projection=first_projection)
    second = _classification_row(predictive=shared, projection=second_projection)
    ENGINE._classify(first)
    ENGINE._classify(second)
    assert shared["masses"] == ENGINE._empty_masses()
    assert first["particle_masses"]["qoi_unavailable_source_mass"] == 0.25
    assert second["particle_masses"]["qoi_unavailable_source_mass"] == 0.0
    assert first["particle_masses"]["target_absent_source_mass"] == 0.75
    assert second["particle_masses"]["target_absent_source_mass"] == 1.0


def test_incomplete_projection_is_an_explicit_exclusion_not_target_absence():
    projection = {"strict_validation": {"status": "passed", "reason_code": None},
                  "complete": False, "currentness": "current", "joins": {},
                  "particle_masses": {"qoi_unavailable_source_mass": 0.0,
                                      "target_absent_source_mass": 0.0}}
    row = _classification_row(projection=projection)
    ENGINE._classify(row)
    assert row["provisional_eligibility"] == "ineligible"
    assert "projection_incomplete" in row["findings"]
    assert "material_qoi_unavailable_mass" not in row["findings"]


def test_projection_details_exposes_structured_incomplete_predicate():
    raw = {"strict_validation": {"status": "passed", "reason_code": None},
           "findings": [], "complete": False,
           "record": {"authority": {}, "models": []}}
    details = ENGINE._projection_details(raw, {}, {}, None, None)
    assert details["completeness_status"] == "projection_incomplete"
    assert "projection_incomplete" in details["findings"]


def test_particle_masses_use_original_pruned_weight_and_authoritative_draw_mass():
    models = [{"source_weight": 0.6, "draw_basis": "parametric_bootstrap",
               "source_draw_count": 100, "retained_draw_count": 10,
               "draw_mass_retained": 1.0}]
    record = {"completeness": {"dropped_models": [
        {"label": "two", "original_weight": 0.4, "reason": "no_usable_parameter_draws"}
    ]}}
    masses = ENGINE._masses(models, record)
    assert masses["observed_bootstrap_source_mass"] == 0.6
    assert masses["basis_unknown_or_pruned_source_mass"] == 0.4
    assert masses["dropped_draw_mass"] == 0.0
    assert masses["draw_mass_unknown_source_mass"] == 0.0
    assert ENGINE._dropped_model_diagnostics(record) == [
        {"label": "two", "original_weight": 0.4, "reason": "no_usable_parameter_draws"}
    ]


def test_missing_draw_mass_is_unknown_not_inferred_from_thinned_counts():
    masses = ENGINE._masses([{
        "source_weight": 1.0, "draw_basis": "parametric_bootstrap",
        "source_draw_count": 100, "retained_draw_count": 10,
    }], {})
    assert masses["dropped_draw_mass"] == 0.0
    assert masses["draw_mass_unknown_source_mass"] == 1.0


def test_scan_design_events_use_typed_realization_and_acquisition_linked_recovery(tmp_path: Path):
    from isar.drive.acquisition.inventory import canonical_json

    root = tmp_path / "campaign"
    directory = root / "report" / "scan-design"
    directory.mkdir(parents=True)
    inventory = {
        "schema": "isar.scan_design_inventory/2",
        "selected_candidate_id": "candidate-a",
        "candidates": [{
            "schema": "isar.scan_design_candidate/2", "candidate_id": "candidate-a",
            "window_family": "target_core/1",
        }],
    }
    digest = ENGINE._sha256_bytes(canonical_json(inventory).encode("utf-8"))
    (directory / f"{digest}.json").write_text(json.dumps(inventory), encoding="utf-8")
    ref = {"kind": "scan_design_inventory", "sha256": digest,
           "path": f"report/scan-design/{digest}.json"}
    events = [
        {"kind": "scan_design_realization", "payload": {
            "schema": "isar.scan_design_realization/1", "inventory_ref": ref,
            "candidate_id": "candidate-a", "adjustment": "minimum_target_support",
        }},
        {"kind": "physical_submission", "payload": {
            "scan_design_inventory_ref": ref, "acquisition_id": "acq-1",
        }},
        {"kind": "recovery_completed", "payload": {"acquisition_id": "acq-1"}},
        {"kind": "recovery_pending", "payload": {"acquisition_id": "unrelated"}},
    ]
    (root / "report" / "session_log.jsonl").write_text(
        "".join(json.dumps(event) + "\n" for event in events), encoding="utf-8")
    details = ENGINE._scan_design_details(root, ref, acquisition_ids=("acq-1",))
    assert details["status"] == "passed"
    assert details["realization_events"] == [
        "recovery_completed", "scan_design_realization:minimum_target_support"]


def test_summary_and_findings_report_row_local_masses_and_every_population_axis(tmp_path: Path):
    spec = ENGINE.CampaignSpec(
        campaign_id="campaign-a", cohort="ordinary", evidence_tier="primary",
        root=tmp_path / "campaign", root_relative="campaign", lineage="lineage-a",
        summary=None, mapping_kind=None, note=None)
    row = _classification_row(projection={
        "strict_validation": {"status": "passed", "reason_code": None},
        "complete": True, "currentness": "current", "joins": {},
        "particle_masses": {"qoi_unavailable_source_mass": 0.25,
                            "target_absent_source_mass": 0.75},
    })
    row.update(row_kind="pooled_outcome", campaign_id="campaign-a", cohort="ordinary",
               evidence_tier="primary", lineage="lineage-a", master_session_key="master:a",
               detector_id="det")
    ENGINE._classify(row)
    result = ENGINE.CensusResult(
        campaign_set=ENGINE.CampaignSet((spec,), tmp_path / "campaigns.toml"),
        rows=[row], warnings=[], input_hashes={}, elapsed_seconds=0.0)
    summary = ENGINE._summary(result)
    assert summary["groups"]["primary_combined"]["particle_source_masses"][
        "qoi_unavailable_source_mass"] == 0.25
    assert summary["groups"]["primary_combined"]["particle_source_masses"][
        "target_absent_source_mass"] == 0.75
    for group in ("campaign:campaign-a", "lineage:lineage-a", "cohort:ordinary",
                  "primary_cohort:ordinary", "supplementary"):
        assert "all_reason_counts" in summary["groups"][group]
        assert "shadow_categories" in summary["groups"][group]
        assert "projection_currentness" in summary["groups"][group]
        assert "particle_source_masses" in summary["groups"][group]
    findings = ENGINE._findings_markdown(summary)
    assert "| campaign:campaign-a |" in findings
    assert "| lineage:lineage-a |" in findings
    assert "## All observed reason codes" in findings
    assert "Projection currentness:" in findings
    assert "## Historical limitations" in findings


def test_legacy_predictive_basis_weights_are_described_without_current_authority(tmp_path: Path):
    root = tmp_path / "campaign"
    directory = root / "data" / "master_predictive"
    directory.mkdir(parents=True)
    payload = {
        "schema": "isar.master_predictive/1",
        "models": [
            {"label": "one", "basis": "laplace_hesse", "weight": 0.6},
            {"label": "two", "basis": "parametric_bootstrap", "weight": 0.3},
            {"label": "three", "basis": "future_basis", "weight": 0.1},
        ],
    }
    path = directory / "legacy.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    predictive = ENGINE._predictive_details(
        root, "data/master_predictive/legacy.json", {})
    assert predictive["strict_validation"] == {
        "status": "unsupported", "reason_code": "legacy_unauthenticated"}
    assert predictive["masses"] == ENGINE._empty_masses()
    legacy = predictive["legacy_unauthenticated"]
    assert legacy["authority"] == "legacy_unauthenticated"
    assert legacy["raw_legacy_weight_total"] == 1.0
    assert legacy["basis_weight_distribution"] == {
        "laplace_hesse": 0.6, "nested_sampling": 0.0,
        "parametric_bootstrap": 0.3, "legacy_basis_unknown": 0.1,
    }
    assert legacy["models"][0] == {
        "label": "one", "basis": "laplace_hesse", "raw_legacy_weight": 0.6}

    spec = ENGINE.CampaignSpec(
        campaign_id="legacy", cohort="ordinary", evidence_tier="primary", root=root,
        root_relative="campaign", lineage="legacy", summary=None, mapping_kind=None, note=None)
    row = _classification_row(predictive=predictive)
    row.update(row_kind="pooled_outcome", campaign_id="legacy", cohort="ordinary",
               evidence_tier="primary", lineage="legacy", master_session_key="master:legacy",
               detector_id="det")
    ENGINE._classify(row)
    result = ENGINE.CensusResult(
        campaign_set=ENGINE.CampaignSet((spec,), tmp_path / "campaigns.toml"), rows=[row],
        warnings=[], input_hashes={}, elapsed_seconds=0.0)
    summary = ENGINE._summary(result)
    described = summary["groups"]["primary_combined"]["legacy_particle_description"]
    assert described["artifact_rows"] == 1
    assert described["basis_weight_distribution"]["laplace_hesse"] == 0.6
    assert summary["groups"]["primary_combined"]["particle_source_masses"][
        "observed_laplace_source_mass"] == 0.0
    findings = ENGINE._findings_markdown(summary)
    assert "## Legacy `/1` descriptive particle weights" in findings
    assert "- `laplace_hesse`: 0.6" in findings
    assert "do not establish current-contract particle authority or homogeneity" in findings
