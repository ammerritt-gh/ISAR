"""Pure restricted-O41 authority profile and scan-window selector.

The module deliberately performs no I/O and imports no acquisition runtime.  Callers authenticate
the persisted parents, adapt them into :class:`O41SelectorInput`, and receive one frozen decision
whose predicates and uncertainty calculation reproduce the accepted Slice-8 gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Mapping

from isar.schema.eig_policy import ScanWindowEIGConfig


SELECTOR_CONTRACT = "isar.scan_window_authoritative_selector/o41.1"
AUTHORITATIVE_PROFILE_INVALID = "scan_window_eig_authoritative_profile_invalid"
PRIMARY_POLICY_ID = "eig4_slice8_excitation_energy"
PRIMARY_VARIANT_ID = "primary_0p5_mev"
PRIMARY_POLICY_KIND = "isar.scalar_absolute_partitions/o36.5"
PRIMARY_QOI = {"id": "excitation.energy", "unit": "meV"}
PRIMARY_PARAMETERS = {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0, 0.25]}
SIMULATION_COUNT = 128
BLOCK_COUNT = 8
MARGIN_MULTIPLIER = 2.0

OUTCOME_TARGET = "select_target_core"
OUTCOME_LEGACY = "retain_legacy_control"
OUTCOME_NON_EIG = "use_non_eig_path"
OUTCOME_UNAVAILABLE = "selection_evidence_unavailable"

ROLE_TARGET = "target_core"
ROLE_LEGACY = "legacy"
ROLE_NONE = "none"

AUTHORITY_GRANTED = "granted"
AUTHORITY_NOT_GRANTED = "not_granted"
AUTHORITY_UNAVAILABLE = "unavailable"

_VALIDATION_STATUSES = {"validated", "refused", "unavailable"}
_JOIN_STATUSES = {"matched", "mismatch", "unavailable"}
_ELIGIBILITY_STATUSES = {"eligible", "ineligible", "unassessable"}
_APPLICABILITY_STATUSES = {"applicable", "invalidated", "unavailable"}


def _valid_fingerprint(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


@dataclass(frozen=True)
class O41AuthoritativeProfile:
    """Authenticated identity of the one policy/configuration O41 permits."""

    policy_fingerprint: str
    variant_fingerprint: str
    policy_set_fingerprint: str
    scoring_config_fingerprint: str
    selector_contract: str = field(default=SELECTOR_CONTRACT, init=False)
    margin_multiplier: float = field(default=MARGIN_MULTIPLIER, init=False)

    def __post_init__(self) -> None:
        if not all(_valid_fingerprint(value) for value in (
                self.policy_fingerprint, self.variant_fingerprint,
                self.policy_set_fingerprint, self.scoring_config_fingerprint)):
            raise ValueError("O41 authoritative profile fingerprints are invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector_contract": self.selector_contract,
            "policy_id": PRIMARY_POLICY_ID,
            "policy_kind": PRIMARY_POLICY_KIND,
            "policy_fingerprint": self.policy_fingerprint,
            "variant_id": PRIMARY_VARIANT_ID,
            "variant_fingerprint": self.variant_fingerprint,
            "policy_set_fingerprint": self.policy_set_fingerprint,
            "source_qoi": dict(PRIMARY_QOI),
            "parameters": {**PRIMARY_PARAMETERS, "offsets": list(PRIMARY_PARAMETERS["offsets"])},
            "simulation_count": SIMULATION_COUNT,
            "block_count": BLOCK_COUNT,
            "scoring_config_fingerprint": self.scoring_config_fingerprint,
            "margin_multiplier": self.margin_multiplier,
        }


def validate_o41_authoritative_profile(config: ScanWindowEIGConfig) -> O41AuthoritativeProfile:
    """Return the exact restricted profile or refuse with the stable setup reason."""
    try:
        if not isinstance(config, ScanWindowEIGConfig):
            raise TypeError("scan-window EIG configuration is not frozen")
        frozen = ScanWindowEIGConfig.from_dict(config.to_dict())
        scoring = frozen.scoring_config
        if scoring.simulation_count != SIMULATION_COUNT or scoring.block_count != BLOCK_COUNT:
            raise ValueError("scoring configuration is not the O41 128/8 profile")
        policy_set = frozen.policy_set
        if policy_set is None or len(policy_set.policies) != 1:
            raise ValueError("exactly one O41 policy is required")
        policy = policy_set.policies[0]
        if (
            policy.policy_id != PRIMARY_POLICY_ID
            or policy.policy_kind != PRIMARY_POLICY_KIND
            or dict(policy.source_qoi) != PRIMARY_QOI
            or len(policy.variants) != 1
        ):
            raise ValueError("policy identity is not the restricted O41 profile")
        variant = policy.variants[0]
        parameters = dict(variant.parameters)
        if isinstance(parameters.get("offsets"), tuple):
            parameters["offsets"] = list(parameters["offsets"])
        if variant.variant_id != PRIMARY_VARIANT_ID or parameters != PRIMARY_PARAMETERS:
            raise ValueError("variant identity is not the restricted O41 profile")
        return O41AuthoritativeProfile(
            policy_fingerprint=policy.policy_fingerprint,
            variant_fingerprint=variant.variant_fingerprint,
            policy_set_fingerprint=policy_set.policy_set_fingerprint,
            scoring_config_fingerprint=scoring.scoring_config_fingerprint,
        )
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError(f"{AUTHORITATIVE_PROFILE_INVALID}: {error}") from error


@dataclass(frozen=True)
class O41CandidateEvidence:
    """Adapter-canonical candidate request presented to the selector."""

    role: str
    candidate_id: str
    request_fingerprint: str
    grid_fingerprint: str
    e_lo: float
    e_hi: float
    executed_energies: tuple[float, ...]
    ncount: int
    scan_command: str
    validated_request_status: str = "validated"
    scored_grid_join_status: str = "matched"

    def __post_init__(self) -> None:
        energies = tuple(float(value) for value in self.executed_energies)
        if (
            self.role not in {ROLE_LEGACY, ROLE_TARGET}
            or not isinstance(self.candidate_id, str) or not self.candidate_id
            or not _valid_fingerprint(self.request_fingerprint)
            or not _valid_fingerprint(self.grid_fingerprint)
            or not _finite(self.e_lo) or not _finite(self.e_hi)
            or not energies or any(not _finite(value) for value in energies)
            or not isinstance(self.ncount, int) or isinstance(self.ncount, bool) or self.ncount <= 0
            or not isinstance(self.scan_command, str) or not self.scan_command
            or self.validated_request_status not in _VALIDATION_STATUSES
            or self.scored_grid_join_status not in _JOIN_STATUSES
        ):
            raise ValueError("O41 candidate evidence is invalid")
        object.__setattr__(self, "executed_energies", energies)

    @property
    def point_count(self) -> int:
        return len(self.executed_energies)


@dataclass(frozen=True)
class O41ScoreEvidence:
    """Authenticated primary comparison reduced to the selector's numerical inputs."""

    status: str
    orientation: str
    paired_block_deltas_per_executed_point_nats: tuple[float, ...]
    evidence_join_status: str = "matched"

    def __post_init__(self) -> None:
        blocks = tuple(float(value) for value in self.paired_block_deltas_per_executed_point_nats)
        if (
            self.status not in {"complete", "unavailable"}
            or self.orientation not in {"target_core_minus_legacy", "legacy_minus_target_core"}
            or self.evidence_join_status not in _JOIN_STATUSES
            or any(not _finite(value) for value in blocks)
            or (self.status == "complete" and len(blocks) != BLOCK_COUNT)
            or (self.status == "unavailable" and blocks)
        ):
            raise ValueError("O41 score evidence is invalid")
        object.__setattr__(self, "paired_block_deltas_per_executed_point_nats", blocks)


