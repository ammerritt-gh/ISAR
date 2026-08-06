from __future__ import annotations

import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest

from isar.drive import loop
from isar.drive.acquisition.acquire import ScanProposal, ShadowScoringSource
from isar.drive.acquisition.eig_shadow import (
    _AUTHORITY_BASIS_FIELDS,
    _exact_master,
    _exact_reference,
    _expected_authority,
    _scan_design_source,
    score_proposal_shadow_inventory,
)
from isar.eig.qoi import adapt_omega0_target_projection
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
        provenance={"target_projection_authority": dict(_LEDGER_BASIS)})
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
        target_projection_authority=None, action_intent=intent,
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


# Exactly what ``isar.session.driver`` freezes on the ledger entry at fit time: the four basis
# fields, and none of the five tracker fields the published projection is equally bound to.
_LEDGER_BASIS = {
    "master_revision": "master-r1", "calibration_fingerprint": "cal-r1",
    "policy_fingerprint": "policy-r1", "action_role": "certification",
}


def _authority_case(monkeypatch):
    """The real `/2` producer chain, plus the exact ledger outcome its projection was built from."""
    helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_design_measure.py"))
    predictive, projection, _qoi, source_input = helpers["_bundle"](monkeypatch)
    outcome = {**dict(source_input.target_outcome), "master_revision": "master-r1"}
    return predictive, projection, outcome


def test_frozen_ledger_basis_is_only_the_four_fields_the_fit_scope_can_know():
    assert set(_LEDGER_BASIS) == set(_AUTHORITY_BASIS_FIELDS)


def test_fresh_campaign_resolves_currentness_from_the_basis_and_the_current_tracker(monkeypatch):
    """A campaign with no prior currentness record still authenticates its own projection.

    The frozen basis names no track or candidate, so consuming it alone left every authority
    decision on ``projection_currentness_unknown`` — a state a fresh campaign could never leave.
    """
    predictive, projection, outcome = _authority_case(monkeypatch)
    entry = SimpleNamespace(provenance={"target_projection_authority": dict(_LEDGER_BASIS)})
    source = SimpleNamespace(target_projection_authority=None)

    expected = _expected_authority(entry, outcome, source)

    assert expected == dict(projection["authority"])
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=expected)
    assert evidence.status == "valid", (evidence.observability_codes, evidence.scientific_codes)
    assert "projection_currentness_unknown" not in evidence.observability_codes

    # The frozen half alone is what the adapter used to receive, and it can never be current.
    stalled = adapt_omega0_target_projection(
        predictive, projection, expected_authority=dict(_LEDGER_BASIS))
    assert "projection_currentness_unknown" in stalled.observability_codes


def test_matching_ledger_and_proposal_basis_compose_the_same_authority(monkeypatch):
    predictive, projection, outcome = _authority_case(monkeypatch)
    entry = SimpleNamespace(provenance={"target_projection_authority": dict(_LEDGER_BASIS)})
    source = SimpleNamespace(target_projection_authority=dict(_LEDGER_BASIS))

    expected = _expected_authority(entry, outcome, source)

    assert expected == dict(projection["authority"])
    assert adapt_omega0_target_projection(
        predictive, projection, expected_authority=expected).status == "valid"


def test_disagreeing_ledger_and_proposal_basis_still_fails_closed(monkeypatch):
    predictive, projection, outcome = _authority_case(monkeypatch)
    entry = SimpleNamespace(provenance={"target_projection_authority": dict(_LEDGER_BASIS)})
    source = SimpleNamespace(
        target_projection_authority={**_LEDGER_BASIS, "calibration_fingerprint": "cal-other"})

    assert _expected_authority(entry, outcome, source) is None
    evidence = adapt_omega0_target_projection(predictive, projection, expected_authority=None)
    assert evidence.status == "invalid"
    assert "projection_currentness_unknown" in evidence.observability_codes


def test_no_exact_outcome_or_basis_leaves_currentness_unknowable(monkeypatch):
    _authority_case(monkeypatch)
    entry = SimpleNamespace(provenance={"target_projection_authority": dict(_LEDGER_BASIS)})
    source = SimpleNamespace(target_projection_authority=None)

    assert _expected_authority(entry, None, source) is None
    assert _expected_authority(None, {"master_revision": "master-r1"}, source) is None


