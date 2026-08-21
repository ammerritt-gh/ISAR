"""Pure, fail-closed offline comparison contracts for EIG Slice 5.1."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import math
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from isar.drive.acquisition.inventory import canonical_json
from isar.drive.acquisition.scan_design import (
    PHYSICAL_WINDOW_GENERATOR_ID,
    PHYSICAL_WINDOW_GENERATOR_VERSION,
    ScanDesignContext,
)
from isar.eig.policy import POLICY_COMPILER_REVISION, compile_qoi_distinction_policy
from isar.schema.eig_policy import (
    QOI_DISTINCTION_POLICY_CONTRACT,
    QOI_DISTINCTION_POLICY_SET_CONTRACT,
    QoIDistinctionPolicy,
    QoIDistinctionPolicySet,
    QoIDistinctionVariant,
    matching_qoi_policies as _matching_qoi_policies,
    validate_qoi_distinction_policy_set,
)
from isar.schema.statistics import semantics_fingerprint

from .design_measure import ScanWindowDesignMeasure, validate_scan_window_design_measure
from .scorer import (
    CRN_REVISION,
    LIKELIHOOD_REVISION,
    SCORER_REVISION,
    CandidateEvaluationInput,
    ScalarQoIDiscretization,
    score_scan_window_candidates,
    validate_scan_window_score_set,
)


OFFLINE_DISCOVERY_CONFIG_CONTRACT = "isar.offline_eig_discovery_config/o36.5.1"
OFFLINE_CASE_SNAPSHOT_CONTRACT = "isar.offline_eig_case_snapshot/o36.5.1"
OFFLINE_CORPUS_INDEX_CONTRACT = "isar.eig_offline_corpus_index/o36.5.1"
OFFLINE_SCORING_CONFIG_CONTRACT = "isar.eig_offline_scoring_config/o36.5.1"
OFFLINE_SCORE_EVIDENCE_CONTRACT = "isar.offline_score_evidence/o36.5.1"
OFFLINE_COMPARISON_RESULT_CONTRACT = "isar.offline_comparison_result/o36.5.1"
OFFLINE_COMPARISON_REPORT_CONTRACT = "isar.eig_offline_comparison_report/o36.5.1"
CANDIDATE_PAIR_REVISION = "isar.offline_eig_candidate_pair/o36.5.1"
COUNTERFACTUAL_TRANSFORM_REVISION = "isar.offline_shadow_context_transform/o36.5.1"
_FINGERPRINT_DOMAIN = "isar.offline_eig/o36.5.1"
SCAN_DESIGN_INVENTORY_SCHEMA_V2 = "isar.scan_design_inventory/2"
_READY = "ready"
_CASE_STATUSES = {
    _READY,
    "comparison_unavailable",
    "scientifically_ineligible",
    "unassessable",
    "nonterminal_or_unclassified",
}
_AUTHORITY = (
    ("acquisition_selection", "ineligible"),
    ("eig_authoritative", "ineligible"),
    ("eig_shadow", "diagnostic"),
)
_INELIGIBLE_AUTHORITY = (
    ("acquisition_selection", "ineligible"),
    ("eig_authoritative", "ineligible"),
    ("eig_shadow", "ineligible"),
)


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
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a non-empty stripped string")
    return value


def _codes(value: Sequence[str]) -> tuple[str, ...]:
    result = tuple(_string(code, "reason code") for code in value)
    if len(result) != len(set(result)):
        raise ValueError("reason codes must be unique")
    return result


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _exact_keys(value: Mapping[str, Any], expected: set[str], name: str) -> None:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{name} fields are invalid")


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    return _freeze(value)


def _generator_identity(value: Mapping[str, Any]) -> dict[str, Any]:
    _exact_keys(value, {"id", "version", "config", "config_fingerprint"}, "generator identity")
    config = value["config"]
    if not isinstance(config, Mapping):
        raise ValueError("generator configuration is invalid")
    fingerprint = hashlib.sha256(canonical_json(_thaw(config)).encode("utf-8")).hexdigest()
    if (
        value["id"] != PHYSICAL_WINDOW_GENERATOR_ID
        or value["version"] != PHYSICAL_WINDOW_GENERATOR_VERSION
        or value["config_fingerprint"] != fingerprint
    ):
        raise ValueError("generator identity is invalid")
    return {
        "id": value["id"],
        "version": value["version"],
        "config": _thaw(config),
        "config_fingerprint": fingerprint,
    }


@dataclass(frozen=True)
class OfflineDiscoveryConfig:
    discovery_ruleset_revision: str = "isar.offline_eig_discovery_ruleset/o36.5.1"
    accepted_terminal_states: tuple[str, ...] = (
        "incomplete", "infeasible", "met", "operator_stopped")
    eligibility_contract: str = "isar.eig_eligibility/o36.2.1"
    qoi_evidence_contract: str = "isar.eig_qoi_projection_evidence/o36.2.1"
    design_measure_contract: str = "isar.scan_window_design_measure/o36.3"
    predictive_contract: str = "isar.master_predictive/2+physical_only.1"
    projection_contract: str = "isar.target_projection/2"
    inventory_contract: str = SCAN_DESIGN_INVENTORY_SCHEMA_V2
    generator_id: str = PHYSICAL_WINDOW_GENERATOR_ID
    generator_version: int = PHYSICAL_WINDOW_GENERATOR_VERSION
    generator_config_contract: str = "isar.physical_window_generator_config/2"
    counterfactual_transform_revision: str = COUNTERFACTUAL_TRANSFORM_REVISION
    contract: str = field(default=OFFLINE_DISCOVERY_CONFIG_CONTRACT, init=False)
    discovery_config_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if (
            self.discovery_ruleset_revision != "isar.offline_eig_discovery_ruleset/o36.5.1"
            or self.accepted_terminal_states != tuple(sorted(set(self.accepted_terminal_states)))
            or not self.accepted_terminal_states
            or self.eligibility_contract != "isar.eig_eligibility/o36.2.1"
            or self.qoi_evidence_contract != "isar.eig_qoi_projection_evidence/o36.2.1"
            or self.design_measure_contract != "isar.scan_window_design_measure/o36.3"
            or self.predictive_contract != "isar.master_predictive/2+physical_only.1"
            or self.projection_contract != "isar.target_projection/2"
            or self.inventory_contract != SCAN_DESIGN_INVENTORY_SCHEMA_V2
            or self.generator_id != PHYSICAL_WINDOW_GENERATOR_ID
            or self.generator_version != PHYSICAL_WINDOW_GENERATOR_VERSION
            or self.generator_config_contract != "isar.physical_window_generator_config/2"
            or self.counterfactual_transform_revision != COUNTERFACTUAL_TRANSFORM_REVISION
        ):
            raise ValueError("offline discovery configuration is unsupported")
        object.__setattr__(self, "discovery_config_fingerprint", _fp("discovery_config", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "discovery_ruleset_revision": self.discovery_ruleset_revision,
            "accepted_terminal_states": list(self.accepted_terminal_states),
            "eligibility_contract": self.eligibility_contract,
            "qoi_evidence_contract": self.qoi_evidence_contract,
            "design_measure_contract": self.design_measure_contract,
            "predictive_contract": self.predictive_contract,
            "projection_contract": self.projection_contract,
            "inventory_contract": self.inventory_contract,
            "generator_id": self.generator_id,
            "generator_version": self.generator_version,
            "generator_config_contract": self.generator_config_contract,
            "counterfactual_transform_revision": self.counterfactual_transform_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "discovery_config_fingerprint": self.discovery_config_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineDiscoveryConfig":
        expected = set(cls().to_dict())
        _exact_keys(value, expected, "offline discovery configuration")
        if value["contract"] != OFFLINE_DISCOVERY_CONFIG_CONTRACT:
            raise ValueError("offline discovery configuration contract is unsupported")
        kwargs = {key: value[key] for key in expected - {"contract", "discovery_config_fingerprint"}}
        kwargs["accepted_terminal_states"] = tuple(kwargs["accepted_terminal_states"])
        item = cls(**kwargs)
        if value["discovery_config_fingerprint"] != item.discovery_config_fingerprint:
            raise ValueError("discovery configuration fingerprint is invalid")
        return item


def offline_shadow_context_transform(
    context: ScanDesignContext,
    generator_config_fingerprint: str,
) -> Mapping[str, Any]:
    """Return the scored counterfactual of ``context`` for offline corpus comparison.

    Named "shadow" historically: the diagnostic-only "shadow" mode this targeted was retired in
    SCENE-4 PR 4.4 (D3), and this now targets "authoritative" — the sole remaining mode whose
    scan-design inventory generates and scores the alternative candidate family. The revision
    string (``COUNTERFACTUAL_TRANSFORM_REVISION``) is a persisted contract and is unchanged.
    """
    if not isinstance(context, ScanDesignContext) or not _valid_fp(generator_config_fingerprint):
        raise TypeError("context and generator configuration fingerprint are required")
    shadow = replace(context, scan_window_eig_mode="authoritative")
    before, after = context.to_dict(), shadow.to_dict()
    changed = sorted(key for key in before if before[key] != after[key])
    if changed not in ([], ["scan_window_eig_mode"]):
        raise ValueError("offline shadow context changed unsupported fields")
    return _freeze({
        "revision": COUNTERFACTUAL_TRANSFORM_REVISION,
        "persisted_context": before,
        "offline_context": after,
        "persisted_context_fingerprint": _fp("scan_design_context", before),
        "offline_context_fingerprint": _fp("scan_design_context", after),
        "generator_config_fingerprint": generator_config_fingerprint,
        "changed_fields": changed,
    })


def _candidate_identity(candidate: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"candidate_id", "energy_meV", "exposure_per_point", "family"}
    if not isinstance(candidate, Mapping) or set(candidate) - allowed:
        raise ValueError("candidate fields are invalid")
    candidate_id = _string(candidate.get("candidate_id"), "candidate_id")
    family = _string(candidate.get("family"), "candidate family")
    grid = candidate.get("energy_meV")
    if (
        not isinstance(grid, Sequence)
        or isinstance(grid, (str, bytes))
        or not grid
        or any(not _finite(value) for value in grid)
    ):
        raise ValueError("candidate energy grid is invalid")
    canonical_grid = tuple(0.0 if float(value) == 0.0 else float(value) for value in grid)
    if len({value.hex() for value in canonical_grid}) != len(canonical_grid):
        raise ValueError("candidate energy grid must be unique")
    exposure = candidate.get("exposure_per_point")
    if not _finite(exposure) or float(exposure) <= 0.0:
        raise ValueError("candidate exposure is invalid")
    return {
        "candidate_id": candidate_id,
        "family": family,
        "energy_meV": list(canonical_grid),
        "exposure_per_point": float(exposure),
    }


def validate_offline_candidate_pair(
    persisted_legacy: Mapping[str, Any],
    shadow_legacy: Mapping[str, Any],
    target_core: Mapping[str, Any],
    *,
    regenerated_persisted_legacy: Mapping[str, Any],
    generator_identity: Mapping[str, Any],
) -> Mapping[str, Any]:
    try:
        persisted = _candidate_identity(persisted_legacy)
        regenerated = _candidate_identity(regenerated_persisted_legacy)
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
    generator = _generator_identity(generator_identity)
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
    payload = {
        "revision": CANDIDATE_PAIR_REVISION,
        "generator": generator,
        "persisted_legacy": persisted,
        "regenerated_persisted_legacy": regenerated,
        "shadow_legacy": legacy,
        "target_core": target,
    }
    return _freeze({**payload, "candidate_pair_fingerprint": _fp("candidate_pair", payload)})


def _source_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    fields = {"reviewed_campaign_id", "master_session_key", "detector_id", "cohort", "evidence_tier"}
    _exact_keys(value, fields | {"source_provenance_fingerprint"}, "source provenance")
    payload = {key: _string(value[key], key) for key in sorted(fields)}
    fingerprint = _fp("source_provenance", payload)
    if value["source_provenance_fingerprint"] != fingerprint:
        raise ValueError("source provenance fingerprint is invalid")
    return {**payload, "source_provenance_fingerprint": fingerprint}


def make_source_provenance(**values: str) -> Mapping[str, Any]:
    payload = {key: _string(value, key) for key, value in sorted(values.items())}
    required = {"reviewed_campaign_id", "master_session_key", "detector_id", "cohort", "evidence_tier"}
    if set(payload) != required:
        raise ValueError("source provenance fields are invalid")
    return _freeze({**payload, "source_provenance_fingerprint": _fp("source_provenance", payload)})


def _campaign_scope(value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    if value is None:
        return None
    _exact_keys(value, {"producer_revision", "fingerprint"}, "campaign configuration scope")
    revision = _string(value["producer_revision"], "scope producer revision")
    if "/" not in revision or not _valid_fp(value["fingerprint"]):
        raise ValueError("campaign configuration scope is invalid")
    return _freeze({"producer_revision": revision, "fingerprint": value["fingerprint"]})


def _validate_ready_evidence(transform: Mapping[str, Any], pair: Mapping[str, Any]) -> None:
    expected_transform = {
        "revision", "persisted_context", "offline_context",
        "persisted_context_fingerprint", "offline_context_fingerprint",
        "generator_config_fingerprint", "changed_fields",
    }
    _exact_keys(transform, expected_transform, "context transform")
    before, after = transform["persisted_context"], transform["offline_context"]
    if (
        transform["revision"] != COUNTERFACTUAL_TRANSFORM_REVISION
        or not isinstance(before, Mapping)
        or not isinstance(after, Mapping)
        or _fp("scan_design_context", before) != transform["persisted_context_fingerprint"]
        or _fp("scan_design_context", after) != transform["offline_context_fingerprint"]
        or after.get("scan_window_eig_mode") != "authoritative"
        or tuple(transform["changed_fields"]) not in ((), ("scan_window_eig_mode",))
    ):
        raise ValueError("ready case context transform is invalid")
    actual_changed = {key for key in set(before) | set(after) if before.get(key) != after.get(key)}
    if actual_changed != set(transform["changed_fields"]):
        raise ValueError("ready case context transform changed fields are invalid")
    expected_pair = {
        "revision", "generator", "persisted_legacy", "regenerated_persisted_legacy",
        "shadow_legacy", "target_core", "candidate_pair_fingerprint",
    }
    _exact_keys(pair, expected_pair, "candidate pair")
    actual = validate_offline_candidate_pair(
        pair["persisted_legacy"], pair["shadow_legacy"], pair["target_core"],
        regenerated_persisted_legacy=pair["regenerated_persisted_legacy"],
        generator_identity=pair["generator"],
    )
    if actual["candidate_pair_fingerprint"] != pair["candidate_pair_fingerprint"]:
        raise ValueError("ready case candidate pair fingerprint is invalid")
    if pair["generator"]["config_fingerprint"] != transform["generator_config_fingerprint"]:
        raise ValueError("ready case generator identities disagree")


@dataclass(frozen=True)
class OfflineCaseSnapshot:
    case_identity: str
    decision_snapshot_identity: str
    source_provenance: Mapping[str, Any]
    campaign_configuration_scope: Mapping[str, Any] | None
    qoi_id: str | None
    qoi_unit: str | None
    status: str
    reason_codes: tuple[str, ...] = ()
    source_fingerprints: Mapping[str, Any] = field(default_factory=dict)
    context_transform: Mapping[str, Any] = field(default_factory=dict)
    candidate_pair: Mapping[str, Any] = field(default_factory=dict)
    authority_scopes: tuple[tuple[str, str], ...] = _AUTHORITY
    contract: str = field(default=OFFLINE_CASE_SNAPSHOT_CONTRACT, init=False)
    case_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        provenance = _source_provenance(self.source_provenance)
        expected_case_identity = provenance["source_provenance_fingerprint"]
        scope = _campaign_scope(self.campaign_configuration_scope)
        if (
            self.case_identity != expected_case_identity
            or not _valid_fp(self.decision_snapshot_identity)
            or self.status not in _CASE_STATUSES
        ):
            raise ValueError("offline case identity or status is invalid")
        if (self.qoi_id is None) != (self.qoi_unit is None):
            raise ValueError("QoI identity must be wholly present or absent")
        if self.qoi_id is not None:
            _string(self.qoi_id, "qoi_id")
            _string(self.qoi_unit, "qoi_unit")
        codes = _codes(self.reason_codes)
        if (self.status == _READY) != (not codes):
            raise ValueError("offline case status and reason codes disagree")
        if self.status == _READY and (scope is None or self.qoi_id is None):
            raise ValueError("ready cases require authenticated scope and QoI identities")
        object.__setattr__(self, "source_provenance", _freeze(provenance))
        object.__setattr__(self, "campaign_configuration_scope", scope)
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "source_fingerprints", _mapping(self.source_fingerprints, "source fingerprints"))
        object.__setattr__(self, "context_transform", _mapping(self.context_transform, "context transform"))
        object.__setattr__(self, "candidate_pair", _mapping(self.candidate_pair, "candidate pair"))
        if self.status == _READY:
            _validate_ready_evidence(self.context_transform, self.candidate_pair)
        expected_authority = _AUTHORITY if self.status == _READY else _INELIGIBLE_AUTHORITY
        if self.authority_scopes != expected_authority:
            raise ValueError("offline case authority scopes are invalid")
        object.__setattr__(self, "case_fingerprint", _fp("case", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "case_identity": self.case_identity,
            "decision_snapshot_identity": self.decision_snapshot_identity,
            "source_provenance": _thaw(self.source_provenance),
            "campaign_configuration_scope": _thaw(self.campaign_configuration_scope),
            "qoi_id": self.qoi_id,
            "qoi_unit": self.qoi_unit,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "source_fingerprints": _thaw(self.source_fingerprints),
            "context_transform": _thaw(self.context_transform),
            "candidate_pair": _thaw(self.candidate_pair),
            "authority_scopes": dict(self.authority_scopes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "case_fingerprint": self.case_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineCaseSnapshot":
        expected = set(cls.__dataclass_fields__) - {"case_fingerprint"}
        expected |= {"case_fingerprint"}
        _exact_keys(value, expected, "offline case snapshot")
        if value["contract"] != OFFLINE_CASE_SNAPSHOT_CONTRACT:
            raise ValueError("offline case snapshot contract is unsupported")
        item = cls(
            case_identity=value["case_identity"],
            decision_snapshot_identity=value["decision_snapshot_identity"],
            source_provenance=value["source_provenance"],
            campaign_configuration_scope=value["campaign_configuration_scope"],
            qoi_id=value["qoi_id"],
            qoi_unit=value["qoi_unit"],
            status=value["status"],
            reason_codes=tuple(value["reason_codes"]),
            source_fingerprints=value["source_fingerprints"],
            context_transform=value["context_transform"],
            candidate_pair=value["candidate_pair"],
            authority_scopes=tuple(sorted(dict(value["authority_scopes"]).items())),
        )
        if value["case_fingerprint"] != item.case_fingerprint:
            raise ValueError("case fingerprint is invalid")
        return item


@dataclass(frozen=True)
class OfflineCorpusIndex:
    discovery_config: OfflineDiscoveryConfig
    cases: tuple[OfflineCaseSnapshot, ...]
    diagnostics: Mapping[str, Any] = field(default_factory=dict, compare=False)
    contract: str = field(default=OFFLINE_CORPUS_INDEX_CONTRACT, init=False)
    counts: Mapping[str, int] = field(default_factory=dict, init=False)
    corpus_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        OfflineDiscoveryConfig.from_dict(self.discovery_config.to_dict())
        cases = tuple(self.cases)
        identities = tuple(case.case_identity for case in cases)
        if identities != tuple(sorted(identities)) or len(identities) != len(set(identities)):
            raise ValueError("offline corpus cases must be ordered and unique")
        counts = {status: sum(case.status == status for case in cases) for status in sorted(_CASE_STATUSES)}
        object.__setattr__(self, "cases", cases)
        object.__setattr__(self, "counts", _freeze(counts))
        object.__setattr__(self, "diagnostics", _mapping(self.diagnostics, "corpus diagnostics"))
        payload = {
            "contract": self.contract,
            "discovery_config": self.discovery_config.to_dict(),
            "cases": [case.to_dict() for case in cases],
            "counts": counts,
        }
        object.__setattr__(self, "corpus_fingerprint", _fp("corpus", payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "discovery_config": self.discovery_config.to_dict(),
            "cases": [case.to_dict() for case in self.cases],
            "counts": _thaw(self.counts),
            "corpus_fingerprint": self.corpus_fingerprint,
            "diagnostics": _thaw(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineCorpusIndex":
        expected = {"contract", "discovery_config", "cases", "counts", "corpus_fingerprint", "diagnostics"}
        _exact_keys(value, expected, "offline corpus index")
        if value["contract"] != OFFLINE_CORPUS_INDEX_CONTRACT:
            raise ValueError("offline corpus index contract is unsupported")
        item = cls(
            OfflineDiscoveryConfig.from_dict(value["discovery_config"]),
            tuple(OfflineCaseSnapshot.from_dict(case) for case in value["cases"]),
            value["diagnostics"],
        )
        if dict(value["counts"]) != dict(item.counts) or value["corpus_fingerprint"] != item.corpus_fingerprint:
            raise ValueError("corpus fingerprint is invalid")
        return item


def validate_offline_corpus_index(value: Any) -> OfflineCorpusIndex:
    if not isinstance(value, OfflineCorpusIndex):
        raise TypeError("value must be an OfflineCorpusIndex")
    return OfflineCorpusIndex.from_dict(value.to_dict())


def matching_qoi_policies(case: OfflineCaseSnapshot, policy_set: QoIDistinctionPolicySet) -> tuple[QoIDistinctionPolicy, ...]:
    """Compatibility adapter from an offline case to the shared exact matcher."""
    case_matches = _matching_qoi_policies(
        policy_set,
        qoi_id=case.qoi_id,
        qoi_unit=case.qoi_unit,
        scope_kind="case_identity",
        scope_fingerprint=case.case_identity,
    )
    campaign_scope = case.campaign_configuration_scope
    campaign_matches = _matching_qoi_policies(
        policy_set,
        qoi_id=case.qoi_id,
        qoi_unit=case.qoi_unit,
        scope_kind="campaign_configuration",
        scope_fingerprint=(None if campaign_scope is None else campaign_scope["fingerprint"]),
    )
    return tuple(
        policy for policy in policy_set.policies
        if policy in case_matches or policy in campaign_matches
    )


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
        if (
            any(not isinstance(value, int) or isinstance(value, bool) for value in (self.seed, self.simulation_count, self.block_count))
            or self.simulation_count < 2 or self.block_count < 2
            or self.block_count > self.simulation_count
            or self.simulation_count % self.block_count
            or self.candidate_pair_revision != CANDIDATE_PAIR_REVISION
            or self.policy_compiler_revision != POLICY_COMPILER_REVISION
            or self.scorer_revision != SCORER_REVISION
            or self.likelihood_revision != LIKELIHOOD_REVISION
            or self.crn_revision != CRN_REVISION
        ):
            raise ValueError("offline scoring configuration is invalid")
        object.__setattr__(self, "scoring_config_fingerprint", _fp("scoring_config", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in (
            "contract", "candidate_pair_revision", "policy_compiler_revision", "scorer_revision",
            "likelihood_revision", "crn_revision", "seed", "simulation_count", "block_count")}

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "scoring_config_fingerprint": self.scoring_config_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineScoringConfig":
        expected = set(cls(1, 2, 2).to_dict())
        _exact_keys(value, expected, "offline scoring configuration")
        if value["contract"] != OFFLINE_SCORING_CONFIG_CONTRACT:
            raise ValueError("offline scoring configuration contract is unsupported")
        item = cls(**{key: value[key] for key in expected - {"contract", "scoring_config_fingerprint"}})
        if value["scoring_config_fingerprint"] != item.scoring_config_fingerprint:
            raise ValueError("scoring configuration fingerprint is invalid")
        return item


def _candidate_summary(view: Mapping[str, Any]) -> dict[str, Any]:
    candidate = _candidate_identity(view)
    return {
        "candidate_id": candidate["candidate_id"], "family": candidate["family"],
        "point_count": len(candidate["energy_meV"]), "exposure_per_point": candidate["exposure_per_point"],
    }


def _score_summary(score: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if score is None:
        return None
    keys = {
        "candidate_id", "status", "reason_codes", "source_reason_code", "candidate_score_fingerprint",
        "executed_point_count", "exposure_per_point", "conditional_target_qoi_information_nats",
        "conditional_target_qoi_information_standard_error_nats", "information_per_executed_point_nats",
        "information_per_executed_point_standard_error_nats",
    }
    return {key: _thaw(score.get(key)) for key in sorted(keys)}


@dataclass(frozen=True)
class OfflineScoreEvidence:
    status: str
    policy_fingerprint: str
    variant_fingerprint: str
    compiled_partition: Mapping[str, Any] | None
    candidate_pair_fingerprint: str
    legacy_candidate: Mapping[str, Any]
    target_candidate: Mapping[str, Any]
    score_set: Mapping[str, Any] | None = None
    legacy_score: Mapping[str, Any] | None = None
    target_score: Mapping[str, Any] | None = None
    pairwise_comparison: Mapping[str, Any] | None = None
    target_core_minus_legacy: Mapping[str, Any] | None = None
    source_reason_codes: tuple[str, ...] = ()
    contract: str = field(default=OFFLINE_SCORE_EVIDENCE_CONTRACT, init=False)
    evidence_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.status not in {"complete", "unavailable"} or not all(_valid_fp(value) for value in (self.policy_fingerprint, self.variant_fingerprint, self.candidate_pair_fingerprint)):
            raise ValueError("offline score evidence metadata is invalid")
        for name in ("legacy_candidate", "target_candidate"):
            value = getattr(self, name)
            _exact_keys(value, {"candidate_id", "family", "point_count", "exposure_per_point"}, name)
            if not isinstance(value["point_count"], int) or value["point_count"] < 1 or not _finite(value["exposure_per_point"]):
                raise ValueError("offline score candidate summary is invalid")
            object.__setattr__(self, name, _freeze(value))
        if self.compiled_partition is not None:
            partition = ScalarQoIDiscretization(
                qoi_id=self.compiled_partition["qoi_id"], qoi_unit=self.compiled_partition["qoi_unit"],
                origin=self.compiled_partition["origin"], bin_width=self.compiled_partition["bin_width"],
                offsets=tuple(self.compiled_partition["offsets"]),
            )
            if partition.to_dict() != _thaw(self.compiled_partition):
                raise ValueError("compiled partition evidence is invalid")
        if self.score_set is not None:
            _exact_keys(
                self.score_set,
                {"contract", "status", "reason_codes", "score_set_fingerprint"},
                "score-set evidence",
            )
            if (self.score_set["status"] not in {"complete", "partial", "unavailable"}
                    or not _valid_fp(self.score_set["score_set_fingerprint"])
                    or tuple(self.score_set["reason_codes"])
                    != _codes(tuple(self.score_set["reason_codes"]))):
                raise ValueError("score-set evidence is invalid")
        score_keys = {
            "candidate_id", "status", "reason_codes", "source_reason_code",
            "candidate_score_fingerprint", "executed_point_count", "exposure_per_point",
            "conditional_target_qoi_information_nats",
            "conditional_target_qoi_information_standard_error_nats",
            "information_per_executed_point_nats",
            "information_per_executed_point_standard_error_nats",
        }
        for name, candidate_name in (
                ("legacy_score", "legacy_candidate"), ("target_score", "target_candidate")):
            score = getattr(self, name)
            if score is None:
                continue
            _exact_keys(score, score_keys, name)
            candidate = getattr(self, candidate_name)
            reasons = _codes(tuple(score["reason_codes"]))
            if (score["candidate_id"] != candidate["candidate_id"]
                    or score["status"] not in {"available", "unavailable"}
                    or not _valid_fp(score["candidate_score_fingerprint"])):
                raise ValueError("candidate score evidence identity is invalid")
            numeric_names = (
                "conditional_target_qoi_information_nats",
                "conditional_target_qoi_information_standard_error_nats",
                "information_per_executed_point_nats",
                "information_per_executed_point_standard_error_nats",
            )
            if score["status"] == "available":
                if (reasons or score["executed_point_count"] != candidate["point_count"]
                        or float(score["exposure_per_point"]).hex()
                        != float(candidate["exposure_per_point"]).hex()
                        or any(not _finite(score[key]) for key in numeric_names)):
                    raise ValueError("available candidate score evidence is invalid")
            elif not reasons or any(score[key] is not None for key in numeric_names):
                raise ValueError("unavailable candidate score evidence is invalid")
        if self.pairwise_comparison is not None:
            pair_keys = {
                "candidate_a_id", "candidate_b_id", "candidate_a_score_fingerprint",
                "candidate_b_score_fingerprint", "delta_information_nats",
                "delta_information_standard_error_nats",
                "delta_information_per_executed_point_nats",
                "delta_information_per_executed_point_standard_error_nats",
                "paired_block_deltas", "paired_block_deltas_per_executed_point_nats",
                "pairwise_comparison_fingerprint",
            }
            _exact_keys(self.pairwise_comparison, pair_keys, "pairwise comparison evidence")
            pair_ids = {self.pairwise_comparison["candidate_a_id"],
                        self.pairwise_comparison["candidate_b_id"]}
            expected_ids = {self.legacy_candidate["candidate_id"],
                            self.target_candidate["candidate_id"]}
            if pair_ids != expected_ids or not _valid_fp(
                    self.pairwise_comparison["pairwise_comparison_fingerprint"]):
                raise ValueError("pairwise comparison evidence is invalid")
        if self.target_core_minus_legacy is not None:
            delta_keys = {
                "delta_information_nats", "delta_information_standard_error_nats",
                "delta_information_per_executed_point_nats",
                "delta_information_per_executed_point_standard_error_nats",
            }
            _exact_keys(self.target_core_minus_legacy, delta_keys, "oriented delta evidence")
            if (any(not _finite(value) for value in self.target_core_minus_legacy.values())
                    or self.target_core_minus_legacy[
                        "delta_information_standard_error_nats"] < 0.0
                    or self.target_core_minus_legacy[
                        "delta_information_per_executed_point_standard_error_nats"] < 0.0):
                raise ValueError("oriented delta evidence is invalid")
        codes = _codes(self.source_reason_codes)
        if self.status == "complete":
            if codes or any(value is None for value in (self.compiled_partition, self.score_set, self.legacy_score, self.target_score, self.pairwise_comparison, self.target_core_minus_legacy)):
                raise ValueError("complete score evidence is incomplete")
            if self.score_set["status"] != "complete":
                raise ValueError("complete score evidence has an incomplete score set")
        elif not codes or self.target_core_minus_legacy is not None:
            raise ValueError("unavailable score evidence is invalid")
        object.__setattr__(self, "source_reason_codes", codes)
        for name in ("compiled_partition", "score_set", "legacy_score", "target_score", "pairwise_comparison", "target_core_minus_legacy"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _freeze(value))
        object.__setattr__(self, "evidence_fingerprint", _fp("score_evidence", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract, "status": self.status,
            "policy_fingerprint": self.policy_fingerprint, "variant_fingerprint": self.variant_fingerprint,
            "compiled_partition": _thaw(self.compiled_partition),
            "candidate_pair_fingerprint": self.candidate_pair_fingerprint,
            "legacy_candidate": _thaw(self.legacy_candidate), "target_candidate": _thaw(self.target_candidate),
            "score_set": _thaw(self.score_set), "legacy_score": _thaw(self.legacy_score),
            "target_score": _thaw(self.target_score), "pairwise_comparison": _thaw(self.pairwise_comparison),
            "target_core_minus_legacy": _thaw(self.target_core_minus_legacy),
            "source_reason_codes": list(self.source_reason_codes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "evidence_fingerprint": self.evidence_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineScoreEvidence":
        expected = set(cls.__dataclass_fields__)
        _exact_keys(value, expected, "offline score evidence")
        if value["contract"] != OFFLINE_SCORE_EVIDENCE_CONTRACT:
            raise ValueError("offline score evidence contract is unsupported")
        kwargs = {key: value[key] for key in expected - {"contract", "evidence_fingerprint"}}
        kwargs["source_reason_codes"] = tuple(kwargs["source_reason_codes"])
        item = cls(**kwargs)
        if value["evidence_fingerprint"] != item.evidence_fingerprint:
            raise ValueError("score evidence fingerprint is invalid")
        return item


def _pre_score_evidence(case: OfflineCaseSnapshot, policy: QoIDistinctionPolicy, variant: QoIDistinctionVariant, partition: ScalarQoIDiscretization | None, codes: Sequence[str]) -> OfflineScoreEvidence:
    pair = case.candidate_pair
    return OfflineScoreEvidence(
        status="unavailable", policy_fingerprint=policy.policy_fingerprint,
        variant_fingerprint=variant.variant_fingerprint,
        compiled_partition=None if partition is None else partition.to_dict(),
        candidate_pair_fingerprint=pair["candidate_pair_fingerprint"],
        legacy_candidate=_candidate_summary(pair["shadow_legacy"]),
        target_candidate=_candidate_summary(pair["target_core"]),
        source_reason_codes=tuple(codes),
    )


def _score_evidence(case: OfflineCaseSnapshot, policy: QoIDistinctionPolicy, variant: QoIDistinctionVariant, partition: ScalarQoIDiscretization, score_set: Any) -> tuple[OfflineScoreEvidence, tuple[str, ...], Mapping[str, Any] | None]:
    pair = case.candidate_pair
    legacy_id = pair["shadow_legacy"]["candidate_id"]
    target_id = pair["target_core"]["candidate_id"]
    scores = {score["candidate_id"]: score for score in score_set.candidate_scores}
    comparison = next((item for item in score_set.pairwise_comparisons if {item["candidate_a_id"], item["candidate_b_id"]} == {legacy_id, target_id}), None)
    codes = ["score_set_incomplete"]
    codes.extend(score_set.reason_codes)
    for score in score_set.candidate_scores:
        codes.extend(score["reason_codes"])
        if score["source_reason_code"]:
            codes.append(score["source_reason_code"])
    codes = list(dict.fromkeys(codes))
    score_set_summary = {"contract": score_set.contract, "status": score_set.status, "reason_codes": list(score_set.reason_codes), "score_set_fingerprint": score_set.score_set_fingerprint}
    common = dict(
        policy_fingerprint=policy.policy_fingerprint, variant_fingerprint=variant.variant_fingerprint,
        compiled_partition=partition.to_dict(), candidate_pair_fingerprint=pair["candidate_pair_fingerprint"],
        legacy_candidate=_candidate_summary(pair["shadow_legacy"]), target_candidate=_candidate_summary(pair["target_core"]),
        score_set=score_set_summary, legacy_score=_score_summary(scores.get(legacy_id)), target_score=_score_summary(scores.get(target_id)),
    )
    if score_set.status != "complete" or comparison is None:
        return OfflineScoreEvidence(status="unavailable", source_reason_codes=tuple(codes), **common), tuple(codes), None
    sign = 1.0 if comparison["candidate_b_id"] == target_id else -1.0
    deltas = {
        "delta_information_nats": sign * comparison["delta_information_nats"],
        "delta_information_standard_error_nats": comparison["delta_information_standard_error_nats"],
        "delta_information_per_executed_point_nats": sign * comparison["delta_information_per_executed_point_nats"],
        "delta_information_per_executed_point_standard_error_nats": comparison["delta_information_per_executed_point_standard_error_nats"],
    }
    evidence = OfflineScoreEvidence(
        status="complete", source_reason_codes=(), pairwise_comparison=_thaw(comparison),
        target_core_minus_legacy=deltas, **common,
    )
    return evidence, (), deltas


@dataclass(frozen=True)
class OfflineComparisonResult:
    case_identity: str
    policy_id: str
    variant_id: str
    status: str
    reason_codes: tuple[str, ...]
    score_evidence: OfflineScoreEvidence | None = None
    delta_information_nats: float | None = None
    delta_information_standard_error_nats: float | None = None
    delta_information_per_executed_point_nats: float | None = None
    delta_information_per_executed_point_standard_error_nats: float | None = None
    authority_scopes: tuple[tuple[str, str], ...] = _AUTHORITY
    contract: str = field(default=OFFLINE_COMPARISON_RESULT_CONTRACT, init=False)
    result_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if not _valid_fp(self.case_identity) or self.status not in {"complete", "unavailable"} or self.authority_scopes != _AUTHORITY:
            raise ValueError("offline comparison result metadata is invalid")
        codes = _codes(self.reason_codes)
        values = (
            self.delta_information_nats, self.delta_information_standard_error_nats,
            self.delta_information_per_executed_point_nats,
            self.delta_information_per_executed_point_standard_error_nats,
        )
        if self.status == "complete":
            _string(self.policy_id, "policy_id")
            _string(self.variant_id, "variant_id")
            if codes or self.score_evidence is None or self.score_evidence.status != "complete" or any(not _finite(value) for value in values):
                raise ValueError("complete comparison result is invalid")
            evidence_delta = self.score_evidence.target_core_minus_legacy
            names = (
                "delta_information_nats", "delta_information_standard_error_nats",
                "delta_information_per_executed_point_nats", "delta_information_per_executed_point_standard_error_nats",
            )
            if any(not math.isclose(float(value), float(evidence_delta[name]), rel_tol=1e-12, abs_tol=1e-12) for value, name in zip(values, names)):
                raise ValueError("comparison deltas disagree with score evidence")
        elif not codes or any(value is not None for value in values):
            raise ValueError("unavailable comparison result is invalid")
        if self.score_evidence is not None and not isinstance(self.score_evidence, OfflineScoreEvidence):
            raise TypeError("score evidence is invalid")
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "result_fingerprint", _fp("result", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract, "case_identity": self.case_identity,
            "policy_id": self.policy_id, "variant_id": self.variant_id,
            "status": self.status, "reason_codes": list(self.reason_codes),
            "score_evidence": None if self.score_evidence is None else self.score_evidence.to_dict(),
            "delta_information_nats": self.delta_information_nats,
            "delta_information_standard_error_nats": self.delta_information_standard_error_nats,
            "delta_information_per_executed_point_nats": self.delta_information_per_executed_point_nats,
            "delta_information_per_executed_point_standard_error_nats": self.delta_information_per_executed_point_standard_error_nats,
            "authority_scopes": dict(self.authority_scopes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "result_fingerprint": self.result_fingerprint}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineComparisonResult":
        expected = set(cls.__dataclass_fields__)
        _exact_keys(value, expected, "offline comparison result")
        if value["contract"] != OFFLINE_COMPARISON_RESULT_CONTRACT:
            raise ValueError("offline comparison result contract is unsupported")
        item = cls(
            case_identity=value["case_identity"], policy_id=value["policy_id"], variant_id=value["variant_id"],
            status=value["status"], reason_codes=tuple(value["reason_codes"]),
            score_evidence=None if value["score_evidence"] is None else OfflineScoreEvidence.from_dict(value["score_evidence"]),
            delta_information_nats=value["delta_information_nats"],
            delta_information_standard_error_nats=value["delta_information_standard_error_nats"],
            delta_information_per_executed_point_nats=value["delta_information_per_executed_point_nats"],
            delta_information_per_executed_point_standard_error_nats=value["delta_information_per_executed_point_standard_error_nats"],
            authority_scopes=tuple(sorted(dict(value["authority_scopes"]).items())),
        )
        if value["result_fingerprint"] != item.result_fingerprint:
            raise ValueError("result fingerprint is invalid")
        return item


@dataclass(frozen=True)
class OfflineComparisonReport:
    corpus_fingerprint: str
    policy_set_fingerprint: str
    scoring_config_fingerprint: str
    status: str
    results: tuple[OfflineComparisonResult, ...]
    diagnostics: Mapping[str, Any] = field(default_factory=dict, compare=False)
    contract: str = field(default=OFFLINE_COMPARISON_REPORT_CONTRACT, init=False)
    counts: Mapping[str, int] = field(default_factory=dict, init=False)
    authority_scopes: tuple[tuple[str, str], ...] = field(default=_INELIGIBLE_AUTHORITY, init=False)
    report_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.status not in {"complete", "partial", "unavailable"} or not all(_valid_fp(value) for value in (self.corpus_fingerprint, self.policy_set_fingerprint, self.scoring_config_fingerprint)):
            raise ValueError("offline report metadata is invalid")
        results = tuple(self.results)
        keys = tuple((result.case_identity, result.policy_id, result.variant_id) for result in results)
        if any(not isinstance(result, OfflineComparisonResult) for result in results) or keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise ValueError("offline report results must be ordered and unique")
        complete = sum(result.status == "complete" for result in results)
        unavailable = sum(result.status == "unavailable" for result in results)
        diagnostics = _mapping(self.diagnostics, "report diagnostics")
        global_refusal = bool(dict(diagnostics).get("reason_codes"))
        valid_status = (
            (self.status == "complete" and unavailable == 0 and not global_refusal)
            or (self.status == "partial" and complete > 0 and unavailable > 0 and not global_refusal)
            or (self.status == "unavailable" and complete == 0 and (unavailable > 0 or global_refusal))
        )
        if not valid_status:
            raise ValueError("offline report status is inconsistent with its results")
        authority = _INELIGIBLE_AUTHORITY if self.status == "unavailable" else _AUTHORITY
        counts = {"complete": complete, "unavailable": unavailable}
        object.__setattr__(self, "results", results)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "authority_scopes", authority)
        object.__setattr__(self, "counts", _freeze(counts))
        object.__setattr__(self, "report_fingerprint", _fp("report", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract, "corpus_fingerprint": self.corpus_fingerprint,
            "policy_set_fingerprint": self.policy_set_fingerprint,
            "scoring_config_fingerprint": self.scoring_config_fingerprint, "status": self.status,
            "results": [result.to_dict() for result in self.results], "counts": _thaw(self.counts),
            "authority_scopes": dict(self.authority_scopes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {**self._payload(), "report_fingerprint": self.report_fingerprint, "diagnostics": _thaw(self.diagnostics)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OfflineComparisonReport":
        expected = {"contract", "corpus_fingerprint", "policy_set_fingerprint", "scoring_config_fingerprint", "status", "results", "counts", "authority_scopes", "report_fingerprint", "diagnostics"}
        _exact_keys(value, expected, "offline comparison report")
        if value["contract"] != OFFLINE_COMPARISON_REPORT_CONTRACT:
            raise ValueError("offline comparison report contract is unsupported")
        item = cls(
            value["corpus_fingerprint"], value["policy_set_fingerprint"], value["scoring_config_fingerprint"],
            value["status"], tuple(OfflineComparisonResult.from_dict(result) for result in value["results"]),
            value["diagnostics"],
        )
        if dict(value["counts"]) != dict(item.counts) or dict(value["authority_scopes"]) != dict(item.authority_scopes) or value["report_fingerprint"] != item.report_fingerprint:
            raise ValueError("report_fingerprint is invalid")
        return item


def validate_offline_comparison_report(value: Any) -> OfflineComparisonReport:
    if not isinstance(value, OfflineComparisonReport):
        raise TypeError("value must be an OfflineComparisonReport")
    return OfflineComparisonReport.from_dict(value.to_dict())


def _validated_scientific_case(case: OfflineCaseSnapshot, value: Any) -> tuple[ScanWindowDesignMeasure, tuple[CandidateEvaluationInput, ...]]:
    if value is None:
        raise LookupError("offline_scientific_case_missing")
    if not isinstance(value, tuple) or len(value) != 2 or not isinstance(value[1], Sequence):
        raise ValueError("offline_scientific_case_mismatch")
    try:
        measure = validate_scan_window_design_measure(value[0])
    except (TypeError, ValueError) as error:
        raise ValueError("offline_scientific_case_mismatch") from error
    candidates = tuple(value[1])
    if len(candidates) != 2 or any(not isinstance(item, CandidateEvaluationInput) for item in candidates):
        raise ValueError("offline_scientific_case_mismatch")
    expected = dict(case.source_fingerprints)
    qoi = dict(measure.qoi)
    if (
        expected.get("design_measure") != measure.design_measure_fingerprint
        or expected.get("source_draw_identity") != measure.source_draw_identity_fingerprint
        or expected.get("predictive_basis") != dict(measure.source_identities).get("predictive_basis_fingerprint")
        or qoi.get("id") != case.qoi_id or qoi.get("unit") != case.qoi_unit
    ):
        raise ValueError("offline_scientific_case_mismatch")
    pair = case.candidate_pair
    views = {pair[name]["candidate_id"]: pair[name] for name in ("shadow_legacy", "target_core")}
    if {item.candidate_id for item in candidates} != set(views):
        raise ValueError("offline_scientific_case_mismatch")
    for item in candidates:
        evaluation, view = item.evaluation, views[item.candidate_id]
        if getattr(evaluation, "energy_meV", ()) and tuple(evaluation.energy_meV) != tuple(view["energy_meV"]):
            raise ValueError("offline_scientific_case_mismatch")
        if getattr(evaluation, "exposure_per_point", None) is not None and float(evaluation.exposure_per_point) != float(view["exposure_per_point"]):
            raise ValueError("offline_scientific_case_mismatch")
        basis = getattr(evaluation, "predictive_basis_fingerprint", None)
        if basis is not None and basis != expected.get("predictive_basis"):
            raise ValueError("offline_scientific_case_mismatch")
    return measure, candidates


ScientificCaseProvider = Callable[[OfflineCaseSnapshot], Any]


def score_offline_comparison(
    corpus: OfflineCorpusIndex,
    policies: QoIDistinctionPolicySet,
    config: OfflineScoringConfig,
    scientific_case_provider: ScientificCaseProvider,
) -> OfflineComparisonReport:
    """Resolve policies before lazily requesting each case's two evaluations."""
    validate_offline_corpus_index(corpus)
    validate_qoi_distinction_policy_set(policies)
    OfflineScoringConfig.from_dict(config.to_dict())
    if not callable(scientific_case_provider):
        raise TypeError("scientific_case_provider must be callable")
    results: list[OfflineComparisonResult] = []
    for case in corpus.cases:
        if case.status != _READY:
            continue
        matches = matching_qoi_policies(case, policies)
        if not matches:
            results.append(OfflineComparisonResult(case.case_identity, "", "", "unavailable", ("qoi_policy_missing",)))
            continue
        if len(matches) != 1:
            results.append(OfflineComparisonResult(case.case_identity, "", "", "unavailable", ("qoi_policy_ambiguous",)))
            continue
        policy = matches[0]
        compiled: list[tuple[QoIDistinctionVariant, ScalarQoIDiscretization]] = []
        for variant in policy.variants:
            try:
                compiled.append((variant, compile_qoi_distinction_policy(policy, variant)))
            except ValueError as error:
                if str(error) != "qoi_policy_unsupported":
                    raise
                evidence = _pre_score_evidence(case, policy, variant, None, ("qoi_policy_unsupported",))
                results.append(OfflineComparisonResult(case.case_identity, policy.policy_id, variant.variant_id, "unavailable", ("qoi_policy_unsupported",), evidence))
        if not compiled:
            continue
        supplied = scientific_case_provider(case)
        try:
            measure, candidates = _validated_scientific_case(case, supplied)
        except LookupError:
            for variant, partition in compiled:
                evidence = _pre_score_evidence(case, policy, variant, partition, ("offline_scientific_case_missing",))
                results.append(OfflineComparisonResult(case.case_identity, policy.policy_id, variant.variant_id, "unavailable", ("offline_scientific_case_missing",), evidence))
            continue
        except (TypeError, ValueError):
            for variant, partition in compiled:
                evidence = _pre_score_evidence(case, policy, variant, partition, ("offline_scientific_case_mismatch",))
                results.append(OfflineComparisonResult(case.case_identity, policy.policy_id, variant.variant_id, "unavailable", ("offline_scientific_case_mismatch",), evidence))
            continue
        for variant, partition in compiled:
            score_set = validate_scan_window_score_set(score_scan_window_candidates(
                measure, candidates, qoi_discretization=partition, seed=config.seed,
                simulation_count=config.simulation_count, block_count=config.block_count,
            ))
            evidence, codes, deltas = _score_evidence(case, policy, variant, partition, score_set)
            if deltas is None:
                results.append(OfflineComparisonResult(case.case_identity, policy.policy_id, variant.variant_id, "unavailable", codes, evidence))
            else:
                results.append(OfflineComparisonResult(
                    case.case_identity, policy.policy_id, variant.variant_id, "complete", (), evidence,
                    deltas["delta_information_nats"], deltas["delta_information_standard_error_nats"],
                    deltas["delta_information_per_executed_point_nats"],
                    deltas["delta_information_per_executed_point_standard_error_nats"],
                ))
    ordered = tuple(sorted(results, key=lambda result: (result.case_identity, result.policy_id, result.variant_id)))
    status = "complete" if not ordered or all(result.status == "complete" for result in ordered) else "partial" if any(result.status == "complete" for result in ordered) else "unavailable"
    return OfflineComparisonReport(
        corpus.corpus_fingerprint, policies.policy_set_fingerprint,
        config.scoring_config_fingerprint, status, ordered,
    )


__all__ = [
    "OfflineDiscoveryConfig", "OfflineCaseSnapshot", "OfflineCorpusIndex",
    "QoIDistinctionVariant", "QoIDistinctionPolicy", "QoIDistinctionPolicySet",
    "OfflineScoringConfig", "OfflineScoreEvidence", "OfflineComparisonResult",
    "OfflineComparisonReport", "make_source_provenance", "offline_shadow_context_transform",
    "validate_offline_candidate_pair", "validate_offline_corpus_index",
    "validate_qoi_distinction_policy_set", "validate_offline_comparison_report",
    "matching_qoi_policies", "compile_qoi_distinction_policy", "score_offline_comparison",
]