@dataclass(frozen=True)
class O41SelectorInput:
    profile: O41AuthoritativeProfile
    eligibility_status: str
    applicability_status: str
    legacy: O41CandidateEvidence
    target_core: O41CandidateEvidence
    score: O41ScoreEvidence

    def __post_init__(self) -> None:
        if (
            not isinstance(self.profile, O41AuthoritativeProfile)
            or self.eligibility_status not in _ELIGIBILITY_STATUSES
            or self.applicability_status not in _APPLICABILITY_STATUSES
            or self.legacy.role != ROLE_LEGACY
            or self.target_core.role != ROLE_TARGET
            or self.legacy.candidate_id == self.target_core.candidate_id
        ):
            raise ValueError("O41 selector input is invalid")


@dataclass(frozen=True)
class O41SelectorResult:
    decision_outcome: str
    physical_role: str
    authority_status: str
    reason_codes: tuple[str, ...]
    structural_conditions: tuple[tuple[str, bool], ...]
    paired_block_deltas_per_executed_point_nats: tuple[float, ...]
    delta_information_per_executed_point_nats: float | None
    paired_standard_error_nats: float | None
    lower_margin_nats: float | None
    selector_contract: str = field(default=SELECTOR_CONTRACT, init=False)
    margin_multiplier: float = field(default=MARGIN_MULTIPLIER, init=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "selector_contract": self.selector_contract,
            "decision_outcome": self.decision_outcome,
            "physical_role": self.physical_role,
            "authority_status": self.authority_status,
            "reason_codes": list(self.reason_codes),
            "structural_conditions": dict(self.structural_conditions),
            "paired_block_deltas_per_executed_point_nats": list(
                self.paired_block_deltas_per_executed_point_nats),
            "delta_information_per_executed_point_nats": (
                self.delta_information_per_executed_point_nats),
            "paired_standard_error_nats": self.paired_standard_error_nats,
            "selection_margin_multiplier": self.margin_multiplier,
            "lower_margin_nats": self.lower_margin_nats,
        }


