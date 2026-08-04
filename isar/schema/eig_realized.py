"""Strict immutable contracts for EIG-4 Slice 7 realized-request evidence."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from isar.schema.eig_shadow import (
    DIAGNOSTIC_AUTHORITY,
    INELIGIBLE_AUTHORITY,
    freeze,
    thaw,
    valid_fingerprint,
)
from isar.schema.statistics import semantics_fingerprint


REALIZED_REQUEST_CONTRACT = "isar.scan_design_realized_request/o36.7"
REALIZED_REFUSAL_CONTRACT = "isar.scan_design_realized_refusal/o36.7"
REALIZED_PAIR_CONTRACT = "isar.scan_design_realized_pair/o36.7"
REALIZED_VARIANT_CONTRACT = "isar.scan_design_realized_variant/o36.7"
REALIZED_SEED_CONTRACT = "isar.scan_window_realized_seed_evidence/o36.7"
REALIZED_SEED_DOMAIN = "isar.scan_window_realized_seed/o36.7"
REALIZED_COMPARISON_CONTRACT = "isar.scan_design_realized_comparison/o36.7"
REALIZED_SEED_ALGORITHM = "sha256-first-8-bytes-big-endian-signed-63-bit"

_FORBIDDEN = frozenset({
    "winner", "winning_candidate", "recommended_candidate", "recommendation",
    "threshold", "selection_threshold", "eig_selected_candidate_id",
})


def _canonical(value: Any) -> str:
    return json.dumps(thaw(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


def _fingerprint(domain: str, value: Any) -> str:
    return semantics_fingerprint(domain, thaw(value))


def _exact(value: Any, keys: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{label} fields are invalid")
    return value


def _codes(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError("reason codes must be a sequence")
    result = tuple(values)
    if (any(not isinstance(item, str) or not item or item != item.strip() for item in result)
            or len(result) != len(set(result))):
        raise ValueError("reason codes must be unique non-empty strings")
    return result


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(set(value) & _FORBIDDEN) or any(
            _contains_forbidden(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden(item) for item in value)
    return False


def _finite(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


@dataclass(frozen=True)
class RealizedScanRequest:
    source_candidate_id: str
    candidate_family: str
    source_grid_fingerprint: str
    validation_status: str
    hkl: tuple[float, float, float]
    e_lo: float
    e_hi: float
    e_step: float
    scan_command: str
    grid_adapter: Mapping[str, Any]
    executed_energies: tuple[float, ...]
    grid_fingerprint: str
    executed_point_count: int
    ncount: int
    point_neutron_cost: int
    request_fingerprint: str
    adjustments: tuple[str, ...] = ()
    contract: str = field(default=REALIZED_REQUEST_CONTRACT, init=False)
    realized_request_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        status_matches_family = (
            self.candidate_family == "legacy_uniform/1"
            and self.validation_status in {
                "finalized_selected_legacy", "submitted_legacy"}
        ) or (
            self.candidate_family == "target_core/1"
            and self.validation_status == "not_instrument_validated_counterfactual"
        )
        if (not isinstance(self.source_candidate_id, str) or not self.source_candidate_id
                or self.candidate_family not in {"legacy_uniform/1", "target_core/1"}
                or not valid_fingerprint(self.source_grid_fingerprint)
                or not status_matches_family
                or len(self.hkl) != 3 or any(not _finite(item) for item in self.hkl)
                or any(not _finite(item) for item in (self.e_lo, self.e_hi, self.e_step))
                or self.e_step <= 0.0 or not isinstance(self.scan_command, str)
                or not self.scan_command or not isinstance(self.grid_adapter, Mapping)
                or not self.executed_energies
                or any(not _finite(item) for item in self.executed_energies)
                or not valid_fingerprint(self.grid_fingerprint)
                or not isinstance(self.executed_point_count, int)
                or self.executed_point_count != len(self.executed_energies)
                or self.executed_point_count <= 0 or not isinstance(self.ncount, int)
                or self.ncount <= 0 or self.point_neutron_cost != self.ncount * self.executed_point_count
                or not valid_fingerprint(self.request_fingerprint)
                or any(not isinstance(item, str) or not item for item in self.adjustments)):
            raise ValueError("realized request is invalid")
        object.__setattr__(self, "grid_adapter", freeze(self.grid_adapter))
        object.__setattr__(self, "realized_request_fingerprint", _fingerprint(
            "isar.scan_design_realized_request_fingerprint/o36.7", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "source_candidate_id": self.source_candidate_id,
            "candidate_family": self.candidate_family,
            "source_grid_fingerprint": self.source_grid_fingerprint,
            "validation_status": self.validation_status,
            "hkl": list(self.hkl),
            "energy_window": {"e_lo": self.e_lo, "e_hi": self.e_hi, "e_step": self.e_step},
            "scan_command": self.scan_command,
            "grid_adapter": thaw(self.grid_adapter),
            "executed_energies": list(self.executed_energies),
            "grid_fingerprint": self.grid_fingerprint,
            "executed_point_count": self.executed_point_count,
            "ncount": self.ncount,
            "point_neutron_cost": self.point_neutron_cost,
            "request_fingerprint": self.request_fingerprint,
            "adjustments": list(self.adjustments),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "realized_request_fingerprint": self.realized_request_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RealizedScanRequest":
        _exact(value, {"contract", "source_candidate_id", "candidate_family",
                       "source_grid_fingerprint", "validation_status", "hkl", "energy_window",
                       "scan_command", "grid_adapter", "executed_energies", "grid_fingerprint",
                       "executed_point_count", "ncount", "point_neutron_cost",
                       "request_fingerprint", "adjustments", "realized_request_fingerprint"},
               "realized request")
        if value["contract"] != REALIZED_REQUEST_CONTRACT:
            raise ValueError("realized request contract is unsupported")
        window = _exact(value["energy_window"], {"e_lo", "e_hi", "e_step"}, "energy window")
        item = cls(
            source_candidate_id=value["source_candidate_id"],
            candidate_family=value["candidate_family"],
            source_grid_fingerprint=value["source_grid_fingerprint"],
            validation_status=value["validation_status"],
            hkl=tuple(value["hkl"]), e_lo=window["e_lo"], e_hi=window["e_hi"],
            e_step=window["e_step"], scan_command=value["scan_command"],
            grid_adapter=value["grid_adapter"], executed_energies=tuple(value["executed_energies"]),
            grid_fingerprint=value["grid_fingerprint"],
            executed_point_count=value["executed_point_count"], ncount=value["ncount"],
            point_neutron_cost=value["point_neutron_cost"],
            request_fingerprint=value["request_fingerprint"], adjustments=tuple(value["adjustments"]))
        if value["realized_request_fingerprint"] != item.realized_request_fingerprint:
            raise ValueError("realized request fingerprint is invalid")
        return item


@dataclass(frozen=True)
class RealizedCandidateRefusal:
    source_candidate_id: str | None
    candidate_family: str
    source_grid_fingerprint: str | None
    reason_codes: tuple[str, ...]
    contract: str = field(default=REALIZED_REFUSAL_CONTRACT, init=False)
    refusal_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        codes = _codes(self.reason_codes)
        if (self.candidate_family != "target_core/1" or not codes
                or (self.source_candidate_id is not None
                    and (not isinstance(self.source_candidate_id, str) or not self.source_candidate_id))
                or (self.source_grid_fingerprint is not None
                    and not valid_fingerprint(self.source_grid_fingerprint))):
            raise ValueError("realized candidate refusal is invalid")
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "refusal_fingerprint", _fingerprint(
            "isar.scan_design_realized_refusal_fingerprint/o36.7", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {"contract": self.contract, "source_candidate_id": self.source_candidate_id,
                "candidate_family": self.candidate_family,
                "source_grid_fingerprint": self.source_grid_fingerprint,
                "reason_codes": list(self.reason_codes)}

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "refusal_fingerprint": self.refusal_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RealizedCandidateRefusal":
        _exact(value, {"contract", "source_candidate_id", "candidate_family",
                       "source_grid_fingerprint", "reason_codes", "refusal_fingerprint"},
               "realized refusal")
        if value["contract"] != REALIZED_REFUSAL_CONTRACT:
            raise ValueError("realized refusal contract is unsupported")
        item = cls(value["source_candidate_id"], value["candidate_family"],
                   value["source_grid_fingerprint"], tuple(value["reason_codes"]))
        if value["refusal_fingerprint"] != item.refusal_fingerprint:
            raise ValueError("realized refusal fingerprint is invalid")
        return item


@dataclass(frozen=True)
class RealizedCandidatePair:
    planned_pair_fingerprint: str
    legacy: RealizedScanRequest
    target_core: RealizedScanRequest | RealizedCandidateRefusal
    contract: str = field(default=REALIZED_PAIR_CONTRACT, init=False)
    realized_pair_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if (not valid_fingerprint(self.planned_pair_fingerprint)
                or self.legacy.candidate_family != "legacy_uniform/1"
                or self.target_core.candidate_family != "target_core/1"):
            raise ValueError("realized pair identity is invalid")
        if (isinstance(self.target_core, RealizedScanRequest)
                and self.target_core.ncount != self.legacy.ncount):
            raise ValueError("realized_exposure_mismatch")
        object.__setattr__(self, "realized_pair_fingerprint", _fingerprint(
            "isar.scan_design_realized_pair_fingerprint/o36.7", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {"contract": self.contract,
                "planned_pair_fingerprint": self.planned_pair_fingerprint,
                "legacy": self.legacy.to_dict(), "target_core": self.target_core.to_dict()}

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "realized_pair_fingerprint": self.realized_pair_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RealizedCandidatePair":
        _exact(value, {"contract", "planned_pair_fingerprint", "legacy", "target_core",
                       "realized_pair_fingerprint"}, "realized pair")
        if value["contract"] != REALIZED_PAIR_CONTRACT:
            raise ValueError("realized pair contract is unsupported")
        target_value = value["target_core"]
        target = (RealizedScanRequest.from_dict(target_value)
                  if target_value.get("contract") == REALIZED_REQUEST_CONTRACT
                  else RealizedCandidateRefusal.from_dict(target_value))
        item = cls(value["planned_pair_fingerprint"],
                   RealizedScanRequest.from_dict(value["legacy"]), target)
        if value["realized_pair_fingerprint"] != item.realized_pair_fingerprint:
            raise ValueError("realized pair fingerprint is invalid")
        return item


@dataclass(frozen=True)
class RealizedSeedEvidence:
    scoring_config_fingerprint: str
    design_measure_fingerprint: str
    realized_pair_fingerprint: str
    seed: int
    contract: str = field(default=REALIZED_SEED_CONTRACT, init=False)
    domain: str = field(default=REALIZED_SEED_DOMAIN, init=False)
    algorithm: str = field(default=REALIZED_SEED_ALGORITHM, init=False)
    seed_evidence_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if (not all(valid_fingerprint(item) for item in (
                self.scoring_config_fingerprint, self.design_measure_fingerprint,
                self.realized_pair_fingerprint)) or not isinstance(self.seed, int)
                or isinstance(self.seed, bool) or not 0 <= self.seed < (1 << 63)):
            raise ValueError("realized seed evidence is invalid")
        object.__setattr__(self, "seed_evidence_fingerprint", _fingerprint(
            "isar.scan_window_realized_seed_evidence_fingerprint/o36.7", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {"contract": self.contract, "domain": self.domain, "algorithm": self.algorithm,
                "digest_prefix_bytes": 8, "signed_mask_bits": 63,
                "scoring_config_fingerprint": self.scoring_config_fingerprint,
                "design_measure_fingerprint": self.design_measure_fingerprint,
                "realized_pair_fingerprint": self.realized_pair_fingerprint, "seed": self.seed}

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "seed_evidence_fingerprint": self.seed_evidence_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RealizedSeedEvidence":
        _exact(value, {"contract", "domain", "algorithm", "digest_prefix_bytes",
                       "signed_mask_bits", "scoring_config_fingerprint",
                       "design_measure_fingerprint", "realized_pair_fingerprint", "seed",
                       "seed_evidence_fingerprint"}, "realized seed")
        if (value["contract"] != REALIZED_SEED_CONTRACT or value["domain"] != REALIZED_SEED_DOMAIN
                or value["algorithm"] != REALIZED_SEED_ALGORITHM
                or value["digest_prefix_bytes"] != 8 or value["signed_mask_bits"] != 63):
            raise ValueError("realized seed contract is unsupported")
        item = cls(value["scoring_config_fingerprint"], value["design_measure_fingerprint"],
                   value["realized_pair_fingerprint"], value["seed"])
        if value["seed_evidence_fingerprint"] != item.seed_evidence_fingerprint:
            raise ValueError("realized seed fingerprint is invalid")
        return item


def derive_realized_seed(*, scoring_config_fingerprint: str,
                         design_measure_fingerprint: str,
                         realized_pair_fingerprint: str) -> RealizedSeedEvidence:
    payload = {"domain": REALIZED_SEED_DOMAIN,
               "scoring_config_fingerprint": scoring_config_fingerprint,
               "design_measure_fingerprint": design_measure_fingerprint,
               "realized_pair_fingerprint": realized_pair_fingerprint}
    digest = hashlib.sha256(_canonical(payload).encode("utf-8")).digest()
    seed = int.from_bytes(digest[:8], "big", signed=False) & ((1 << 63) - 1)
    return RealizedSeedEvidence(scoring_config_fingerprint, design_measure_fingerprint,
                                realized_pair_fingerprint, seed)


@dataclass(frozen=True)
class RealizedScoreVariant:
    policy_id: str
    policy_fingerprint: str
    variant_id: str
    variant_fingerprint: str
    status: str
    score_disposition: str
    reason_codes: tuple[str, ...] = ()
    source_variant_evidence_fingerprint: str | None = None
    source_score_set_fingerprint: str | None = None
    compiled_partition: Mapping[str, Any] | None = None
    score_set: Mapping[str, Any] | None = None
    target_core_minus_legacy: Mapping[str, Any] | None = None
    authority_scopes: tuple[tuple[str, str], ...] = INELIGIBLE_AUTHORITY
    contract: str = field(default=REALIZED_VARIANT_CONTRACT, init=False)
    variant_evidence_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        codes = _codes(self.reason_codes)
        if (not self.policy_id or not self.variant_id
                or not valid_fingerprint(self.policy_fingerprint)
                or not valid_fingerprint(self.variant_fingerprint)
                or self.status not in {"complete", "unavailable"}
                or self.score_disposition not in {"reused", "rescored", "unavailable"}):
            raise ValueError("realized variant identity is invalid")
        complete = self.status == "complete"
        if complete and (codes or self.target_core_minus_legacy is None):
            raise ValueError("complete realized variant is incomplete")
        if self.score_disposition == "reused" and (
                not valid_fingerprint(self.source_variant_evidence_fingerprint)
                or not valid_fingerprint(self.source_score_set_fingerprint)
                or self.score_set is not None):
            raise ValueError("reused realized variant is invalid")
        if self.score_disposition == "rescored" and (self.score_set is None
                                                      or self.compiled_partition is None):
            raise ValueError("rescored realized variant is invalid")
        if self.score_disposition == "unavailable" and (
                not codes or self.score_set is not None or self.target_core_minus_legacy is not None):
            raise ValueError("unavailable realized variant carries numerical evidence")
        expected = DIAGNOSTIC_AUTHORITY if complete else INELIGIBLE_AUTHORITY
        if tuple(self.authority_scopes) != expected:
            raise ValueError("realized variant authority is invalid")
        for name in ("compiled_partition", "score_set", "target_core_minus_legacy"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, freeze(value))
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "variant_evidence_fingerprint", _fingerprint(
            "isar.scan_design_realized_variant_fingerprint/o36.7", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {"contract": self.contract, "policy_id": self.policy_id,
                "policy_fingerprint": self.policy_fingerprint, "variant_id": self.variant_id,
                "variant_fingerprint": self.variant_fingerprint, "status": self.status,
                "score_disposition": self.score_disposition, "reason_codes": list(self.reason_codes),
                "source_variant_evidence_fingerprint": self.source_variant_evidence_fingerprint,
                "source_score_set_fingerprint": self.source_score_set_fingerprint,
                "compiled_partition": thaw(self.compiled_partition), "score_set": thaw(self.score_set),
                "target_core_minus_legacy": thaw(self.target_core_minus_legacy),
                "authority_scopes": dict(self.authority_scopes)}

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "variant_evidence_fingerprint": self.variant_evidence_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RealizedScoreVariant":
        _exact(value, {"contract", "policy_id", "policy_fingerprint", "variant_id",
                       "variant_fingerprint", "status", "score_disposition", "reason_codes",
                       "source_variant_evidence_fingerprint", "source_score_set_fingerprint",
                       "compiled_partition", "score_set", "target_core_minus_legacy",
                       "authority_scopes", "variant_evidence_fingerprint"}, "realized variant")
        if value["contract"] != REALIZED_VARIANT_CONTRACT:
            raise ValueError("realized variant contract is unsupported")
        item = cls(
            policy_id=value["policy_id"], policy_fingerprint=value["policy_fingerprint"],
            variant_id=value["variant_id"], variant_fingerprint=value["variant_fingerprint"],
            status=value["status"], score_disposition=value["score_disposition"],
            reason_codes=tuple(value["reason_codes"]),
            source_variant_evidence_fingerprint=value["source_variant_evidence_fingerprint"],
            source_score_set_fingerprint=value["source_score_set_fingerprint"],
            compiled_partition=value["compiled_partition"], score_set=value["score_set"],
            target_core_minus_legacy=value["target_core_minus_legacy"],
            authority_scopes=tuple(sorted(dict(value["authority_scopes"]).items())))
        if value["variant_evidence_fingerprint"] != item.variant_evidence_fingerprint:
            raise ValueError("realized variant fingerprint is invalid")
        return item


@dataclass(frozen=True)
class ScanDesignRealizedComparison:
    source_inventory_ref: Mapping[str, Any]
    source_inventory_fingerprint: str
    source_shadow_scoring_fingerprint: str
    source_shadow_variant_fingerprints: tuple[str, ...]
    source_scoring_config_fingerprint: str | None
    source_policy_set_fingerprint: str | None
    source_eligibility_fingerprint: str | None
    source_design_measure_fingerprint: str | None
    source_identities: Mapping[str, Any] | None
    realized_pair: RealizedCandidatePair
    score_disposition: str
    variants: tuple[RealizedScoreVariant, ...] = ()
    seed_evidence: RealizedSeedEvidence | None = None
    reason_codes: tuple[str, ...] = ()
    authority_scopes: tuple[tuple[str, str], ...] = INELIGIBLE_AUTHORITY
    contract: str = field(default=REALIZED_COMPARISON_CONTRACT, init=False)
    realized_comparison_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        codes = _codes(self.reason_codes)
        if (not isinstance(self.source_inventory_ref, Mapping)
                or not valid_fingerprint(self.source_inventory_fingerprint)
                or not valid_fingerprint(self.source_shadow_scoring_fingerprint)
                or any(not valid_fingerprint(item)
                       for item in self.source_shadow_variant_fingerprints)
                or self.score_disposition not in {"reused", "rescored", "unavailable"}):
            raise ValueError("realized comparison source identity is invalid")
        for value in (self.source_scoring_config_fingerprint, self.source_policy_set_fingerprint,
                      self.source_eligibility_fingerprint, self.source_design_measure_fingerprint):
            if value is not None and not valid_fingerprint(value):
                raise ValueError("realized comparison source fingerprint is invalid")
        variants = tuple(self.variants)
        keys = tuple((item.policy_id, item.variant_id) for item in variants)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("realized comparison variants are not ordered and unique")
        complete = any(item.status == "complete" for item in variants)
        if self.score_disposition == "unavailable" and (not codes or complete):
            raise ValueError("unavailable realized comparison is invalid")
        if self.score_disposition in {"reused", "rescored"} and not variants:
            raise ValueError("scored realized comparison has no variants")
        if self.score_disposition == "rescored" and self.seed_evidence is None:
            raise ValueError("rescored realized comparison has no seed evidence")
        if self.score_disposition == "reused" and self.seed_evidence is not None:
            raise ValueError("reused realized comparison cannot replace source seed evidence")
        expected = DIAGNOSTIC_AUTHORITY if complete else INELIGIBLE_AUTHORITY
        if tuple(self.authority_scopes) != expected:
            raise ValueError("realized comparison authority is invalid")
        if _contains_forbidden(self.to_unfingerprinted_dict()):
            raise ValueError("realized comparison contains a decision field")
        object.__setattr__(self, "source_inventory_ref", freeze(self.source_inventory_ref))
        if self.source_identities is not None:
            object.__setattr__(self, "source_identities", freeze(self.source_identities))
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "realized_comparison_fingerprint", _fingerprint(
            "isar.scan_design_realized_comparison_fingerprint/o36.7",
            self.to_unfingerprinted_dict()))

    @property
    def realized_variant_fingerprints(self) -> tuple[str, ...]:
        return tuple(item.variant_evidence_fingerprint for item in self.variants)

    @property
    def selected_request_fingerprint(self) -> str:
        return self.realized_pair.legacy.request_fingerprint

    @property
    def selected_grid_fingerprint(self) -> str:
        return self.realized_pair.legacy.grid_fingerprint

    def to_unfingerprinted_dict(self) -> dict[str, Any]:
        return {"contract": self.contract, "source_inventory_ref": thaw(self.source_inventory_ref),
                "source_inventory_fingerprint": self.source_inventory_fingerprint,
                "source_shadow_scoring_fingerprint": self.source_shadow_scoring_fingerprint,
                "source_shadow_variant_fingerprints": list(self.source_shadow_variant_fingerprints),
                "source_scoring_config_fingerprint": self.source_scoring_config_fingerprint,
                "source_policy_set_fingerprint": self.source_policy_set_fingerprint,
                "source_eligibility_fingerprint": self.source_eligibility_fingerprint,
                "source_design_measure_fingerprint": self.source_design_measure_fingerprint,
                "source_identities": thaw(self.source_identities),
                "realized_pair": self.realized_pair.to_dict(),
                "score_disposition": self.score_disposition,
                "variants": [item.to_dict() for item in self.variants],
                "seed_evidence": None if self.seed_evidence is None else self.seed_evidence.to_dict(),
                "reason_codes": list(self.reason_codes),
                "authority_scopes": dict(self.authority_scopes)}

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_unfingerprinted_dict(),
                "realized_comparison_fingerprint": self.realized_comparison_fingerprint}

    def content_hash(self) -> str:
        return hashlib.sha256(_canonical(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignRealizedComparison":
        _exact(value, {"contract", "source_inventory_ref", "source_inventory_fingerprint",
                       "source_shadow_scoring_fingerprint", "source_shadow_variant_fingerprints",
                       "source_scoring_config_fingerprint", "source_policy_set_fingerprint",
                       "source_eligibility_fingerprint", "source_design_measure_fingerprint",
                       "source_identities", "realized_pair", "score_disposition", "variants",
                       "seed_evidence", "reason_codes", "authority_scopes",
                       "realized_comparison_fingerprint"}, "realized comparison")
        if value["contract"] != REALIZED_COMPARISON_CONTRACT:
            raise ValueError("realized comparison contract is unsupported")
        item = cls(
            source_inventory_ref=value["source_inventory_ref"],
            source_inventory_fingerprint=value["source_inventory_fingerprint"],
            source_shadow_scoring_fingerprint=value["source_shadow_scoring_fingerprint"],
            source_shadow_variant_fingerprints=tuple(value["source_shadow_variant_fingerprints"]),
            source_scoring_config_fingerprint=value["source_scoring_config_fingerprint"],
            source_policy_set_fingerprint=value["source_policy_set_fingerprint"],
            source_eligibility_fingerprint=value["source_eligibility_fingerprint"],
            source_design_measure_fingerprint=value["source_design_measure_fingerprint"],
            source_identities=value["source_identities"],
            realized_pair=RealizedCandidatePair.from_dict(value["realized_pair"]),
            score_disposition=value["score_disposition"],
            variants=tuple(RealizedScoreVariant.from_dict(item) for item in value["variants"]),
            seed_evidence=(None if value["seed_evidence"] is None
                           else RealizedSeedEvidence.from_dict(value["seed_evidence"])),
            reason_codes=tuple(value["reason_codes"]),
            authority_scopes=tuple(sorted(dict(value["authority_scopes"]).items())))
        if value["realized_comparison_fingerprint"] != item.realized_comparison_fingerprint:
            raise ValueError("realized comparison fingerprint is invalid")
        return item


def validate_scan_design_realized_comparison(value: Any) -> ScanDesignRealizedComparison:
    if not isinstance(value, ScanDesignRealizedComparison):
        raise TypeError("value must be a ScanDesignRealizedComparison")
    return ScanDesignRealizedComparison.from_dict(value.to_dict())


__all__ = [
    "REALIZED_COMPARISON_CONTRACT", "REALIZED_PAIR_CONTRACT", "REALIZED_REQUEST_CONTRACT",
    "REALIZED_SEED_DOMAIN", "RealizedCandidatePair", "RealizedCandidateRefusal",
    "RealizedScanRequest", "RealizedScoreVariant", "RealizedSeedEvidence",
    "ScanDesignRealizedComparison", "derive_realized_seed",
    "validate_scan_design_realized_comparison",
]
