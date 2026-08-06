"""Slice 7 exact realized-request construction, persistence, and grid parity."""
from __future__ import annotations

import copy
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from isar.drive import loop
from isar.drive.instrument import request_realization as wire
from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.authority import (
    _partition_offset_evidence,
    build_authority_decision,
    load_authority_decision,
    persist_authority_decision,
)
from isar.drive.acquisition.inventory import canonical_json
from isar.drive.acquisition.realized_comparison import (
    attach_realized_metadata,
    authenticate_realized_submission,
    build_realized_comparison,
    construct_realized_pair,
    load_scan_design_realized_comparison,
    persist_realized_comparison,
)
from isar.drive.acquisition.scan_design import (
    SCAN_DESIGN_INVENTORY_SCHEMA_V2,
    SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED,
    ScanDesignRefusal,
    design_scan_windows,
)
from isar.drive.acquisition.scan_grid import UniformGrid
from isar.drive.session_log import read_log
from isar.eig.realized_score import score_realized_pair
from isar.eig.o41_authority import O41AuthoritativeProfile
from isar.eig.shadow_inventory import score_shadow_inventory
from isar.schema.eig_policy import ScanWindowShadowScoringConfig
from isar.schema.eig_realized import (
    RealizedCandidateRefusal,
    RealizedScanRequest,
    RealizedScoreVariant,
    derive_realized_seed,
)
from isar.schema.eig_shadow import DIAGNOSTIC_AUTHORITY, thaw


def _inventory():
    helpers = runpy.run_path(str(Path(__file__).with_name("test_scan_design.py")))
    return design_scan_windows(helpers["_context"](scan_window_eig_mode="shadow"))


def _proposal(inventory) -> ScanProposal:
    candidate = inventory.selected_candidate
    return ScanProposal(
        h=candidate.hkl[0], k=candidate.hkl[1], l=candidate.hkl[2],
        e_lo=candidate.e_lo, e_hi=candidate.e_hi, e_step=candidate.e_step,
        ncount=candidate.ncount, n_points=candidate.n_points,
        kind="production", reason="slice 7 test", score_recompute=lambda _n: {},
        meta={
            "scientific_action_fingerprint": "a" * 64,
            "proposal_fingerprint": "b" * 64,
            "count_diagnostics": {
                "requested_before_runtime_limit": 999_999,
                "runtime_ncount_limit": 12,
                "limit_clamp_logged": True,
            },
            "score": {"method": "counts", "utility": 12.0},
            "scan_design_realized_comparison_fingerprint": "c" * 64,
            "scan_design_realized_selected_grid_fingerprint": "d" * 64,
        },
        scan_design_inventory=inventory,
    )


def _inventory_reference(tmp_path: Path, inventory) -> dict:
    digest = inventory.content_hash()
    path = tmp_path / "report" / "scan-design" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(canonical_json(inventory.to_full_dict()), encoding="utf-8")
    return {"kind": "scan_design_inventory", "sha256": digest,
            "path": f"report/scan-design/{digest}.json"}


def test_counterfactual_finalization_starts_clean_and_cannot_mutate_legacy_state():
    inventory = _inventory()
    proposal = _proposal(inventory)
    original_meta = copy.deepcopy(proposal.meta)
    budget = {"kind": "neutrons", "amount": 10**12, "spent": 7.0,
              "nested": {"legacy": True}}
    seen = {}

    def finalize(target, _objective, _cfg, isolated_budget, *, it, event_sink):
        assert it == 4
        assert event_sink is None
        assert target.score_recompute is None
        assert "score" not in target.meta
        assert "proposal_fingerprint" not in target.meta
        assert not any(key.startswith("scan_design_realized_") for key in target.meta)
        assert "runtime_ncount_limit" not in target.meta["count_diagnostics"]
        assert "limit_clamp_logged" not in target.meta["count_diagnostics"]
        isolated_budget["nested"]["legacy"] = False
        target.meta["proposal_fingerprint"] = "e" * 64
        seen["target"] = target

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), budget,
        it=4, finalizer=finalize)

    assert isinstance(pair.target_core, RealizedScanRequest)
    assert pair.target_core.ncount == pair.legacy.ncount
    assert pair.target_core.request_fingerprint == "e" * 64
    assert proposal.meta == original_meta
    assert budget["nested"]["legacy"] is True
    assert seen["target"] is not proposal


def test_counterfactual_exposure_change_is_a_refusal_not_a_coerced_pair():
    inventory = _inventory()
    proposal = _proposal(inventory)

    def finalize(target, *_args, **_kwargs):
        target.ncount -= 1
        target.meta["proposal_fingerprint"] = "e" * 64

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=finalize)

    assert isinstance(pair.target_core, RealizedCandidateRefusal)
    assert pair.target_core.reason_codes == ("realized_exposure_mismatch",)


def test_one_clone_exposure_adjustment_persists_visible_unavailable_authority(
        tmp_path):
    """A target-only adapter adjustment is a durable legacy outcome, never a silent no-op."""
    inventory = _inventory()
    inventory = replace(
        inventory,
        context=replace(inventory.context, scientific_action_fingerprint="9" * 64))
    proposal = _proposal(inventory)

    def finalize(target, *_args, **_kwargs):
        target.ncount -= 1
        target.meta["proposal_fingerprint"] = "e" * 64

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=finalize)
    inventory_ref = _inventory_reference(tmp_path, inventory)
    comparison = build_realized_comparison(
        inventory, inventory_ref, pair, remaining_seconds=0.0, rescorer=None)
    comparison_ref = persist_realized_comparison(tmp_path, comparison)
    profile = O41AuthoritativeProfile(
        policy_fingerprint="1" * 64, variant_fingerprint="2" * 64,
        policy_set_fingerprint="3" * 64, scoring_config_fingerprint="4" * 64)

    decision = build_authority_decision(
        inventory=inventory, inventory_ref=inventory_ref,
        comparison=comparison, comparison_ref=comparison_ref, profile=profile,
        legacy_validation_status="validated", target_validation_status="refused")
    reference = persist_authority_decision(tmp_path, decision)

    loaded = load_authority_decision(reference, tmp_path)
    assert loaded.decision_outcome == "selection_evidence_unavailable"
    assert loaded.physical_role == "legacy"
    assert loaded.target_core_validation["validated_request_status"] == "refused"
    assert "realized_exposure_mismatch" in loaded.target_core_validation["adjustments"]


