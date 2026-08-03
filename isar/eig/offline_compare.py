"""Pure, fail-closed offline comparison contracts for EIG Slice 5.

The module deliberately knows nothing about campaign directories or report files.  A
runner may discover and persist its envelopes, but the scientific comparison remains
an immutable, content-addressed transformation of supplied values.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import math
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from isar.drive.objective import OBJECTIVE_SCHEMA, ObjectiveSpec
from isar.drive.acquisition.scan_design import (
    DEFAULT_WINDOW_GENERATOR_CONFIG, PHYSICAL_WINDOW_GENERATOR_ID, PHYSICAL_WINDOW_GENERATOR_VERSION,
    SCAN_DESIGN_INVENTORY_SCHEMA, ScanDesignContext,
)
from isar.schema.statistics import semantics_fingerprint

from .design_measure import ScanWindowDesignMeasure, validate_scan_window_design_measure
from .scorer import (
    CRN_REVISION, LIKELIHOOD_REVISION, SCORER_REVISION, CandidateEvaluationInput,
    ScalarQoIDiscretization, ScanWindowScoreSet, score_scan_window_candidates,
    validate_scan_window_score_set,
)


OFFLINE_DISCOVERY_CONFIG_CONTRACT = "isar.offline_eig_discovery_config/o36.5"
OFFLINE_CASE_SNAPSHOT_CONTRACT = "isar.offline_eig_case_snapshot/o36.5"
OFFLINE_CORPUS_INDEX_CONTRACT = "isar.eig_offline_corpus_index/o36.5"
QOI_DISTINCTION_POLICY_CONTRACT = "isar.qoi_distinction_policy/o36.5"
QOI_DISTINCTION_POLICY_SET_CONTRACT = "isar.qoi_distinction_policy_set/o36.5"
OFFLINE_SCORING_CONFIG_CONTRACT = "isar.eig_offline_scoring_config/o36.5"
OFFLINE_COMPARISON_REPORT_CONTRACT = "isar.eig_offline_comparison_report/o36.5"
CANDIDATE_PAIR_REVISION = "isar.offline_eig_candidate_pair/o36.5"
POLICY_COMPILER_REVISION = "isar.offline_eig_policy_compiler/o36.5"
_FINGERPRINT_DOMAIN = "isar.offline_eig/o36.5"
_READY = "ready"
_CASE_STATUSES = {_READY, "scientifically_ineligible", "unassessable", "nonterminal_or_unclassified"}
_AUTHORITY = (("acquisition_selection", "ineligible"), ("eig_authoritative", "ineligible"),
              ("eig_shadow", "diagnostic"))
_INELIGIBLE_AUTHORITY = (("acquisition_selection", "ineligible"), ("eig_authoritative", "ineligible"),
                         ("eig_shadow", "ineligible"))


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


def _fp(kind: str, value: Any) -> str:
    return semantics_fingerprint(f"{_FINGERPRINT_DOMAIN}/{kind}", _thaw(value))


def _valid_fp(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(ch in "0123456789abcdef" for ch in value)


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty stripped string")
    return value


def _codes(value: Sequence[str]) -> tuple[str, ...]:
    result = tuple(_string(code, "reason code") for code in value)
    if len(result) != len(set(result)):
        raise ValueError("reason codes must be unique")
    return result


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return _freeze(value)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _exact_keys(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise ValueError(f"{name} has unsupported fields: {sorted(extra)!r}")


def campaign_configuration_fingerprint(objective: ObjectiveSpec) -> str:
    """Fingerprint only the current campaign configuration relevant to a comparison."""
    if not isinstance(objective, ObjectiveSpec):
        raise TypeError("objective must be an ObjectiveSpec")
    raw = objective.to_dict()
    if raw.get("schema") != OBJECTIVE_SCHEMA:
        raise ValueError("objective schema is unsupported")
    payload = {
        "schema": raw["schema"], "path": raw["path"], "tolerance_meV": raw["tolerance_meV"],
        "target_branch": raw["target_branch"], "target_label": raw["target_label"],
        "target_selector": raw["target_selector"], "target_energy_meV": raw["target_energy_meV"],
        "energy_search_meV": raw["energy_search_meV"],
        "identity_search_meV": raw["identity_search_meV"],
        "target_temperature_K": raw["target_temperature_K"], "hypotheses": raw["hypotheses"],
        "target_hypothesis": raw["target_hypothesis"],
        "hypothesis_fingerprint": raw["hypothesis_fingerprint"],
    }
    return _fp("campaign_configuration", payload)


@dataclass(frozen=True)
class OfflineDiscoveryConfig:
    """Versioned identity of the offline corpus classifier, not a campaign."""
    discovery_ruleset_revision: str = "isar.offline_eig_discovery_ruleset/o36.5"
    accepted_terminal_states: tuple[str, ...] = ("incomplete", "infeasible", "met", "operator_stopped")
    eligibility_contract: str = "isar.eig_eligibility/o36.2.1"
    qoi_evidence_contract: str = "isar.eig_qoi_projection_evidence/o36.2.1"
    design_measure_contract: str = "isar.scan_window_design_measure/o36.3"
    predictive_contract: str = "isar.master_predictive/2+physical_only.1"
    projection_contract: str = "isar.target_projection/2"
    inventory_contract: str = SCAN_DESIGN_INVENTORY_SCHEMA
    generator_id: str = PHYSICAL_WINDOW_GENERATOR_ID
    generator_version: int = PHYSICAL_WINDOW_GENERATOR_VERSION
    generator_config_fingerprint: str = DEFAULT_WINDOW_GENERATOR_CONFIG.fingerprint
    counterfactual_transform_revision: str = "isar.offline_shadow_context_transform/o36.5"
    contract: str = field(default=OFFLINE_DISCOVERY_CONFIG_CONTRACT, init=False)
    discovery_config_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if (self.discovery_ruleset_revision != "isar.offline_eig_discovery_ruleset/o36.5"
                or self.accepted_terminal_states != tuple(sorted(set(self.accepted_terminal_states)))
                or not self.accepted_terminal_states
                or any(not isinstance(state, str) or not state for state in self.accepted_terminal_states)
                or self.eligibility_contract != "isar.eig_eligibility/o36.2.1"
                or self.qoi_evidence_contract != "isar.eig_qoi_projection_evidence/o36.2.1"
                or self.design_measure_contract != "isar.scan_window_design_measure/o36.3"
                or self.predictive_contract != "isar.master_predictive/2+physical_only.1"
                or self.projection_contract != "isar.target_projection/2"
                or self.inventory_contract != SCAN_DESIGN_INVENTORY_SCHEMA
                or self.generator_id != PHYSICAL_WINDOW_GENERATOR_ID
                or self.generator_version != PHYSICAL_WINDOW_GENERATOR_VERSION
                or self.generator_config_fingerprint != DEFAULT_WINDOW_GENERATOR_CONFIG.fingerprint
                or self.counterfactual_transform_revision != "isar.offline_shadow_context_transform/o36.5"):
            raise ValueError("offline discovery config is unsupported")
        object.__setattr__(self, "discovery_config_fingerprint", _fp("discovery_config", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {"contract": self.contract, "discovery_ruleset_revision": self.discovery_ruleset_revision,
                "accepted_terminal_states": list(self.accepted_terminal_states),
                "eligibility_contract": self.eligibility_contract, "qoi_evidence_contract": self.qoi_evidence_contract,
                "design_measure_contract": self.design_measure_contract, "predictive_contract": self.predictive_contract,
                "projection_contract": self.projection_contract, "inventory_contract": self.inventory_contract,
                "generator_id": self.generator_id, "generator_version": self.generator_version,
                "generator_config_fingerprint": self.generator_config_fingerprint,
                "counterfactual_transform_revision": self.counterfactual_transform_revision}

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "discovery_config_fingerprint": self.discovery_config_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineDiscoveryConfig":
        allowed = {"contract", "discovery_ruleset_revision", "accepted_terminal_states", "eligibility_contract", "qoi_evidence_contract", "design_measure_contract", "predictive_contract", "projection_contract", "inventory_contract", "generator_id", "generator_version", "generator_config_fingerprint", "counterfactual_transform_revision", "discovery_config_fingerprint"}
        _exact_keys(value, allowed, "discovery config")
        if value.get("contract") != OFFLINE_DISCOVERY_CONFIG_CONTRACT:
            raise ValueError("discovery config contract is unsupported")
        values = {key: value[key] for key in allowed - {"contract", "discovery_config_fingerprint"}}
        values["accepted_terminal_states"] = tuple(values["accepted_terminal_states"])
        item = cls(**values)
        if value.get("discovery_config_fingerprint") != item.discovery_config_fingerprint:
            raise ValueError("discovery_config_fingerprint does not authenticate discovery config")
        return item


def offline_shadow_context_transform(context: ScanDesignContext) -> Mapping[str, Any]:
    """Apply the one permitted offline context edit and authenticate both contexts."""
    if not isinstance(context, ScanDesignContext):
        raise TypeError("context must be a ScanDesignContext")
    shadow = replace(context, scan_window_eig_mode="shadow")
    before, after = context.to_dict(), shadow.to_dict()
    changed = sorted(key for key in before if before[key] != after[key])
    if changed not in ([], ["scan_window_eig_mode"]):
        raise ValueError("offline shadow context changed fields beyond scan_window_eig_mode")
    return _freeze({"revision": "isar.offline_shadow_context_transform/o36.5",
                    "persisted_context": before, "offline_context": after,
                    "persisted_context_fingerprint": _fp("scan_design_context", before),
                    "offline_context_fingerprint": _fp("scan_design_context", after),
                    "changed_fields": changed})


def _candidate_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    _exact_keys(candidate, {"candidate_id", "energy_meV", "exposure_per_point", "family", "annotation"}, "candidate")
    candidate_id = _string(candidate.get("candidate_id"), "candidate_id")
    family = _string(candidate.get("family", candidate_id), "candidate family")
    grid = candidate.get("energy_meV")
    if not isinstance(grid, Sequence) or isinstance(grid, (str, bytes)) or not grid or any(not _finite(x) for x in grid):
        raise ValueError("candidate energy_meV is invalid")
    canonical_grid = tuple(0.0 if float(x) == 0.0 else float(x) for x in grid)
    if len({value.hex() for value in canonical_grid}) != len(canonical_grid):
        raise ValueError("candidate energy_meV must be unique")
    exposure = candidate.get("exposure_per_point")
    if not _finite(exposure) or float(exposure) <= 0.0:
        raise ValueError("candidate exposure_per_point is invalid")
    return {"candidate_id": candidate_id, "family": family,
            "energy_meV": list(canonical_grid), "exposure_per_point": float(exposure)}


def validate_offline_candidate_pair(
        persisted_legacy: Mapping[str, Any], shadow_legacy: Mapping[str, Any],
        target_core: Mapping[str, Any], *,
        regenerated_persisted_legacy: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Authenticate regenerated candidates and the equal-exposure offline pair."""
    try:
        persisted = _candidate_identity(persisted_legacy)
    except (TypeError, ValueError) as error:
        raise ValueError("persisted_legacy_candidate_mismatch") from error
    try:
        legacy = _candidate_identity(shadow_legacy)
    except (TypeError, ValueError) as error:
        raise ValueError("shadow_legacy_candidate_mismatch") from error
    try:
        target = _candidate_identity(target_core)
    except (TypeError, ValueError) as error:
        raise ValueError("candidate_family_missing") from error
    if regenerated_persisted_legacy is not None:
        try:
            regenerated = _candidate_identity(regenerated_persisted_legacy)
        except (TypeError, ValueError) as error:
            raise ValueError("persisted_legacy_candidate_mismatch") from error
        if persisted != regenerated:
            raise ValueError("persisted_legacy_candidate_mismatch")
    if persisted != legacy:
        raise ValueError("shadow_legacy_candidate_mismatch")
    if legacy["family"] != "legacy_uniform/1" or target["family"] != "target_core/1":
        raise ValueError("candidate_family_missing")
    if legacy["candidate_id"] == target["candidate_id"] or legacy["energy_meV"] == target["energy_meV"]:
        raise ValueError("candidate_pair_degenerate")
    if float(legacy["exposure_per_point"]).hex() != float(target["exposure_per_point"]).hex():
        raise ValueError("candidate_exposure_mismatch")
    regenerated = persisted if regenerated_persisted_legacy is None else _candidate_identity(regenerated_persisted_legacy)
    return _freeze({"revision": CANDIDATE_PAIR_REVISION, "persisted_legacy": persisted,
                    "regenerated_persisted_legacy": regenerated, "shadow_legacy": legacy, "target_core": target,
                    "candidate_pair_fingerprint": _fp("candidate_pair", {"revision": CANDIDATE_PAIR_REVISION,
                        "persisted": persisted, "regenerated": regenerated, "legacy": legacy, "target": target})})


