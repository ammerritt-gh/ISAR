"""The legacy-only scan-design seam and its content-addressed audit artifact."""
from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.scan_design import (
    LEGACY_SCAN_DESIGN_GENERATOR,
    LEGACY_SELECTION_BASIS,
    SCAN_DESIGN_INVENTORY_SCHEMA,
    ScanDesignContext,
    design_legacy_scan,
)
from isar.drive.loop import (
    _persist_scan_design_inventory,
    _proposal_from_submission,
    _record_scan_design_realization,
    _stamp_proposal_provenance,
)


def _context(**overrides) -> ScanDesignContext:
    values = {
        "scientific_action_fingerprint": "scientific-action",
        "hkl": (2.1, 0.0, 0.0),
        "q": 0.1,
        "kind": "quicklook",
        "reason": "cold-start survey",
        "window_family": "target",
        "declared_energy_lo": None,
        "declared_energy_hi": None,
        "target_prediction_mean_meV": 5.0,
        "target_prediction_sigma_meV": 0.2,
        "design_prediction_mean_meV": 5.0,
        "design_prediction_sigma_meV": 0.2,
        "resolution_fwhm_meV": 0.8,
        "survey": False,
        "energy_search_meV": (-2.0, 7.0),
        "target_band": None,
        "rival_band": None,
        "widen_factor": 1.0,
        "positive_only": False,
        "policy_max_points": 100,
        "runtime_max_points": 200,
        "tolerance_meV": 0.5,
        "quicklook_ncount": 1_000,
        "production_ncount": 10_000,
        "identity_detection_ncount": 2_000,
        "dynamic_ncount": False,
        "tolerance_margin": 0.8,
        "ncount_growth_cap": 4.0,
        "max_ncount_per_scan": 1_000_000,
        "diagnostic_halfwidth_meV": None,
        "certification_halfwidth_meV": None,
        "effective_exposure": None,
        "submitted_exposure": None,
        "production_scans": 0,
        "last_submitted_ncount": None,
        "runtime_max_neutrons_per_point": None,
        "runtime_queue_neutron_budget": None,
        "budget_kind": "neutrons",
        "budget_amount": 1e12,
        "budget_spent": 0.0,
    }
    values.update(overrides)
    return ScanDesignContext(**values)


def test_context_is_frozen_and_strict_json_serializable():
    context = _context()
    with pytest.raises(FrozenInstanceError):
        context.kind = "production"
    assert not any(isinstance(value, (dict, list, set)) for value in context.__dict__.values())
    json.dumps(context.to_dict(), allow_nan=False)


def test_legacy_design_returns_one_exact_uniform_candidate():
    inventory = design_legacy_scan(_context())
    candidate = inventory.selected_candidate

    assert inventory.inventory_version == SCAN_DESIGN_INVENTORY_SCHEMA
    assert inventory.selection_basis == LEGACY_SELECTION_BASIS
    assert len(inventory.candidates) == 1
    assert candidate.e_lo == pytest.approx(-7.0)
    assert candidate.e_hi == pytest.approx(6.2)
    assert candidate.e_step == pytest.approx(0.2)
    assert candidate.n_points == 67
    assert candidate.ncount == 1_000
    assert candidate.nominal_cost == 67_000
    assert candidate.to_dict()["generator"] == LEGACY_SCAN_DESIGN_GENERATOR


def test_declared_rival_window_is_not_rederived_from_target_prediction():
    inventory = design_legacy_scan(_context(
        kind="identity_rival_probe",
        window_family="rival",
        declared_energy_lo=7.0,
        declared_energy_hi=7.4,
        target_prediction_mean_meV=5.0,
        target_prediction_sigma_meV=0.2,
        design_prediction_mean_meV=7.2,
        design_prediction_sigma_meV=2.0,
        survey=True,
    ))
    candidate = inventory.selected_candidate
    assert candidate.e_lo == 7.0
    assert candidate.e_hi == 7.4
    assert candidate.ncount == 2_000


def test_context_candidate_and_inventory_hashes_are_stable_and_sensitive():
    first = design_legacy_scan(_context())
    duplicate = design_legacy_scan(_context())
    changed = design_legacy_scan(_context(resolution_fwhm_meV=1.0))

    assert first.context.fingerprint == duplicate.context.fingerprint
    assert first.selected_candidate_id == duplicate.selected_candidate_id
    assert first.content_hash() == duplicate.content_hash()
    assert changed.content_hash() != first.content_hash()
    payload = first.to_full_dict()
    assert "eig" not in json.dumps(payload).lower()
    json.dumps(payload, allow_nan=False)


def test_provenance_artifact_joins_planned_candidate_to_final_request(tmp_path: Path):
    inventory = design_legacy_scan(_context())
    proposal = ScanProposal(
        h=2.1,
        k=0.0,
        l=0.0,
        e_lo=-7.0,
        e_hi=6.2,
        e_step=0.2,
        ncount=1_000,
        kind="quicklook",
        reason="cold-start survey",
        n_points=67,
        meta={"scientific_action_fingerprint": "scientific-action"},
        scan_design_inventory=inventory,
    )
    _stamp_proposal_provenance(proposal)
    request_fingerprint = proposal.meta["proposal_fingerprint"]
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "session.jsonl")

    ref = _persist_scan_design_inventory(cfg, proposal, it=3)

    assert ref == proposal.meta["scan_design_inventory_ref"]
    assert proposal.meta["scan_design_candidate_id"] == inventory.selected_candidate_id
    assert proposal.meta["scan_design_context_fingerprint"] == inventory.context.fingerprint
    artifact = tmp_path / ref["path"]
    assert artifact.is_file()
    assert json.loads(artifact.read_text(encoding="utf-8")) == inventory.to_full_dict()
    assert proposal.meta["proposal_fingerprint"] == request_fingerprint
    _stamp_proposal_provenance(proposal)
    assert proposal.meta["proposal_fingerprint"] == request_fingerprint


