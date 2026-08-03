from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from isar.drive import loop
from isar.drive.acquisition.acquire import ScanProposal, ShadowScoringSource
from isar.drive.acquisition.eig_shadow import (
    _exact_master,
    _exact_reference,
    _scan_design_source,
    score_proposal_shadow_inventory,
)
from isar.drive.acquisition.inventory import MeasurementIntent, canonical_json
from isar.drive.acquisition.scan_design import ScanDesignContext, design_scan_windows
from isar.drive.session_log import AnalysisLimitExceeded, read_log
from isar.eig.shadow_inventory import ScanDesignShadowScoring
from isar.schema.eig_policy import ScanWindowShadowScoringConfig


def _inventory():
    return design_scan_windows(ScanDesignContext(
        scientific_action_fingerprint="action", hkl=(2.1, 0.0, 0.0), q=0.1,
        kind="production", reason="test", window_family="target",
        declared_energy_lo=None, declared_energy_hi=None,
        target_prediction_mean_meV=5.0, target_prediction_sigma_meV=0.2,
        design_prediction_mean_meV=5.0, design_prediction_sigma_meV=0.2,
        resolution_fwhm_meV=0.8, survey=False, energy_search_meV=(-2.0, 7.0),
        target_band=None, rival_band=None, widen_factor=1.0, positive_only=False,
        policy_max_points=100, runtime_max_points=200, tolerance_meV=0.5,
        scan_window_eig_mode="shadow", minimum_target_support_points=6,
        quicklook_ncount=1_000, production_ncount=10_000,
        identity_detection_ncount=2_000, dynamic_ncount=False, tolerance_margin=0.8,
        ncount_growth_cap=4.0, max_ncount_per_scan=1_000_000,
        diagnostic_halfwidth_meV=None, certification_halfwidth_meV=None,
        effective_exposure=None, submitted_exposure=None, production_scans=0,
        last_submitted_ncount=None, runtime_max_neutrons_per_point=None,
        runtime_queue_neutron_budget=None, budget_kind="neutrons", budget_amount=1e12,
        budget_spent=0.0,
    ))


def _proposal():
    inventory = _inventory()
    candidate = inventory.selected_candidate
    return ScanProposal(
        h=2.1, k=0.0, l=0.0,
        e_lo=candidate.e_lo, e_hi=candidate.e_hi, e_step=candidate.e_step,
        ncount=candidate.ncount, n_points=candidate.n_points,
        kind="production", reason="test",
        meta={"scientific_action_fingerprint": "action", "proposal_fingerprint": "request"},
        scan_design_inventory=inventory,
    )


def _cfg(tmp_path):
    return SimpleNamespace(
        run_nonce="run", workspace=tmp_path,
        log_path=tmp_path / "report" / "session_log.jsonl")


def test_shadow_adapter_runs_inside_charged_operation_without_changing_request(
        monkeypatch, tmp_path):
    proposal = _proposal()
    selected = proposal.scan_design_inventory.selected_candidate_id
    request = (proposal.e_lo, proposal.e_hi, proposal.e_step, proposal.n_points,
               proposal.ncount, proposal.meta["proposal_fingerprint"])

    def score(value, *_args, **_kwargs):
        value.scan_design_inventory = value.scan_design_inventory.with_shadow_scoring(
            ScanDesignShadowScoring.unavailable_internal("qoi_policy_missing"))
        return value

    monkeypatch.setattr(loop, "score_proposal_shadow_inventory", score)
    loop._score_shadow_proposal(
        proposal, SimpleNamespace(analysis_limit_seconds=100.0), _cfg(tmp_path), it=3)

    assert proposal.scan_design_inventory.selected_candidate_id == selected
    assert request == (proposal.e_lo, proposal.e_hi, proposal.e_step, proposal.n_points,
                       proposal.ncount, proposal.meta["proposal_fingerprint"])
    clock = [event["payload"] for event in read_log(_cfg(tmp_path).log_path)
             if event["kind"] == "analysis_clock"]
    assert [item["phase"] for item in clock] == ["start", "end"]
    assert {item["stage"] for item in clock} == {"scan_window_shadow_scoring"}
    assert clock[-1]["elapsed_seconds"] >= 0.0


@pytest.mark.parametrize(("raised", "reason"), [
    (AnalysisLimitExceeded("soft deadline"), "shadow_scoring_deadline"),
    (RuntimeError("backend crash"), "shadow_scoring_failed"),
])
def test_shadow_deadline_and_crash_fail_open_on_the_same_legacy_candidate(
        monkeypatch, tmp_path, raised, reason):
    proposal = _proposal()
    selected = proposal.scan_design_inventory.selected_candidate_id

    def fail(*_args, **_kwargs):
        raise raised

    monkeypatch.setattr(loop, "score_proposal_shadow_inventory", fail)
    loop._score_shadow_proposal(
        proposal, SimpleNamespace(analysis_limit_seconds=100.0), _cfg(tmp_path), it=4)

    shadow = proposal.scan_design_inventory.shadow_scoring
    assert proposal.scan_design_inventory.selected_candidate_id == selected
    assert shadow.status == "unavailable"
    assert shadow.reason_codes == (reason,)
    failures = [event["payload"] for event in read_log(_cfg(tmp_path).log_path)
                if event["kind"] == "exception"]
    assert failures[-1]["action"] == reason
    assert failures[-1]["severity"] == "high"


