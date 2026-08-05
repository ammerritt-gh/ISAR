from __future__ import annotations

import copy

import pytest

from isar.eig.o41_authority import (
    O41CandidateEvidence,
    O41PartitionOffsetEvidence,
    O41ScoreEvidence,
    O41SelectorInput,
    ROLE_LEGACY,
    ROLE_TARGET,
    select_o41_authority,
    validate_o41_authoritative_profile,
)
from isar.schema.eig_authority import (
    AUTHORITY_DECISION_CONTRACT,
    ScanDesignAuthorityDecision,
)
from isar.schema.eig_policy import (
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    ScanWindowEIGConfig,
)


def _profile():
    variant = QoIDistinctionVariant(
        "primary_0p5_mev", {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0, 0.25]})
    policy = QoIDistinctionPolicy(
        "eig4_slice8_excitation_energy", "isar.scalar_absolute_partitions/o36.5",
        {"id": "excitation.energy", "unit": "meV"},
        {"kind": "campaign_configuration", "fingerprint": "a" * 64,
         "annotation": {"campaign_name": "test", "experiment_description": "test",
                        "date": "2026-08-04"}},
        {"kind": "explicit_user_analysis/1", "source_id": "owner", "rationale": "test"},
        (variant,))
    return validate_o41_authoritative_profile(
        ScanWindowEIGConfig(policy_set=QoIDistinctionPolicySet((policy,))))


def _candidate(role: str, points: int, lo: float, hi: float) -> O41CandidateEvidence:
    energies = tuple(lo + (hi - lo) * index / (points - 1) for index in range(points))
    digit = "1" if role == ROLE_LEGACY else "2"
    return O41CandidateEvidence(
        role, f"{role}-id", digit * 64, ("3" if role == ROLE_LEGACY else "4") * 64,
        lo, hi, energies, 100, f"scan {lo} {hi}")


def _decision() -> ScanDesignAuthorityDecision:
    profile = _profile()
    legacy = _candidate(ROLE_LEGACY, 46, -2.0, 7.0)
    target = _candidate(ROLE_TARGET, 17, 0.4, 3.6)
    result = select_o41_authority(O41SelectorInput(
        profile, "eligible", "applicable", legacy, target,
        O41ScoreEvidence(
            "complete", "target_core_minus_legacy", (0.25,) * 8,
            partition_offsets=(
                O41PartitionOffsetEvidence(0.0, 1.0, 1.0),
                O41PartitionOffsetEvidence(0.25, 1.0, 1.0)))))
    selected = target
    return ScanDesignAuthorityDecision(
        configured_mode="authoritative",
        scientific_action_fingerprint="9" * 64,
        eligibility_result_fingerprint="8" * 64,
        primary_evidence_fingerprint="7" * 64,
        authority_scopes=(("physical_scan_window_selection", "restricted_o41"),),
        source_inventory_ref={"path": "report/scan-design/a.json", "sha256": "a" * 64},
        source_inventory_fingerprint="b" * 64,
        realized_comparison_ref={"path": "report/scan-design-realized/c.json", "sha256": "c" * 64},
        realized_comparison_fingerprint="d" * 64,
        profile=profile.to_dict(),
        legacy_validation={
            "role": "legacy", "candidate_id": legacy.candidate_id,
            "request_fingerprint": legacy.request_fingerprint,
            "grid_fingerprint": legacy.grid_fingerprint,
            "validated_request_status": legacy.validated_request_status,
            "scored_grid_join_status": legacy.scored_grid_join_status,
            "executed_point_count": legacy.point_count,
            "ncount": legacy.ncount,
            "adjustments": [],
        },
        target_core_validation={
            "role": "target_core", "candidate_id": target.candidate_id,
            "request_fingerprint": target.request_fingerprint,
            "grid_fingerprint": target.grid_fingerprint,
            "validated_request_status": target.validated_request_status,
            "scored_grid_join_status": target.scored_grid_join_status,
            "executed_point_count": target.point_count,
            "ncount": target.ncount,
            "adjustments": [],
        },
        decision_outcome=result.decision_outcome,
        physical_role=result.physical_role,
        authority_status=result.authority_status,
        reason_codes=result.reason_codes,
        structural_conditions=result.structural_conditions,
        paired_block_deltas_per_executed_point_nats=(0.25,) * 8,
        delta_information_per_executed_point_nats=result.delta_information_per_executed_point_nats,
        paired_standard_error_nats=result.paired_standard_error_nats,
        selection_margin_multiplier=result.margin_multiplier,
        lower_margin_nats=result.lower_margin_nats,
        refresh_count=0,
        selected_request={
            "role": "target_core", "candidate_family": "target_core/1",
            "candidate_id": target.candidate_id,
            "request_fingerprint": target.request_fingerprint,
            "grid_fingerprint": target.grid_fingerprint,
            "hkl": [1.0, 0.0, 0.0],
            "energy_window": {"e_lo": 0.4, "e_hi": 3.6, "e_step": 0.2},
            "executed_energies": list(target.executed_energies),
            "executed_point_count": target.point_count,
            "ncount": target.ncount,
            "point_neutron_cost": target.ncount * target.point_count,
            "scan_command": target.scan_command,
        },
    )


def test_authority_decision_round_trip_and_content_hash_are_stable():
    decision = _decision()
    assert decision.contract == AUTHORITY_DECISION_CONTRACT
    assert ScanDesignAuthorityDecision.from_dict(decision.to_dict()) == decision
    assert len(decision.content_hash()) == 64


def test_authority_decision_rejects_tampering():
    payload = copy.deepcopy(_decision().to_dict())
    payload["selected_request"]["ncount"] += 1
    with pytest.raises(ValueError):
        ScanDesignAuthorityDecision.from_dict(payload)


def test_authority_decision_enforces_outcome_role_mapping():
    payload = copy.deepcopy(_decision().to_dict())
    payload["decision"]["physical_role"] = "legacy"
    payload["selected_request"]["role"] = "legacy"
    with pytest.raises(ValueError, match="outcome mapping"):
        ScanDesignAuthorityDecision.from_dict(payload)
