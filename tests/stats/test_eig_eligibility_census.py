"""Focused contract tests for the read-only EIG-4 Slice 1 census tool."""
from __future__ import annotations

import importlib.util
import json
import sys
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import numpy as np
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
    private_one = str((tmp_path / "private-marker-one").resolve())
    private_two = str((tmp_path / "private-marker-two").resolve())
    spec = result.campaign_set.campaigns[0]
    result.campaign_set = ENGINE.CampaignSet(
        (replace(spec, lineage=private_one, note=private_two),),
        result.campaign_set.source)
    for row in result.rows:
        row["lineage"] = private_one
        row["metadata"] = {private_one: private_one, private_two: private_two}
    first = ENGINE.write_census_bundle(result, tmp_path / "out-one", repo_root=tmp_path)
    second = ENGINE.write_census_bundle(result, tmp_path / "out-two", repo_root=tmp_path)
    for name in ("masters.jsonl", "masters.csv", "summary.json", "findings.md"):
        assert first[name].read_bytes() == second[name].read_bytes()
    with pytest.raises(ENGINE.ConfigError):
        ENGINE.write_census_bundle(result, tmp_path / "campaign" / "nested-output", repo_root=tmp_path)
    for path in first.values():
        content = path.read_text(encoding="utf-8")
        assert str(tmp_path) not in content
        assert "private-marker" not in content
        assert Path(tmp_path).drive not in content
        assert Path.home().name not in content
    rows = json.loads(first["masters.jsonl"].read_text(encoding="utf-8").splitlines()[0])
    assert len(rows["metadata"]) == 2
    assert all(key.startswith("<redacted-absolute-path>") for key in rows["metadata"])