def test_shadow_boundary_never_catches_process_interrupts(monkeypatch, tmp_path):
    proposal = _proposal()

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(loop, "score_proposal_shadow_inventory", interrupt)
    with pytest.raises(KeyboardInterrupt):
        loop._score_shadow_proposal(
            proposal, SimpleNamespace(analysis_limit_seconds=100.0), _cfg(tmp_path), it=5)


def test_exact_source_resolution_never_substitutes_a_later_same_q_master():
    first = {"q": [2.1, 0.0, 0.0], "master_revision": "revision-1",
             "predictive_ref": "data/master_predictive/first.json",
             "target_projection_ref": "data/target_projection/first.json"}
    later = {"q": [2.1, 0.0, 0.0], "master_revision": "revision-2",
             "predictive_ref": "data/master_predictive/later.json",
             "target_projection_ref": "data/target_projection/later.json"}
    first_entry = SimpleNamespace(detectors={"det": first}, provenance={})
    later_entry = SimpleNamespace(detectors={"det": later}, provenance={})
    workspace = SimpleNamespace(snapshot=SimpleNamespace(scans={
        "master:first": first_entry, "master:later": later_entry}))
    source = SimpleNamespace(
        master_session_key="master:first", detector_id="det", master_revision="revision-1")

    entry, outcome = _exact_master(workspace, source)

    assert entry is first_entry
    assert outcome is first
    assert _exact_reference(
        "data/master_predictive/first.json", outcome, "predictive_ref") is not None
    assert _exact_reference(
        "data/master_predictive/later.json", outcome, "predictive_ref") is None
    assert _exact_reference(
        "data/target_projection/later.json", outcome, "target_projection_ref") is None

    wrong_revision = SimpleNamespace(
        master_session_key="master:first", detector_id="det", master_revision="revision-2")
    assert _exact_master(workspace, wrong_revision) == (first_entry, None)


def test_source_action_mismatch_refuses_before_workspace_loading(tmp_path):
    proposal = _proposal()
    proposal.shadow_scoring_source = SimpleNamespace(
        scientific_action_fingerprint="different-action")

    score_proposal_shadow_inventory(
        proposal, SimpleNamespace(), tmp_path)

    assert proposal.scan_design_inventory.shadow_scoring.reason_codes == (
        "shadow_source_action_mismatch",)


def _write_source_events(tmp_path, inventory, *, ambiguous=False):
    digest = inventory.content_hash()
    design_path = tmp_path / "report" / "scan-design" / f"{digest}.json"
    design_path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text(canonical_json(inventory.to_full_dict()), encoding="utf-8")
    reference = {
        "kind": "scan_design_inventory", "sha256": digest,
        "path": f"report/scan-design/{digest}.json",
    }
    common = {
        "scan_design_inventory_ref": reference,
        "scan_design_candidate_id": inventory.selected_candidate_id,
        "scan_design_context_fingerprint": inventory.context.fingerprint,
    }
    events = [
        {"kind": "physical_submission", "payload": {
            **common, "acquisition_id": "physical-a", "proposal_fingerprint": "request-a"}},
        {"kind": "pooled_master_update", "payload": {
            "master_session_key": "master", "detector_id": "det",
            "master_revision": "revision-1", "acquisition_id": "physical-a",
            "proposal_fingerprint": "request-a"}},
        {"kind": "physical_submission", "payload": {
            **common, "acquisition_id": "physical-later",
            "proposal_fingerprint": "request-later"}},
        {"kind": "pooled_master_update", "payload": {
            "master_session_key": "master", "detector_id": "det",
            "master_revision": "revision-2", "acquisition_id": "physical-later",
            "proposal_fingerprint": "request-later"}},
    ]
    if ambiguous:
        events.extend([
            {"kind": "physical_submission", "payload": {
                **common, "acquisition_id": "physical-other",
                "proposal_fingerprint": "request-other"}},
            {"kind": "pooled_master_update", "payload": {
                "master_session_key": "master", "detector_id": "det",
                "master_revision": "revision-1", "acquisition_id": "physical-other",
                "proposal_fingerprint": "request-other"}},
        ])
    log_path = tmp_path / "report" / "session_log.jsonl"
    log_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in events),
        encoding="utf-8")
    return reference