def _result(outcome: str, role: str, authority: str, *reasons: str,
            structural: Mapping[str, bool] | None = None,
            blocks: tuple[float, ...] = (), delta: float | None = None,
            standard_error: float | None = None,
            lower: float | None = None) -> O41SelectorResult:
    return O41SelectorResult(
        decision_outcome=outcome,
        physical_role=role,
        authority_status=authority,
        reason_codes=tuple(dict.fromkeys(reasons)),
        structural_conditions=tuple(sorted((structural or {}).items())),
        paired_block_deltas_per_executed_point_nats=blocks,
        delta_information_per_executed_point_nats=delta,
        paired_standard_error_nats=standard_error,
        lower_margin_nats=lower,
    )


def _paired_summary(blocks: tuple[float, ...]) -> tuple[float, float, float]:
    count = len(blocks)
    delta = math.fsum(blocks) / count
    variance = math.fsum((value - delta) ** 2 for value in blocks) / (count - 1)
    standard_error = math.sqrt(variance) / math.sqrt(count)
    return delta, standard_error, delta - MARGIN_MULTIPLIER * standard_error


def select_o41_authority(value: O41SelectorInput) -> O41SelectorResult:
    """Apply the complete O41 selector once without I/O, mutation, or fallback loops."""
    if not isinstance(value, O41SelectorInput):
        raise TypeError("value must be an O41SelectorInput")
    if value.applicability_status == "invalidated":
        return _result(OUTCOME_NON_EIG, ROLE_NONE, AUTHORITY_NOT_GRANTED,
                       "eig_realization_invalidated_by_recovery")
    if value.eligibility_status == "ineligible":
        return _result(OUTCOME_NON_EIG, ROLE_NONE, AUTHORITY_NOT_GRANTED, "o36_ineligible")
    if value.eligibility_status != "eligible" or value.applicability_status != "applicable":
        return _result(OUTCOME_UNAVAILABLE, ROLE_LEGACY, AUTHORITY_UNAVAILABLE,
                       "o41_applicability_unavailable")

    candidates = (value.legacy, value.target_core)
    if any(item.validated_request_status != "validated" for item in candidates):
        return _result(OUTCOME_UNAVAILABLE, ROLE_LEGACY, AUTHORITY_UNAVAILABLE,
                       "candidate_request_validation_unavailable")
    if any(item.scored_grid_join_status != "matched" for item in candidates):
        return _result(OUTCOME_UNAVAILABLE, ROLE_LEGACY, AUTHORITY_UNAVAILABLE,
                       "scored_grid_join_unavailable")
    if (value.score.status != "complete"
            or value.score.evidence_join_status != "matched"
            or value.score.orientation != "target_core_minus_legacy"):
        return _result(OUTCOME_UNAVAILABLE, ROLE_LEGACY, AUTHORITY_UNAVAILABLE,
                       "primary_score_evidence_unavailable")

    legacy = value.legacy
    target = value.target_core
    legacy_bounds = sorted((float(legacy.e_lo), float(legacy.e_hi)))
    target_bounds = sorted((float(target.e_lo), float(target.e_hi)))
    structural = {
        "wire_request_contained": (
            target_bounds[0] >= legacy_bounds[0] and target_bounds[1] <= legacy_bounds[1]),
        "executed_support_contained": (
            min(target.executed_energies) >= min(legacy.executed_energies)
            and max(target.executed_energies) <= max(legacy.executed_energies)),
        "executed_span_strictly_smaller": (
            max(target.executed_energies) - min(target.executed_energies)
            < max(legacy.executed_energies) - min(legacy.executed_energies)),
        "executed_points_strictly_fewer": target.point_count < legacy.point_count,
        "per_point_ncount_equal": target.ncount == legacy.ncount,
    }
    blocks = value.score.paired_block_deltas_per_executed_point_nats
    delta, standard_error, lower = _paired_summary(blocks)
    failed = tuple(name for name, passed in structural.items() if not passed)
    if failed:
        return _result(OUTCOME_LEGACY, ROLE_LEGACY, AUTHORITY_GRANTED,
                       *failed, structural=structural, blocks=blocks, delta=delta,
                       standard_error=standard_error, lower=lower)
    if lower <= 0.0:
        return _result(OUTCOME_LEGACY, ROLE_LEGACY, AUTHORITY_GRANTED,
                       "o41_margin_not_decisive", structural=structural, blocks=blocks,
                       delta=delta, standard_error=standard_error, lower=lower)
    return _result(OUTCOME_TARGET, ROLE_TARGET, AUTHORITY_GRANTED,
                   "o41_margin_decisive", structural=structural, blocks=blocks,
                   delta=delta, standard_error=standard_error, lower=lower)


__all__ = [
    "AUTHORITATIVE_PROFILE_INVALID", "AUTHORITY_GRANTED", "AUTHORITY_NOT_GRANTED",
    "AUTHORITY_UNAVAILABLE", "BLOCK_COUNT", "MARGIN_MULTIPLIER", "O41AuthoritativeProfile",
    "O41CandidateEvidence", "O41ScoreEvidence", "O41SelectorInput", "O41SelectorResult",
    "OUTCOME_LEGACY", "OUTCOME_NON_EIG", "OUTCOME_TARGET", "OUTCOME_UNAVAILABLE",
    "PRIMARY_PARAMETERS", "PRIMARY_POLICY_ID", "PRIMARY_POLICY_KIND", "PRIMARY_QOI",
    "PRIMARY_VARIANT_ID", "ROLE_LEGACY", "ROLE_NONE", "ROLE_TARGET", "SELECTOR_CONTRACT",
    "SIMULATION_COUNT", "select_o41_authority", "validate_o41_authoritative_profile",
]
