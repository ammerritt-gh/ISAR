from __future__ import annotations

import math

import pytest

from isar.eig.o41_authority import (
    AUTHORITATIVE_PROFILE_INVALID,
    AUTHORITY_GRANTED,
    AUTHORITY_NOT_GRANTED,
    AUTHORITY_UNAVAILABLE,
    O41CandidateEvidence,
    O41ScoreEvidence,
    O41SelectorInput,
    OUTCOME_LEGACY,
    OUTCOME_NON_EIG,
    OUTCOME_TARGET,
    OUTCOME_UNAVAILABLE,
    ROLE_LEGACY,
    ROLE_NONE,
    ROLE_TARGET,
    select_o41_authority,
    validate_o41_authoritative_profile,
)
from isar.schema.eig_policy import (
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    ScanWindowEIGConfig,
    ScanWindowShadowScoringConfig,
)


def _config(*, policy_id: str = "eig4_slice8_excitation_energy",
            variant_id: str = "primary_0p5_mev", simulation_count: int = 128,
            block_count: int = 8, extra_policy: bool = False) -> ScanWindowEIGConfig:
    annotation = {
        "campaign_name": "o41-test",
        "experiment_description": "restricted authority fixture",
        "date": "2026-08-04",
    }
    provenance = {
        "kind": "explicit_user_analysis/1",
        "source_id": "owner-o41",
        "rationale": "exercise the accepted restricted profile",
    }
    variant = QoIDistinctionVariant(
        variant_id, {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0, 0.25]})
    primary = QoIDistinctionPolicy(
        policy_id, "isar.scalar_absolute_partitions/o36.5",
        {"id": "excitation.energy", "unit": "meV"},
        {"kind": "campaign_configuration", "fingerprint": "a" * 64,
         "annotation": annotation}, provenance, (variant,))
    policies = [primary]
    if extra_policy:
        policies.append(QoIDistinctionPolicy(
            "unrelated", "isar.scalar_absolute_partitions/o36.5",
            {"id": "excitation.energy", "unit": "meV"},
            {"kind": "campaign_configuration", "fingerprint": "a" * 64,
             "annotation": annotation}, provenance,
            (QoIDistinctionVariant(
                "other", {"origin": 0.0, "bin_width": 1.0, "offsets": [0.0]}),)))
    return ScanWindowEIGConfig(
        policy_set=QoIDistinctionPolicySet(tuple(sorted(policies, key=lambda item: item.policy_id))),
        scoring_config=ScanWindowShadowScoringConfig(
            simulation_count=simulation_count, block_count=block_count),
    )


def _candidate(role: str, *, points: int, lo: float, hi: float, ncount: int = 100,
               validation: str = "validated", join: str = "matched") -> O41CandidateEvidence:
    step = (hi - lo) / (points - 1)
    energies = tuple(lo + index * step for index in range(points))
    return O41CandidateEvidence(
        role=role,
        candidate_id=f"{role}-candidate",
        request_fingerprint=("1" if role == ROLE_LEGACY else "2") * 64,
        grid_fingerprint=("3" if role == ROLE_LEGACY else "4") * 64,
        e_lo=lo,
        e_hi=hi,
        executed_energies=energies,
        ncount=ncount,
        scan_command=f"scan {lo} {hi} {step}",
        validated_request_status=validation,
        scored_grid_join_status=join,
    )


def _input(*, blocks=(0.3,) * 8, eligibility="eligible", applicability="applicable",
           legacy=None, target=None, score_status="complete", orientation="target_core_minus_legacy",
           evidence_join="matched") -> O41SelectorInput:
    return O41SelectorInput(
        profile=validate_o41_authoritative_profile(_config()),
        eligibility_status=eligibility,
        applicability_status=applicability,
        legacy=legacy or _candidate(ROLE_LEGACY, points=46, lo=-2.0, hi=7.0),
        target_core=target or _candidate(ROLE_TARGET, points=17, lo=0.4, hi=3.6),
        score=O41ScoreEvidence(
            status=score_status,
            orientation=orientation,
            paired_block_deltas_per_executed_point_nats=(
                tuple(blocks) if score_status == "complete" else ()),
            evidence_join_status=evidence_join,
        ),
    )