def test_realization_records_chain_selected_candidate_to_each_exact_request(tmp_path: Path):
    inventory = design_legacy_scan(_context())
    proposal = ScanProposal(
        h=2.1,
        k=0.0,
        l=0.0,
        e_lo=-7.0,
        e_hi=6.2,
        e_step=0.25,
        ncount=900,
        kind="quicklook",
        reason="cold-start survey",
        n_points=54,
        meta={"scientific_action_fingerprint": "scientific-action"},
        scan_design_inventory=inventory,
    )
    _stamp_proposal_provenance(proposal)
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "session.jsonl")
    _persist_scan_design_inventory(cfg, proposal, it=3)
    first_request = proposal.meta["proposal_fingerprint"]

    previous = first_request
    proposal.e_step = 0.1
    proposal.n_points = 133
    _stamp_proposal_provenance(proposal)
    assert _record_scan_design_realization(
        cfg,
        proposal,
        adjustment="minimum_target_support",
        previous_request_fingerprint=previous,
        it=3,
        attempt=0,
    )

    realizations = [
        event["payload"]
        for event in (
            json.loads(line)
            for line in cfg.log_path.read_text(encoding="utf-8").splitlines()
        )
        if event["kind"] == "scan_design_realization"
    ]
    assert [item["adjustment"] for item in realizations] == [
        "initial_finalization_clamp",
        "minimum_target_support",
    ]
    assert realizations[1]["previous_request_fingerprint"] == first_request
    assert realizations[1]["request_fingerprint"] == proposal.meta["proposal_fingerprint"]
    assert realizations[1]["inventory_ref"] == proposal.meta["scan_design_inventory_ref"]
    assert realizations[1]["candidate_id"] == inventory.selected_candidate_id
    assert realizations[1]["context_fingerprint"] == inventory.context.fingerprint
    assert realizations[1]["e_step"] == 0.1
    assert realizations[1]["n_points"] == 133
    assert realizations[1]["ncount"] == 900


def test_recovered_submission_retains_realized_request_and_design_join():
    ref = {
        "kind": "scan_design_inventory",
        "sha256": "abc123",
        "path": "report/scan-design/abc123.json",
    }
    payload = {
        "hkl": [2.1, 0.0, 0.0],
        "kind": "production",
        "acquisition_id": "A1",
        "acquisition_target_energy": 5.0,
        "search_energy_lo": -1.0,
        "search_energy_hi": 7.0,
        "search_energy_step": 0.125,
        "requested_points": 65,
        "submitted_ncount": 123_456,
        "proposal_fingerprint": "request-realized",
        "scan_design_inventory_ref": ref,
        "scan_design_candidate_id": "candidate-selected",
        "scan_design_context_fingerprint": "context-selected",
    }

    recovered = _proposal_from_submission(payload)

    assert (recovered.e_lo, recovered.e_hi, recovered.e_step) == (-1.0, 7.0, 0.125)
    assert recovered.n_points == 65
    assert recovered.ncount == 123_456
    assert recovered.meta["proposal_fingerprint"] == "request-realized"
    assert recovered.meta["scan_design_inventory_ref"] == ref
    assert recovered.meta["scan_design_candidate_id"] == "candidate-selected"
    assert recovered.meta["scan_design_context_fingerprint"] == "context-selected"


def test_content_address_deduplicates_and_repairs_corruption(tmp_path: Path):
    inventory = design_legacy_scan(_context())
    proposal = SimpleNamespace(
        h=2.1,
        k=0.0,
        l=0.0,
        e_lo=-7.0,
        e_hi=6.2,
        e_step=0.2,
        n_points=67,
        ncount=1_000,
        kind="quicklook",
        scan_design_inventory=inventory,
        meta={
            "scientific_action_fingerprint": "scientific-action",
            "proposal_fingerprint": "request",
        },
    )
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "session.jsonl")

    first = _persist_scan_design_inventory(cfg, proposal, it=1)
    artifact = tmp_path / first["path"]
    original = artifact.read_text(encoding="utf-8")
    second = _persist_scan_design_inventory(cfg, proposal, it=2)
    assert second == first
    assert artifact.read_text(encoding="utf-8") == original

    artifact.write_text('{"corrupt": true}', encoding="utf-8")
    repaired = _persist_scan_design_inventory(cfg, proposal, it=3)
    assert repaired == first
    assert json.loads(artifact.read_text(encoding="utf-8")) == inventory.to_full_dict()
    events = [
        json.loads(line)
        for line in cfg.log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert any(
        event["kind"] == "exception"
        and event["payload"]["action"] == "scan_design_artifact_mismatch"
        for event in events
    )