def test_shared_projection_payload_is_joined_per_master_not_cached_as_a_verdict():
    predictive = {
        "strict_validation": {"status": "passed", "reason_code": None},
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


@pytest.mark.parametrize(("predictive_status", "projection_status"), [
    ("failed", "passed"),
    ("passed", "failed"),
])
def test_projection_joins_and_particle_masses_require_both_strict_artifacts(
        predictive_status: str, projection_status: str):
    predictive = {
        "strict_validation": {"status": predictive_status, "reason_code": "tempting_invalid"},
        "_record": {
            "models": [{"label": "one", "identity_label": "one", "weight": 1.0,
                        "source_weight": 1.0, "retained_draw_count": 2}],
        },
        "masses": ENGINE._empty_masses(),
    }
    raw = {
        "strict_validation": {"status": projection_status, "reason_code": "tempting_invalid"},
        "findings": [],
        "record": {
            "authority": {},
            "models": [{"label": "one", "identity_label": "one", "weight": 1.0,
                        "target_present_by_draw": [False, False],
                        "target_qoi_draws": [None, None]}],
        },
    }
    details = ENGINE._projection_details(raw, predictive, {}, None, None)
    assert set(details["joins"].values()) == {"unknown"}
    assert details["particle_masses"] == {
        "qoi_unavailable_source_mass": 0.0,
        "target_absent_source_mass": 0.0,
    }
    row = _classification_row(predictive=predictive, projection=details)
    ENGINE._classify(row)
    assert "material_qoi_unavailable_mass" not in row["findings"]
    assert "material_target_absent_mass" not in row["findings"]


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
        "outcome": {"omega0": 2.0, "track_state": "confirmed", "pool_consistent": True,
                    "target_delivery_eligible": True, "modality": "fold"},
        "member_join": {"status": "pass", "reason_code": None,
                        "acquisition_provenance_status": "pass"},
        "acquisition_intent": "production",
        "selection": {"strict_validation": {"status": "passed", "reason_code": None}},
        "selection_record": {}, "selection_diagnostics": {},
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
    from isar.drive.acquisition.scan_design import ScanDesignContext, design_scan_windows

    root = tmp_path / "campaign"
    directory = root / "report" / "scan-design"
    directory.mkdir(parents=True)
    context = ScanDesignContext(
        scientific_action_fingerprint="science", hkl=(2.1, 0.0, 0.0), q=0.1,
        kind="quicklook", reason="test", window_family="target", declared_energy_lo=None,
        declared_energy_hi=None, target_prediction_mean_meV=5.0, target_prediction_sigma_meV=0.2,
        design_prediction_mean_meV=5.0, design_prediction_sigma_meV=0.2, resolution_fwhm_meV=0.8,
        survey=False, energy_search_meV=(-2.0, 7.0), target_band=None, rival_band=None,
        widen_factor=1.0, positive_only=False, policy_max_points=100, runtime_max_points=200,
        tolerance_meV=0.5, quicklook_ncount=1000, production_ncount=10000,
        identity_detection_ncount=2000, dynamic_ncount=False, tolerance_margin=0.8,
        ncount_growth_cap=4.0, max_ncount_per_scan=1000000, diagnostic_halfwidth_meV=None,
        certification_halfwidth_meV=None, effective_exposure=None, submitted_exposure=None,
        production_scans=0, last_submitted_ncount=None, runtime_max_neutrons_per_point=None,
        runtime_queue_neutron_budget=None, budget_kind="neutrons", budget_amount=1e12, budget_spent=0.0)
    produced = design_scan_windows(context)
    inventory = produced.to_full_dict()
    digest = produced.content_hash()
    (directory / f"{digest}.json").write_text(json.dumps(inventory), encoding="utf-8")
    ref = {"kind": "scan_design_inventory", "sha256": digest,
           "path": f"report/scan-design/{digest}.json"}
    events = [
        {"kind": "scan_design_realization", "payload": {
            "schema": "isar.scan_design_realization/1", "inventory_ref": ref,
            "candidate_id": produced.selected_candidate_id, "adjustment": "minimum_target_support",
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
    assert details["selected_candidate_family"] == produced.selected_candidate.candidate_family
    assert details["realization_events"] == [
        "recovery_completed", "scan_design_realization:minimum_target_support"]
    malformed = json.loads(json.dumps(inventory))
    malformed["selection"]["selected_candidate_id"] = "not-a-candidate"
    malformed_digest = ENGINE._sha256_bytes(canonical_json(malformed).encode("utf-8"))
    (directory / f"{malformed_digest}.json").write_text(json.dumps(malformed), encoding="utf-8")
    malformed_ref = {"kind": "scan_design_inventory", "sha256": malformed_digest,
                     "path": f"report/scan-design/{malformed_digest}.json"}
    assert ENGINE._scan_design_details(root, malformed_ref)["reason_code"] == "selected_candidate_invalid"
    (directory / f"{digest}.json").write_text(json.dumps(malformed), encoding="utf-8")
    assert ENGINE._scan_design_details(root, ref)["reason_code"] == "content_address_mismatch"


def _produced_predictive_artifact(monkeypatch, weights: dict[str, float], bases: dict[str, str]) -> dict:
    """Build a real current `/2` predictive artifact with controlled producer draws."""
    from isar.analyze.context import ScanContext
    from isar.session import predictive_artifact

    manifest = {
        "schema": "isar.model_manifest/1",
        "components": [{"type": "phonon_dho", "prefix": "phonon", "peak": "stokes+astokes"},
                       {"type": "constant_background", "prefix": "bg"}],
        "parameters": {"phonon.Omega_d": 3.0, "phonon.Gamma": 0.3,
                       "phonon.A": 100.0, "bg.BG": 4.0},
    }
    names = ("phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG")
    draws = np.asarray([[3.0, 0.3, 100.0, 4.0], [3.1, 0.35, 110.0, 5.0]])
    monkeypatch.setattr(predictive_artifact, "model_manifest_of",
                        lambda result, by_hyp: manifest)
    monkeypatch.setattr(
        predictive_artifact, "bounded_parameter_draws",
        lambda result, **kwargs: (
            names, draws, bases[result.label], {
                "draw_weighting": "equal_weight_resampled", "draw_weights": None,
                "draw_ids": ["draw-0", "draw-1"], "source_draw_count": 2,
                "retained_draw_count": 2, "dropped_draws": [], "draw_mass_retained": 1.0,
                "draw_cap": 64, "thinned_from_draw_count": 2,
            }))
    context = ScanContext(
        x=np.linspace(-2.0, 6.0, 5), counts=np.zeros(5), mask=np.ones(5, bool), T=100.0,
        center=0.0, gauss_fwhm=0.4, lorentz_fwhm=0.1, instrument="IN8",
        energy_status="absolute", exposure_scale=np.ones(5), exposure_reference=1.0)
    fits = {label: SimpleNamespace(label=label, omega0=3.0) for label in weights}
    scores = [SimpleNamespace(label=label, weight=weight) for label, weight in weights.items()]
    return predictive_artifact.build_master_predictive(
        SimpleNamespace(omega0=3.0), scores, fits, {label: object() for label in weights}, context,
        seed=11, n_members=1)


@pytest.mark.parametrize(("basis", "key"), [
    ("parametric_bootstrap", "observed_bootstrap_source_mass"),
    ("nested_sampling", "observed_nested_source_mass"),
    ("laplace_hesse", "observed_laplace_source_mass"),
])
def test_authenticated_current_predictive_bases_are_producer_grounded(
        tmp_path: Path, monkeypatch, basis: str, key: str):
    payload = _produced_predictive_artifact(monkeypatch, {"one": 1.0}, {"one": basis})
    root = tmp_path / "campaign"
    path = root / "data" / "master_predictive" / "producer.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    details = ENGINE._predictive_details(root, "data/master_predictive/producer.json", {})
    assert details["strict_validation"] == {"status": "passed", "reason_code": None}
    assert details["masses"][key] == 1.0
    assert details["masses"]["basis_unknown_or_pruned_source_mass"] == 0.0


def test_rejected_current_predictive_payload_cannot_contribute_mass_or_exclusions(tmp_path: Path):
    root = tmp_path / "campaign"
    path = root / "data" / "master_predictive" / "rejected.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "schema": ENGINE.PREDICTIVE_ARTIFACT_SCHEMA,
        "models": [{
            "label": "tempting", "source_weight": 1.0,
            "draw_basis": "nested_sampling", "draw_mass_retained": 0.0,
        }],
        "completeness": {"dropped_models": [{
            "label": "also-tempting", "original_weight": 1.0,
            "reason": "no_usable_parameter_draws",
        }]},
    }), encoding="utf-8")
    predictive = ENGINE._predictive_details(
        root, "data/master_predictive/rejected.json", {})
    assert predictive["strict_validation"]["status"] == "failed"
    assert predictive["masses"] == ENGINE._empty_masses()

    row = _classification_row(predictive=predictive)
    ENGINE._classify(row)
    assert "material_nonbootstrap_mass" not in row["findings"]
    assert "material_particle_basis_unknown_or_pruned" not in row["findings"]


def test_mixed_producer_bases_trigger_material_nonbootstrap_not_unknown(tmp_path: Path, monkeypatch):
    payload = _produced_predictive_artifact(
        monkeypatch, {"bootstrap": 0.5, "nested": 0.25, "laplace": 0.25},
        {"bootstrap": "parametric_bootstrap", "nested": "nested_sampling", "laplace": "laplace_hesse"})
    root = tmp_path / "campaign"
    path = root / "data" / "master_predictive" / "producer.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    predictive = ENGINE._predictive_details(root, "data/master_predictive/producer.json", {})
    assert predictive["strict_validation"] == {"status": "passed", "reason_code": None}
    row = _classification_row(predictive=predictive)
    ENGINE._classify(row)
    assert "material_nonbootstrap_mass" in row["findings"]
    assert "material_particle_basis_unknown_or_pruned" not in row["findings"]


@pytest.mark.parametrize(("delivery", "modality", "finding"), [
    (None, "fold", "target_delivery_eligibility_unknown"),
    ("yes", "fold", "target_delivery_eligibility_unknown"),
    (False, "fold", "target_delivery_ineligible"),
    (True, "fold", None),
    (True, None, "modality_unknown"),
    (True, "fork", "modality_fork"),
    (True, "other", "modality_not_fold"),
])
def test_delivery_and_modality_gates_are_visible_at_campaign_level(tmp_path: Path, delivery, modality, finding: str):
    _write_campaign(tmp_path, outcome={"pooled": True, "omega0": 2.0, "track_state": "confirmed",
                                       "pool_consistent": True, "target_delivery_eligible": delivery,
                                       "modality": modality})
    row = next(item for item in ENGINE.analyze_campaign_set(_campaign_set(tmp_path), tmp_path).rows
               if item["row_kind"] == "pooled_outcome")
    if finding is None:
        assert "target_delivery_eligibility_unknown" not in row["findings"]
        assert "target_delivery_ineligible" not in row["findings"]
        assert "modality_unknown" not in row["findings"]
    else:
        assert finding in row["findings"]


@pytest.mark.parametrize(("target", "qoi"), [
    ([True], [None]), ([False], [1.0]), ([1], [1.0]), ([True, False], [1.0]),
])
def test_projection_alignment_is_structural_even_for_low_weight(target, qoi):
    predictive = {
        "strict_validation": {"status": "passed", "reason_code": None},
        "_record": {"models": [{"label": "one", "identity_label": "one", "weight": 1e-9,
                                  "retained_draw_count": 2}]},
    }
    record = {"models": [{"label": "one", "identity_label": "one", "weight": 1e-9,
                            "target_present_by_draw": target, "target_qoi_draws": qoi}], "authority": {}}
    raw = {"strict_validation": {"status": "passed", "reason_code": None},
           "findings": [], "record": record}
    details = ENGINE._projection_details(raw, predictive, {}, None, None)
    assert details["joins"]["draw_alignment"] == "fail"


def test_projection_alignment_checks_predictive_retained_count_independently():
    predictive = {"_record": {"models": [{
        "label": "one", "identity_label": "one", "weight": 1.0,
        "retained_draw_count": 3,
    }]}}
    record = {"models": [{
        "label": "one", "identity_label": "one", "weight": 1.0,
        "target_present_by_draw": [True, False], "target_qoi_draws": [1.0, None],
    }], "authority": {}}
    assert len(record["models"][0]["target_present_by_draw"]) == len(
        record["models"][0]["target_qoi_draws"])
    assert ENGINE._projection_joins(record, predictive)["draw_alignment"] == "fail"


def test_reason_precedence_selects_selection_before_projection_particle_and_shadow():
    row = _classification_row(
        predictive={"strict_validation": {"status": "passed", "reason_code": None},
                    "masses": {**ENGINE._empty_masses(), "observed_nested_source_mass": 1.0}},
        projection={"strict_validation": {"status": "passed", "reason_code": None}, "complete": True,
                    "currentness": "stale", "joins": {},
                    "particle_masses": {"qoi_unavailable_source_mass": 1.0, "target_absent_source_mass": 0.0}})
    row["shadow"] = {"strict_validation": {"status": "passed", "reason_code": None}, "category": "failure:comparison"}
    row["selection_record"] = {"model_weight_kind": "akaike_relative_weight", "selection_eligibility": "ineligible"}
    ENGINE._classify(row)
    assert row["primary_scientific_exclusion"] == "selection_ineligible"


def test_observability_reason_precedence_is_independent_of_evaluation_order():
    row = _classification_row()
    row["outcome"]["target_delivery_eligible"] = None
    row["outcome"]["modality"] = None
    row["member_join"] = {"status": "unknown", "reason_code": "member_provenance_missing",
                          "acquisition_provenance_status": "card_state_unavailable"}
    row["selection"] = {"strict_validation": {"status": "missing", "reason_code": "record_missing"}}
    row["predictive"] = {"strict_validation": {"status": "unsupported", "reason_code": "legacy_unauthenticated"},
                         "masses": ENGINE._empty_masses()}
    row["projection"]["currentness"] = "unknown"
    ENGINE._classify(row)
    assert row["primary_observability_gap"] == "target_delivery_eligibility_unknown"


def test_observability_precedence_keeps_predictive_before_projection_particle_and_shadow():
    row = _classification_row(
        predictive={
            "strict_validation": {"status": "failed", "reason_code": "predictive_artifact_invalid"},
            "masses": {**ENGINE._empty_masses(),
                       "basis_unknown_or_pruned_source_mass": 0.25}},
        projection={
            "strict_validation": {"status": "missing", "reason_code": "reference_unreadable"},
            "complete": None, "currentness": "unknown",
            "joins": {"predictive_basis": "unknown", "draw_alignment": "unknown"},
            "particle_masses": {"qoi_unavailable_source_mass": 0.0,
                                "target_absent_source_mass": 0.0},
        })
    row["selection_record"] = {
        "model_weight_kind": "akaike_relative_weight", "selection_eligibility": "eligible",
        "candidate_records": [],
        "candidate_generation_route": {
            "route_kind": "standard_two_stage", "labels_before_guideline": [],
            "offered_labels": [], "scored_labels": []},
    }
    row["shadow"] = {
        "strict_validation": {"status": "passed", "reason_code": None},
        "category": "legacy_unobservable",
    }
    ENGINE._classify(row)
    assert row["primary_observability_gap"] == "predictive_predictive_artifact_invalid"
    assert {
        "predictive_predictive_artifact_invalid", "projection_reference_unreadable",
        "projection_currentness_unknown", "projection_predictive_basis_unknown",
        "projection_draw_alignment_unknown", "material_particle_basis_unknown_or_pruned",
        "shadow_legacy_unauthenticated",
    } <= set(row["findings"])
    assert not set(ENGINE._SCIENTIFIC_REASON_PRECEDENCE) & set(
        ENGINE._OBSERVABILITY_REASON_PRECEDENCE)


def test_shadow_soft_deadline_requires_the_exact_current_contract_tuple():
    from isar.analyze.shadow_selection import unavailable_shadow_selection
    from isar.schema.statistics import semantics_fingerprint

    strict = {"status": "passed", "reason_code": None}
    neutral = unavailable_shadow_selection(
        "shadow_skipped_soft_deadline", stage="execution_policy",
        selection_context_fingerprint=semantics_fingerprint("test.shadow", {}),
        candidate_generation_route={"route_kind": "standard_two_stage"},
        legacy_model_selection_fingerprint=None, legacy_selected_label="one",
        legacy_model_weights={"one": 1.0})
    assert ENGINE._strict_block(neutral, ENGINE.validate_shadow_model_selection) == strict
    deadline_failure = unavailable_shadow_selection(
        "deadline_other", stage="comparison",
        selection_context_fingerprint=semantics_fingerprint("test.shadow", {}),
        candidate_generation_route={"route_kind": "standard_two_stage"},
        legacy_model_selection_fingerprint=None, legacy_selected_label="one",
        legacy_model_weights={"one": 1.0})
    assert ENGINE._shadow_category(neutral, strict) == "soft_deadline_skip"
    failure_strict = ENGINE._strict_block(deadline_failure, ENGINE.validate_shadow_model_selection)
    assert failure_strict == strict
    assert ENGINE._shadow_category(deadline_failure, failure_strict) == "failure:comparison"
    legacy = dict(neutral)
    legacy["schema"] = "isar.shadow_model_selection/1"
    legacy.pop("shadow_execution_kind")
    legacy.pop("reused_model_identities")
    legacy.pop("freshly_fitted_model_identities")
    legacy_strict = ENGINE._strict_block(legacy, ENGINE.validate_shadow_model_selection)
    assert legacy_strict == strict
    assert ENGINE._shadow_category(legacy, legacy_strict) == "legacy_unobservable"


def _producer_fit_semantics(*, label: str, constraints: list[dict], reasons: list[str]):
    from isar.schema.statistics import (FIT_SELECTION_SCHEMA, authority_scopes,
                                        semantics_fingerprint, validate_for_write)

    data = semantics_fingerprint("test.data", {"counts": [2.0, 3.0]})
    model = semantics_fingerprint("test.model", {"label": label, "parameters": ["a"]})
    constraint = semantics_fingerprint("test.constraints", constraints)
    comparison = semantics_fingerprint("test.comparison", {
        "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model,
        "constraint_fingerprint": constraint,
        "estimator_kind": "soft_penalized_likelihood",
    })
    origins = {item["origin"] for item in constraints}
    return validate_for_write({
        "schema": FIT_SELECTION_SCHEMA,
        "estimator_kind": "soft_penalized_likelihood",
        "fit_kind": "unconstrained",
        "effective_constraints": constraints,
        "aggregate_constraint_origin": next(iter(origins)) if len(origins) == 1 else "mixed",
        "selection_eligibility": "ineligible_soft_penalized",
        "selection_ineligibility_reasons": reasons,
        "data_likelihood_fingerprint": data,
        "model_parameterization_fingerprint": model,
        "constraint_fingerprint": constraint,
        "comparison_context_fingerprint": comparison,
        "conditioning_fingerprint": comparison,
        "authority_scopes": authority_scopes(single_fit_delivery="active"),
    }, FIT_SELECTION_SCHEMA)


def test_selection_diagnostics_use_producer_candidate_fit_semantics_with_multiplicity(tmp_path: Path):
    from isar.session import driver

    constraints = [
        {"parameter": "phonon.Gamma", "constraint_kind": "soft", "mean": 0.3, "sigma": 0.1,
         "origin": "prior_predictive"},
        {"parameter": "phonon.A", "constraint_kind": "hard", "value": 25.0,
         "origin": "post_selection"},
    ]
    reasons = ["data_dependent_rescue", "post_selection_conditioning"]
    semantics = {
        label: _producer_fit_semantics(
            label=label, constraints=deepcopy(constraints), reasons=list(reasons))
        for label in ("one", "two")
    }
    results = [
        SimpleNamespace(label=label, fit_semantics=semantics[label],
                        within_model={"omega0_samples": np.array([1.0, 1.1])})
        for label in ("one", "two")
    ]
    scores = [
        SimpleNamespace(label=label, cost=10.0 + index, k=2, aic=14.0 + index,
                        weight=0.6 - 0.2 * index, survives=True,
                        fit_semantics=semantics[label])
        for index, label in enumerate(("one", "two"))
    ]
    route = {
        "route": "full_two_stage",
        "labels_before_guideline": ["one", "three", "two"],
        "guideline_structure_filter": ["one", "two"],
        "offered_labels": ["one", "two"],
        "scored_labels": ["one", "two"],
    }
    record = driver._build_selection_semantics(
        results, SimpleNamespace(scores=scores, pooled=object()), route,
        error_attachment_labels=["one", "two"])
    assert record is not None
    strict = ENGINE._strict_block(record, ENGINE.validate_fit_selection_semantics)
    assert strict == {"status": "passed", "reason_code": None}
    assert all(set(candidate) == {
        "label", "cost", "effective_parameter_count", "aic", "akaike_weight", "fit_semantics"
    } for candidate in record["candidate_records"])
    assert record["candidate_generation_route"] == route
    diagnostics = ENGINE._selection_diagnostics(record, strict)
    assert diagnostics["estimator_kinds"] == ["soft_penalized_likelihood"] * 2
    assert diagnostics["candidate_selection_eligibilities"] == ["ineligible_soft_penalized"] * 2
    assert diagnostics["selection_ineligibility_reason_codes"] == [
        "data_dependent_rescue", "data_dependent_rescue",
        "post_selection_conditioning", "post_selection_conditioning"]
    assert diagnostics["soft_effective_constraint_count"] == 2
    assert diagnostics["hard_effective_constraint_count"] == 2
    assert diagnostics["constraint_kinds"] == ["hard", "hard", "soft", "soft"]
    assert diagnostics["constraint_origins"] == [
        "post_selection", "post_selection", "prior_predictive", "prior_predictive"]
    assert diagnostics["rescue_or_post_selection_reason_codes"] == [
        "data_dependent_rescue", "data_dependent_rescue",
        "post_selection_conditioning", "post_selection_conditioning"]
    assert diagnostics["route"]["route_kind"] == "standard_two_stage"
    assert diagnostics["route"]["guideline_altered"] is True
    assert diagnostics["route"]["offered_scored_incomplete"] is False
    guideline_findings = ENGINE._selection_science_findings(record, strict)
    assert "selection_guideline_altered_model_set" in guideline_findings
    assert "selection_model_set_incomplete" not in guideline_findings
    assert "selection_not_full_two_stage" not in guideline_findings

    incomplete_record = deepcopy(record)
    incomplete_record["candidate_generation_route"]["labels_before_guideline"] = ["one", "two"]
    incomplete_record["candidate_generation_route"]["scored_labels"] = ["one"]
    incomplete_strict = ENGINE._strict_block(
        incomplete_record, ENGINE.validate_fit_selection_semantics)
    assert incomplete_strict == {"status": "passed", "reason_code": None}
    incomplete_diagnostics = ENGINE._selection_diagnostics(
        incomplete_record, incomplete_strict)
    assert incomplete_diagnostics["route"]["guideline_altered"] is False
    assert incomplete_diagnostics["route"]["offered_scored_incomplete"] is True
    incomplete_findings = ENGINE._selection_science_findings(
        incomplete_record, incomplete_strict)
    assert "selection_guideline_altered_model_set" not in incomplete_findings
    assert "selection_model_set_incomplete" in incomplete_findings
    assert "selection_not_full_two_stage" not in incomplete_findings

    shortcut_record = deepcopy(record)
    shortcut_record["candidate_generation_route"]["route"] = "structure_hint_shortcut"
    shortcut_strict = ENGINE._strict_block(
        shortcut_record, ENGINE.validate_fit_selection_semantics)
    assert shortcut_strict == {"status": "passed", "reason_code": None}
    shortcut_diagnostics = ENGINE._selection_diagnostics(shortcut_record, shortcut_strict)
    assert shortcut_diagnostics["route"]["route_kind"] == "structure_hint_shortcut"
    assert "selection_not_full_two_stage" in ENGINE._selection_science_findings(
        shortcut_record, shortcut_strict)
    row = _classification_row()
    row["selection_record"] = record
    row["selection"] = {"strict_validation": strict}
    row["selection_diagnostics"] = diagnostics
    ENGINE._classify(row)
    row.update(row_kind="pooled_outcome", campaign_id="selection", cohort="ordinary",
               evidence_tier="primary", lineage="selection", master_session_key="master:selection",
               detector_id="det")
    spec = ENGINE.CampaignSpec(
        campaign_id="selection", cohort="ordinary", evidence_tier="primary",
        root=tmp_path, root_relative=".", lineage="selection",
        summary=None, mapping_kind=None, note=None)
    summary = ENGINE._summary(ENGINE.CensusResult(
        campaign_set=ENGINE.CampaignSet((spec,), tmp_path / "campaigns.toml"), rows=[row],
        warnings=[], input_hashes={}, elapsed_seconds=0.0))
    group = summary["groups"]["primary_combined"]
    assert group["selection_estimator_kinds"] == {"soft_penalized_likelihood": 2}
    assert group["selection_candidate_eligibilities"] == {"ineligible_soft_penalized": 2}
    assert group["selection_ineligibility_reason_codes"] == {
        "data_dependent_rescue": 2, "post_selection_conditioning": 2}
    assert group["selection_constraint_kinds"] == {"hard": 2, "soft": 2}
    assert group["selection_constraint_origins"] == {
        "post_selection": 2, "prior_predictive": 2}


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
    row["selection_diagnostics"] = {
        "estimator_kinds": ["data_likelihood_mle"], "candidate_selection_eligibilities": ["eligible"],
        "selection_ineligibility_reason_codes": [],
        "constraint_kinds": ["soft"], "constraint_origins": ["prior_predictive"],
        "rescue_or_post_selection_reason_codes": ["post_selection_conditioning"],
    }
    ENGINE._classify(row)
    supplementary = deepcopy(row)
    supplementary.update(campaign_id="campaign-b", evidence_tier="supplementary",
                         master_session_key="master:b")
    supplementary["selection_diagnostics"] = row["selection_diagnostics"]
    supplementary["findings"] = []
    ENGINE._classify(supplementary)
    supplementary_spec = ENGINE.CampaignSpec(
        campaign_id="campaign-b", cohort="ordinary", evidence_tier="supplementary",
        root=tmp_path / "campaign-b", root_relative="campaign-b", lineage="lineage-a",
        summary=None, mapping_kind=None, note=None)
    result = ENGINE.CensusResult(
        campaign_set=ENGINE.CampaignSet((spec, supplementary_spec), tmp_path / "campaigns.toml"),
        rows=[row, supplementary], warnings=[], input_hashes={}, elapsed_seconds=0.0)
    summary = ENGINE._summary(result)
    assert summary["groups"]["primary_combined"]["particle_source_masses"][
        "qoi_unavailable_source_mass"] == 0.25
    assert summary["groups"]["primary_combined"]["particle_source_masses"][
        "target_absent_source_mass"] == 0.75
    for group in ("campaign:campaign-a", "campaign:campaign-b", "lineage:lineage-a", "cohort:ordinary",
                  "primary_cohort:ordinary", "primary_combined", "supplementary"):
        assert "all_reason_counts" in summary["groups"][group]
        assert "shadow_categories" in summary["groups"][group]
        assert "projection_currentness" in summary["groups"][group]
        assert "particle_source_masses" in summary["groups"][group]
        assert "selection_estimator_kinds" in summary["groups"][group]
        assert summary["groups"][group]["selection_estimator_kinds"]["data_likelihood_mle"] > 0
        assert summary["groups"][group]["selection_candidate_eligibilities"]["eligible"] > 0
        assert summary["groups"][group]["selection_constraint_kinds"]["soft"] > 0
        assert summary["groups"][group]["selection_constraint_origins"]["prior_predictive"] > 0
        assert summary["groups"][group]["selection_rescue_or_post_selection_reasons"]["post_selection_conditioning"] > 0
    findings = ENGINE._findings_markdown(summary)
    assert "| campaign:campaign-a |" in findings
    assert "| lineage:lineage-a |" in findings
    assert "## All observed reason codes" in findings
    assert "Projection currentness:" in findings
    assert "| Population | Estimators | Candidate eligibility | Ineligibility reasons | Constraints | Origins | Rescue/post-selection |" in findings
    for group in ("campaign:campaign-a", "lineage:lineage-a", "cohort:ordinary",
                  "primary_combined", "supplementary"):
        assert f"| {group} |" in findings
    assert "### campaign:campaign-a" not in findings
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