def test_generator_refused_target_persists_unavailable_authority_without_exception(
        tmp_path):
    inventory = _inventory()
    target = next(item for item in inventory.candidates
                  if item.candidate_family == "target_core/1")
    inventory = replace(
        inventory,
        context=replace(inventory.context, scientific_action_fingerprint="8" * 64),
        candidates=tuple(item for item in inventory.candidates if item is not target),
        refusals=inventory.refusals + (ScanDesignRefusal(
            candidate_family="target_core/1",
            reason_code="target_core_truncated_after_domain_clip",
            detail=(("e_hi", target.e_hi), ("e_lo", target.e_lo),
                    ("e_step", target.e_step), ("n_points", target.n_points))),),
    )
    proposal = _proposal(inventory)
    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=lambda *_args, **_kwargs: None)
    assert isinstance(pair.target_core, RealizedCandidateRefusal)
    assert pair.target_core.source_candidate_id is None
    inventory_ref = _inventory_reference(tmp_path, inventory)
    comparison = build_realized_comparison(
        inventory, inventory_ref, pair, remaining_seconds=0.0, rescorer=None)
    comparison_ref = persist_realized_comparison(tmp_path, comparison)
    profile = O41AuthoritativeProfile(
        policy_fingerprint="1" * 64, variant_fingerprint="2" * 64,
        policy_set_fingerprint="3" * 64, scoring_config_fingerprint="4" * 64)

    decision = build_authority_decision(
        inventory=inventory, inventory_ref=inventory_ref,
        comparison=comparison, comparison_ref=comparison_ref, profile=profile,
        legacy_validation_status="validated", target_validation_status="refused")

    assert decision.decision_outcome == "selection_evidence_unavailable"
    assert decision.physical_role == "legacy"
    assert decision.target_core_validation["validated_request_status"] == "refused"
    assert decision.target_core_validation["scored_grid_join_status"] == "unavailable"


def test_bounded_authority_refresh_restores_the_initial_authenticated_legacy_without_third_pass(
        monkeypatch, tmp_path):
    """A refused target consumes the refresh; a refused refreshed legacy ends at pass two."""
    proposal, comparison, _payload = _persist_unavailable_comparison(tmp_path)
    proposal.scan_design_inventory = replace(
        proposal.scan_design_inventory,
        context=replace(
            proposal.scan_design_inventory.context,
            scan_window_eig_mode="authoritative"))
    proposal.meta["scan_design_inventory_ref"] = _inventory_reference(
        tmp_path, proposal.scan_design_inventory)
    initial = loop._clone_proposal(proposal)
    calls = []

    class RefusingClient:
        def validate(self, body):
            calls.append(body)
            return {"would_queue": False, "blockers": ["test_refusal"],
                    "requested_points": body.get("n_points", 0), "feasible_points": 0}

    monkeypatch.setattr(
        loop, "load_scan_design_realized_comparison", lambda *_args: comparison)
    refreshes = []
    monkeypatch.setattr(
        loop, "_refresh_realized_comparison",
        lambda *_args, **kwargs: refreshes.append(kwargs.get("force")))
    decisions = []
    monkeypatch.setattr(
        loop, "build_authority_decision",
        lambda *_args, **_kwargs: decisions.append(True))
    exact_config = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_o41_authority.py"))["_config"]()
    cfg = SimpleNamespace(
        workspace=tmp_path, log_path=tmp_path / "report" / "session_log.jsonl",
        client=RefusingClient(), sample="sample", sample_background_frozen=None)

    returned = loop._prepare_authoritative_submission(
        cfg, SimpleNamespace(scan_window_eig=exact_config, steering={}), proposal,
        {"kind": "neutrons", "amount": 1e9, "spent": 0.0},
        {"would_queue": True, "requested_points": proposal.n_points,
         "feasible_points": proposal.n_points},
        it=3)

    assert returned["would_queue"] is True
    assert refreshes == [True]
    assert len(calls) == 2  # target on pass one, refreshed legacy on pass two
    assert decisions == []
    assert proposal.__dict__ == initial.__dict__


def test_historical_coarsening_refuses_if_adapter_still_reports_over_cap(monkeypatch, tmp_path):
    proposal = ScanProposal(
        h=1.0, k=0.0, l=0.0, e_lo=0.0, e_hi=20.0, e_step=0.01,
        ncount=100, n_points=2_001, kind="production", reason="test")
    over_cap = SimpleNamespace(e_step=0.2, n_points=101)
    monkeypatch.setattr(loop, "resolve_uniform_grid", lambda *_args, **_kwargs: over_cap)

    with pytest.raises(loop.InstrumentPointLimitExceeded,
                       match="instrument_point_limit_exceeded"):
        loop._guard_point_count(
            proposal, 100, "get_state", tmp_path / "session.jsonl", it=1)

    assert proposal.e_step == 0.01
    assert proposal.n_points == 2_001
    assert not (tmp_path / "session.jsonl").exists()
    rejection = loop.classify_rejection("instrument_point_limit_exceeded")
    assert rejection.rejection_class.value == "structural"
    assert rejection.scope == "request"


