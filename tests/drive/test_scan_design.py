"""The scan-design seam: candidate families, shadow plumbing, and the audit artifact."""
from __future__ import annotations

import json
import hashlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest

from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.scan_design import (
    FAMILY_BROAD_RECOVERY,
    FAMILY_LEGACY_UNIFORM,
    FAMILY_TARGET_CORE,
    FAMILY_TARGET_HIGH_ANCHOR,
    FAMILY_TARGET_LOW_ANCHOR,
    FAMILY_TARGET_PAIRED,
    FAMILY_TARGET_PLUS_RIVAL,
    OBJECTIVE_PROJECTION_SCHEMA,
    PHYSICAL_WINDOW_GENERATOR_ID,
    PHYSICAL_WINDOW_GENERATOR_VERSION,
    PREDICTIVE_PROVIDER_ID,
    PREDICTIVE_PROVIDER_SCHEMA,
    SCAN_DESIGN_CANDIDATE_SCHEMA,
    SCAN_DESIGN_INVENTORY_SCHEMA,
    SELECTION_BASIS_LEGACY_CONTROL_OFF,
    SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED,
    STEERING_REGIME,
    PhysicalWindowGeneratorConfig,
    ScanDesignContext,
    ScanDesignInventory,
    _declared_window,
    _energy_window,
    design_scan_windows,
)
from isar.drive.acquisition.grid_adapters import DEFAULT_GRID_ADAPTER
from isar.drive.acquisition.scan_grid import (
    format_scan_command,
    resolve_uniform_grid,
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
    inventory = design_scan_windows(_context())
    candidate = inventory.selected_candidate

    assert inventory.inventory_version == SCAN_DESIGN_INVENTORY_SCHEMA
    assert inventory.selection_basis == SELECTION_BASIS_LEGACY_CONTROL_OFF
    assert len(inventory.candidates) == 1
    assert not inventory.refusals
    assert candidate.e_lo == pytest.approx(-7.0)
    assert candidate.e_hi == pytest.approx(6.2)
    assert candidate.e_step == pytest.approx(0.2)
    assert candidate.n_points == 67
    assert candidate.ncount == 1_000
    assert candidate.nominal_cost == 67_000
    assert candidate.candidate_family == FAMILY_LEGACY_UNIFORM
    assert candidate.to_dict()["generator_id"] == PHYSICAL_WINDOW_GENERATOR_ID
    assert candidate.to_dict()["generator_version"] == PHYSICAL_WINDOW_GENERATOR_VERSION


def test_declared_rival_window_is_not_rederived_from_target_prediction():
    inventory = design_scan_windows(_context(
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
    first = design_scan_windows(_context())
    duplicate = design_scan_windows(_context())
    changed = design_scan_windows(_context(resolution_fwhm_meV=1.0))

    assert first.context.fingerprint == duplicate.context.fingerprint
    assert first.selected_candidate_id == duplicate.selected_candidate_id
    assert first.content_hash() == duplicate.content_hash()
    assert changed.content_hash() != first.content_hash()
    payload = first.to_full_dict()
    # Slice 6 always emits the diagnostic envelope, but an unscored inventory still carries no
    # utility, ranking, winner, or authoritative selection.
    assert _eig_mentions(payload) == {
        "scan_window_eig_mode", "eig_authoritative", "eig_shadow"}
    assert payload["shadow_scoring"]["status"] == "not_requested"
    json.dumps(payload, allow_nan=False)


def test_provenance_artifact_joins_planned_candidate_to_final_request(tmp_path: Path):
    inventory = design_scan_windows(_context())
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
    assert hashlib.sha256(artifact.read_bytes()).hexdigest() == ref["sha256"]
    assert json.loads(artifact.read_text(encoding="utf-8")) == inventory.to_full_dict()
    assert proposal.meta["scan_design_selection_basis"] == inventory.selection_basis
    assert proposal.meta["scan_design_shadow_status"] == "not_requested"
    assert proposal.meta["scan_design_shadow_fingerprint"] == (
        inventory.shadow_scoring.shadow_scoring_fingerprint)
    assert proposal.meta["scan_design_shadow_variant_fingerprints"] == []
    assert proposal.meta["proposal_fingerprint"] == request_fingerprint
    _stamp_proposal_provenance(proposal)
    assert proposal.meta["proposal_fingerprint"] == request_fingerprint


def test_realization_records_chain_selected_candidate_to_each_exact_request(tmp_path: Path):
    inventory = design_scan_windows(_context())
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
    inventory = design_scan_windows(_context())
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


# --- candidate families, shadow plumbing, and the grid seam --------------------------------------

def _walk_keys(value):
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def _eig_mentions(payload) -> set[str]:
    return {key for key in _walk_keys(payload) if "eig" in key.lower()}


def _families(inventory) -> list[str]:
    return [candidate.candidate_family for candidate in inventory.candidates]


def _refusals(inventory) -> dict[str, str]:
    return {refusal.candidate_family: refusal.reason_code
            for refusal in inventory.refusals}


def test_shadow_mode_generates_the_family_and_still_selects_the_legacy_control():
    off = design_scan_windows(_context())
    shadow = design_scan_windows(_context(scan_window_eig_mode="shadow"))

    assert _families(off) == [FAMILY_LEGACY_UNIFORM]
    assert off.selection_basis == SELECTION_BASIS_LEGACY_CONTROL_OFF
    assert shadow.selection_basis == SELECTION_BASIS_SHADOW_LEGACY_CONTROL_UNSCORED
    assert _families(shadow)[0] == FAMILY_LEGACY_UNIFORM
    assert set(_families(shadow)) | set(_refusals(shadow)) == {
        FAMILY_LEGACY_UNIFORM, FAMILY_TARGET_CORE, FAMILY_TARGET_PAIRED,
        FAMILY_TARGET_LOW_ANCHOR, FAMILY_TARGET_HIGH_ANCHOR, FAMILY_TARGET_PLUS_RIVAL,
        FAMILY_BROAD_RECOVERY}
    # The executed design is untouched by the mode — same window, same exposure, same identity.
    legacy_off, legacy_shadow = off.selected_candidate, shadow.selected_candidate
    assert (legacy_shadow.e_lo, legacy_shadow.e_hi, legacy_shadow.e_step,
            legacy_shadow.n_points, legacy_shadow.ncount) == (
        legacy_off.e_lo, legacy_off.e_hi, legacy_off.e_step,
        legacy_off.n_points, legacy_off.ncount)
    assert shadow.selected_candidate_id == off.selected_candidate_id
    payload = shadow.to_full_dict()
    assert _eig_mentions(payload) == {
        "scan_window_eig_mode", "eig_authoritative", "eig_shadow"}
    assert payload["shadow_scoring"]["status"] == "unavailable"


def test_shadow_inventory_is_deterministic_and_json_serializable():
    first = design_scan_windows(_context(scan_window_eig_mode="shadow"))
    second = design_scan_windows(_context(scan_window_eig_mode="shadow"))

    assert first.content_hash() == second.content_hash()
    assert [c.candidate_id for c in first.candidates] == [
        c.candidate_id for c in second.candidates]
    json.dumps(first.to_full_dict(), allow_nan=False)


def test_inventory_v3_rejects_nonlegacy_selection_and_inconsistent_basis():
    inventory = design_scan_windows(_context(scan_window_eig_mode="shadow"))
    target = next(candidate for candidate in inventory.candidates
                  if candidate.candidate_family == FAMILY_TARGET_CORE)

    nonlegacy = inventory.to_full_dict()
    nonlegacy["selection"]["selected_candidate_id"] = target.candidate_id
    with pytest.raises(ValueError, match="legacy"):
        ScanDesignInventory.from_dict(nonlegacy)

    inconsistent = inventory.to_full_dict()
    inconsistent["selection"]["basis"] = "shadow_legacy_control_scored"
    with pytest.raises(ValueError, match="basis"):
        ScanDesignInventory.from_dict(inconsistent)


def test_inventory_declares_generator_config_and_provider_identity():
    payload = design_scan_windows(
        _context(scan_window_eig_mode="shadow")).to_full_dict()

    assert payload["generator"]["id"] == PHYSICAL_WINDOW_GENERATOR_ID
    assert payload["generator"]["version"] == PHYSICAL_WINDOW_GENERATOR_VERSION
    config = payload["generator"]["config"]
    assert config["config_schema"] == "isar.physical_window_generator_config/2"
    # Numeric precision is not a construction constant: which decimals a grid identity carries is
    # owned by the instrument grid adapter, and the dead ``grid_energy_decimals`` field that claimed
    # otherwise (serialized and fingerprinted, never read) is gone.
    assert "grid_energy_decimals" not in config
    assert config["step_resolution_fraction"] == pytest.approx(0.25)
    assert config["core_fwhm_multiple"] == pytest.approx(2.0)
    assert config["core_sigma_multiple"] == pytest.approx(3.0)
    assert config["broad_recovery"] == {"default_lo_meV": -2.0, "default_hi_meV": 7.0}
    assert payload["generator"]["config_fingerprint"] == (
        PhysicalWindowGeneratorConfig().fingerprint)
    assert payload["provider_identity"] == {
        "predictive_provider_id": PREDICTIVE_PROVIDER_ID,
        "predictive_provider_schema": PREDICTIVE_PROVIDER_SCHEMA,
        "generator_id": PHYSICAL_WINDOW_GENERATOR_ID,
        "generator_version": PHYSICAL_WINDOW_GENERATOR_VERSION,
        "objective_projection_schema": OBJECTIVE_PROJECTION_SCHEMA,
        "steering_regime": STEERING_REGIME,
    }
    assert payload["selection"]["scan_window_eig_mode"] == "shadow"


def test_generator_configuration_strictly_round_trips_nondefault_values():
    configured = PhysicalWindowGeneratorConfig(core_fwhm_multiple=4.25)
    restored = PhysicalWindowGeneratorConfig.from_dict(configured.to_dict())
    assert restored == configured
    assert restored.fingerprint == configured.fingerprint

    malformed = configured.to_dict()
    malformed["unexpected"] = True
    with pytest.raises(ValueError, match="fields"):
        PhysicalWindowGeneratorConfig.from_dict(malformed)


def test_family_windows_follow_the_frozen_generator_constants():
    inventory = design_scan_windows(_context(scan_window_eig_mode="shadow"))
    by_family = {c.candidate_family: c for c in inventory.candidates}
    # e_pred 5.0, FWHM 0.8, sigma 0.2 -> half-core = max(2*0.8, 3*0.2) = 1.6
    core = by_family[FAMILY_TARGET_CORE]
    assert (core.e_lo, core.e_hi) == pytest.approx((3.4, 6.6))
    assert core.e_step == pytest.approx(0.2)
    low = by_family[FAMILY_TARGET_LOW_ANCHOR]
    assert (low.e_lo, low.e_hi) == pytest.approx((3.4 - 2.4, 6.6))
    high = by_family[FAMILY_TARGET_HIGH_ANCHOR]
    assert (high.e_lo, high.e_hi) == pytest.approx((3.4, min(6.6 + 2.4, 7.0)))
    paired = by_family[FAMILY_TARGET_PAIRED]
    assert (paired.e_lo, paired.e_hi) == pytest.approx((-2.0, 6.6))
    broad = by_family[FAMILY_BROAD_RECOVERY]
    assert (broad.e_lo, broad.e_hi) == pytest.approx((-2.0, 7.0))


def test_every_generated_candidate_stays_inside_the_authorized_domain_and_limits():
    inventory = design_scan_windows(_context(
        scan_window_eig_mode="shadow", kind="production", policy_max_points=20,
        runtime_max_points=12, runtime_max_neutrons_per_point=20_000,
        dynamic_ncount=True, last_submitted_ncount=40_000, effective_exposure=40_000,
        diagnostic_halfwidth_meV=2.0))

    assert len(inventory.candidates) > 1
    for candidate in inventory.candidates:
        assert candidate.n_points <= 12, candidate.candidate_family
        # The dynamic ladder asks for more than the instrument allows; every candidate is already
        # clamped here, so loop-side finalization has nothing left to change.
        assert candidate.diagnostics["requested_before_runtime_limit"] > 20_000
        assert candidate.ncount == 20_000, candidate.candidate_family
        assert candidate.hkl == (2.1, 0.0, 0.0) and candidate.q == 0.1
        assert candidate.kind == "production" and candidate.reason == "cold-start survey"
        if candidate.candidate_family == FAMILY_LEGACY_UNIFORM:
            continue
        assert candidate.e_lo >= -2.0 - 1e-12, candidate.candidate_family
        assert candidate.e_hi <= 7.0 + 1e-12, candidate.candidate_family
        assert candidate.energies[0] >= -2.0 - 1e-6
        assert candidate.energies[-1] <= 7.0 + 1e-6


def test_a_family_that_cannot_hold_target_support_is_refused_not_omitted():
    inventory = design_scan_windows(_context(
        scan_window_eig_mode="shadow", target_band=(5.0, 0.05),
        minimum_target_support_points=50))

    refusals = _refusals(inventory)
    assert refusals[FAMILY_TARGET_CORE] == "insufficient_target_support"
    detail = next(r for r in inventory.refusals
                  if r.candidate_family == FAMILY_TARGET_CORE).to_dict()["detail"]
    assert detail["required"] == 50 and detail["target_support_points"] < 50
    # The control is never gated by the alternative families' invariants: it is what executes.
    assert _families(inventory)[0] == FAMILY_LEGACY_UNIFORM


def test_anti_stokes_gate_decides_whether_a_paired_window_exists():
    admissible = design_scan_windows(_context(scan_window_eig_mode="shadow"))
    paired = next(c for c in admissible.candidates
                  if c.candidate_family == FAMILY_TARGET_PAIRED)
    assert paired.e_lo < 0.0 < paired.e_hi

    forbidden = design_scan_windows(_context(
        scan_window_eig_mode="shadow", positive_only=True))
    assert FAMILY_TARGET_PAIRED not in _families(forbidden)
    assert _refusals(forbidden)[FAMILY_TARGET_PAIRED] == "anti_stokes_not_admissible"


def test_rival_coverage_is_additive_and_never_produces_a_rival_only_window():
    inventory = design_scan_windows(_context(
        scan_window_eig_mode="shadow", rival_band=(6.5, 0.1)))
    combined = next(c for c in inventory.candidates
                    if c.candidate_family == FAMILY_TARGET_PLUS_RIVAL)
    # Contiguous union with the core: a canonical target action never gets a window that has
    # abandoned its own target for the rival.
    assert combined.e_lo <= 3.4 + 1e-12
    assert combined.e_hi >= 6.7 - 1e-12

    without = design_scan_windows(_context(scan_window_eig_mode="shadow"))
    assert _refusals(without)[FAMILY_TARGET_PLUS_RIVAL] == "no_rival_band_declared"


def test_a_declared_rival_probe_window_is_never_widened_by_the_generator():
    inventory = design_scan_windows(_context(
        scan_window_eig_mode="shadow",
        kind="identity_rival_probe",
        window_family="rival",
        declared_energy_lo=7.0,
        declared_energy_hi=7.4,
        design_prediction_mean_meV=7.2,
        design_prediction_sigma_meV=2.0,
        survey=True,
    ))

    assert _families(inventory) == [FAMILY_LEGACY_UNIFORM]
    assert set(_refusals(inventory).values()) == {"declared_rival_window_is_authoritative"}
    assert (inventory.selected_candidate.e_lo,
            inventory.selected_candidate.e_hi) == (7.0, 7.4)


def test_identical_grids_deduplicate_with_alias_provenance():
    # A search band exactly equal to the target core makes several construction rules land on the
    # same physical scan; one candidate must survive, carrying the others as provenance.
    inventory = design_scan_windows(_context(
        scan_window_eig_mode="shadow", energy_search_meV=(3.4, 6.6)))
    repeat = design_scan_windows(_context(
        scan_window_eig_mode="shadow", energy_search_meV=(3.4, 6.6)))
    core = next(c for c in inventory.candidates
                if c.candidate_family == FAMILY_TARGET_CORE)

    assert core.alias_families == (FAMILY_TARGET_LOW_ANCHOR, FAMILY_TARGET_HIGH_ANCHOR,
                                   FAMILY_BROAD_RECOVERY)
    assert core.to_dict()["alias_families"] == list(core.alias_families)
    grids = [(c.energies, c.ncount) for c in inventory.candidates]
    assert len(grids) == len(set(grids))
    assert [c.candidate_id for c in repeat.candidates] == [
        c.candidate_id for c in inventory.candidates]


def test_authoritative_mode_builds_the_same_candidate_space_as_shadow():
    authoritative = design_scan_windows(_context(scan_window_eig_mode="authoritative"))
    shadow = design_scan_windows(_context(scan_window_eig_mode="shadow"))
    assert [candidate.candidate_id for candidate in authoritative.candidates] == [
        candidate.candidate_id for candidate in shadow.candidates]
    assert authoritative.selected_candidate.candidate_family == FAMILY_LEGACY_UNIFORM
    with pytest.raises(ValueError, match="scan_window_eig_mode must be one of"):
        _context(scan_window_eig_mode="on")


def test_canonical_grid_helper_agrees_with_the_legacy_window_arithmetic():
    for e_pred, fwhm, sigma in ((5.0, 0.8, 0.2), (3.0, 0.5, 1.4), (0.0, 1.0, 0.0)):
        e_lo, e_hi, e_step, n_points = _energy_window(
            e_pred, fwhm, sigma, energy_search_meV=(-2.0, 7.0), max_points=100)
        grid = resolve_uniform_grid(e_lo, e_hi, 0.25 * max(fwhm, 1e-3), max_points=100)
        assert (grid.e_lo, grid.e_hi, grid.n_points) == (e_lo, e_hi, n_points)
        assert grid.e_step == pytest.approx(e_step)

    declared = _declared_window(-2.0, 7.0, 0.05, max_points=40)
    grid = resolve_uniform_grid(-2.0, 7.0, 0.25 * 0.05, max_points=40)
    assert (grid.e_lo, grid.e_hi, grid.e_step, grid.n_points) == declared


def test_candidate_grid_is_explicit_ordered_and_inclusive_of_the_upper_bound():
    candidate = design_scan_windows(_context()).selected_candidate
    payload = candidate.to_dict()

    assert payload["scoring_grid"] == list(DEFAULT_GRID_ADAPTER.executed_energies(
        candidate.e_lo, candidate.e_step, candidate.n_points))
    assert len(payload["scoring_grid"]) == candidate.n_points
    assert payload["grid_adapter"] == DEFAULT_GRID_ADAPTER.identity()
    assert payload["scoring_grid"] == sorted(payload["scoring_grid"])
    # TAVI's ``deltaE lo hi step`` is inclusive: the last point lands on the declared upper bound.
    assert payload["scoring_grid"][0] == pytest.approx(candidate.e_lo)
    assert payload["scoring_grid"][-1] == pytest.approx(candidate.e_hi, abs=candidate.e_step)
    assert payload["scan_command"] == format_scan_command(
        candidate.e_lo, candidate.e_hi, candidate.e_step)
    assert payload["grid_fingerprint"] == candidate.grid.fingerprint(candidate.ncount)


def test_shadow_inventory_persists_every_candidate_and_refusal(tmp_path: Path):
    inventory = design_scan_windows(_context(scan_window_eig_mode="shadow"))
    selected = inventory.selected_candidate
    proposal = ScanProposal(
        h=2.1, k=0.0, l=0.0, e_lo=selected.e_lo, e_hi=selected.e_hi,
        e_step=selected.e_step, ncount=selected.ncount, kind="quicklook",
        reason="cold-start survey", n_points=selected.n_points,
        meta={"scientific_action_fingerprint": "scientific-action"},
        scan_design_inventory=inventory,
    )
    _stamp_proposal_provenance(proposal)
    cfg = SimpleNamespace(workspace=tmp_path, log_path=tmp_path / "session.jsonl")

    ref = _persist_scan_design_inventory(cfg, proposal, it=1)
    stored = json.loads((tmp_path / ref["path"]).read_text(encoding="utf-8"))

    assert stored == inventory.to_full_dict()
    assert [c["candidate_family"] for c in stored["candidates"]] == _families(inventory)
    assert stored["refused_candidates"]
    assert all(entry["refused"] is True and entry["reason_code"]
               for entry in stored["refused_candidates"])
    assert proposal.meta["scan_design_candidate_id"] == selected.candidate_id


def test_candidate_id_covers_the_grid_and_exposure_but_not_alias_provenance():
    candidate = design_scan_windows(_context()).selected_candidate
    aliased = design_scan_windows(_context(scan_window_eig_mode="shadow")).selected_candidate
    assert candidate.candidate_id == aliased.candidate_id
    assert SCAN_DESIGN_CANDIDATE_SCHEMA.endswith("/2")

    payload = json.loads(json.dumps(candidate.to_dict()))
    assert payload["candidate_id"] == candidate.candidate_id


# ---- degenerate and clipped windows ----------------------------------------------------------
#
# All three of these were reachable from live instrument state: TAVI's ``GET /resolution`` accepts
# any positive float (``Infinity`` included, since ``json.loads`` accepts it), a campaign may leave
# a search band unbounded, and a rival probe is evaluated with no ``target_band`` at all.


def test_a_single_point_window_is_refused_rather_than_proposed():
    """A grid the instrument will not run is not a candidate, however well it would score.

    TAVI refuses any single-point request outright (``HTTP 400 ... Only 1 scan point!``), and
    ``resolve_point_count`` normalizes an inverted window to its one starting point — so both
    degeneracies present as ``n_points == 1``. ``broad_recovery/1`` reaches it from ordinary state:
    on a narrow authorized domain the resolution-derived step is wider than the whole domain.
    """
    context = _context(scan_window_eig_mode="shadow", energy_search_meV=(6.9, 7.0),
                       design_prediction_mean_meV=6.95, target_prediction_mean_meV=6.95,
                       design_prediction_sigma_meV=0.02, target_prediction_sigma_meV=0.02,
                       resolution_fwhm_meV=1.0)
    inventory = design_scan_windows(context)

    assert FAMILY_BROAD_RECOVERY not in _families(inventory)
    assert _refusals(inventory)[FAMILY_BROAD_RECOVERY] == "degenerate_window"
    detail = dict(next(refusal.detail for refusal in inventory.refusals
                       if refusal.candidate_family == FAMILY_BROAD_RECOVERY))
    assert detail["n_points"] == 1 and detail["minimum_points"] == 2
    # Every candidate that survives is executable, and the legacy control is untouched.
    assert all(candidate.n_points >= 2 and candidate.e_hi >= candidate.e_lo
               for candidate in inventory.candidates)
    assert _families(inventory)[0] == FAMILY_LEGACY_UNIFORM
    assert inventory.selected_candidate.candidate_family == FAMILY_LEGACY_UNIFORM


def test_the_degeneracy_refusal_never_masks_a_more_specific_one():
    # It is the least informative diagnosis, so it runs last: a target family that also lost its
    # core must still say THAT, not merely that the leftover window had one point.
    context = _context(scan_window_eig_mode="shadow", energy_search_meV=(6.9, 7.0),
                       design_prediction_mean_meV=6.95, target_prediction_mean_meV=6.95,
                       design_prediction_sigma_meV=0.02, target_prediction_sigma_meV=0.02,
                       resolution_fwhm_meV=1.0)
    refusals = _refusals(design_scan_windows(context))
    assert refusals[FAMILY_TARGET_CORE] == "target_core_truncated_after_domain_clip"
    assert refusals[FAMILY_TARGET_LOW_ANCHOR] == "insufficient_target_support"


def test_an_infinite_resolution_still_designs_the_legacy_window_in_off_mode():
    # Regression: the legacy candidate's grid fingerprint serialized raw floats, so a degenerate
    # step made ``design_scan_windows`` raise "not JSON compliant: nan" on a context the pre-seam
    # designer handled. Off mode must produce the legacy candidate for exactly the contexts the old
    # code produced it for.
    context = _context(resolution_fwhm_meV=float("inf"), target_band=(5.0, 0.2))
    inventory = design_scan_windows(context)

    legacy = inventory.selected_candidate
    assert _families(inventory) == [FAMILY_LEGACY_UNIFORM]
    assert (legacy.e_lo, legacy.e_hi, legacy.e_step, legacy.n_points) == _energy_window(
        context.design_prediction_mean_meV, context.resolution_fwhm_meV,
        context.design_prediction_sigma_meV,
        survey=context.survey, energy_search_meV=context.energy_search_meV,
        target_band=context.target_band, rival_band=context.rival_band,
        widen=context.widen_factor, positive_only=context.positive_only,
        max_points=min(context.policy_max_points, context.runtime_max_points))
    # The single visited point is e_lo itself — never e_lo + 0·inf, which is NaN.
    assert legacy.energies == (legacy.e_lo,)
    assert legacy.grid.fingerprint(legacy.ncount)
    assert json.loads(json.dumps(inventory.to_full_dict()))


def test_an_unbounded_search_band_refuses_the_affected_family_not_the_inventory():
    # Regression: ``(-inf, +inf)`` overflowed ``int(floor(inf))`` inside the grid resolver and took
    # the whole inventory — including the already-built legacy control — down with an OverflowError.
    context = _context(energy_search_meV=(float("-inf"), float("inf")))
    off = design_scan_windows(context)
    shadow = design_scan_windows(_context(
        energy_search_meV=(float("-inf"), float("inf")), scan_window_eig_mode="shadow"))

    # Off-mode parity: the legacy control is byte-identical in both modes and is still selected.
    assert _families(off) == [FAMILY_LEGACY_UNIFORM]
    assert shadow.candidates[0].candidate_id == off.selected_candidate.candidate_id
    assert shadow.selected_candidate_id == shadow.candidates[0].candidate_id
    # The unbounded family is a structured non-result, not a crash.
    assert _refusals(shadow)[FAMILY_BROAD_RECOVERY] == "nonfinite_window_arithmetic"
    detail = next(r for r in shadow.refusals
                  if r.candidate_family == FAMILY_BROAD_RECOVERY).to_dict()["detail"]
    assert detail["e_lo"] == "-inf" and detail["e_hi"] == "+inf"
    assert json.loads(json.dumps(shadow.to_full_dict()))


def test_a_target_family_clipped_off_its_own_core_is_refused_not_emitted():
    # Regression: the core-containment invariant of target_plus_rival/1 holds only *before* the
    # authorized-domain clip, and the insufficient_target_support gate is skipped when no
    # target_band is declared — exactly the state a rival probe is evaluated in. The result was a
    # rival-only window with zero target support presented as a target candidate.
    rival_only = _context(scan_window_eig_mode="shadow", design_prediction_mean_meV=6.0,
                          target_prediction_mean_meV=6.0, resolution_fwhm_meV=0.5,
                          energy_search_meV=(-4.0, 2.0), rival_band=(0.5, 0.2),
                          target_band=None)
    inventory = design_scan_windows(rival_only)

    assert _families(inventory) == [FAMILY_LEGACY_UNIFORM]
    assert _refusals(inventory)[FAMILY_TARGET_PLUS_RIVAL] == "rival_only_after_domain_clip"
    assert _refusals(inventory)[FAMILY_TARGET_PAIRED] == "target_core_lost_after_domain_clip"
    detail = next(r for r in inventory.refusals
                  if r.candidate_family == FAMILY_TARGET_PLUS_RIVAL).to_dict()["detail"]
    assert detail["target_core_lo"] == 5.0 and detail["target_core_hi"] == 7.0

    # Declaring a target band does not change *why* the window is unusable: it abandoned the target,
    # which is a strictly more specific fact than undersampling it. The refusal reason is therefore
    # the same with and without a declared band — before the sliver repair the declared-band case
    # reported the weaker `insufficient_target_support` because the support gate ran first.
    declared = design_scan_windows(_context(
        scan_window_eig_mode="shadow", design_prediction_mean_meV=6.0,
        target_prediction_mean_meV=6.0, resolution_fwhm_meV=0.5,
        energy_search_meV=(-4.0, 2.0), rival_band=(0.5, 0.2), target_band=(6.0, 0.2)))
    assert _families(declared) == [FAMILY_LEGACY_UNIFORM]
    assert _refusals(declared)[FAMILY_TARGET_PLUS_RIVAL] == "rival_only_after_domain_clip"


def test_dedup_merges_only_candidates_that_emit_the_same_request():
    # Regression: dedup keyed on (visited energies, ncount) while candidate_id also covers the
    # scan command, so two families whose windows differ by less than half a step merged into a
    # candidate whose emitted deltaE string was not the absorbed family's.
    inventory = design_scan_windows(_context(
        scan_window_eig_mode="shadow", energy_search_meV=(3.4, 6.65)))
    core = next(c for c in inventory.candidates if c.candidate_family == FAMILY_TARGET_CORE)
    anchor = next(c for c in inventory.candidates
                  if c.candidate_family == FAMILY_TARGET_HIGH_ANCHOR)

    # Same visited points and exposure — the old key would have merged them...
    assert (core.energies, core.ncount) == (anchor.energies, anchor.ncount)
    # ...but they are different requests, and therefore different candidates.
    assert core.grid.scan_command() != anchor.grid.scan_command()
    assert core.candidate_id != anchor.candidate_id
    assert FAMILY_TARGET_HIGH_ANCHOR not in core.alias_families

    # Genuinely identical requests still merge into one identity with alias provenance.
    identical = design_scan_windows(_context(
        scan_window_eig_mode="shadow", energy_search_meV=(3.4, 6.6)))
    merged = next(c for c in identical.candidates
                  if c.candidate_family == FAMILY_TARGET_CORE)
    assert FAMILY_TARGET_HIGH_ANCHOR in merged.alias_families
    commands = [c.grid.scan_command() for c in identical.candidates]
    assert len(commands) == len(set(commands))


# ---- the sliver gate --------------------------------------------------------------------------
#
# The post-clip gate used to refuse only *complete* disjointness from the target core, so a window
# reduced to a sliver of it survived as a full-fledged target candidate.


def test_a_window_clipped_to_a_sliver_of_its_core_is_refused_not_emitted():
    # The exact repro: core [3.4, 6.6] (e_pred 5.0, FWHM 0.8) against an authorized domain of
    # [6.5, 7.0]. Every target family clips to a 0.1 meV sliver that does not contain the predicted
    # centre; the old bounds-overlap test passed all of them.
    inventory = design_scan_windows(_context(
        scan_window_eig_mode="shadow", energy_search_meV=(6.5, 7.0)))

    # Only the legacy control and the deliberately target-agnostic broad sweep survive.
    assert _families(inventory) == [FAMILY_LEGACY_UNIFORM, FAMILY_BROAD_RECOVERY]
    refusals = _refusals(inventory)
    assert refusals[FAMILY_TARGET_CORE] == "target_core_lost_after_domain_clip"
    assert refusals[FAMILY_TARGET_LOW_ANCHOR] == "target_core_lost_after_domain_clip"
    assert refusals[FAMILY_TARGET_HIGH_ANCHOR] == "target_core_lost_after_domain_clip"
    detail = next(r for r in inventory.refusals
                  if r.candidate_family == FAMILY_TARGET_CORE).to_dict()["detail"]
    assert detail["target_center"] == 5.0
    assert detail["e_lo"] == 6.5 and detail["e_hi"] == 6.6


def test_target_core_needs_its_whole_core_while_anchors_may_lose_margin():
    # Domain [4.0, 7.0] keeps the predicted centre (5.0) but cuts the low side of the core [3.4, 6.6].
    inventory = design_scan_windows(_context(
        scan_window_eig_mode="shadow", energy_search_meV=(4.0, 7.0)))

    assert _refusals(inventory)[FAMILY_TARGET_CORE] == (
        "target_core_truncated_after_domain_clip")
    # ...while an anchor, which never promised the full core, survives on centre + support.
    anchor = next(c for c in inventory.candidates
                  if c.candidate_family == FAMILY_TARGET_LOW_ANCHOR)
    assert (anchor.e_lo, anchor.e_hi) == pytest.approx((4.0, 6.6))


def test_minimum_target_support_is_enforced_against_a_fallback_band_without_a_target_band():
    # No declared target_band: before the repair the support gate silently did not run at all, so a
    # window with three points anywhere near the prediction counted as a target measurement.
    context = _context(scan_window_eig_mode="shadow", target_band=None,
                       policy_max_points=8, runtime_max_points=8,
                       minimum_target_support_points=6)
    inventory = design_scan_windows(context)

    assert _refusals(inventory)[FAMILY_TARGET_PAIRED] == "insufficient_target_support"
    detail = next(r for r in inventory.refusals
                  if r.candidate_family == FAMILY_TARGET_PAIRED).to_dict()["detail"]
    assert detail["support_band_source"] == "resolution_fallback"
    # The fallback band spans exactly the intended core: mean ± 2σ == e_pred ± half-core.
    assert detail["support_band_mean"] == 5.0
    assert detail["support_band_sigma"] == pytest.approx(0.8)
    assert detail["target_support_points"] < 6
    # A window that does sample the core well is untouched, and broad_recovery/1 stays exempt.
    assert FAMILY_TARGET_CORE in _families(inventory)
    assert FAMILY_BROAD_RECOVERY in _families(inventory)


def test_a_declared_target_band_still_wins_over_the_fallback():
    inventory = design_scan_windows(_context(
        scan_window_eig_mode="shadow", target_band=(5.0, 0.05),
        minimum_target_support_points=50))
    detail = next(r for r in inventory.refusals
                  if r.candidate_family == FAMILY_TARGET_CORE).to_dict()["detail"]
    assert detail["support_band_source"] == "declared"
    assert detail["support_band_sigma"] == pytest.approx(0.05)


def test_a_pre_canonical_encoding_still_loads_at_its_semantic_address(tmp_path, caplog):
    """A ``main``-era artifact authenticates semantically but not byte-for-byte.

    The writer changed from ``json.dump(..., indent=2)`` to canonical bytes; the content address is
    the semantics, so every artifact the previous release wrote still authenticates. Rejecting them
    turned an intact historical acquisition into a silent ``scan_design_provenance_invalid`` for
    every shadow replay of a resumed workspace.
    """
    import logging

    from isar.drive.acquisition.inventory import canonical_json
    from isar.drive.acquisition.scan_design import load_scan_design_inventory

    inventory = design_scan_windows(_context(scan_window_eig_mode="shadow"))
    digest = inventory.content_hash()
    path = tmp_path / "report" / "scan-design" / f"{digest}.json"
    path.parent.mkdir(parents=True)
    payload = json.loads(canonical_json(inventory.to_full_dict()))
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")   # the main-era encoding
    assert hashlib.sha256(path.read_bytes()).hexdigest() != digest
    reference = {"kind": "scan_design_inventory", "sha256": digest,
                 "path": f"report/scan-design/{digest}.json"}

    with caplog.at_level(logging.WARNING):
        loaded = load_scan_design_inventory(reference, tmp_path)

    assert loaded.content_hash() == digest
    assert "legacy_artifact_encoding" in caplog.text

    # Tampered semantics are still refused — the fallback is on encoding, not on content.
    path.write_text(json.dumps({**payload, "selected_candidate_id": "tampered"}, indent=2),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="content address is invalid"):
        load_scan_design_inventory(reference, tmp_path)