def _validate_ready_case_evidence(context_transform: Mapping[str, Any], candidate_pair: Mapping[str, Any]) -> None:
    _exact_keys(context_transform, {"revision", "persisted_context", "offline_context", "persisted_context_fingerprint", "offline_context_fingerprint", "changed_fields"}, "context transform")
    if (context_transform.get("revision") != "isar.offline_shadow_context_transform/o36.5"
            or not _valid_fp(context_transform.get("persisted_context_fingerprint"))
            or not _valid_fp(context_transform.get("offline_context_fingerprint"))
            or tuple(context_transform.get("changed_fields", ())) not in ((), ("scan_window_eig_mode",))
            or not isinstance(context_transform.get("persisted_context"), Mapping)
            or not isinstance(context_transform.get("offline_context"), Mapping)
            or _fp("scan_design_context", context_transform["persisted_context"])
            != context_transform["persisted_context_fingerprint"]
            or _fp("scan_design_context", context_transform["offline_context"])
            != context_transform["offline_context_fingerprint"]):
        raise ValueError("ready case context transform is invalid")
    before, after = context_transform["persisted_context"], context_transform["offline_context"]
    if (after.get("scan_window_eig_mode") != "shadow"
            or {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
            != set(context_transform["changed_fields"])):
        raise ValueError("ready case context transform changed fields are invalid")
    _exact_keys(candidate_pair, {"revision", "persisted_legacy", "regenerated_persisted_legacy", "shadow_legacy", "target_core", "candidate_pair_fingerprint"}, "candidate pair")
    regenerated = candidate_pair["regenerated_persisted_legacy"]
    actual = validate_offline_candidate_pair(candidate_pair["persisted_legacy"], candidate_pair["shadow_legacy"], candidate_pair["target_core"], regenerated_persisted_legacy=regenerated)
    if actual["candidate_pair_fingerprint"] != candidate_pair.get("candidate_pair_fingerprint"):
        raise ValueError("ready case candidate pair fingerprint is invalid")


@dataclass(frozen=True)
class OfflineCaseSnapshot:
    case_identity: str
    campaign_configuration_fingerprint: str
    qoi_id: str
    qoi_unit: str
    status: str
    reason_codes: tuple[str, ...] = ()
    source_fingerprints: Mapping[str, Any] = field(default_factory=dict)
    context_transform: Mapping[str, Any] = field(default_factory=dict)
    candidate_pair: Mapping[str, Any] = field(default_factory=dict)
    authority_scopes: tuple[tuple[str, str], ...] = _AUTHORITY
    contract: str = field(default=OFFLINE_CASE_SNAPSHOT_CONTRACT, init=False)
    case_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        _string(self.qoi_id, "qoi_id"); _string(self.qoi_unit, "qoi_unit")
        if (not _valid_fp(self.case_identity) or not _valid_fp(self.campaign_configuration_fingerprint)
                or self.status not in _CASE_STATUSES):
            raise ValueError("offline case snapshot metadata is invalid")
        codes = _codes(self.reason_codes)
        if (self.status == _READY) != (not codes):
            raise ValueError("offline case status and reason codes disagree")
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "source_fingerprints", _mapping(self.source_fingerprints, "source_fingerprints"))
        object.__setattr__(self, "context_transform", _mapping(self.context_transform, "context_transform"))
        object.__setattr__(self, "candidate_pair", _mapping(self.candidate_pair, "candidate_pair"))
        if self.status == _READY:
            _validate_ready_case_evidence(self.context_transform, self.candidate_pair)
        expected_authority = _AUTHORITY if self.status == _READY else _INELIGIBLE_AUTHORITY
        if self.authority_scopes != expected_authority:
            raise ValueError("offline case authority scopes are invalid")
        object.__setattr__(self, "case_fingerprint", _fp("case", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {"contract": self.contract, "case_identity": self.case_identity,
                "campaign_configuration_fingerprint": self.campaign_configuration_fingerprint,
                "qoi_id": self.qoi_id, "qoi_unit": self.qoi_unit, "status": self.status,
                "reason_codes": list(self.reason_codes), "source_fingerprints": _thaw(self.source_fingerprints),
                "context_transform": _thaw(self.context_transform), "candidate_pair": _thaw(self.candidate_pair),
                "authority_scopes": dict(self.authority_scopes)}

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "case_fingerprint": self.case_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineCaseSnapshot":
        allowed = {"contract", "case_identity", "campaign_configuration_fingerprint", "qoi_id", "qoi_unit", "status", "reason_codes", "source_fingerprints", "context_transform", "candidate_pair", "authority_scopes", "case_fingerprint"}
        _exact_keys(value, allowed, "offline case snapshot")
        if value.get("contract") != OFFLINE_CASE_SNAPSHOT_CONTRACT:
            raise ValueError("offline case snapshot contract is unsupported")
        item = cls(case_identity=value["case_identity"], campaign_configuration_fingerprint=value["campaign_configuration_fingerprint"],
                   qoi_id=value["qoi_id"], qoi_unit=value["qoi_unit"], status=value["status"],
                   reason_codes=tuple(value.get("reason_codes", ())), source_fingerprints=value.get("source_fingerprints", {}),
                   context_transform=value.get("context_transform", {}), candidate_pair=value.get("candidate_pair", {}),
                   authority_scopes=tuple(sorted(dict(value.get("authority_scopes", {})).items())))
        if value.get("case_fingerprint") != item.case_fingerprint:
            raise ValueError("case_fingerprint does not authenticate offline case snapshot")
        return item


@dataclass(frozen=True)
class OfflineCorpusIndex:
    discovery_config: OfflineDiscoveryConfig
    cases: tuple[OfflineCaseSnapshot, ...]
    contract: str = field(default=OFFLINE_CORPUS_INDEX_CONTRACT, init=False)
    counts: Mapping[str, int] = field(default_factory=dict, init=False)
    authority_scopes: tuple[tuple[str, str], ...] = field(default=_AUTHORITY, init=False)
    corpus_fingerprint: str = field(default="", init=False)
    diagnostics: Mapping[str, Any] = field(default_factory=dict, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.discovery_config, OfflineDiscoveryConfig):
            raise TypeError("discovery_config must be an OfflineDiscoveryConfig")
        if (not isinstance(self.cases, tuple) or any(not isinstance(case, OfflineCaseSnapshot) for case in self.cases)
                or tuple(case.case_identity for case in self.cases) != tuple(sorted(case.case_identity for case in self.cases))
                or len({case.case_identity for case in self.cases}) != len(self.cases)):
            raise ValueError("offline corpus cases must be sorted and unique")
        counts = {status: sum(case.status == status for case in self.cases) for status in sorted(_CASE_STATUSES)}
        object.__setattr__(self, "counts", _freeze(counts)); object.__setattr__(self, "diagnostics", _mapping(self.diagnostics, "diagnostics"))
        payload = {"contract": self.contract, "discovery_config": self.discovery_config.to_dict(),
                   "cases": [case.to_dict() for case in self.cases], "counts": counts,
                   "authority_scopes": dict(self.authority_scopes)}
        object.__setattr__(self, "corpus_fingerprint", _fp("corpus", payload))

    def to_dict(self) -> dict[str, Any]:
        return {"contract": self.contract, "discovery_config": self.discovery_config.to_dict(), "cases": [c.to_dict() for c in self.cases],
                "counts": _thaw(self.counts), "authority_scopes": dict(self.authority_scopes),
                "corpus_fingerprint": self.corpus_fingerprint, "diagnostics": _thaw(self.diagnostics)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineCorpusIndex":
        _exact_keys(value, {"contract", "discovery_config", "cases", "counts", "authority_scopes", "corpus_fingerprint", "diagnostics"}, "offline corpus index")
        if value.get("contract") != OFFLINE_CORPUS_INDEX_CONTRACT: raise ValueError("corpus index contract is unsupported")
        item = cls(OfflineDiscoveryConfig.from_dict(value["discovery_config"]), tuple(OfflineCaseSnapshot.from_dict(x) for x in value["cases"]), diagnostics=value.get("diagnostics", {}))
        if (dict(value.get("counts", {})) != dict(item.counts) or dict(value.get("authority_scopes", {})) != dict(item.authority_scopes)
                or value.get("corpus_fingerprint") != item.corpus_fingerprint): raise ValueError("corpus_index_stale")
        return item


def validate_offline_corpus_index(value: Any) -> OfflineCorpusIndex:
    if not isinstance(value, OfflineCorpusIndex):
        raise TypeError("value must be an OfflineCorpusIndex")
    # Reconstructing through the strict persistence gate catches tampering of every
    # nested fingerprint, ordering rule, count, and enclosing corpus fingerprint.
    return OfflineCorpusIndex.from_dict(value.to_dict())


@dataclass(frozen=True)
class QoIDistinctionVariant:
    variant_id: str
    parameters: Mapping[str, Any]
    variant_fingerprint: str = field(default="", init=False)
    def __post_init__(self) -> None:
        _string(self.variant_id, "variant_id"); object.__setattr__(self, "parameters", _mapping(self.parameters, "variant parameters"))
        object.__setattr__(self, "variant_fingerprint", _fp("policy_variant", {"variant_id": self.variant_id, "parameters": _thaw(self.parameters)}))
    def to_dict(self) -> dict[str, Any]: return {"variant_id": self.variant_id, "parameters": _thaw(self.parameters), "variant_fingerprint": self.variant_fingerprint}
    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QoIDistinctionVariant":
        _exact_keys(value, {"variant_id", "parameters", "variant_fingerprint"}, "policy variant")
        item = cls(value["variant_id"], value["parameters"])
        if value.get("variant_fingerprint") != item.variant_fingerprint:
            raise ValueError("variant_fingerprint does not authenticate policy variant")
        return item


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
        _string(self.policy_id, "policy_id"); _string(self.policy_kind, "policy_kind")
        source = _mapping(self.source_qoi, "source_qoi"); _exact_keys(source, {"id", "unit"}, "source_qoi"); _string(source.get("id"), "source_qoi.id"); _string(source.get("unit"), "source_qoi.unit")
        scope = _mapping(self.scope, "scope"); _exact_keys(scope, {"kind", "fingerprint", "annotation"}, "policy scope")
        annotation = scope.get("annotation")
        if (scope.get("kind") not in {"case_identity", "campaign_configuration"}
                or not _valid_fp(scope.get("fingerprint")) or not isinstance(annotation, Mapping)
                or set(annotation) != {"campaign_name", "experiment_description", "date"}
                or any(not isinstance(annotation[name], str) or not annotation[name].strip()
                       for name in annotation)):
            raise ValueError("policy scope is invalid")
        variants = tuple(self.variants)
        if (not variants or any(not isinstance(v, QoIDistinctionVariant) for v in variants)
                or tuple(v.variant_id for v in variants) != tuple(sorted(v.variant_id for v in variants))
                or len({v.variant_id for v in variants}) != len(variants)): raise ValueError("policy variants must be sorted and unique")
        provenance = _mapping(self.provenance, "policy provenance")
        _exact_keys(provenance, {"kind", "source_id", "source_fingerprint", "rationale", "author", "authority_role"}, "policy provenance")
        kind = provenance.get("kind")
        if kind not in {"scientific_protocol/1", "experiment_objective/1", "explicit_user_analysis/1", "exploratory_sensitivity/1"}:
            raise ValueError("policy provenance kind is invalid")
        if not isinstance(provenance.get("rationale"), str) or not provenance["rationale"].strip():
            raise ValueError("policy provenance requires rationale")
        if kind in {"scientific_protocol/1", "experiment_objective/1"} and not _valid_fp(provenance.get("source_fingerprint")):
            raise ValueError("policy provenance requires source_fingerprint")
        if kind in {"explicit_user_analysis/1", "exploratory_sensitivity/1"} and (not isinstance(provenance.get("source_id"), str) or not provenance["source_id"].strip()):
            raise ValueError("policy provenance requires source_id")
        object.__setattr__(self, "source_qoi", source); object.__setattr__(self, "scope", scope); object.__setattr__(self, "provenance", provenance); object.__setattr__(self, "variants", variants)
        object.__setattr__(self, "policy_fingerprint", _fp("policy", self._payload()))
    def _payload(self) -> dict[str, Any]: return {"contract": self.contract, "policy_id": self.policy_id, "policy_kind": self.policy_kind, "source_qoi": _thaw(self.source_qoi), "scope": {k: _thaw(v) for k,v in self.scope.items() if k != "annotation"}, "provenance": _thaw(self.provenance), "variants": [v.to_dict() for v in self.variants]}
    def to_dict(self) -> dict[str, Any]: return {**self._payload(), "scope": _thaw(self.scope), "policy_fingerprint": self.policy_fingerprint}
    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QoIDistinctionPolicy":
        allowed = {"contract", "policy_id", "policy_kind", "source_qoi", "scope", "provenance", "variants", "policy_fingerprint"}
        _exact_keys(value, allowed, "QoI distinction policy")
        if value.get("contract") != QOI_DISTINCTION_POLICY_CONTRACT:
            raise ValueError("QoI distinction policy contract is unsupported")
        item = cls(value["policy_id"], value["policy_kind"], value["source_qoi"], value["scope"], value["provenance"], tuple(QoIDistinctionVariant.from_dict(x) for x in value["variants"]))
        if value.get("policy_fingerprint") != item.policy_fingerprint:
            raise ValueError("policy_fingerprint does not authenticate QoI distinction policy")
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
        if (any(not isinstance(p, QoIDistinctionPolicy) for p in policies)
                or tuple(p.policy_id for p in policies) != tuple(sorted(p.policy_id for p in policies))
                or len({p.policy_id for p in policies}) != len(policies)): raise ValueError("policies must be sorted and unique")
        object.__setattr__(self, "policies", policies); object.__setattr__(self, "policy_set_fingerprint", _fp("policy_set", {"contract": self.contract, "policies": [p._payload() for p in policies]}))
    def to_dict(self) -> dict[str, Any]: return {"contract": self.contract, "policies": [p.to_dict() for p in self.policies], "policy_set_fingerprint": self.policy_set_fingerprint}
    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QoIDistinctionPolicySet":
        _exact_keys(value, {"contract", "policies", "policy_set_fingerprint"}, "QoI distinction policy set")
        if value.get("contract") != QOI_DISTINCTION_POLICY_SET_CONTRACT:
            raise ValueError("QoI distinction policy set contract is unsupported")
        item = cls(tuple(QoIDistinctionPolicy.from_dict(x) for x in value["policies"]))
        if value.get("policy_set_fingerprint") != item.policy_set_fingerprint:
            raise ValueError("policy_set_fingerprint does not authenticate policy set")
        return item


def validate_qoi_distinction_policy_set(value: Any) -> QoIDistinctionPolicySet:
    if not isinstance(value, QoIDistinctionPolicySet): raise TypeError("value must be a QoIDistinctionPolicySet")
    expected = _fp("policy_set", {"contract": value.contract, "policies": [p._payload() for p in value.policies]})
    if value.contract != QOI_DISTINCTION_POLICY_SET_CONTRACT or value.policy_set_fingerprint != expected: raise ValueError("policy set fingerprint is invalid")
    return value


def matching_qoi_policies(case: OfflineCaseSnapshot, policy_set: QoIDistinctionPolicySet) -> tuple[QoIDistinctionPolicy, ...]:
    validate_qoi_distinction_policy_set(policy_set)
    matches = []
    for policy in policy_set.policies:
        if dict(policy.source_qoi) != {"id": case.qoi_id, "unit": case.qoi_unit}: continue
        scope = policy.scope
        identity = (case.case_identity if scope["kind"] == "case_identity"
                    else case.campaign_configuration_fingerprint)
        if scope["fingerprint"] == identity: matches.append(policy)
    return tuple(matches)


def compile_qoi_distinction_policy(policy: QoIDistinctionPolicy, variant: QoIDistinctionVariant) -> ScalarQoIDiscretization:
    if not isinstance(policy, QoIDistinctionPolicy) or not isinstance(variant, QoIDistinctionVariant) or variant not in policy.variants: raise TypeError("policy and variant must be authenticated policy records")
    if policy.policy_kind != "isar.scalar_absolute_partitions/o36.5": raise ValueError("qoi_policy_unsupported")
    parameters = variant.parameters
    _exact_keys(parameters, {"origin", "bin_width", "offsets"}, "scalar policy parameters")
    return ScalarQoIDiscretization(qoi_id=policy.source_qoi["id"], qoi_unit=policy.source_qoi["unit"], origin=parameters["origin"], bin_width=parameters["bin_width"], offsets=tuple(parameters["offsets"]))


@dataclass(frozen=True)
class OfflineScoringConfig:
    seed: int
    simulation_count: int
    block_count: int
    candidate_pair_revision: str = CANDIDATE_PAIR_REVISION
    policy_compiler_revision: str = POLICY_COMPILER_REVISION
    scorer_revision: str = SCORER_REVISION
    likelihood_revision: str = LIKELIHOOD_REVISION
    crn_revision: str = CRN_REVISION
    contract: str = field(default=OFFLINE_SCORING_CONFIG_CONTRACT, init=False)
    scoring_config_fingerprint: str = field(default="", init=False)
    def __post_init__(self) -> None:
        if (not all(isinstance(x, int) and not isinstance(x, bool) for x in (self.seed, self.simulation_count, self.block_count))
                or self.simulation_count < 2 or self.block_count < 2 or self.block_count > self.simulation_count or self.simulation_count % self.block_count): raise ValueError("offline scoring configuration is invalid")
        if (self.candidate_pair_revision != CANDIDATE_PAIR_REVISION or self.policy_compiler_revision != POLICY_COMPILER_REVISION or self.scorer_revision != SCORER_REVISION or self.likelihood_revision != LIKELIHOOD_REVISION or self.crn_revision != CRN_REVISION): raise ValueError("offline scoring revisions are unsupported")
        object.__setattr__(self, "scoring_config_fingerprint", _fp("scoring_config", self._payload()))
    def _payload(self) -> dict[str, Any]: return {"contract": self.contract, "candidate_pair_revision": self.candidate_pair_revision, "policy_compiler_revision": self.policy_compiler_revision, "scorer_revision": self.scorer_revision, "likelihood_revision": self.likelihood_revision, "crn_revision": self.crn_revision, "seed": self.seed, "simulation_count": self.simulation_count, "block_count": self.block_count}
    def to_dict(self) -> dict[str, Any]: return {**self._payload(), "scoring_config_fingerprint": self.scoring_config_fingerprint}
    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineScoringConfig":
        allowed = {"contract", "candidate_pair_revision", "policy_compiler_revision", "scorer_revision", "likelihood_revision", "crn_revision", "seed", "simulation_count", "block_count", "scoring_config_fingerprint"}
        _exact_keys(value, allowed, "offline scoring config")
        if value.get("contract") != OFFLINE_SCORING_CONFIG_CONTRACT:
            raise ValueError("offline scoring config contract is unsupported")
        item = cls(seed=value["seed"], simulation_count=value["simulation_count"], block_count=value["block_count"], candidate_pair_revision=value["candidate_pair_revision"], policy_compiler_revision=value["policy_compiler_revision"], scorer_revision=value["scorer_revision"], likelihood_revision=value["likelihood_revision"], crn_revision=value["crn_revision"])
        if value.get("scoring_config_fingerprint") != item.scoring_config_fingerprint:
            raise ValueError("scoring_config_fingerprint does not authenticate scoring config")
        return item


@dataclass(frozen=True)
class OfflineComparisonResult:
    case_identity: str; policy_id: str; variant_id: str; status: str; reason_codes: tuple[str, ...]
    score_set_fingerprint: str | None = None; delta_information_nats: float | None = None
    delta_information_standard_error_nats: float | None = None; delta_information_per_executed_point_nats: float | None = None
    delta_information_per_executed_point_standard_error_nats: float | None = None
    authority_scopes: tuple[tuple[str, str], ...] = _AUTHORITY
    result_fingerprint: str = field(default="", init=False)
    def __post_init__(self) -> None:
        if self.status not in {"complete", "unavailable"} or self.authority_scopes != _AUTHORITY:
            raise ValueError("offline comparison result authority or status is invalid")
        _string(self.case_identity, "result case_identity")
        object.__setattr__(self, "reason_codes", _codes(self.reason_codes))
        values = (self.delta_information_nats, self.delta_information_standard_error_nats,
                  self.delta_information_per_executed_point_nats,
                  self.delta_information_per_executed_point_standard_error_nats)
        if self.status == "complete":
            if (self.reason_codes or not _valid_fp(self.score_set_fingerprint)
                    or any(not _finite(value) for value in values)
                    or self.delta_information_standard_error_nats < 0.0
                    or self.delta_information_per_executed_point_standard_error_nats < 0.0):
                raise ValueError("complete offline comparison result is invalid")
        elif self.reason_codes == () or any(value is not None for value in values):
            raise ValueError("unavailable offline comparison result is invalid")
        payload = self.to_dict(include_fingerprint=False)
        object.__setattr__(self, "result_fingerprint", _fp("result", payload))
    def to_dict(self, *, include_fingerprint: bool = True) -> dict[str, Any]:
        value = {"case_identity": self.case_identity, "policy_id": self.policy_id, "variant_id": self.variant_id, "status": self.status, "reason_codes": list(self.reason_codes), "score_set_fingerprint": self.score_set_fingerprint, "delta_information_nats": self.delta_information_nats, "delta_information_standard_error_nats": self.delta_information_standard_error_nats, "delta_information_per_executed_point_nats": self.delta_information_per_executed_point_nats, "delta_information_per_executed_point_standard_error_nats": self.delta_information_per_executed_point_standard_error_nats, "authority_scopes": dict(self.authority_scopes)}
        return {**value, "result_fingerprint": self.result_fingerprint} if include_fingerprint else value
    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineComparisonResult":
        allowed = {"case_identity", "policy_id", "variant_id", "status", "reason_codes", "score_set_fingerprint", "delta_information_nats", "delta_information_standard_error_nats", "delta_information_per_executed_point_nats", "delta_information_per_executed_point_standard_error_nats", "authority_scopes", "result_fingerprint"}
        _exact_keys(value, allowed, "offline comparison result")
        item = cls(value["case_identity"], value["policy_id"], value["variant_id"], value["status"], tuple(value["reason_codes"]), value.get("score_set_fingerprint"), value.get("delta_information_nats"), value.get("delta_information_standard_error_nats"), value.get("delta_information_per_executed_point_nats"), value.get("delta_information_per_executed_point_standard_error_nats"), tuple(sorted(dict(value["authority_scopes"]).items())))
        if value.get("result_fingerprint") != item.result_fingerprint:
            raise ValueError("result_fingerprint does not authenticate offline comparison result")
        return item


@dataclass(frozen=True)
class OfflineComparisonReport:
    corpus_fingerprint: str; policy_set_fingerprint: str; scoring_config_fingerprint: str; status: str
    results: tuple[OfflineComparisonResult, ...]; diagnostics: Mapping[str, Any] = field(default_factory=dict, compare=False)
    contract: str = field(default=OFFLINE_COMPARISON_REPORT_CONTRACT, init=False); counts: Mapping[str, int] = field(default_factory=dict, init=False)
    authority_scopes: tuple[tuple[str, str], ...] = field(default=_INELIGIBLE_AUTHORITY, init=False); report_fingerprint: str = field(default="", init=False)
    def __post_init__(self) -> None:
        if self.status not in {"complete", "partial", "unavailable"} or not all(_valid_fp(x) for x in (self.corpus_fingerprint, self.policy_set_fingerprint, self.scoring_config_fingerprint)): raise ValueError("offline report metadata is invalid")
        results = tuple(self.results)
        if (any(not isinstance(r, OfflineComparisonResult) for r in results) or tuple((r.case_identity,r.policy_id,r.variant_id) for r in results) != tuple(sorted((r.case_identity,r.policy_id,r.variant_id) for r in results))): raise ValueError("offline report results must be ordered")
        expected_authority = _INELIGIBLE_AUTHORITY if self.status == "unavailable" else _AUTHORITY
        object.__setattr__(self, "authority_scopes", expected_authority)
        counts = {name: sum(r.status == name for r in results) for name in ("complete", "unavailable")}; object.__setattr__(self, "counts", _freeze(counts)); object.__setattr__(self, "diagnostics", _mapping(self.diagnostics, "diagnostics"))
        payload = {"contract": self.contract, "corpus_fingerprint": self.corpus_fingerprint, "policy_set_fingerprint": self.policy_set_fingerprint, "scoring_config_fingerprint": self.scoring_config_fingerprint, "status": self.status, "results": [r.to_dict() for r in results], "counts": counts, "authority_scopes": dict(self.authority_scopes)}
        object.__setattr__(self, "report_fingerprint", _fp("report", payload))
    def to_dict(self) -> dict[str, Any]: return {"contract": self.contract, "corpus_fingerprint": self.corpus_fingerprint, "policy_set_fingerprint": self.policy_set_fingerprint, "scoring_config_fingerprint": self.scoring_config_fingerprint, "status": self.status, "results": [r.to_dict() for r in self.results], "counts": _thaw(self.counts), "authority_scopes": dict(self.authority_scopes), "report_fingerprint": self.report_fingerprint, "diagnostics": _thaw(self.diagnostics)}
    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineComparisonReport":
        allowed = {"contract", "corpus_fingerprint", "policy_set_fingerprint", "scoring_config_fingerprint", "status", "results", "counts", "authority_scopes", "report_fingerprint", "diagnostics"}
        _exact_keys(value, allowed, "offline comparison report")
        if value.get("contract") != OFFLINE_COMPARISON_REPORT_CONTRACT:
            raise ValueError("offline comparison report contract is unsupported")
        item = cls(value["corpus_fingerprint"], value["policy_set_fingerprint"], value["scoring_config_fingerprint"], value["status"], tuple(OfflineComparisonResult.from_dict(x) for x in value["results"]), value.get("diagnostics", {}))
        if (dict(value.get("counts", {})) != dict(item.counts)
                or dict(value.get("authority_scopes", {})) != dict(item.authority_scopes)
                or value.get("report_fingerprint") != item.report_fingerprint):
            raise ValueError("report_fingerprint does not authenticate offline comparison report")
        return item


def validate_offline_comparison_report(value: Any) -> OfflineComparisonReport:
    if not isinstance(value, OfflineComparisonReport):
        raise TypeError("value must be an OfflineComparisonReport")
    return OfflineComparisonReport.from_dict(value.to_dict())


def _validated_scientific_case(
        case: OfflineCaseSnapshot,
        value: Any,
) -> tuple[ScanWindowDesignMeasure, tuple[CandidateEvaluationInput, ...]]:
    """Join caller-supplied numerical inputs back to one authenticated case."""
    if (not isinstance(value, tuple) or len(value) != 2
            or not isinstance(value[1], Sequence)
            or isinstance(value[1], (str, bytes))):
        raise ValueError("offline_scientific_case_mismatch")
    try:
        measure = validate_scan_window_design_measure(value[0])
    except (TypeError, ValueError) as error:
        raise ValueError("offline_scientific_case_mismatch") from error
    candidates = tuple(value[1])
    if (len(candidates) != 2
            or any(not isinstance(item, CandidateEvaluationInput) for item in candidates)):
        raise ValueError("offline_scientific_case_mismatch")

    expected = dict(case.source_fingerprints)
    qoi = dict(measure.qoi)
    if (expected.get("design_measure") != measure.design_measure_fingerprint
            or expected.get("source_draw_identity") != measure.source_draw_identity_fingerprint
            or expected.get("predictive_basis")
            != dict(measure.source_identities).get("predictive_basis_fingerprint")
            or qoi.get("id") != case.qoi_id or qoi.get("unit") != case.qoi_unit):
        raise ValueError("offline_scientific_case_mismatch")

    pair = dict(case.candidate_pair)
    pair_views = {
        pair[name]["candidate_id"]: pair[name]
        for name in ("shadow_legacy", "target_core")
    }
    if set(item.candidate_id for item in candidates) != set(pair_views):
        raise ValueError("offline_scientific_case_mismatch")
    for item in candidates:
        view = pair_views[item.candidate_id]
        evaluation = item.evaluation
        if (not getattr(evaluation, "available", False)
                or tuple(evaluation.energy_meV) != tuple(view["energy_meV"])
                or float(evaluation.exposure_per_point) != float(view["exposure_per_point"])
                or evaluation.predictive_basis_fingerprint != expected.get("predictive_basis")):
            raise ValueError("offline_scientific_case_mismatch")
    return measure, candidates


def score_offline_comparison(corpus: OfflineCorpusIndex, policies: QoIDistinctionPolicySet, config: OfflineScoringConfig, scientific_cases: Mapping[str, tuple[ScanWindowDesignMeasure, Sequence[CandidateEvaluationInput]]]) -> OfflineComparisonReport:
    """Score supplied ready cases; reported deltas are always target_core minus legacy."""
    if not isinstance(corpus, OfflineCorpusIndex) or not isinstance(config, OfflineScoringConfig): raise TypeError("corpus and config must be frozen offline contracts")
    validate_offline_corpus_index(corpus)
    OfflineScoringConfig.from_dict(config.to_dict())
    validate_qoi_distinction_policy_set(policies); results: list[OfflineComparisonResult] = []
    for case in corpus.cases:
        if case.status != _READY:
            continue
        matches = matching_qoi_policies(case, policies)
        if not matches: results.append(OfflineComparisonResult(case.case_identity, "", "", "unavailable", ("qoi_policy_missing",))); continue
        if len(matches) != 1: results.append(OfflineComparisonResult(case.case_identity, "", "", "unavailable", ("qoi_policy_ambiguous",))); continue
        policy = matches[0]
        for variant in policy.variants:
            if case.status != _READY or case.case_identity not in scientific_cases: results.append(OfflineComparisonResult(case.case_identity, policy.policy_id, variant.variant_id, "unavailable", ("corpus_index_stale",))); continue
            try:
                discretization = compile_qoi_distinction_policy(policy, variant)
            except ValueError as error:
                if str(error) != "qoi_policy_unsupported":
                    raise
                results.append(OfflineComparisonResult(case.case_identity, policy.policy_id, variant.variant_id, "unavailable", ("qoi_policy_unsupported",)))
                continue
            try:
                measure, candidates = _validated_scientific_case(
                    case, scientific_cases[case.case_identity])
            except (TypeError, ValueError):
                results.append(OfflineComparisonResult(
                    case.case_identity, policy.policy_id, variant.variant_id,
                    "unavailable", ("offline_scientific_case_mismatch",)))
                continue
            score_set = validate_scan_window_score_set(score_scan_window_candidates(measure, candidates, qoi_discretization=discretization, seed=config.seed, simulation_count=config.simulation_count, block_count=config.block_count))
            comparisons = score_set.pairwise_comparisons
            legacy_id = dict(case.candidate_pair).get("shadow_legacy", {}).get("candidate_id")
            target_id = dict(case.candidate_pair).get("target_core", {}).get("candidate_id")
            pair = next((x for x in comparisons if {x["candidate_a_id"], x["candidate_b_id"]} == {legacy_id, target_id}), None)
            if pair is None: results.append(OfflineComparisonResult(case.case_identity, policy.policy_id, variant.variant_id, "unavailable", ("candidate_family_missing",), score_set.score_set_fingerprint)); continue
            sign = 1.0 if pair["candidate_b_id"] == target_id else -1.0
            results.append(OfflineComparisonResult(case.case_identity, policy.policy_id, variant.variant_id, "complete", (), score_set.score_set_fingerprint, sign * pair["delta_information_nats"], pair["delta_information_standard_error_nats"], sign * pair["delta_information_per_executed_point_nats"], pair["delta_information_per_executed_point_standard_error_nats"]))
    status = "complete" if not results or all(r.status == "complete" for r in results) else "partial" if any(r.status == "complete" for r in results) else "unavailable"
    return OfflineComparisonReport(corpus.corpus_fingerprint, policies.policy_set_fingerprint, config.scoring_config_fingerprint, status, tuple(sorted(results, key=lambda r: (r.case_identity,r.policy_id,r.variant_id))))


__all__ = ["OfflineDiscoveryConfig", "OfflineCaseSnapshot", "OfflineCorpusIndex", "QoIDistinctionVariant", "QoIDistinctionPolicy", "QoIDistinctionPolicySet", "OfflineScoringConfig", "OfflineComparisonResult", "OfflineComparisonReport", "campaign_configuration_fingerprint", "offline_shadow_context_transform", "validate_offline_candidate_pair", "validate_offline_corpus_index", "validate_qoi_distinction_policy", "validate_qoi_distinction_policy_set", "validate_offline_comparison_report", "matching_qoi_policies", "compile_qoi_distinction_policy", "score_offline_comparison"]
