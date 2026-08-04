"""Slice 7 exact realized-request construction, persistence, and grid parity."""
from __future__ import annotations

import copy
import runpy
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from isar.drive import loop
from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.inventory import canonical_json
from isar.drive.acquisition.realized_comparison import (
    build_realized_comparison,
    construct_realized_pair,
    load_scan_design_realized_comparison,
    persist_realized_comparison,
)
from isar.drive.acquisition.scan_design import design_scan_windows
from isar.drive.session_log import read_log
from isar.eig.realized_score import score_realized_pair
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


def test_changed_exposure_rescores_the_complete_equal_exposure_pair(monkeypatch, tmp_path):
    helpers = runpy.run_path(str(
        Path(__file__).parents[1] / "eig" / "test_shadow_inventory.py"))
    inventory, scoring = helpers["_complete_scoring"](monkeypatch)
    inventory = inventory.with_shadow_scoring(scoring)
    proposal = _proposal(inventory)
    proposal.ncount -= 1

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
    proposal.meta.update({
        "proposal_fingerprint": comparison.selected_request_fingerprint,
        "scan_design_realized_comparison_ref": reference,
        "scan_design_realized_comparison_fingerprint": (
            comparison.realized_comparison_fingerprint),
        "scan_design_realized_selected_request_fingerprint": (
            comparison.selected_request_fingerprint),
        "scan_design_realized_selected_grid_fingerprint": comparison.selected_grid_fingerprint,
    })
    payload = dict(proposal.meta)
    payload["job_id"] = "job-1"
    return proposal, comparison, payload


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
    assert proposal.meta["scan_design_realized_selected_request_fingerprint"] == (
        proposal.meta["proposal_fingerprint"])
    assert proposal.meta["scan_design_realized_selected_grid_fingerprint"] == (
        comparison.selected_grid_fingerprint)
