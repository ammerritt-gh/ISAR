"""Strict durable schema for restricted O41 scan-design authority decisions."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, Mapping

AUTHORITY_DECISION_CONTRACT = "isar.scan_design_authority_decision/o41.1"
_OUTCOMES = {
    "select_target_core", "retain_legacy_control", "use_non_eig_path",
    "selection_evidence_unavailable",
}
_ROLES = {"target_core", "legacy", "none"}
_AUTHORITY = {"granted", "not_granted", "unavailable"}
_JOIN_STATUSES = {"matched", "mismatch", "unavailable"}
_VALIDATION_STATUSES = {"validated", "refused", "unavailable"}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _fingerprint(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(_thaw(value)).encode("utf-8")).hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _valid_fp(value: Any) -> bool:
    return (
        isinstance(value, str) and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _finite_or_none(value: Any) -> bool:
    return value is None or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
        and math.isfinite(float(value)))


def _ref(value: Any, name: str) -> Mapping[str, Any]:
    if (not isinstance(value, Mapping)
            or set(value) not in ({"path", "sha256"}, {"kind", "path", "sha256"})):
        raise ValueError(f"{name} reference fields are invalid")
    path = value["path"]
    if (not isinstance(path, str) or not path or path.startswith(("/", "\\"))
            or ".." in path.replace("\\", "/").split("/") or not _valid_fp(value["sha256"])):
        raise ValueError(f"{name} reference is invalid")
    normalized = {"path": path.replace("\\", "/"), "sha256": value["sha256"]}
    if "kind" in value:
        if not isinstance(value["kind"], str) or not value["kind"]:
            raise ValueError(f"{name} reference kind is invalid")
        normalized["kind"] = value["kind"]
    return _freeze(normalized)


def _candidate(value: Any, role: str) -> Mapping[str, Any]:
    expected = {
        "role", "candidate_id", "request_fingerprint", "grid_fingerprint",
        "validated_request_status", "scored_grid_join_status", "executed_point_count",
        "ncount", "adjustments",
    }
    if not isinstance(value, Mapping) or set(value) != expected or value["role"] != role:
        raise ValueError(f"{role} validation fields are invalid")
    adjustments = value["adjustments"]
    if (
        not isinstance(value["candidate_id"], str) or not value["candidate_id"]
        or not _valid_fp(value["request_fingerprint"])
        or not _valid_fp(value["grid_fingerprint"])
        or value["validated_request_status"] not in _VALIDATION_STATUSES
        or value["scored_grid_join_status"] not in _JOIN_STATUSES
        or not isinstance(value["executed_point_count"], int)
        or isinstance(value["executed_point_count"], bool)
        or value["executed_point_count"] <= 0
        or not isinstance(value["ncount"], int) or isinstance(value["ncount"], bool)
        or value["ncount"] <= 0
        or not isinstance(adjustments, (list, tuple))
        or any(not isinstance(item, str) or not item for item in adjustments)
    ):
        raise ValueError(f"{role} validation is invalid")
    return _freeze({**dict(value), "adjustments": list(adjustments)})


def _selected_request(value: Any, role: str) -> Mapping[str, Any] | None:
    if role == "none":
        if value is not None:
            raise ValueError("non-EIG authority decision cannot carry a selected request")
        return None
    expected = {
        "role", "candidate_family", "candidate_id", "request_fingerprint", "grid_fingerprint", "hkl",
        "energy_window", "executed_energies", "executed_point_count", "ncount",
        "point_neutron_cost", "scan_command",
    }
    if not isinstance(value, Mapping) or set(value) != expected or value["role"] != role:
        raise ValueError("selected authority request fields are invalid")
    hkl = value["hkl"]
    window = value["energy_window"]
    energies = value["executed_energies"]
    count = value["executed_point_count"]
    ncount = value["ncount"]
    finite = lambda item: (
        isinstance(item, (int, float)) and not isinstance(item, bool)
        and math.isfinite(float(item)))
    if (
        not isinstance(value["candidate_id"], str) or not value["candidate_id"]
        or value["candidate_family"] != (
            "legacy_uniform/1" if role == "legacy" else "target_core/1")
        or not _valid_fp(value["request_fingerprint"])
        or not _valid_fp(value["grid_fingerprint"])
        or not isinstance(hkl, (list, tuple)) or len(hkl) != 3 or any(not finite(item) for item in hkl)
        or not isinstance(window, Mapping) or set(window) != {"e_lo", "e_hi", "e_step"}
        or any(not finite(window[key]) for key in window) or float(window["e_step"]) <= 0.0
        or not isinstance(energies, (list, tuple)) or not energies
        or any(not finite(item) for item in energies)
        or not isinstance(count, int) or isinstance(count, bool) or count != len(energies)
        or not isinstance(ncount, int) or isinstance(ncount, bool) or ncount <= 0
        or value["point_neutron_cost"] != count * ncount
        or not isinstance(value["scan_command"], str) or not value["scan_command"]
    ):
        raise ValueError("selected authority request is invalid")
    return _freeze({**dict(value), "hkl": list(hkl), "energy_window": dict(window),
                    "executed_energies": list(energies)})


@dataclass(frozen=True)
class ScanDesignAuthorityDecision:
    configured_mode: str
    scientific_action_fingerprint: str
    eligibility_result_fingerprint: str | None
    primary_evidence_fingerprint: str | None
    authority_scopes: tuple[tuple[str, str], ...]
    source_inventory_ref: Mapping[str, Any]
    source_inventory_fingerprint: str
    realized_comparison_ref: Mapping[str, Any]
    realized_comparison_fingerprint: str
    profile: Mapping[str, Any]
    legacy_validation: Mapping[str, Any]
    target_core_validation: Mapping[str, Any]
    decision_outcome: str
    physical_role: str
    authority_status: str
    reason_codes: tuple[str, ...]
    structural_conditions: tuple[tuple[str, bool], ...]
    paired_block_deltas_per_executed_point_nats: tuple[float, ...]
    delta_information_per_executed_point_nats: float | None
    paired_standard_error_nats: float | None
    selection_margin_multiplier: float
    lower_margin_nats: float | None
    refresh_count: int
    selected_request: Mapping[str, Any] | None
    contract: str = field(default=AUTHORITY_DECISION_CONTRACT, init=False)
    authority_decision_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        inventory_ref = _ref(self.source_inventory_ref, "source inventory")
        comparison_ref = _ref(self.realized_comparison_ref, "realized comparison")
        if (
            self.configured_mode != "authoritative"
            or not _valid_fp(self.scientific_action_fingerprint)
            or (self.eligibility_result_fingerprint is not None
                and not _valid_fp(self.eligibility_result_fingerprint))
            or (self.primary_evidence_fingerprint is not None
                and not _valid_fp(self.primary_evidence_fingerprint))
            or not _valid_fp(self.source_inventory_fingerprint)
            or not _valid_fp(self.realized_comparison_fingerprint)
            or inventory_ref["sha256"] != self.source_inventory_ref["sha256"]
            or comparison_ref["sha256"] != self.realized_comparison_ref["sha256"]
            or self.decision_outcome not in _OUTCOMES
            or self.physical_role not in _ROLES
            or self.authority_status not in _AUTHORITY
            or not isinstance(self.refresh_count, int) or isinstance(self.refresh_count, bool)
            or self.refresh_count not in {0, 1}
            or not _finite_or_none(self.delta_information_per_executed_point_nats)
            or not _finite_or_none(self.paired_standard_error_nats)
            or not _finite_or_none(self.lower_margin_nats)
            or float(self.selection_margin_multiplier) != 2.0
        ):
            raise ValueError("authority decision identity is invalid")
        expected_mapping = {
            "select_target_core": ("target_core", "granted"),
            "retain_legacy_control": ("legacy", "granted"),
            "use_non_eig_path": ("none", "not_granted"),
            "selection_evidence_unavailable": ("legacy", "unavailable"),
        }
        if (self.physical_role, self.authority_status) != expected_mapping[self.decision_outcome]:
            raise ValueError("authority decision outcome mapping is invalid")
        scopes = tuple(sorted(self.authority_scopes))
        expected_scopes = (("physical_scan_window_selection", "restricted_o41"),) \
            if self.authority_status == "granted" else ()
        if scopes != expected_scopes:
            raise ValueError("authority decision scopes are invalid")
        codes = tuple(dict.fromkeys(self.reason_codes))
        if any(not isinstance(code, str) or not code for code in codes):
            raise ValueError("authority decision reason codes are invalid")
        structural = tuple(sorted(self.structural_conditions))
        if any(not isinstance(name, str) or not name or not isinstance(passed, bool)
               for name, passed in structural):
            raise ValueError("authority decision structural conditions are invalid")
        blocks = tuple(float(value) for value in self.paired_block_deltas_per_executed_point_nats)
        if any(not math.isfinite(value) for value in blocks):
            raise ValueError("authority decision paired blocks are invalid")
        if self.authority_status == "granted" and len(blocks) != 8:
            raise ValueError("granted authority decision requires eight paired blocks")
        profile = _freeze(dict(self.profile))
        if profile.get("selector_contract") != "isar.scan_window_authoritative_selector/o41.1":
            raise ValueError("authority decision selector profile is unsupported")
        legacy = _candidate(self.legacy_validation, "legacy")
        target = _candidate(self.target_core_validation, "target_core")
        selected = _selected_request(self.selected_request, self.physical_role)
        object.__setattr__(self, "source_inventory_ref", inventory_ref)
        object.__setattr__(self, "realized_comparison_ref", comparison_ref)
        object.__setattr__(self, "profile", profile)
        object.__setattr__(self, "legacy_validation", legacy)
        object.__setattr__(self, "target_core_validation", target)
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "authority_scopes", scopes)
        object.__setattr__(self, "structural_conditions", structural)
        object.__setattr__(self, "paired_block_deltas_per_executed_point_nats", blocks)
        object.__setattr__(self, "selected_request", selected)
        object.__setattr__(self, "authority_decision_fingerprint", _fingerprint(
            self.to_unfingerprinted_dict()))

    def to_unfingerprinted_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "configured_mode": self.configured_mode,
            "scientific_action_fingerprint": self.scientific_action_fingerprint,
            "eligibility_result_fingerprint": self.eligibility_result_fingerprint,
            "primary_evidence_fingerprint": self.primary_evidence_fingerprint,
            "authority_scopes": [list(item) for item in self.authority_scopes],
            "source_inventory_ref": _thaw(self.source_inventory_ref),
            "source_inventory_fingerprint": self.source_inventory_fingerprint,
            "realized_comparison_ref": _thaw(self.realized_comparison_ref),
            "realized_comparison_fingerprint": self.realized_comparison_fingerprint,
            "profile": _thaw(self.profile),
            "candidate_validation": {
                "legacy": _thaw(self.legacy_validation),
                "target_core": _thaw(self.target_core_validation),
            },
            "decision": {
                "outcome": self.decision_outcome,
                "physical_role": self.physical_role,
                "authority_status": self.authority_status,
                "reason_codes": list(self.reason_codes),
                "structural_conditions": dict(self.structural_conditions),
                "paired_block_deltas_per_executed_point_nats": list(
                    self.paired_block_deltas_per_executed_point_nats),
                "delta_information_per_executed_point_nats": (
                    self.delta_information_per_executed_point_nats),
                "paired_standard_error_nats": self.paired_standard_error_nats,
                "selection_margin_multiplier": self.selection_margin_multiplier,
                "lower_margin_nats": self.lower_margin_nats,
                "refresh_count": self.refresh_count,
            },
            "selected_request": _thaw(self.selected_request),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_unfingerprinted_dict(),
                "authority_decision_fingerprint": self.authority_decision_fingerprint}

    def content_hash(self) -> str:
        return hashlib.sha256(_canonical_json(self.to_dict()).encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignAuthorityDecision":
        expected = {
            "contract", "configured_mode", "scientific_action_fingerprint",
            "eligibility_result_fingerprint", "primary_evidence_fingerprint",
            "authority_scopes", "source_inventory_ref", "source_inventory_fingerprint",
            "realized_comparison_ref", "realized_comparison_fingerprint", "profile",
            "candidate_validation", "decision", "selected_request",
            "authority_decision_fingerprint",
        }
        if not isinstance(value, Mapping) or set(value) != expected:
            raise ValueError("authority decision fields are invalid")
        if value["contract"] != AUTHORITY_DECISION_CONTRACT:
            raise ValueError("authority decision contract is unsupported")
        validation = value["candidate_validation"]
        decision = value["decision"]
        if not isinstance(validation, Mapping) or set(validation) != {"legacy", "target_core"}:
            raise ValueError("authority candidate validation fields are invalid")
        decision_fields = {
            "outcome", "physical_role", "authority_status", "reason_codes",
            "structural_conditions", "paired_block_deltas_per_executed_point_nats",
            "delta_information_per_executed_point_nats", "paired_standard_error_nats",
            "selection_margin_multiplier", "lower_margin_nats", "refresh_count",
        }
        if not isinstance(decision, Mapping) or set(decision) != decision_fields:
            raise ValueError("authority selector result fields are invalid")
        item = cls(
            configured_mode=value["configured_mode"],
            scientific_action_fingerprint=value["scientific_action_fingerprint"],
            eligibility_result_fingerprint=value["eligibility_result_fingerprint"],
            primary_evidence_fingerprint=value["primary_evidence_fingerprint"],
            authority_scopes=tuple(tuple(part) for part in value["authority_scopes"]),
            source_inventory_ref=value["source_inventory_ref"],
            source_inventory_fingerprint=value["source_inventory_fingerprint"],
            realized_comparison_ref=value["realized_comparison_ref"],
            realized_comparison_fingerprint=value["realized_comparison_fingerprint"],
            profile=value["profile"],
            legacy_validation=validation["legacy"],
            target_core_validation=validation["target_core"],
            decision_outcome=decision["outcome"], physical_role=decision["physical_role"],
            authority_status=decision["authority_status"],
            reason_codes=tuple(decision["reason_codes"]),
            structural_conditions=tuple(sorted(dict(decision["structural_conditions"]).items())),
            paired_block_deltas_per_executed_point_nats=tuple(
                decision["paired_block_deltas_per_executed_point_nats"]),
            delta_information_per_executed_point_nats=(
                decision["delta_information_per_executed_point_nats"]),
            paired_standard_error_nats=decision["paired_standard_error_nats"],
            selection_margin_multiplier=decision["selection_margin_multiplier"],
            lower_margin_nats=decision["lower_margin_nats"],
            refresh_count=decision["refresh_count"], selected_request=value["selected_request"],
        )
        if value["authority_decision_fingerprint"] != item.authority_decision_fingerprint:
            raise ValueError("authority decision fingerprint is invalid")
        return item


def validate_scan_design_authority_decision(value: Any) -> ScanDesignAuthorityDecision:
    if not isinstance(value, ScanDesignAuthorityDecision):
        raise TypeError("value must be a ScanDesignAuthorityDecision")
    return ScanDesignAuthorityDecision.from_dict(value.to_dict())


__all__ = [
    "AUTHORITY_DECISION_CONTRACT", "ScanDesignAuthorityDecision",
    "validate_scan_design_authority_decision",
]