def test_exact_profile_is_accepted_and_selector_owns_margin():
    profile = validate_o41_authoritative_profile(_config())
    assert profile.selector_contract == "isar.scan_window_authoritative_selector/o41.1"
    assert profile.margin_multiplier == 2.0
    assert profile.to_dict()["simulation_count"] == 128
    assert profile.to_dict()["block_count"] == 8


@pytest.mark.parametrize("config", [
    _config(policy_id="wrong"),
    _config(variant_id="wrong"),
    _config(simulation_count=64),
    _config(simulation_count=128, block_count=4),
    _config(extra_policy=True),
])
def test_non_o41_profiles_are_refused(config):
    with pytest.raises(ValueError, match=AUTHORITATIVE_PROFILE_INVALID):
        validate_o41_authoritative_profile(config)


def test_decisive_density_win_selects_target_even_when_total_information_can_be_lower():
    result = select_o41_authority(_input(blocks=(0.02570423,) * 8))
    assert (result.decision_outcome, result.physical_role, result.authority_status) == (
        OUTCOME_TARGET, ROLE_TARGET, AUTHORITY_GRANTED)
    assert result.lower_margin_nats == pytest.approx(0.02570423)


def test_paired_sample_standard_error_reproduces_o41_gate():
    blocks = (0.2, 0.4, 0.6, 0.8, 0.1, 0.3, 0.5, 0.7)
    result = select_o41_authority(_input(blocks=blocks))
    mean = math.fsum(blocks) / len(blocks)
    variance = math.fsum((value - mean) ** 2 for value in blocks) / (len(blocks) - 1)
    expected_se = math.sqrt(variance) / math.sqrt(len(blocks))
    assert result.delta_information_per_executed_point_nats == pytest.approx(mean)
    assert result.paired_standard_error_nats == pytest.approx(expected_se)
    assert result.lower_margin_nats == pytest.approx(mean - 2.0 * expected_se)


@pytest.mark.parametrize("blocks", [
    (0.01, -0.01) * 4,
    (-0.2,) * 8,
    (0.0,) * 8,
])
def test_unresolved_negative_and_zero_advantage_retain_legacy(blocks):
    result = select_o41_authority(_input(blocks=blocks))
    assert (result.decision_outcome, result.physical_role, result.authority_status) == (
        OUTCOME_LEGACY, ROLE_LEGACY, AUTHORITY_GRANTED)
    assert "o41_margin_not_decisive" in result.reason_codes


@pytest.mark.parametrize(("target", "reason"), [
    (_candidate(ROLE_TARGET, points=17, lo=-2.2, hi=3.6), "wire_request_contained"),
    (_candidate(ROLE_TARGET, points=46, lo=0.4, hi=3.6), "executed_points_strictly_fewer"),
    (_candidate(ROLE_TARGET, points=17, lo=0.4, hi=3.6, ncount=101),
     "per_point_ncount_equal"),
])
def test_structural_failure_retains_legacy(target, reason):
    result = select_o41_authority(_input(target=target))
    assert result.decision_outcome == OUTCOME_LEGACY
    assert reason in result.reason_codes


@pytest.mark.parametrize("kwargs", [
    {"target": _candidate(ROLE_TARGET, points=17, lo=0.4, hi=3.6, validation="refused")},
    {"target": _candidate(ROLE_TARGET, points=17, lo=0.4, hi=3.6, join="mismatch")},
    {"score_status": "unavailable"},
    {"orientation": "legacy_minus_target_core"},
    {"evidence_join": "mismatch"},
    {"eligibility": "unassessable"},
])
def test_incomplete_or_malformed_selection_evidence_fails_closed(kwargs):
    result = select_o41_authority(_input(**kwargs))
    assert (result.decision_outcome, result.physical_role, result.authority_status) == (
        OUTCOME_UNAVAILABLE, ROLE_LEGACY, AUTHORITY_UNAVAILABLE)


def test_o36_ineligible_and_recovery_invalidation_use_non_eig_path():
    ineligible = select_o41_authority(_input(eligibility="ineligible"))
    invalidated = select_o41_authority(_input(applicability="invalidated"))
    for result in (ineligible, invalidated):
        assert (result.decision_outcome, result.physical_role, result.authority_status) == (
            OUTCOME_NON_EIG, ROLE_NONE, AUTHORITY_NOT_GRANTED)