@pytest.mark.parametrize(("field", "value"), [
    ("master_revision", "master-r2"),
    ("track_revision", 2),
    ("track_state", "ambiguous"),
    ("candidate_id", "other-candidate"),
    ("candidate_state", "ambiguous"),
    ("selected_candidate_index", 1),
])
def test_a_moved_master_or_tracker_still_reports_the_projection_stale(monkeypatch, field, value):
    """Composing from the CURRENT outcome must not blunt staleness on any bound field."""
    predictive, projection, outcome = _authority_case(monkeypatch)
    entry = SimpleNamespace(provenance={"target_projection_authority": dict(_LEDGER_BASIS)})
    source = SimpleNamespace(target_projection_authority=None)
    basis = dict(_LEDGER_BASIS)
    if field == "master_revision":
        basis[field] = value
    moved = {**outcome, field: value}

    expected = _expected_authority(
        SimpleNamespace(provenance={"target_projection_authority": basis}), moved, source)

    assert expected != dict(projection["authority"])
    evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=expected)
    assert "projection_stale" in evidence.scientific_codes
    assert "projection_currentness_unknown" not in evidence.observability_codes
    assert _expected_authority(entry, outcome, source) == dict(projection["authority"])


def test_publisher_mirrored_fallbacks_cannot_hide_a_refit_master(monkeypatch):
    """A basis without ``master_revision``/``action_role`` resolves as the publisher resolves it.

    Both fields fall back exactly as ``driver._publish_target_projections`` does, so an unmoved
    master still compares current — and a master refit since the projection was published still
    carries a new revision on the ledger outcome, so it is still reported stale.
    """
    predictive, projection, outcome = _authority_case(monkeypatch)
    partial = {name: value for name, value in _LEDGER_BASIS.items()
               if name not in {"master_revision", "action_role"}}
    entry = SimpleNamespace(provenance={"target_projection_authority": partial})
    source = SimpleNamespace(target_projection_authority=None)

    # ``action_role`` falls back to "master", which the certification projection was not bound to.
    assert _expected_authority(entry, outcome, source)["master_revision"] == "master-r1"
    assert _expected_authority(entry, outcome, source)["action_role"] == "master"

    master_role = {**partial, "action_role": "certification"}
    entry = SimpleNamespace(provenance={"target_projection_authority": master_role})
    assert _expected_authority(entry, outcome, source) == dict(projection["authority"])
    assert adapt_omega0_target_projection(
        predictive, projection, expected_authority=_expected_authority(
            entry, outcome, source)).status == "valid"

    refit = {**outcome, "master_revision": "master-r2"}
    evidence = adapt_omega0_target_projection(
        predictive, projection,
        expected_authority=_expected_authority(entry, refit, source))
    assert "projection_stale" in evidence.scientific_codes


def test_a_main_era_artifact_replays_and_an_unreadable_one_is_logged(tmp_path, caplog):
    """The shadow replay reads ``physical_submission`` events that survive resumes.

    A ``main``-era inventory (written with ``indent=2``) authenticates semantically, so the replay
    must still resolve it — silently downgrading every past acquisition to
    ``scan_design_provenance_invalid`` is what the raw-byte-only check did. A genuinely unreadable
    artifact still refuses, but never without a log entry.
    """
    import logging

    inventory = _inventory()
    reference = _write_source_events(tmp_path, inventory)
    design_path = tmp_path / "report" / "scan-design" / f"{reference['sha256']}.json"
    payload = json.loads(design_path.read_text(encoding="utf-8"))
    design_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        source, events = _scan_design_source(tmp_path, "physical-a", "request-a")
    assert source["inventory_fingerprint"] == inventory.content_hash()
    assert events == ()

    design_path.write_text("{not json", encoding="utf-8")
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        source, reasons = _scan_design_source(tmp_path, "physical-a", "request-a")
    assert source is None and reasons == ("scan_design_provenance_invalid",)
    assert "scan_design_provenance_invalid" in caplog.text