def test_identical_realized_pair_reuses_the_complete_slice6_score(monkeypatch, tmp_path):
    helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_shadow_inventory.py"))
    inventory, scoring = helpers["_complete_scoring"](monkeypatch)
    inventory = inventory.with_shadow_scoring(scoring)
    proposal = _proposal(inventory)

    def finalize(target, *_args, **_kwargs):
        target.meta["proposal_fingerprint"] = "e" * 64

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=finalize)
    called = False

    def forbidden(*_args):
        nonlocal called
        called = True
        raise AssertionError("an identical pair must not rerun Monte Carlo")

    comparison = build_realized_comparison(
        inventory, _inventory_reference(tmp_path, inventory), pair,
        remaining_seconds=5.0, rescorer=forbidden)

    assert not called
    assert comparison.score_disposition == "reused"
    assert comparison.seed_evidence is None
    assert comparison.source_shadow_variant_fingerprints == tuple(
        item.variant_evidence_fingerprint for item in scoring.variants)
    assert all(item.score_disposition in {"reused", "unavailable"}
               for item in comparison.variants)


@pytest.mark.parametrize("legacy_change", ["exposure", "grid"])
def test_changed_legacy_request_rescores_the_complete_equal_exposure_pair(
        monkeypatch, tmp_path, legacy_change):
    helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_shadow_inventory.py"))
    inventory, scoring = helpers["_complete_scoring"](monkeypatch)
    inventory = inventory.with_shadow_scoring(scoring)
    proposal = _proposal(inventory)
    if legacy_change == "exposure":
        proposal.ncount -= 1
    else:
        proposal.e_step *= 2.0
        proposal.n_points = wire._emitted_point_count(
            proposal.e_lo, proposal.e_hi, proposal.e_step)

    def finalize(target, *_args, **_kwargs):
        target.meta["proposal_fingerprint"] = "e" * 64

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=finalize)
    calls = []

    def rescore(realized_pair, remaining):
        calls.append((realized_pair, remaining))
        source = scoring.variants[0]
        variant = RealizedScoreVariant(
            policy_id=source.policy_id, policy_fingerprint=source.policy_fingerprint,
            variant_id=source.variant_id, variant_fingerprint=source.variant_fingerprint,
            status="complete", score_disposition="rescored",
            compiled_partition=dict(source.compiled_partition), score_set={"test": "paired"},
            target_core_minus_legacy={"delta_information_nats": 1.0},
            authority_scopes=DIAGNOSTIC_AUTHORITY)
        seed = derive_realized_seed(
            scoring_config_fingerprint=scoring.scoring_config_fingerprint,
            design_measure_fingerprint=scoring.design_measure["design_measure_fingerprint"],
            realized_pair_fingerprint=realized_pair.realized_pair_fingerprint)
        return (variant,), seed

    comparison = build_realized_comparison(
        inventory, _inventory_reference(tmp_path, inventory), pair,
        remaining_seconds=4.5, rescorer=rescore)

    assert len(calls) == 1 and calls[0][0] is pair
    assert pair.legacy.ncount == pair.target_core.ncount == proposal.ncount
    if legacy_change == "grid":
        assert pair.legacy.executed_energies != tuple(
            inventory.selected_candidate.energies)
    assert comparison.score_disposition == "rescored"
    assert comparison.variants[0].score_disposition == "rescored"


def _persist_unavailable_comparison(tmp_path: Path):
    inventory = _inventory()
    proposal = _proposal(inventory)

    def finalize(target, *_args, **_kwargs):
        target.meta["proposal_fingerprint"] = "e" * 64

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=finalize)
    inventory_ref = _inventory_reference(tmp_path, inventory)
    comparison = build_realized_comparison(
        inventory, inventory_ref, pair, remaining_seconds=0.0, rescorer=None)
    reference = persist_realized_comparison(tmp_path, comparison)
    proposal.meta["proposal_fingerprint"] = comparison.selected_request_fingerprint
    attach_realized_metadata(proposal, reference, comparison)
    payload = dict(proposal.meta)
    payload["job_id"] = "job-1"
    return proposal, comparison, payload


@pytest.mark.parametrize("echo_requested_points", [True, False])
def test_a_partially_infeasible_target_core_is_refused_and_legacy_is_submitted(
        monkeypatch, tmp_path, echo_requested_points):
    """The authority cannot trim a target-core window — the window *is* the candidate's identity —
    so a partially-infeasible one must be refused here. Selecting it would hand the submit boundary
    a grid TAVI rejects with HTTP 400, which is how a whole campaign executed zero scans."""
    proposal, comparison, _payload = _persist_unavailable_comparison(tmp_path)
    proposal.scan_design_inventory = replace(
        proposal.scan_design_inventory,
        context=replace(
            proposal.scan_design_inventory.context,
            scan_window_eig_mode="authoritative"))
    proposal.meta["scan_design_inventory_ref"] = _inventory_reference(
        tmp_path, proposal.scan_design_inventory)
    legacy_command = wire._validate_body(
        proposal, sample="sample", background=None)["parameters"]["scan_command1"]
    roles = []

    class PartialTargetClient:
        """Legacy validates cleanly; the target core comes back one point short.

        ``echo_requested_points=False`` is the adapter that reports only the mask. The mask is
        positional against the requested point list, so its length IS the requested count — without
        that fallback the partial target slips through the check unnoticed.
        """

        def validate(self, body):
            _axis, lo, hi, step = body["parameters"]["scan_command1"].split()
            points = int((float(hi) - float(lo)) / float(step)) + 1
            if body["parameters"]["scan_command1"] == legacy_command:
                roles.append("legacy")
                return {"would_queue": True, "blockers": [],
                        "requested_points": points, "feasible_points": points}
            roles.append("target_core")
            payload = {"would_queue": True, "blockers": [],
                       "feasible_points": max(points - 1, 0), "partial": True,
                       "planned_feasible_mask": [False] + [True] * max(points - 1, 0)}
            if echo_requested_points:
                payload["requested_points"] = points
            return payload

    monkeypatch.setattr(
        loop, "load_scan_design_realized_comparison", lambda *_args: comparison)
    monkeypatch.setattr(
        loop, "_refresh_realized_comparison", lambda *_args, **_kwargs: None)
    captured = []

    def capture(**kwargs):
        captured.append(kwargs)
        raise RuntimeError("selector not exercised by this test")

    monkeypatch.setattr(loop, "build_authority_decision", capture)
    exact_config = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_o41_authority.py"))["_config"]()
    log_path = tmp_path / "report" / "session_log.jsonl"
    cfg = SimpleNamespace(
        workspace=tmp_path, log_path=log_path,
        client=PartialTargetClient(), sample="sample", sample_background_frozen=None)
    legacy_validation = {"would_queue": True, "requested_points": proposal.n_points,
                         "feasible_points": proposal.n_points}

    returned = loop._prepare_authoritative_submission(
        cfg, SimpleNamespace(scan_window_eig=exact_config, steering={}), proposal,
        {"kind": "neutrons", "amount": 1e9, "spent": 0.0}, legacy_validation, it=3)

    assert returned["feasible_points"] == returned["requested_points"], \
        "a partially-infeasible target must never be the submitted request"
    assert "target_core" in roles
    target_events = [event["payload"] for event in read_log(log_path)
                     if event["kind"] == "validate"
                     and event["payload"].get("candidate_role") == "target_core"]
    assert target_events
    assert {event["refusal_reason"] for event in target_events} == {
        "target_core_partially_infeasible"}
    # The target is refused *despite* would_queue being true, so the selector — if it is reached at
    # all — is told the target is unusable rather than being offered an unsubmittable window.
    assert all(entry["target_validation_status"] == "refused" for entry in captured)


