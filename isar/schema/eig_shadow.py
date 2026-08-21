"""Lightweight immutable contracts for scan-window scoring evidence.

This schema module owns serialization, structural invariants, and semantic fingerprints only.  It
must remain importable without NumPy/SciPy or the campaign runtime.  Deep policy, eligibility,
candidate-pair, and Slice 4 numerical authentication belongs to :mod:`isar.eig.shadow_inventory`.

Module, class, and contract names here ("shadow") are historical: the diagnostic-only "shadow"
mode was retired in SCENE-4 PR 4.4 (D3) and every contract below now serves the sole remaining
scoring regime, ``scan_window_eig_mode == "authoritative"``. The contract strings (e.g.
``SHADOW_SCORING_CONTRACT``) and the ``ScanDesignShadowScoring`` class name are persisted-record
identifiers and are never renamed (H1 forbids a silent contract break); kept unrenamed as a module
too given ~10 import sites elsewhere.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from isar.schema.statistics import semantics_fingerprint


SHADOW_SCORING_CONTRACT = "isar.scan_design_shadow_scoring/o36.6"
SHADOW_VARIANT_CONTRACT = "isar.scan_design_shadow_variant_score/o36.6"
SHADOW_SEED_CONTRACT = "isar.scan_window_shadow_seed_evidence/o36.6"
SHADOW_SEED_DOMAIN = "isar.scan_window_shadow_seed/o36.6"
SHADOW_SEED_ALGORITHM = "sha256-first-8-bytes-big-endian-signed-63-bit"
SCORE_REFUSAL_CONTRACT = "isar.scan_design_score_refusal_evidence/o36.6"

STATUS_NOT_REQUESTED = "not_requested"
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"

DIAGNOSTIC_AUTHORITY = (
    ("acquisition_selection", "ineligible"),
    ("eig_authoritative", "ineligible"),
    ("eig_shadow", "diagnostic"),
)
INELIGIBLE_AUTHORITY = (
    ("acquisition_selection", "ineligible"),
    ("eig_authoritative", "ineligible"),
    ("eig_shadow", "ineligible"),
)
_FORBIDDEN_DECISION_FIELDS = frozenset({
    "winner", "winning_candidate", "recommended_candidate", "recommendation",
    "threshold", "selection_threshold", "eig_selected_candidate_id",
})


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw(item) for item in value]
    return value


def valid_fingerprint(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64 and value == value.lower()
            and all(character in "0123456789abcdef" for character in value))


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} fields are invalid")
    return value


def _codes(values: Sequence[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, (list, tuple)):
        raise ValueError("reason codes must be a list or tuple")
    result = tuple(values)
    if (any(not isinstance(value, str) or not value or value != value.strip()
            for value in result)
            or len(result) != len(set(result))):
        raise ValueError("reason codes must be unique non-empty strings")
    return result


def _fingerprint(domain: str, payload: Any) -> str:
    return semantics_fingerprint(domain, thaw(payload))


def _without_diagnostics(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _without_diagnostics(item) for key, item in value.items()
                if key not in {"diagnostics", "message"}}
    if isinstance(value, (list, tuple)):
        return [_without_diagnostics(item) for item in value]
    return value


def _contains_forbidden_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return (bool(set(value) & _FORBIDDEN_DECISION_FIELDS)
                or any(_contains_forbidden_field(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_field(item) for item in value)
    return False


def _nonnumeric(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value)
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and key and _nonnumeric(item)
                   for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return all(_nonnumeric(item) for item in value)
    return False


@dataclass(frozen=True)
class ShadowSeedEvidence:
    scoring_config_fingerprint: str
    design_measure_fingerprint: str
    candidate_pair_fingerprint: str
    seed: int
    seed_derivation_revision: str = SHADOW_SEED_DOMAIN
    contract: str = field(default=SHADOW_SEED_CONTRACT, init=False)
    algorithm: str = field(default=SHADOW_SEED_ALGORITHM, init=False)
    digest_prefix_bytes: int = field(default=8, init=False)
    signed_mask_bits: int = field(default=63, init=False)
    seed_evidence_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if (not all(valid_fingerprint(value) for value in (
                self.scoring_config_fingerprint, self.design_measure_fingerprint,
                self.candidate_pair_fingerprint))
                or self.seed_derivation_revision != SHADOW_SEED_DOMAIN
                or not isinstance(self.seed, int) or isinstance(self.seed, bool)
                or not 0 <= self.seed < (1 << 63)):
            raise ValueError("shadow seed evidence is invalid")
        object.__setattr__(self, "seed_evidence_fingerprint", _fingerprint(
            "isar.scan_window_shadow_seed_evidence_fingerprint/o36.6", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract, "domain": self.seed_derivation_revision,
            "algorithm": self.algorithm, "digest_prefix_bytes": self.digest_prefix_bytes,
            "signed_mask_bits": self.signed_mask_bits,
            "scoring_config_fingerprint": self.scoring_config_fingerprint,
            "design_measure_fingerprint": self.design_measure_fingerprint,
            "candidate_pair_fingerprint": self.candidate_pair_fingerprint, "seed": self.seed,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "seed_evidence_fingerprint": self.seed_evidence_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShadowSeedEvidence":
        _exact_keys(value, {
            "contract", "domain", "algorithm", "digest_prefix_bytes", "signed_mask_bits",
            "scoring_config_fingerprint", "design_measure_fingerprint",
            "candidate_pair_fingerprint", "seed", "seed_evidence_fingerprint",
        }, "shadow seed evidence")
        if (value["contract"] != SHADOW_SEED_CONTRACT
                or value["algorithm"] != SHADOW_SEED_ALGORITHM
                or value["digest_prefix_bytes"] != 8 or value["signed_mask_bits"] != 63):
            raise ValueError("shadow seed evidence contract is unsupported")
        item = cls(
            scoring_config_fingerprint=value["scoring_config_fingerprint"],
            design_measure_fingerprint=value["design_measure_fingerprint"],
            candidate_pair_fingerprint=value["candidate_pair_fingerprint"],
            seed=value["seed"], seed_derivation_revision=value["domain"],
        )
        if value["seed_evidence_fingerprint"] != item.seed_evidence_fingerprint:
            raise ValueError("seed_evidence_fingerprint does not authenticate seed evidence")
        return item


def derive_shadow_seed(*, scoring_config_fingerprint: str,
                       design_measure_fingerprint: str,
                       candidate_pair_fingerprint: str,
                       seed_derivation_revision: str = SHADOW_SEED_DOMAIN
                       ) -> ShadowSeedEvidence:
    if seed_derivation_revision != SHADOW_SEED_DOMAIN:
        raise ValueError("shadow seed derivation revision is unsupported")
    payload = {
        "domain": SHADOW_SEED_DOMAIN,
        "scoring_config_fingerprint": scoring_config_fingerprint,
        "design_measure_fingerprint": design_measure_fingerprint,
        "candidate_pair_fingerprint": candidate_pair_fingerprint,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=False)
    seed &= (1 << 63) - 1
    return ShadowSeedEvidence(
        scoring_config_fingerprint=scoring_config_fingerprint,
        design_measure_fingerprint=design_measure_fingerprint,
        candidate_pair_fingerprint=candidate_pair_fingerprint, seed=seed,
        seed_derivation_revision=seed_derivation_revision,
    )


@dataclass(frozen=True)
class ScoreRefusalEvidence:
    """Fingerprintable, nonnumeric account of one incomplete Slice 4 score set."""

    score_set_status: str
    score_set_reason_codes: tuple[str, ...]
    candidate_refusals: tuple[Mapping[str, Any], ...]
    contract: str = field(default=SCORE_REFUSAL_CONTRACT, init=False)
    score_refusal_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        codes = _codes(self.score_set_reason_codes)
        if (tuple(self.score_set_reason_codes) != codes
                or self.score_set_status not in {STATUS_PARTIAL, STATUS_UNAVAILABLE}):
            raise ValueError("score refusal status is invalid")
        candidates = tuple(freeze(item) for item in self.candidate_refusals)
        if not candidates:
            raise ValueError("score refusal evidence must be nonnumeric identity evidence")
        available = 0
        unavailable = 0
        for candidate in candidates:
            if (not isinstance(candidate, Mapping)
                    or set(candidate) != {"candidate_id", "candidate_family", "status",
                                          "reason_codes", "source_reason_code"}
                    or not all(isinstance(candidate[name], str) and candidate[name]
                               for name in ("candidate_id", "candidate_family", "status"))
                    or candidate["status"] not in {"available", "unavailable"}
                    or tuple(candidate["reason_codes"]) != _codes(candidate["reason_codes"])
                    or (candidate["source_reason_code"] is not None
                        and (not isinstance(candidate["source_reason_code"], str)
                             or not candidate["source_reason_code"]))):
                raise ValueError("score refusal evidence must be nonnumeric identity evidence")
            candidate_codes = tuple(candidate["reason_codes"])
            source_code = candidate["source_reason_code"]
            if candidate["status"] == "available":
                available += 1
                if candidate_codes or source_code is not None:
                    raise ValueError("available score refusal candidate carries a refusal")
            else:
                unavailable += 1
                if not candidate_codes:
                    raise ValueError("unavailable score refusal candidate lacks a reason")
                if (source_code is not None
                        and "predictive_evaluation_unavailable" not in candidate_codes):
                    raise ValueError("source refusal reason lacks predictive unavailability")
        if ((self.score_set_status == STATUS_PARTIAL
             and not (available and unavailable))
                or (self.score_set_status == STATUS_UNAVAILABLE
                    and (available or not unavailable))):
            raise ValueError("score refusal status does not match candidate availability")
        object.__setattr__(self, "score_set_reason_codes", codes)
        object.__setattr__(self, "candidate_refusals", candidates)
        object.__setattr__(self, "score_refusal_fingerprint", _fingerprint(
            "isar.scan_design_score_refusal_fingerprint/o36.6", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract, "score_set_status": self.score_set_status,
            "score_set_reason_codes": list(self.score_set_reason_codes),
            "candidate_refusals": thaw(self.candidate_refusals),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "score_refusal_fingerprint": self.score_refusal_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScoreRefusalEvidence":
        _exact_keys(value, {"contract", "score_set_status", "score_set_reason_codes",
                            "candidate_refusals", "score_refusal_fingerprint"},
                    "score refusal evidence")
        if value["contract"] != SCORE_REFUSAL_CONTRACT:
            raise ValueError("score refusal evidence contract is unsupported")
        if (not isinstance(value["score_set_reason_codes"], (list, tuple))
                or not isinstance(value["candidate_refusals"], (list, tuple))
                or any(not isinstance(item, Mapping)
                       or not isinstance(item.get("reason_codes"), (list, tuple))
                       for item in value["candidate_refusals"])):
            raise ValueError("score refusal reason codes must be sequences")
        item = cls(
            score_set_status=value["score_set_status"],
            score_set_reason_codes=tuple(value["score_set_reason_codes"]),
            candidate_refusals=tuple(value["candidate_refusals"]),
        )
        if value["score_refusal_fingerprint"] != item.score_refusal_fingerprint:
            raise ValueError("score refusal fingerprint is invalid")
        return item


@dataclass(frozen=True)
class ScanDesignShadowVariant:
    policy_id: str
    policy_fingerprint: str
    variant_id: str
    variant_fingerprint: str
    status: str
    reason_codes: tuple[str, ...]
    compiled_partition: Mapping[str, Any] | None = None
    score_set: Mapping[str, Any] | None = None
    target_core_minus_legacy: Mapping[str, Any] | None = None
    score_refusal_evidence: ScoreRefusalEvidence | None = None
    authority_scopes: tuple[tuple[str, str], ...] = INELIGIBLE_AUTHORITY
    diagnostics: Mapping[str, Any] = field(default_factory=dict, compare=False)
    contract: str = field(default=SHADOW_VARIANT_CONTRACT, init=False)
    variant_evidence_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.policy_id, str) or not self.policy_id
                or not isinstance(self.variant_id, str) or not self.variant_id
                or not valid_fingerprint(self.policy_fingerprint)
                or not valid_fingerprint(self.variant_fingerprint)
                or self.status not in {STATUS_COMPLETE, STATUS_UNAVAILABLE}):
            raise ValueError("shadow variant identity is invalid")
        codes = _codes(self.reason_codes)
        partition = None if self.compiled_partition is None else freeze(self.compiled_partition)
        score_set = None if self.score_set is None else freeze(self.score_set)
        delta = (None if self.target_core_minus_legacy is None
                 else freeze(self.target_core_minus_legacy))
        refusal = self.score_refusal_evidence
        if refusal is not None and not isinstance(refusal, ScoreRefusalEvidence):
            raise TypeError("score_refusal_evidence is invalid")
        expected = DIAGNOSTIC_AUTHORITY if self.status == STATUS_COMPLETE else INELIGIBLE_AUTHORITY
        if tuple(self.authority_scopes) != expected:
            raise ValueError("shadow variant authority is invalid")
        if self.status == STATUS_COMPLETE:
            if codes or partition is None or score_set is None or delta is None or refusal is not None:
                raise ValueError("complete shadow variant evidence is incomplete")
        elif not codes or score_set is not None or delta is not None:
            raise ValueError("unavailable shadow variant carries partial numerical evidence")
        diagnostics = freeze(self.diagnostics)
        if not isinstance(diagnostics, Mapping) or _contains_forbidden_field({
                "compiled_partition": partition, "score_set": score_set,
                "target_core_minus_legacy": delta, "diagnostics": diagnostics}):
            raise ValueError("shadow variant structure is invalid")
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "compiled_partition", partition)
        object.__setattr__(self, "score_set", score_set)
        object.__setattr__(self, "target_core_minus_legacy", delta)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "variant_evidence_fingerprint", _fingerprint(
            "isar.scan_design_shadow_variant_fingerprint/o36.6",
            _without_diagnostics(self._payload())))

    def _payload(self) -> dict[str, Any]:
        value = {
            "contract": self.contract, "policy_id": self.policy_id,
            "policy_fingerprint": self.policy_fingerprint, "variant_id": self.variant_id,
            "variant_fingerprint": self.variant_fingerprint, "status": self.status,
            "reason_codes": list(self.reason_codes),
            "compiled_partition": thaw(self.compiled_partition),
            "score_set": thaw(self.score_set),
            "target_core_minus_legacy": thaw(self.target_core_minus_legacy),
            "authority_scopes": dict(self.authority_scopes),
        }
        if self.score_refusal_evidence is not None:
            value["score_refusal_evidence"] = self.score_refusal_evidence.to_dict()
        return value

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "variant_evidence_fingerprint": self.variant_evidence_fingerprint,
                "diagnostics": thaw(self.diagnostics)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignShadowVariant":
        required = {
            "contract", "policy_id", "policy_fingerprint", "variant_id",
            "variant_fingerprint", "status", "reason_codes", "compiled_partition",
            "score_set", "target_core_minus_legacy", "authority_scopes",
            "variant_evidence_fingerprint", "diagnostics",
        }
        if frozenset(value) not in {
                frozenset(required), frozenset(required | {"score_refusal_evidence"})}:
            raise ValueError("shadow variant evidence fields are invalid")
        if value["contract"] != SHADOW_VARIANT_CONTRACT:
            raise ValueError("shadow variant evidence contract is unsupported")
        item = cls(
            policy_id=value["policy_id"], policy_fingerprint=value["policy_fingerprint"],
            variant_id=value["variant_id"], variant_fingerprint=value["variant_fingerprint"],
            status=value["status"], reason_codes=tuple(value["reason_codes"]),
            compiled_partition=value["compiled_partition"], score_set=value["score_set"],
            target_core_minus_legacy=value["target_core_minus_legacy"],
            score_refusal_evidence=(
                ScoreRefusalEvidence.from_dict(value["score_refusal_evidence"])
                if "score_refusal_evidence" in value else None),
            authority_scopes=tuple(sorted(dict(value["authority_scopes"]).items())),
            diagnostics=value["diagnostics"],
        )
        if value["variant_evidence_fingerprint"] != item.variant_evidence_fingerprint:
            raise ValueError("variant_evidence_fingerprint is invalid")
        return item


@dataclass(frozen=True)
class ScanDesignShadowScoring:
    status: str
    reason_codes: tuple[str, ...]
    scoring_config_fingerprint: str | None = None
    policy_set_fingerprint: str | None = None
    scoring_config: Mapping[str, Any] | None = None
    policy_set: Mapping[str, Any] | None = None
    policy_match: Mapping[str, Any] | None = None
    eligibility: Mapping[str, Any] | None = None
    design_measure: Mapping[str, Any] | None = None
    candidate_pair: Mapping[str, Any] | None = None
    seed_evidence: ShadowSeedEvidence | None = None
    variants: tuple[ScanDesignShadowVariant, ...] = ()
    authority_scopes: tuple[tuple[str, str], ...] = INELIGIBLE_AUTHORITY
    diagnostics: Mapping[str, Any] = field(default_factory=dict, compare=False)
    contract: str = field(default=SHADOW_SCORING_CONTRACT, init=False)
    shadow_scoring_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.status not in {STATUS_NOT_REQUESTED, STATUS_COMPLETE,
                               STATUS_PARTIAL, STATUS_UNAVAILABLE}:
            raise ValueError("shadow scoring status is invalid")
        codes = _codes(self.reason_codes)
        variants = tuple(self.variants)
        keys = tuple((item.policy_id, item.variant_id) for item in variants)
        if (any(not isinstance(item, ScanDesignShadowVariant) for item in variants)
                or keys != tuple(sorted(keys)) or len(keys) != len(set(keys))):
            raise ValueError("shadow scoring variants must be ordered and unique")
        complete = sum(item.status == STATUS_COMPLETE for item in variants)
        unavailable = len(variants) - complete
        valid_status = (
            self.status == STATUS_NOT_REQUESTED and not variants and bool(codes)
            or self.status == STATUS_COMPLETE and complete > 0 and not unavailable and not codes
            or self.status == STATUS_PARTIAL and complete > 0 and unavailable > 0 and not codes
            or self.status == STATUS_UNAVAILABLE and complete == 0 and (bool(codes) or bool(variants)))
        if not valid_status:
            raise ValueError("shadow scoring status is inconsistent with variant evidence")
        if self.status == STATUS_NOT_REQUESTED:
            scored_inputs = (
                self.scoring_config_fingerprint, self.policy_set_fingerprint,
                self.scoring_config, self.policy_set, self.policy_match, self.eligibility,
                self.design_measure, self.candidate_pair, self.seed_evidence,
            )
            if (codes != ("scan_window_eig_off",) or variants
                    or any(item is not None for item in scored_inputs)
                    or tuple(self.authority_scopes) != INELIGIBLE_AUTHORITY):
                raise ValueError("not-requested shadow scoring carries scored evidence")
        expected = DIAGNOSTIC_AUTHORITY if complete else INELIGIBLE_AUTHORITY
        if tuple(self.authority_scopes) != expected:
            raise ValueError("shadow scoring authority is invalid")
        for name in ("scoring_config_fingerprint", "policy_set_fingerprint"):
            value = getattr(self, name)
            if value is not None and not valid_fingerprint(value):
                raise ValueError(f"{name} is invalid")
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "variants", variants)
        for name in ("scoring_config", "policy_set", "policy_match", "eligibility",
                     "design_measure", "candidate_pair", "diagnostics"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, Mapping):
                    raise ValueError(f"shadow scoring {name} must be a mapping")
                object.__setattr__(self, name, freeze(value))
        if (_contains_forbidden_field(self.to_unfingerprinted_dict())
                or _contains_forbidden_field(self.diagnostics)):
            raise ValueError("shadow scoring contains an authoritative decision field")
        object.__setattr__(self, "shadow_scoring_fingerprint", _fingerprint(
            "isar.scan_design_shadow_scoring_fingerprint/o36.6",
            _without_diagnostics(self.to_unfingerprinted_dict())))

    def to_unfingerprinted_dict(self) -> dict[str, Any]:
        value = {
            "contract": self.contract, "status": self.status,
            "reason_codes": list(self.reason_codes),
            "scoring_config_fingerprint": self.scoring_config_fingerprint,
            "policy_set_fingerprint": self.policy_set_fingerprint,
            "scoring_config": thaw(self.scoring_config), "policy_set": thaw(self.policy_set),
            "policy_match": thaw(self.policy_match), "eligibility": thaw(self.eligibility),
            "design_measure": thaw(self.design_measure), "candidate_pair": thaw(self.candidate_pair),
            "seed_evidence": None if self.seed_evidence is None else self.seed_evidence.to_dict(),
            "variants": [item.to_dict() for item in self.variants],
            "authority_scopes": dict(self.authority_scopes),
        }
        return value

    def to_dict(self) -> dict[str, Any]:
        return {**self.to_unfingerprinted_dict(),
                "shadow_scoring_fingerprint": self.shadow_scoring_fingerprint,
                "diagnostics": thaw(self.diagnostics)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignShadowScoring":
        if _contains_forbidden_field(value):
            raise ValueError("shadow scoring contains an authoritative decision field")
        required = {
            "contract", "status", "reason_codes", "scoring_config_fingerprint",
            "policy_set_fingerprint", "scoring_config", "policy_set", "policy_match",
            "eligibility", "design_measure", "candidate_pair", "seed_evidence", "variants",
            "authority_scopes", "shadow_scoring_fingerprint", "diagnostics",
        }
        if frozenset(value) != frozenset(required):
            raise ValueError("shadow scoring fields are invalid")
        if value["contract"] != SHADOW_SCORING_CONTRACT:
            raise ValueError("shadow scoring contract is unsupported")
        item = cls(
            status=value["status"], reason_codes=tuple(value["reason_codes"]),
            scoring_config_fingerprint=value["scoring_config_fingerprint"],
            policy_set_fingerprint=value["policy_set_fingerprint"],
            scoring_config=value["scoring_config"], policy_set=value["policy_set"],
            policy_match=value["policy_match"], eligibility=value["eligibility"],
            design_measure=value["design_measure"], candidate_pair=value["candidate_pair"],
            seed_evidence=(None if value["seed_evidence"] is None
                           else ShadowSeedEvidence.from_dict(value["seed_evidence"])),
            variants=tuple(ScanDesignShadowVariant.from_dict(entry) for entry in value["variants"]),
            authority_scopes=tuple(sorted(dict(value["authority_scopes"]).items())),
            diagnostics=value["diagnostics"],
        )
        if value["shadow_scoring_fingerprint"] != item.shadow_scoring_fingerprint:
            raise ValueError("shadow_scoring_fingerprint is invalid")
        return item

    @classmethod
    def not_requested(cls) -> "ScanDesignShadowScoring":
        return cls(status=STATUS_NOT_REQUESTED, reason_codes=("scan_window_eig_off",))

    @classmethod
    def unavailable(cls, reason_code: str, **authenticated: Any) -> "ScanDesignShadowScoring":
        return cls(status=STATUS_UNAVAILABLE, reason_codes=(reason_code,), **authenticated)

    @classmethod
    def unavailable_internal(cls, reason_code: str = "shadow_scoring_failed",
                             diagnostics: Mapping[str, Any] | None = None
                             ) -> "ScanDesignShadowScoring":
        return cls(status=STATUS_UNAVAILABLE, reason_codes=(reason_code,),
                   diagnostics={} if diagnostics is None else diagnostics)


def validate_scan_design_shadow_scoring(value: Any) -> ScanDesignShadowScoring:
    if not isinstance(value, ScanDesignShadowScoring):
        raise TypeError("value must be a ScanDesignShadowScoring")
    return ScanDesignShadowScoring.from_dict(value.to_dict())


__all__ = [
    "DIAGNOSTIC_AUTHORITY", "INELIGIBLE_AUTHORITY", "SCORE_REFUSAL_CONTRACT",
    "SHADOW_SCORING_CONTRACT", "SHADOW_SEED_DOMAIN", "SHADOW_VARIANT_CONTRACT",
    "STATUS_COMPLETE", "STATUS_NOT_REQUESTED", "STATUS_PARTIAL", "STATUS_UNAVAILABLE",
    "ScanDesignShadowScoring", "ScanDesignShadowVariant", "ScoreRefusalEvidence",
    "ShadowSeedEvidence", "derive_shadow_seed", "freeze", "thaw", "valid_fingerprint",
    "validate_scan_design_shadow_scoring",
]