def test_pooled_master_source_join_uses_exact_revision_and_request(tmp_path):
    inventory = _inventory()
    _write_source_events(tmp_path, inventory)

    sources = loop._exact_pooled_master_sources(tmp_path)

    assert sources[("master", "det", "revision-1")] == {
        "source_acquisition_id": "physical-a",
        "source_submission_request_fingerprint": "request-a",
    }
    assert sources[("master", "det", "revision-2")]["source_acquisition_id"] == (
        "physical-later")
    valid, _events = _scan_design_source(tmp_path, "physical-a", "request-a")
    assert valid["inventory_fingerprint"] == inventory.content_hash()
    assert _scan_design_source(tmp_path, "physical-a", "request-later")[0] is None


def test_ambiguous_pooled_source_join_refuses_instead_of_substituting(tmp_path):
    inventory = _inventory()
    _write_source_events(tmp_path, inventory, ambiguous=True)

    assert ("master", "det", "revision-1") not in loop._exact_pooled_master_sources(tmp_path)


def test_exact_pooled_source_runs_producer_grounded_numeric_path(monkeypatch, tmp_path):
    helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_design_measure.py"))
    predictive, projection, _qoi, source_input = helpers["_bundle"](monkeypatch)
    raw_intent = dict(source_input.action_intent)
    intent = MeasurementIntent(
        action_class=raw_intent["action_class"], hkl=tuple(raw_intent["hkl"]),
        energy_lo=raw_intent["energy_lo"], energy_hi=raw_intent["energy_hi"],
        window_family=raw_intent["window_family"],
        target_branch_role=raw_intent["target_branch_role"],
        rival_branch_role=raw_intent["rival_branch_role"],
        evidence_basis_fingerprint=raw_intent["evidence_basis_fingerprint"],
        hypotheses_fingerprint=raw_intent["hypotheses_fingerprint"],
        campaign_slice_fingerprint=raw_intent["campaign_slice_fingerprint"],
        evidence_sink=raw_intent["evidence_sink"],
    )
    scan_helpers = runpy.run_path(str(Path(__file__).with_name("test_scan_design.py")))
    inventory = design_scan_windows(scan_helpers["_context"](
        scientific_action_fingerprint=intent.scientific_action_fingerprint,
        hkl=tuple(raw_intent["hkl"]), q=0.0, kind="production",
        scan_window_eig_mode="shadow"))
    candidate = inventory.selected_candidate
    proposal = ScanProposal(
        h=2.0, k=0.0, l=0.0, e_lo=candidate.e_lo, e_hi=candidate.e_hi,
        e_step=candidate.e_step, ncount=candidate.ncount, n_points=candidate.n_points,
        kind="production", reason="test",
        meta={"scientific_action_fingerprint": intent.scientific_action_fingerprint},
        scan_design_inventory=inventory,
    )
    _write_source_events(tmp_path, inventory)
    selection = dict(source_input.selection_record)
    shadow = dict(source_input.shadow_record)
    outcome = {
        **dict(source_input.target_outcome),
        "master_revision": "master-r1",
        "predictive_ref": "data/master_predictive/exact.json",
        "target_projection_ref": "data/target_projection/exact.json",
        "fit_selection_semantics": selection,
        "shadow_model_selection": shadow,
    }
    entry = SimpleNamespace(
        detectors={"det": outcome},
        provenance={"target_projection_authority": projection["authority"]})
    fake_workspace = SimpleNamespace(
        snapshot=SimpleNamespace(scans={"master": entry}),
        master_predictive=lambda ref: predictive,
        target_projection=lambda ref: projection,
    )
    proposal.shadow_scoring_source = ShadowScoringSource(
        master_session_key="master", detector_id="det", master_revision="master-r1",
        source_acquisition_id="physical-a",
        source_submission_request_fingerprint="request-a",
        scientific_action_fingerprint=intent.scientific_action_fingerprint,
        predictive_ref="data/master_predictive/exact.json",
        target_projection_ref="data/target_projection/exact.json",
        target_outcome=dict(outcome), selection_record=selection, shadow_record=shadow,
        target_projection_authority=dict(projection["authority"]), action_intent=intent,
        action_provenance=dict(source_input.action_provenance),
    )
    scope = "a" * 64
    policies = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_shadow_inventory.py"))["_policies"](scope)
    objective = SimpleNamespace(scan_window_eig=SimpleNamespace(
        policy_set=policies,
        scoring_config=ScanWindowShadowScoringConfig(simulation_count=8, block_count=2)))
    monkeypatch.setattr("isar.workspace_api.Workspace.open", lambda _path: fake_workspace)
    monkeypatch.setattr(
        "isar.drive.acquisition.eig_shadow.current_objective_comparison_scope",
        lambda _objective: {"fingerprint": scope})

    score_proposal_shadow_inventory(proposal, objective, tmp_path)

    scoring = proposal.scan_design_inventory.shadow_scoring
    assert scoring.status == "complete", (
        scoring.reason_codes, scoring.eligibility, scoring.design_measure)
    assert proposal.scan_design_inventory.selected_candidate_id == inventory.selected_candidate_id