def test_strict_loader_recomputes_point_count_and_rejects_unknown_adapter(tmp_path):
    _proposal_value, comparison, _payload = _persist_unavailable_comparison(tmp_path)
    legacy = comparison.realized_pair.legacy
    bad_grid = UniformGrid(
        legacy.e_lo, legacy.e_hi, legacy.e_step, legacy.executed_point_count - 1)
    bad_legacy = replace(
        legacy,
        executed_energies=bad_grid.energies,
        executed_point_count=bad_grid.n_points,
        grid_fingerprint=bad_grid.fingerprint(legacy.ncount),
        point_neutron_cost=bad_grid.n_points * legacy.ncount,
    )
    bad_count = replace(
        comparison, realized_pair=replace(comparison.realized_pair, legacy=bad_legacy))
    with pytest.raises(ValueError, match="grid does not reproduce"):
        load_scan_design_realized_comparison(
            persist_realized_comparison(tmp_path, bad_count), tmp_path)

    unknown_adapter = replace(
        comparison, realized_pair=replace(
            comparison.realized_pair,
            legacy=replace(legacy, grid_adapter={
                "adapter_schema": "isar.instrument_grid_adapter/1",
                "adapter_id": "future_adapter",
                "adapter_version": 2,
            })))
    with pytest.raises(ValueError, match="adapter is unsupported"):
        load_scan_design_realized_comparison(
            persist_realized_comparison(tmp_path, unknown_adapter), tmp_path)


def test_legacy_status_is_family_strict_but_historical_spelling_still_loads(tmp_path):
    _proposal_value, comparison, _payload = _persist_unavailable_comparison(tmp_path)
    legacy = comparison.realized_pair.legacy
    assert legacy.validation_status == "finalized_selected_legacy"
    historical = replace(
        comparison, realized_pair=replace(
            comparison.realized_pair,
            legacy=replace(legacy, validation_status="submitted_legacy")))
    loaded = load_scan_design_realized_comparison(
        persist_realized_comparison(tmp_path, historical), tmp_path)
    assert loaded.realized_pair.legacy.validation_status == "submitted_legacy"
    with pytest.raises(ValueError, match="realized request is invalid"):
        replace(legacy, validation_status="not_instrument_validated_counterfactual")


