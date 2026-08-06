"""Frozen QoI distinction policies and live shadow-scoring configuration.

The distinction-policy records are the shared, provider-neutral form of the
``o36.5`` contracts first introduced by the offline comparison campaign.  This
module deliberately depends only on schema-level fingerprinting so live EIG
code can consume the evidence without importing offline orchestration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping

from isar.schema.statistics import semantics_fingerprint


QOI_DISTINCTION_POLICY_CONTRACT = "isar.qoi_distinction_policy/o36.5"
QOI_DISTINCTION_POLICY_SET_CONTRACT = "isar.qoi_distinction_policy_set/o36.5"
SHADOW_SCORING_CONFIG_CONTRACT = "isar.scan_window_shadow_scoring_config/o36.6"
SHADOW_SCORING_REVISION = "isar.scan_window_shadow_scoring/o36.6"
SHADOW_SEED_DERIVATION_REVISION = "isar.scan_window_shadow_seed/o36.6"
SCAN_WINDOW_EIG_CONFIG_CONTRACT = "isar.scan_window_eig_objective_config/o36.6"

_POLICY_FINGERPRINT_DOMAIN = "isar.offline_eig/o36.5"
_SHADOW_FINGERPRINT_DOMAIN = "isar.scan_window_eig/o36.6"
MAX_QOI_POLICIES = 32
MAX_RUNTIME_VARIANTS = 8


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


def _policy_fp(kind: str, value: Any) -> str:
    return semantics_fingerprint(f"{_POLICY_FINGERPRINT_DOMAIN}/{kind}", _thaw(value))


def _shadow_fp(kind: str, value: Any) -> str:
    return semantics_fingerprint(f"{_SHADOW_FINGERPRINT_DOMAIN}/{kind}", _thaw(value))


def _valid_fp(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty stripped string")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} fields are invalid")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return _freeze(value)


@dataclass(frozen=True)
class QoIDistinctionVariant:
    variant_id: str
    parameters: Mapping[str, Any]
    variant_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        _string(self.variant_id, "variant_id")
        object.__setattr__(self, "parameters", _mapping(self.parameters, "variant parameters"))
        object.__setattr__(self, "variant_fingerprint", _policy_fp("variant", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {"variant_id": self.variant_id, "parameters": _thaw(self.parameters)}

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "variant_fingerprint": self.variant_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QoIDistinctionVariant":
        _exact_keys(value, {"variant_id", "parameters", "variant_fingerprint"}, "policy variant")
        item = cls(value["variant_id"], value["parameters"])
        if value["variant_fingerprint"] != item.variant_fingerprint:
            raise ValueError("variant_fingerprint does not authenticate policy variant")
        return item


def _validate_provenance(value: Mapping[str, Any]) -> Mapping[str, Any]:
    allowed = {"kind", "source_id", "source_fingerprint", "rationale", "author", "authority_role"}
    if not isinstance(value, Mapping) or set(value) - allowed:
        raise ValueError("policy provenance fields are invalid")
    kind = _string(value.get("kind"), "provenance kind")
    if kind not in {
        "scientific_protocol/1", "experiment_objective/1",
        "explicit_user_analysis/1", "exploratory_sensitivity/1",
    }:
        raise ValueError("policy provenance kind is unsupported")
    rationale = _string(value.get("rationale"), "provenance rationale")
    result = {"kind": kind, "rationale": rationale}
    for name in ("source_id", "source_fingerprint", "author", "authority_role"):
        if value.get(name) is not None:
            result[name] = _string(value[name], name)
    if kind in {"scientific_protocol/1", "experiment_objective/1"}:
        if not _valid_fp(result.get("source_fingerprint")):
            raise ValueError("authenticated provenance source fingerprint is required")
    elif "source_id" not in result:
        raise ValueError("stable provenance source ID is required")
    return _freeze(result)


@dataclass(frozen=True)
class QoIDistinctionPolicy:
    policy_id: str
    policy_kind: str
    source_qoi: Mapping[str, str]
    scope: Mapping[str, Any]
    provenance: Mapping[str, Any]
    variants: tuple[QoIDistinctionVariant, ...]
    contract: str = field(default=QOI_DISTINCTION_POLICY_CONTRACT, init=False)
    policy_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        _string(self.policy_id, "policy_id")
        kind = _string(self.policy_kind, "policy_kind")
        if "/" not in kind:
            raise ValueError("policy kind must be versioned")
        _exact_keys(self.source_qoi, {"id", "unit"}, "source QoI")
        source_qoi = {
            "id": _string(self.source_qoi["id"], "QoI id"),
            "unit": _string(self.source_qoi["unit"], "QoI unit"),
        }
        _exact_keys(self.scope, {"kind", "fingerprint", "annotation"}, "policy scope")
        scope_kind = self.scope["kind"]
        if scope_kind not in {"case_identity", "campaign_configuration"} or not _valid_fp(self.scope["fingerprint"]):
            raise ValueError("policy scope is invalid")
        annotation = self.scope["annotation"]
        _exact_keys(annotation, {"campaign_name", "experiment_description", "date"}, "scope annotation")
        normalized_annotation = {key: _string(annotation[key], key) for key in sorted(annotation)}
        variants = tuple(self.variants)
        ids = tuple(variant.variant_id for variant in variants)
        if (
            not variants
            or any(not isinstance(variant, QoIDistinctionVariant) for variant in variants)
            or ids != tuple(sorted(ids))
            or len(ids) != len(set(ids))
        ):
            raise ValueError("policy variants must be non-empty, ordered, and unique")
        object.__setattr__(self, "source_qoi", _freeze(source_qoi))
        object.__setattr__(self, "scope", _freeze({
            "kind": scope_kind,
            "fingerprint": self.scope["fingerprint"],
            "annotation": normalized_annotation,
        }))
        object.__setattr__(self, "provenance", _validate_provenance(self.provenance))
        object.__setattr__(self, "variants", variants)
        object.__setattr__(self, "policy_fingerprint", _policy_fp("policy", self._semantic_payload()))

    def _semantic_payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "policy_id": self.policy_id,
            "policy_kind": self.policy_kind,
            "source_qoi": _thaw(self.source_qoi),
            "scope": {"kind": self.scope["kind"], "fingerprint": self.scope["fingerprint"]},
            "provenance": _thaw(self.provenance),
            "variants": [variant.to_dict() for variant in self.variants],
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._semantic_payload(),
            "scope": _thaw(self.scope),
            "policy_fingerprint": self.policy_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QoIDistinctionPolicy":
        expected = {
            "contract", "policy_id", "policy_kind", "source_qoi", "scope",
            "provenance", "variants", "policy_fingerprint",
        }
        _exact_keys(value, expected, "QoI distinction policy")
        if value["contract"] != QOI_DISTINCTION_POLICY_CONTRACT:
            raise ValueError("QoI distinction policy contract is unsupported")
        item = cls(
            value["policy_id"], value["policy_kind"], value["source_qoi"], value["scope"],
            value["provenance"], tuple(QoIDistinctionVariant.from_dict(item) for item in value["variants"]),
        )
        if value["policy_fingerprint"] != item.policy_fingerprint:
            raise ValueError("policy_fingerprint does not authenticate policy")
        return item


def validate_qoi_distinction_policy(value: Any) -> QoIDistinctionPolicy:
    if not isinstance(value, QoIDistinctionPolicy):
        raise TypeError("value must be a QoIDistinctionPolicy")
    return QoIDistinctionPolicy.from_dict(value.to_dict())


@dataclass(frozen=True)
class QoIDistinctionPolicySet:
    policies: tuple[QoIDistinctionPolicy, ...]
    contract: str = field(default=QOI_DISTINCTION_POLICY_SET_CONTRACT, init=False)
    policy_set_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        policies = tuple(self.policies)
        ids = tuple(policy.policy_id for policy in policies)
        if (
            any(not isinstance(policy, QoIDistinctionPolicy) for policy in policies)
            or ids != tuple(sorted(ids))
            or len(ids) != len(set(ids))
        ):
            raise ValueError("policies must be ordered and unique")
        object.__setattr__(self, "policies", policies)
        payload = {"contract": self.contract, "policies": [policy.to_dict() for policy in policies]}
        object.__setattr__(self, "policy_set_fingerprint", _policy_fp("policy_set", payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "policies": [policy.to_dict() for policy in self.policies],
            "policy_set_fingerprint": self.policy_set_fingerprint,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QoIDistinctionPolicySet":
        _exact_keys(value, {"contract", "policies", "policy_set_fingerprint"}, "policy set")
        if value["contract"] != QOI_DISTINCTION_POLICY_SET_CONTRACT:
            raise ValueError("policy set contract is unsupported")
        item = cls(tuple(QoIDistinctionPolicy.from_dict(policy) for policy in value["policies"]))
        if value["policy_set_fingerprint"] != item.policy_set_fingerprint:
            raise ValueError("policy_set_fingerprint does not authenticate policy set")
        return item


def validate_qoi_distinction_policy_set(value: Any) -> QoIDistinctionPolicySet:
    if not isinstance(value, QoIDistinctionPolicySet):
        raise TypeError("value must be a QoIDistinctionPolicySet")
    return QoIDistinctionPolicySet.from_dict(value.to_dict())


def matching_qoi_policies(
    policy_set: QoIDistinctionPolicySet,
    *,
    qoi_id: str | None,
    qoi_unit: str | None,
    scope_kind: str,
    scope_fingerprint: str | None,
) -> tuple[QoIDistinctionPolicy, ...]:
    """Return every exact QoI/scope match without precedence or ordering guesses."""
    validate_qoi_distinction_policy_set(policy_set)
    if qoi_id is None or qoi_unit is None or scope_fingerprint is None:
        return ()
    if scope_kind not in {"case_identity", "campaign_configuration"}:
        raise ValueError("policy scope kind is invalid")
    return tuple(
        policy for policy in policy_set.policies
        if dict(policy.source_qoi) == {"id": qoi_id, "unit": qoi_unit}
        and policy.scope["kind"] == scope_kind
        and policy.scope["fingerprint"] == scope_fingerprint
    )


@dataclass(frozen=True)
class ScanWindowShadowScoringConfig:
    simulation_count: int = 128
    block_count: int = 8
    max_runtime_variants: int = 8
    revision: str = SHADOW_SCORING_REVISION
    seed_derivation_revision: str = SHADOW_SEED_DERIVATION_REVISION
    contract: str = field(default=SHADOW_SCORING_CONFIG_CONTRACT, init=False)
    scoring_config_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        values = (self.simulation_count, self.block_count, self.max_runtime_variants)
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise ValueError("shadow scoring counts must be integers")
        if (
            not 2 <= self.simulation_count <= 128
            or self.block_count < 2
            or self.block_count > self.simulation_count
            or self.simulation_count % self.block_count
            or not 1 <= self.max_runtime_variants <= MAX_RUNTIME_VARIANTS
            or self.revision != SHADOW_SCORING_REVISION
            or self.seed_derivation_revision != SHADOW_SEED_DERIVATION_REVISION
        ):
            raise ValueError("shadow scoring configuration is invalid")
        object.__setattr__(
            self,
            "scoring_config_fingerprint",
            _shadow_fp("scoring_config", self._payload()),
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "revision": self.revision,
            "simulation_count": self.simulation_count,
            "block_count": self.block_count,
            "max_runtime_variants": self.max_runtime_variants,
            "seed_derivation_revision": self.seed_derivation_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "scoring_config_fingerprint": self.scoring_config_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanWindowShadowScoringConfig":
        expected = {
            "contract", "revision", "simulation_count", "block_count",
            "max_runtime_variants", "seed_derivation_revision", "scoring_config_fingerprint",
        }
        _exact_keys(value, expected, "shadow scoring configuration")
        if value["contract"] != SHADOW_SCORING_CONFIG_CONTRACT:
            raise ValueError("shadow scoring configuration contract is unsupported")
        item = cls(**{key: value[key] for key in expected - {"contract", "scoring_config_fingerprint"}})
        if value["scoring_config_fingerprint"] != item.scoring_config_fingerprint:
            raise ValueError("scoring_config_fingerprint does not authenticate configuration")
        return item


@dataclass(frozen=True)
class ScanWindowEIGConfig:
    """Self-contained EIG policy and numerical configuration frozen with an objective."""

    policy_set: QoIDistinctionPolicySet | None = None
    scoring_config: ScanWindowShadowScoringConfig = field(
        default_factory=ScanWindowShadowScoringConfig)
    contract: str = field(default=SCAN_WINDOW_EIG_CONFIG_CONTRACT, init=False)
    scan_window_eig_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.scoring_config, ScanWindowShadowScoringConfig):
            raise TypeError("scoring_config must be a ScanWindowShadowScoringConfig")
        scoring = ScanWindowShadowScoringConfig.from_dict(self.scoring_config.to_dict())
        object.__setattr__(self, "scoring_config", scoring)
        if self.policy_set is not None:
            policy_set = validate_qoi_distinction_policy_set(self.policy_set)
            if len(policy_set.policies) > MAX_QOI_POLICIES:
                raise ValueError(f"at most {MAX_QOI_POLICIES} QoI distinction policies are allowed")
            limit = self.scoring_config.max_runtime_variants
            if any(len(policy.variants) > limit for policy in policy_set.policies):
                raise ValueError(f"policy variants exceed max_runtime_variants={limit}")
            object.__setattr__(self, "policy_set", policy_set)
        object.__setattr__(
            self,
            "scan_window_eig_fingerprint",
            _shadow_fp("objective_config", self._payload()),
        )

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "policy_set": None if self.policy_set is None else self.policy_set.to_dict(),
            "scoring_config": self.scoring_config.to_dict(),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "scan_window_eig_fingerprint": self.scan_window_eig_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanWindowEIGConfig":
        expected = {"contract", "policy_set", "scoring_config", "scan_window_eig_fingerprint"}
        _exact_keys(value, expected, "scan-window EIG objective configuration")
        if value["contract"] != SCAN_WINDOW_EIG_CONFIG_CONTRACT:
            raise ValueError("scan-window EIG objective configuration contract is unsupported")
        item = cls(
            policy_set=(None if value["policy_set"] is None
                        else QoIDistinctionPolicySet.from_dict(value["policy_set"])),
            scoring_config=ScanWindowShadowScoringConfig.from_dict(value["scoring_config"]),
        )
        if value["scan_window_eig_fingerprint"] != item.scan_window_eig_fingerprint:
            raise ValueError("scan_window_eig_fingerprint does not authenticate configuration")
        return item


def validate_scan_window_shadow_scoring_config(value: Any) -> ScanWindowShadowScoringConfig:
    if not isinstance(value, ScanWindowShadowScoringConfig):
        raise TypeError("value must be a ScanWindowShadowScoringConfig")
    return ScanWindowShadowScoringConfig.from_dict(value.to_dict())


def validate_scan_window_eig_config(value: Any) -> ScanWindowEIGConfig:
    if not isinstance(value, ScanWindowEIGConfig):
        raise TypeError("value must be a ScanWindowEIGConfig")
    return ScanWindowEIGConfig.from_dict(value.to_dict())


__all__ = [
    "MAX_QOI_POLICIES", "MAX_RUNTIME_VARIANTS",
    "QOI_DISTINCTION_POLICY_CONTRACT", "QOI_DISTINCTION_POLICY_SET_CONTRACT",
    "SHADOW_SCORING_CONFIG_CONTRACT", "SHADOW_SCORING_REVISION",
    "SHADOW_SEED_DERIVATION_REVISION", "SCAN_WINDOW_EIG_CONFIG_CONTRACT",
    "QoIDistinctionVariant", "QoIDistinctionPolicy", "QoIDistinctionPolicySet",
    "validate_qoi_distinction_policy", "validate_qoi_distinction_policy_set",
    "matching_qoi_policies", "ScanWindowShadowScoringConfig", "ScanWindowEIGConfig",
    "validate_scan_window_shadow_scoring_config", "validate_scan_window_eig_config",
]