def test_pre_submit_authentication_is_exact_and_strips_corrupt_claims_once(
        monkeypatch, tmp_path):
    proposal, comparison, _payload = _persist_unavailable_comparison(tmp_path)
    assert authenticate_realized_submission(proposal, tmp_path) == comparison
    body_before = wire._scan_body(
        proposal, "mock", 7, sample="Al_phonon_DFT", background=None)
    sidecar = tmp_path / proposal.meta["scan_design_realized_comparison_ref"]["path"]
    sidecar.write_bytes(sidecar.read_bytes() + b" ")
    refreshes = []
    monkeypatch.setattr(
        loop, "_refresh_realized_comparison",
        lambda *_args, **_kwargs: refreshes.append(True))
    cfg = SimpleNamespace(
        workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl")

    loop._authenticate_realized_before_submission(
        cfg, SimpleNamespace(), proposal, {}, it=4)

    assert refreshes == [True]
    assert proposal.meta["scan_design_eig_applicability"] == "unavailable"
    assert not any(proposal.meta.get(field) is not None
                   for field in loop._REALIZED_SUBMISSION_FIELDS)
    assert wire._scan_body(
        proposal, "mock", 7, sample="Al_phonon_DFT", background=None) == body_before
    failure = [event for event in read_log(cfg.log_path)
               if event["kind"] == "exception"][-1]
    assert failure["payload"]["action"] == "realized_submission_authentication_failed"
    assert failure["payload"]["severity"] == "high"


def test_refresh_persistence_failure_and_process_interrupt_have_distinct_boundaries(
        monkeypatch, tmp_path):
    inventory = _inventory()
    proposal = _proposal(inventory)
    proposal.meta["scan_design_inventory_ref"] = _inventory_reference(tmp_path, inventory)
    cfg = SimpleNamespace(
        workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl",
        run_nonce="run", max_points=200, max_points_source="default",
        instrument_limits=None)
    objective = SimpleNamespace(
        analysis_limit_seconds=100.0, tolerance_meV=0.5,
        steering={"max_scan_points": 100, "production_batch_ncount": 10_000,
                  "max_ncount_per_scan": 1_000_000})
    monkeypatch.setattr(
        loop, "persist_realized_comparison",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))

    assert loop._refresh_realized_comparison(
        cfg, objective, proposal,
        {"kind": "neutrons", "amount": 10**12, "spent": 0.0}, it=2) is None
    assert proposal.meta["scan_design_eig_applicability"] == "unavailable"
    assert not any(proposal.meta.get(field) is not None
                   for field in loop._REALIZED_SUBMISSION_FIELDS)
    assert any(event["kind"] == "exception"
               and event["payload"].get("action") == "realized_comparison_refresh_failed"
               and event["payload"].get("severity") == "high"
               for event in read_log(cfg.log_path))

    monkeypatch.setattr(
        loop, "persist_realized_comparison",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        loop._refresh_realized_comparison(
            cfg, objective, proposal,
            {"kind": "neutrons", "amount": 10**12, "spent": 0.0}, it=3, force=True)


def test_allowance_exhaustion_replaces_stale_claims_and_historical_v2_never_rescores(
        monkeypatch, tmp_path):
    inventory = _inventory()
    proposal = _proposal(inventory)
    proposal.meta["scan_design_inventory_ref"] = _inventory_reference(tmp_path, inventory)
    proposal.meta.update({field: "stale" for field in loop._REALIZED_SUBMISSION_FIELDS})
    proposal.realized_scoring_remaining_seconds = 0.0
    cfg = SimpleNamespace(
        workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl",
        run_nonce="run", max_points=200, max_points_source="default",
        instrument_limits=None)
    objective = SimpleNamespace(
        analysis_limit_seconds=100.0, tolerance_meV=0.5,
        steering={"max_scan_points": 100, "production_batch_ncount": 10_000,
                  "max_ncount_per_scan": 1_000_000})

    reference = loop._refresh_realized_comparison(
        cfg, objective, proposal,
        {"kind": "neutrons", "amount": 10**12, "spent": 0.0}, it=5, force=True)
    comparison = load_scan_design_realized_comparison(reference, tmp_path)
    assert comparison.score_disposition == "unavailable"
    assert comparison.reason_codes == ("realized_scoring_deadline",)
    assert proposal.meta["scan_design_realized_selected_request_fingerprint"] == (
        proposal.meta["proposal_fingerprint"])

    historical = replace(
        inventory, inventory_version=SCAN_DESIGN_INVENTORY_SCHEMA_V2,
        shadow_scoring=None,
        selection_basis=SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED)
    historical_proposal = _proposal(historical)
    historical_proposal.meta["scan_design_inventory_ref"] = _inventory_reference(
        tmp_path, historical)
    monkeypatch.setattr(
        loop, "score_proposal_realized_pair",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not rescore /2")))
    assert loop._refresh_realized_comparison(
        cfg, objective, historical_proposal,
        {"kind": "neutrons", "amount": 10**12, "spent": 0.0}, it=6) is None
    assert historical_proposal.meta["scan_design_eig_applicability"] == "unavailable"


@pytest.mark.parametrize("returned", [
    [],
    [float("nan")],
    [float("inf")],
])
def test_returned_grid_rejects_length_and_nonfinite_values(tmp_path, returned):
    proposal, _comparison, payload = _persist_unavailable_comparison(tmp_path)
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl")
    assert loop._compare_returned_realized_grid(
        cfg, proposal, payload, {"result": {"scan_values_1": returned}}, it=2) == (
            "mismatch", "executed_grid_mismatch")


def test_returned_grid_accepts_only_representation_noise(tmp_path):
    proposal, comparison, payload = _persist_unavailable_comparison(tmp_path)
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl")
    returned = list(comparison.realized_pair.legacy.executed_energies)
    returned[len(returned) // 2] += 5e-17

    assert loop._compare_returned_realized_grid(
        cfg, proposal, payload, {"result": {"scan_values_1": returned}}, it=2) == (
            "matched", None)

    returned[len(returned) // 2] += 0.0004
    status, reason = loop._compare_returned_realized_grid(
        cfg, proposal, payload, {"result": {"scan_values_1": returned}}, it=2)
    assert (status, reason) == ("mismatch", "executed_grid_mismatch")

    reversed_grid = list(reversed(comparison.realized_pair.legacy.executed_energies))
    assert loop._compare_returned_realized_grid(
        cfg, proposal, payload, {"result": {"scan_values_1": reversed_grid}}, it=2) == (
            "mismatch", "executed_grid_mismatch")


def test_unreadable_recovery_sidecar_fails_open(tmp_path):
    proposal, _comparison, payload = _persist_unavailable_comparison(tmp_path)
    payload["scan_design_realized_comparison_ref"] = {
        **payload["scan_design_realized_comparison_ref"], "sha256": "f" * 64,
        "path": "report/scan-design-realizations/" + "f" * 64 + ".json",
    }
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl")

    assert loop._compare_returned_realized_grid(
        cfg, proposal, payload, {"result": {"scan_values_1": []}}, it=None) == (
            "unavailable", "realized_comparison_unavailable")
    failures = [event["payload"] for event in read_log(cfg.log_path)
                if event["kind"] == "exception"]
    assert failures[-1]["action"] == "realized_comparison_unavailable"
    assert failures[-1]["severity"] == "high"


def test_recovery_restores_sidecar_provenance_without_recomputing_and_then_invalidates_claims(
        tmp_path):
    proposal, _comparison, payload = _persist_unavailable_comparison(tmp_path)
    payload.update({
        "hkl": [proposal.h, proposal.k, proposal.l], "kind": proposal.kind,
        "submitted_ncount": proposal.ncount, "n_points": proposal.n_points,
        "search_energy_lo": proposal.e_lo, "search_energy_hi": proposal.e_hi,
        "search_energy_step": proposal.e_step,
        "scan_design_realized_variant_fingerprints": ["a" * 64],
        "scan_design_eig_applicability": "applicable",
    })

    recovered = loop._proposal_from_submission(payload)

    for field in loop._REALIZED_SUBMISSION_FIELDS:
        assert recovered.meta[field] == payload[field]
    loop._invalidate_realized_comparison_for_recovery(recovered)
    assert recovered.meta["scan_design_realized_comparison_ref"] == payload[
        "scan_design_realized_comparison_ref"]
    assert recovered.meta["scan_design_realized_comparison_fingerprint"] == payload[
        "scan_design_realized_comparison_fingerprint"]
    assert recovered.meta["scan_design_eig_applicability"] == "invalidated"
    assert recovered.meta["scan_design_eig_invalidation_reason"] == (
        "eig_realization_invalidated_by_recovery")
    assert not any(field in recovered.meta for field in (
        "scan_design_realized_variant_fingerprints",
        "scan_design_realized_selected_request_fingerprint",
        "scan_design_realized_selected_grid_fingerprint",
    ))
    invalidated_payload = {**payload, **recovered.meta}
    for field in (
            "scan_design_realized_variant_fingerprints",
            "scan_design_realized_selected_request_fingerprint",
            "scan_design_realized_selected_grid_fingerprint"):
        invalidated_payload.pop(field, None)
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl")
    assert loop._compare_returned_realized_grid(
        cfg, recovered, invalidated_payload, {"result": {"scan_values_1": []}}, it=2) == (
            "not_applicable", None)


def test_public_loader_rejects_tampered_bytes_and_decision_fields(tmp_path):
    _proposal_value, comparison, _payload = _persist_unavailable_comparison(tmp_path)
    reference = persist_realized_comparison(tmp_path, comparison)
    path = tmp_path / reference["path"]
    path.write_bytes(path.read_bytes() + b" ")

    try:
        load_scan_design_realized_comparison(reference, tmp_path)
    except ValueError as error:
        assert "address" in str(error)
    else:
        raise AssertionError("tampered content-addressed evidence was accepted")

    try:
        replace(comparison, source_identities={"winner": "target_core/1"})
    except ValueError as error:
        assert "decision field" in str(error)
    else:
        raise AssertionError("realized evidence accepted an authority-bearing field")


def test_public_loader_deeply_reproduces_a_rescored_numeric_sidecar(monkeypatch, tmp_path):
    helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_shadow_inventory.py"))
    predictive, eligibility, inventory = helpers["_producer_bundle"](monkeypatch)
    scope = helpers["_fp"]("campaign")
    policies = helpers["_policies"](scope)
    config = ScanWindowShadowScoringConfig(simulation_count=8, block_count=2)
    source_scoring = score_shadow_inventory(
        inventory, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=policies, scoring_config=config,
        scope_kind="campaign_configuration", scope_fingerprint=scope)
    inventory = inventory.with_shadow_scoring(source_scoring)
    proposal = _proposal(inventory)
    proposal.ncount -= 1

    def finalize(target, *_args, **_kwargs):
        target.meta["proposal_fingerprint"] = "e" * 64

    pair = construct_realized_pair(
        proposal, inventory, SimpleNamespace(), SimpleNamespace(), {},
        it=1, finalizer=finalize)
    reference = _inventory_reference(tmp_path, inventory)
    comparison = build_realized_comparison(
        inventory, reference, pair, remaining_seconds=5.0,
        rescorer=lambda realized_pair, _remaining: score_realized_pair(
            realized_pair, predictive_artifact=predictive, eligibility_input=eligibility,
            policy_set=policies, scoring_config=config,
            scope_kind="campaign_configuration", scope_fingerprint=scope))
    sidecar_ref = persist_realized_comparison(tmp_path, comparison)

    loaded = load_scan_design_realized_comparison(sidecar_ref, tmp_path)

    assert loaded.to_dict() == comparison.to_dict()
    assert loaded.score_disposition == "rescored"

    first = comparison.variants[0]
    changed_partition = copy.deepcopy(thaw(first.compiled_partition))
    changed_partition["bin_width"] += 0.125
    changed_score = copy.deepcopy(thaw(first.score_set))
    changed_score["candidate_scores"][0]["conditional_target_qoi_information_nats"] += 0.25
    mutations = (
        replace(comparison, realized_pair=replace(
            comparison.realized_pair, planned_pair_fingerprint="f" * 64)),
        replace(comparison, variants=(replace(first, policy_fingerprint="f" * 64),
                                      *comparison.variants[1:])),
        replace(comparison, variants=(replace(first, compiled_partition=changed_partition),
                                      *comparison.variants[1:])),
        replace(comparison, variants=(replace(first, score_set=changed_score),
                                      *comparison.variants[1:])),
        replace(comparison, source_design_measure_fingerprint="f" * 64),
    )
    for tampered in mutations:
        tampered_ref = persist_realized_comparison(tmp_path, tampered)
        with pytest.raises(ValueError):
            load_scan_design_realized_comparison(tampered_ref, tmp_path)

    target = comparison.realized_pair.target_core
    refused_target = RealizedCandidateRefusal(
        source_candidate_id=target.source_candidate_id,
        candidate_family=target.candidate_family,
        source_grid_fingerprint=target.source_grid_fingerprint,
        reason_codes=("realized_pair_unavailable",))
    refused_with_scores = replace(
        comparison, realized_pair=replace(
            comparison.realized_pair, target_core=refused_target))
    with pytest.raises(ValueError):
        load_scan_design_realized_comparison(
            persist_realized_comparison(tmp_path, refused_with_scores), tmp_path)


def test_runtime_scoring_failure_persists_unavailable_exact_request_and_keeps_legacy(
        monkeypatch, tmp_path):
    inventory = _inventory()
    proposal = _proposal(inventory)
    proposal.meta["scan_design_inventory_ref"] = _inventory_reference(tmp_path, inventory)
    before = (proposal.e_lo, proposal.e_hi, proposal.e_step, proposal.ncount)
    cfg = SimpleNamespace(
        workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl",
        run_nonce="run", max_points=200, max_points_source="default",
        instrument_limits=None)
    objective = SimpleNamespace(
        analysis_limit_seconds=100.0, tolerance_meV=0.5,
        steering={"max_scan_points": 100, "production_batch_ncount": 10_000,
                  "max_ncount_per_scan": 1_000_000})

    def fail(*_args, **_kwargs):
        raise RuntimeError("Monte Carlo backend failed")

    monkeypatch.setattr(loop, "score_proposal_realized_pair", fail)

    reference = loop._refresh_realized_comparison(
        cfg, objective, proposal,
        {"kind": "neutrons", "amount": 10**12, "spent": 0.0}, it=3)
    comparison = load_scan_design_realized_comparison(reference, tmp_path)

    assert before == (proposal.e_lo, proposal.e_hi, proposal.e_step, proposal.ncount)
    assert comparison.score_disposition == "unavailable"
    assert comparison.reason_codes == ("realized_scoring_failed",)
    assert comparison.variants == ()
    assert comparison.seed_evidence is None
    assert proposal.meta["scan_design_realized_selected_request_fingerprint"] == (
        proposal.meta["proposal_fingerprint"])
    assert proposal.meta["scan_design_realized_selected_grid_fingerprint"] == (
        comparison.selected_grid_fingerprint)


def test_unexpected_target_construction_is_logged_and_retains_exact_legacy(
        monkeypatch, tmp_path):
    inventory = _inventory()
    proposal = _proposal(inventory)
    proposal.meta["scan_design_inventory_ref"] = _inventory_reference(tmp_path, inventory)
    before = (proposal.e_lo, proposal.e_hi, proposal.e_step, proposal.ncount)
    cfg = SimpleNamespace(
        workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl",
        run_nonce="run", max_points=200, max_points_source="default",
        instrument_limits=None)
    objective = SimpleNamespace(
        analysis_limit_seconds=100.0, tolerance_meV=0.5,
        steering={"max_scan_points": 100, "production_batch_ncount": 10_000,
                  "max_ncount_per_scan": 1_000_000})

    def broken_target(*_args, **_kwargs):
        raise RuntimeError("counterfactual finalizer defect")

    monkeypatch.setattr(loop, "_finalize_proposal", broken_target)
    reference = loop._refresh_realized_comparison(
        cfg, objective, proposal,
        {"kind": "neutrons", "amount": 10**12, "spent": 0.0}, it=7)
    comparison = load_scan_design_realized_comparison(reference, tmp_path)

    assert before == (proposal.e_lo, proposal.e_hi, proposal.e_step, proposal.ncount)
    assert comparison.selected_request_fingerprint == proposal.meta["proposal_fingerprint"]
    assert comparison.score_disposition == "unavailable"
    assert comparison.reason_codes == ("realized_construction_failed",)
    assert any(event["kind"] == "exception"
               and event["payload"].get("action") == "realized_construction_failed"
               and event["payload"].get("severity") == "high"
               for event in read_log(cfg.log_path))


def test_realized_comparison_event_kind_is_registered(tmp_path):
    record = loop.log_event(
        tmp_path / "session.jsonl", "scan_design_realized_comparison", {"ok": True})
    assert "kind_unknown" not in record


def _offset_score_set(legacy_partitions, target_partitions):
    return SimpleNamespace(candidate_scores=(
        {"candidate_id": "legacy-request",
         "partition_information_nats": list(legacy_partitions)},
        {"candidate_id": "target-request",
         "partition_information_nats": list(target_partitions)},
    ))


def test_per_offset_information_reaches_the_selector_in_partition_order():
    """The selector's offset-agreement test needs every compiled offset, not the pooled margin."""
    offsets = _partition_offset_evidence(
        _offset_score_set((0.4, 0.0), (0.9, 0.0)),
        {"offsets": [0.0, 0.25]}, {"legacy-request"}, {"target-request"})

    assert [item.offset for item in offsets] == [0.0, 0.25]
    assert [item.legacy_information_nats for item in offsets] == [0.4, 0.0]
    assert [item.target_core_information_nats for item in offsets] == [0.9, 0.0]


@pytest.mark.parametrize("score_set", [
    _offset_score_set((0.4,), (0.9,)),
    SimpleNamespace(candidate_scores=(
        {"candidate_id": "legacy-request", "partition_information_nats": [0.4, 0.0]},)),
])
def test_missing_or_short_per_offset_information_fails_closed(score_set):
    assert _partition_offset_evidence(
        score_set, {"offsets": [0.0, 0.25]},
        {"legacy-request"}, {"target-request"}) is None


def test_a_refused_rescore_persists_and_reports_its_own_reason_code(monkeypatch, tmp_path):
    """A reasoned refusal (here: the Test9 policy-scope mismatch) must survive to the artifact and
    to the operator meta — a bare "unavailable" cannot be diagnosed after the beamtime."""
    from isar.eig.realized_score import RealizedScoringUnavailable

    inventory = _inventory()
    proposal = _proposal(inventory)
    proposal.meta["scan_design_inventory_ref"] = _inventory_reference(tmp_path, inventory)
    cfg = SimpleNamespace(
        workspace=tmp_path, log_path=tmp_path / "report" / "session.jsonl",
        run_nonce="run", max_points=200, max_points_source="default",
        instrument_limits=None)
    objective = SimpleNamespace(
        analysis_limit_seconds=100.0, tolerance_meV=0.5,
        steering={"max_scan_points": 100, "production_batch_ncount": 10_000,
                  "max_ncount_per_scan": 1_000_000})

    def refuse(*_args, **_kwargs):
        raise RealizedScoringUnavailable("qoi_policy_missing")

    monkeypatch.setattr(loop, "score_proposal_realized_pair", refuse)
    reference = loop._refresh_realized_comparison(
        cfg, objective, proposal,
        {"kind": "neutrons", "amount": 10**12, "spent": 0.0}, it=4)
    comparison = load_scan_design_realized_comparison(reference, tmp_path)

    assert comparison.score_disposition == "unavailable"
    assert comparison.reason_codes == ("qoi_policy_missing",)
    assert proposal.meta["scan_design_realized_reason_codes"] == ["qoi_policy_missing"]
    assert any(event["kind"] == "exception"
               and event["payload"].get("action") == "qoi_policy_missing"
               for event in read_log(cfg.log_path))


# ------------------------------------- PR #9 R26: neither swallowed exception may go unrecorded
def test_the_shared_authority_refresh_records_the_failure_that_caused_it(monkeypatch, tmp_path):
    """A repaired evidence failure still has to leave a cause behind.

    The bounded refresh can turn a tampered or truncated realized-comparison artifact into a clean
    decision. Without an event here the only trace is ``refresh_count: 1`` on the eventual
    ``scan_design_authority_decision``, so the operator never learns the artifact on disk was corrupt.
    """
    proposal, comparison, _payload = _persist_unavailable_comparison(tmp_path)
    proposal.scan_design_inventory = replace(
        proposal.scan_design_inventory,
        context=replace(proposal.scan_design_inventory.context,
                        scan_window_eig_mode="authoritative"))
    proposal.meta["scan_design_inventory_ref"] = _inventory_reference(
        tmp_path, proposal.scan_design_inventory)
    loads = {"n": 0}

    def load(*_args):
        loads["n"] += 1
        if loads["n"] == 1:
            raise ValueError("realized-comparison raw-byte address is invalid")
        return comparison

    class RefusingClient:
        def validate(self, body):
            return {"would_queue": False, "blockers": ["test_refusal"],
                    "requested_points": body.get("n_points", 0), "feasible_points": 0}

    monkeypatch.setattr(loop, "load_scan_design_realized_comparison", load)
    monkeypatch.setattr(loop, "_refresh_realized_comparison", lambda *_a, **_k: None)
    exact_config = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_o41_authority.py"))["_config"]()
    cfg = SimpleNamespace(
        workspace=tmp_path, log_path=tmp_path / "report" / "session_log.jsonl",
        client=RefusingClient(), sample="sample", sample_background_frozen=None)

    loop._prepare_authoritative_submission(
        cfg, SimpleNamespace(scan_window_eig=exact_config, steering={}), proposal,
        {"kind": "neutrons", "amount": 1e9, "spent": 0.0},
        {"would_queue": True, "requested_points": proposal.n_points,
         "feasible_points": proposal.n_points},
        it=3)

    refreshes = [event["payload"] for event in read_log(cfg.log_path)
                 if event["kind"] == "exception"
                 and event["payload"]["action"] == "scan_design_authority_refresh"]
    assert len(refreshes) == 1
    assert "raw-byte address is invalid" in refreshes[0]["error"]
    assert refreshes[0]["refresh_count"] == 1


def test_a_repaired_pre_submit_authentication_still_records_the_drift(monkeypatch, tmp_path):
    """Drift between a proposal and its realized comparison AT THE SUBMISSION BOUNDARY is the
    stale-evidence signal. When the forced refresh repairs it, the first failure is the only record
    that it ever happened — so it is logged where it is caught, not only when the retry also fails."""
    proposal, comparison, _payload = _persist_unavailable_comparison(tmp_path)
    attempts = {"n": 0}

    def authenticate(*_args, **_kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ValueError("submission identity is inconsistent")
        return comparison

    monkeypatch.setattr(loop, "authenticate_realized_submission", authenticate)
    monkeypatch.setattr(loop, "_refresh_realized_comparison", lambda *_a, **_k: None)
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "report" / "session_log.jsonl")

    loop._authenticate_realized_before_submission(
        cfg, SimpleNamespace(), proposal, {}, it=4)

    assert attempts["n"] == 2
    # The repair succeeded, so the EIG claims survive — and the drift is still on the record.
    assert proposal.meta.get("scan_design_eig_applicability") != "unavailable"
    events = [event["payload"] for event in read_log(cfg.log_path)
              if event["kind"] == "exception"]
    assert [item["action"] for item in events] == [
        "realized_submission_authentication_refresh"]
    assert "submission identity is inconsistent" in events[0]["error"]


# ------------- PR #9 R27: a malformed grid RETURNED BY TAVI is a mismatch, not missing evidence
def test_authority_branch_calls_a_malformed_returned_grid_a_mismatch(monkeypatch, tmp_path):
    """``canonicalize_returned_energies`` must sit outside the evidence-load ``try``.

    Inside it, a non-finite or short grid returned by the INSTRUMENT was reported as
    ``scan_design_authority_unavailable`` — telling the operator ISAR's evidence was missing when
    TAVI had actually returned a malformed grid, and hiding the scan from the slice-8 gate, which
    keys on ``"mismatch"``.
    """
    proposal, comparison, payload = _persist_unavailable_comparison(tmp_path)
    energies = list(comparison.realized_pair.legacy.executed_energies)
    decision = SimpleNamespace(
        physical_role="legacy",
        authority_decision_fingerprint="a" * 64,
        selected_request={"request_fingerprint": payload["proposal_fingerprint"],
                          "grid_fingerprint": "g" * 64,
                          "executed_energies": energies})
    monkeypatch.setattr(loop, "load_authority_decision", lambda *_a, **_k: decision)
    payload["scan_design_authority_decision_ref"] = {
        "kind": "scan_design_authority_decision", "sha256": "a" * 64,
        "path": f"report/scan-design/{'a' * 64}.json"}
    payload["scan_design_authority_decision_fingerprint"] = "a" * 64
    payload["scan_design_authority_selected_role"] = "legacy"
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "report" / "session_log.jsonl")

    for returned in ([], [float("nan")] * len(energies), [float("inf")] * len(energies)):
        assert loop._compare_returned_realized_grid(
            cfg, proposal, payload, {"result": {"scan_values_1": returned}}, it=2) == (
                "mismatch", "executed_grid_mismatch")

    actions = [event["payload"]["action"] for event in read_log(cfg.log_path)
               if event["kind"] == "exception"]
    assert actions == ["executed_grid_mismatch"] * 3
    assert "scan_design_authority_unavailable" not in actions
    # An intact grid still matches, so the move did not weaken the comparison.
    assert loop._compare_returned_realized_grid(
        cfg, proposal, payload, {"result": {"scan_values_1": energies}}, it=2) == ("matched", None)
