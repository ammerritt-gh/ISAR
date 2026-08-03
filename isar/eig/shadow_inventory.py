"""Pure Slice 6 scoring and authenticated evidence for scan-design inventories.

This module deliberately knows nothing about workspaces, acquisition planning, or selection.  It
accepts one already-built provider-neutral inventory plus authenticated Slice 2--5 inputs, evaluates
only the legacy/target-core pair, and returns diagnostic evidence.  The acquisition layer remains
responsible for attaching that evidence and continues to execute the legacy control.
"""
from __future__ import annotations

import hashlib
import logging
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Mapping, Sequence

from isar.predictive import evaluate_master_predictive
from isar.schema.statistics import semantics_fingerprint

from .design_measure import build_scan_window_design_measure, validate_scan_window_design_measure
from .eligibility import (
    ELIGIBILITY_CONTRACT,
    OBSERVABILITY_REASON_ORDER,
    SCIENTIFIC_REASON_ORDER,
    EligibilityInput,
)
from .policy import compile_qoi_distinction_policy
from .scorer import (
    CandidateEvaluationInput,
    ScalarQoIDiscretization,
    score_scan_window_candidates,
    validate_scan_window_score_set,
)
from isar.schema.eig_policy import (
    MAX_QOI_POLICIES,
    QoIDistinctionPolicySet,
    ScanWindowShadowScoringConfig,
    validate_scan_window_shadow_scoring_config,
    matching_qoi_policies,
    validate_qoi_distinction_policy_set,
)


SHADOW_SCORING_CONTRACT = "isar.scan_design_shadow_scoring/o36.6"
SHADOW_VARIANT_CONTRACT = "isar.scan_design_shadow_variant_score/o36.6"
SHADOW_SEED_CONTRACT = "isar.scan_window_shadow_seed_evidence/o36.6"
SHADOW_SEED_DOMAIN = "isar.scan_window_shadow_seed/o36.6"
SHADOW_SEED_ALGORITHM = "sha256-first-8-bytes-big-endian-signed-63-bit"

STATUS_NOT_REQUESTED = "not_requested"
STATUS_COMPLETE = "complete"
STATUS_PARTIAL = "partial"
STATUS_UNAVAILABLE = "unavailable"

_DIAGNOSTIC_AUTHORITY = (
    ("acquisition_selection", "ineligible"),
    ("eig_authoritative", "ineligible"),
    ("eig_shadow", "diagnostic"),
)
_INELIGIBLE_AUTHORITY = (
    ("acquisition_selection", "ineligible"),
    ("eig_authoritative", "ineligible"),
    ("eig_shadow", "ineligible"),
)
_FAMILY_LEGACY = "legacy_uniform/1"
_FAMILY_TARGET = "target_core/1"
_FORBIDDEN_DECISION_FIELDS = frozenset({
    "winner", "winning_candidate", "recommended_candidate", "recommendation",
    "threshold", "selection_threshold", "eig_selected_candidate_id",
})
log = logging.getLogger(__name__)


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


def _exact_keys(value: Any, expected: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError(f"{label} fields are invalid")
    return value


def _valid_fingerprint(value: Any) -> bool:
    return (isinstance(value, str) and len(value) == 64 and value == value.lower()
            and all(character in "0123456789abcdef" for character in value))


def _codes(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(str(value) for value in values)
    if (any(not value or value != value.strip() for value in result)
            or len(result) != len(set(result))):
        raise ValueError("reason codes must be unique non-empty strings")
    return result


def _fingerprint(domain: str, payload: Any) -> str:
    return semantics_fingerprint(domain, _thaw(payload))


def _semantic_payload(value: Mapping[str, Any], fingerprint_field: str,
                      *, diagnostics_field: str = "diagnostics") -> dict[str, Any]:
    payload = _thaw(value)
    payload.pop(fingerprint_field)
    payload.pop(diagnostics_field, None)
    return payload


def _contains_forbidden_field(value: Any) -> bool:
    if isinstance(value, Mapping):
        return (bool(set(value) & _FORBIDDEN_DECISION_FIELDS)
                or any(_contains_forbidden_field(item) for item in value.values()))
    if isinstance(value, (list, tuple)):
        return any(_contains_forbidden_field(item) for item in value)
    return False


def _validate_eligibility_summary(value: Mapping[str, Any]) -> None:
    expected = {
        "contract", "status", "scientific_codes", "observability_codes",
        "input_fingerprint", "result_fingerprint",
    }
    _exact_keys(value, expected, "EIG eligibility summary")
    science = value["scientific_codes"]
    gaps = value["observability_codes"]
    if (value["contract"] != ELIGIBILITY_CONTRACT
            or not isinstance(science, list) or not isinstance(gaps, list)
            or any(code not in SCIENTIFIC_REASON_ORDER for code in science)
            or any(code not in OBSERVABILITY_REASON_ORDER for code in gaps)
            or science != sorted(set(science), key=SCIENTIFIC_REASON_ORDER.index)
            or gaps != sorted(set(gaps), key=OBSERVABILITY_REASON_ORDER.index)
            or not _valid_fingerprint(value["input_fingerprint"])
            or not _valid_fingerprint(value["result_fingerprint"])):
        raise ValueError("EIG eligibility summary is invalid")
    status = "ineligible" if science else "unassessable" if gaps else "eligible"
    scopes = ({"eig_shadow": "candidate", "eig_authoritative": "ineligible",
               "acquisition_selection": "ineligible"}
              if status == "eligible" else {
                  "eig_shadow": "ineligible", "eig_authoritative": "ineligible",
                  "acquisition_selection": "ineligible"})
    if value["status"] != status:
        raise ValueError("EIG eligibility status is invalid")
    semantic = {
        "contract": value["contract"], "status": status,
        "scientific_codes": science, "observability_codes": gaps,
        "input_fingerprint": value["input_fingerprint"], "authority_scopes": scopes,
    }
    import json
    encoded = json.dumps(semantic, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != value["result_fingerprint"]:
        raise ValueError("EIG eligibility result fingerprint is invalid")


def _without_diagnostics(value: Any) -> Any:
    """Remove non-semantic diagnostic/prose fields at every nested evidence level."""
    if isinstance(value, Mapping):
        return {
            str(key): _without_diagnostics(item)
            for key, item in value.items()
            if key not in {"diagnostics", "message"}
        }
    if isinstance(value, (list, tuple)):
        return [_without_diagnostics(item) for item in value]
    return value


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
        if (not all(_valid_fingerprint(value) for value in (
                self.scoring_config_fingerprint, self.design_measure_fingerprint,
                self.candidate_pair_fingerprint))
                or self.seed_derivation_revision != SHADOW_SEED_DOMAIN
                or not isinstance(self.seed, int) or isinstance(self.seed, bool)
                or self.seed < 0 or self.seed >= (1 << 63)):
            raise ValueError("shadow seed evidence is invalid")
        object.__setattr__(self, "seed_evidence_fingerprint", _fingerprint(
            "isar.scan_window_shadow_seed_evidence_fingerprint/o36.6", self._payload()))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "domain": self.seed_derivation_revision,
            "algorithm": self.algorithm,
            "digest_prefix_bytes": self.digest_prefix_bytes,
            "signed_mask_bits": self.signed_mask_bits,
            "scoring_config_fingerprint": self.scoring_config_fingerprint,
            "design_measure_fingerprint": self.design_measure_fingerprint,
            "candidate_pair_fingerprint": self.candidate_pair_fingerprint,
            "seed": self.seed,
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
            seed=value["seed"],
            seed_derivation_revision=value["domain"],
        )
        if value["seed_evidence_fingerprint"] != item.seed_evidence_fingerprint:
            raise ValueError("seed_evidence_fingerprint does not authenticate seed evidence")
        return item


def derive_shadow_seed(*, scoring_config_fingerprint: str,
                       design_measure_fingerprint: str,
                       candidate_pair_fingerprint: str,
                       seed_derivation_revision: str = SHADOW_SEED_DOMAIN) -> ShadowSeedEvidence:
    """Derive the frozen signed-63-bit Slice 6 seed."""
    if seed_derivation_revision != SHADOW_SEED_DOMAIN:
        raise ValueError("shadow seed derivation revision is unsupported")
    payload = {
        "domain": SHADOW_SEED_DOMAIN,
        "scoring_config_fingerprint": scoring_config_fingerprint,
        "design_measure_fingerprint": design_measure_fingerprint,
        "candidate_pair_fingerprint": candidate_pair_fingerprint,
    }
    import json
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                         allow_nan=False).encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(encoded).digest()[:8], "big", signed=False)
    seed &= (1 << 63) - 1
    return ShadowSeedEvidence(
        scoring_config_fingerprint=scoring_config_fingerprint,
        design_measure_fingerprint=design_measure_fingerprint,
        candidate_pair_fingerprint=candidate_pair_fingerprint,
        seed=seed,
        seed_derivation_revision=seed_derivation_revision,
    )


def _validate_score_set_mapping(value: Mapping[str, Any]) -> None:
    """Authenticate persisted Slice 4 evidence without requiring current backend versions."""
    expected = {
        "contract", "scorer_revision", "likelihood_revision", "crn_revision", "status",
        "reason_codes", "design_measure_fingerprint", "source_draw_identity_fingerprint",
        "qoi_discretization_fingerprint", "seed", "simulation_count", "block_count",
        "numerical_backend", "candidate_scores", "pairwise_comparisons", "authority_scopes",
        "score_set_fingerprint", "diagnostics",
    }
    _exact_keys(value, expected, "scan-window score set")
    if (value["contract"] != "isar.scan_window_eig_score_set/o36.4"
            or value["scorer_revision"] != "isar.full_vector_poisson_mixture_scorer/o36.4"
            or value["likelihood_revision"] != "isar.full_vector_poisson_likelihood/1"
            or value["crn_revision"] != "isar.sha256_energy_keyed_poisson_crn/1"
            or value["status"] != "complete" or value["reason_codes"] != []
            or dict(value["authority_scopes"]) != {
                "acquisition_selection": "ineligible", "eig_authoritative": "ineligible",
                "eig_shadow": "diagnostic"}
            or not all(_valid_fingerprint(value[name]) for name in (
                "design_measure_fingerprint", "source_draw_identity_fingerprint",
                "qoi_discretization_fingerprint"))
            or not isinstance(value["seed"], int) or isinstance(value["seed"], bool)
            or not isinstance(value["simulation_count"], int)
            or not isinstance(value["block_count"], int)
            or value["simulation_count"] < 2 or value["block_count"] < 2
            or value["block_count"] > value["simulation_count"]
            or value["simulation_count"] % value["block_count"]):
        raise ValueError("scan-window score-set contract is unsupported")
    backend = value["numerical_backend"]
    if (not isinstance(backend, Mapping)
            or set(backend) != {"numpy_version", "scipy_version", "poisson_inverse_cdf",
                               "mixture_logsumexp"}
            or any(not isinstance(item, str) or not item.strip() for item in backend.values())):
        raise ValueError("score-set numerical backend identities are invalid")
    common = {
        "scorer_revision": value["scorer_revision"],
        "likelihood_revision": value["likelihood_revision"],
        "crn_revision": value["crn_revision"],
        "design_measure_fingerprint": value["design_measure_fingerprint"],
        "qoi_discretization_fingerprint": value["qoi_discretization_fingerprint"],
        "seed": value["seed"], "simulation_count": value["simulation_count"],
        "block_count": value["block_count"], "numerical_backend": dict(backend),
    }
    scores = value["candidate_scores"]
    comparisons = value["pairwise_comparisons"]
    if not isinstance(scores, list) or not isinstance(comparisons, list):
        raise ValueError("score-set nested evidence is malformed")
    indexed: dict[str, Mapping[str, Any]] = {}
    for score in scores:
        if not isinstance(score, Mapping) or "candidate_score_fingerprint" not in score:
            raise ValueError("candidate score is malformed")
        semantic = dict(score)
        diagnostics = semantic.pop("diagnostics", None)
        stored = semantic.pop("candidate_score_fingerprint")
        expected_fp = _fingerprint(
            "isar.scan_window_candidate_score_fingerprint/o36.4",
            {**common, "candidate": semantic},
        )
        if stored != expected_fp or not isinstance(diagnostics, Mapping):
            raise ValueError("candidate score fingerprint is invalid")
        candidate_id = score.get("candidate_id")
        if not isinstance(candidate_id, str) or candidate_id in indexed:
            raise ValueError("candidate score identities are invalid")
        blocks = score.get("block_information_nats")
        point_count = score.get("executed_point_count")
        if (score.get("status") != "available" or score.get("reason_codes") != []
                or not isinstance(blocks, list) or len(blocks) != value["block_count"]
                or any(not isinstance(item, (int, float)) or isinstance(item, bool)
                       or not math.isfinite(float(item)) for item in blocks)
                or not isinstance(point_count, int) or isinstance(point_count, bool)
                or point_count <= 0):
            raise ValueError("available candidate score numerics are invalid")
        center = math.fsum(float(item) for item in blocks) / value["block_count"]
        standard_error = math.sqrt(
            math.fsum((float(item) - center) ** 2 for item in blocks)
            / (value["block_count"] - 1)) / math.sqrt(value["block_count"])
        expected_values = {
            "conditional_target_qoi_information_nats": center,
            "conditional_target_qoi_information_standard_error_nats": standard_error,
            "information_per_executed_point_nats": center / point_count,
            "information_per_executed_point_standard_error_nats": standard_error / point_count,
        }
        if any(not isinstance(score.get(name), (int, float))
               or isinstance(score.get(name), bool)
               or not math.isclose(float(score[name]), expected_value,
                                   rel_tol=1e-12, abs_tol=1e-12)
               for name, expected_value in expected_values.items()):
            raise ValueError("candidate block estimates are invalid")
        indexed[candidate_id] = score
    if tuple(indexed) != tuple(sorted(indexed)) or len(indexed) != 2 or len(comparisons) != 1:
        raise ValueError("complete shadow score set must contain exactly one ordered pair")
    for comparison in comparisons:
        if not isinstance(comparison, Mapping) or "pairwise_comparison_fingerprint" not in comparison:
            raise ValueError("pairwise comparison is malformed")
        semantic = dict(comparison)
        stored = semantic.pop("pairwise_comparison_fingerprint")
        expected_fp = _fingerprint(
            "isar.scan_window_pairwise_comparison_fingerprint/o36.4",
            {**common, "comparison": semantic},
        )
        if stored != expected_fp:
            raise ValueError("pairwise comparison fingerprint is invalid")
        first = indexed.get(comparison.get("candidate_a_id"))
        second = indexed.get(comparison.get("candidate_b_id"))
        if (first is None or second is None
                or comparison.get("candidate_a_score_fingerprint")
                != first.get("candidate_score_fingerprint")
                or comparison.get("candidate_b_score_fingerprint")
                != second.get("candidate_score_fingerprint")):
            raise ValueError("pairwise comparison candidate join is invalid")
        blocks = comparison.get("paired_block_deltas")
        per_point = comparison.get("paired_block_deltas_per_executed_point_nats")
        if (not isinstance(blocks, list) or not isinstance(per_point, list)
                or len(blocks) != value["block_count"] or len(per_point) != value["block_count"]):
            raise ValueError("pairwise block evidence is invalid")
        first_blocks = first.get("block_information_nats")
        second_blocks = second.get("block_information_nats")
        if (not isinstance(first_blocks, list) or not isinstance(second_blocks, list)
                or len(first_blocks) != len(blocks) or len(second_blocks) != len(blocks)):
            raise ValueError("candidate block evidence is invalid")
        expected_blocks = [float(right) - float(left)
                           for left, right in zip(first_blocks, second_blocks)]
        first_points, second_points = first.get("executed_point_count"), second.get("executed_point_count")
        expected_per_point = [float(right) / int(second_points) - float(left) / int(first_points)
                              for left, right in zip(first_blocks, second_blocks)]
        if any(not math.isclose(float(left), right, rel_tol=1e-12, abs_tol=1e-12)
               for left, right in zip(blocks, expected_blocks)):
            raise ValueError("paired block deltas do not join candidate scores")
        if any(not math.isclose(float(left), right, rel_tol=1e-12, abs_tol=1e-12)
               for left, right in zip(per_point, expected_per_point)):
            raise ValueError("paired per-point deltas do not join candidate scores")
        absolute_center = (float(second["conditional_target_qoi_information_nats"])
                           - float(first["conditional_target_qoi_information_nats"]))
        per_point_center = (float(second["information_per_executed_point_nats"])
                            - float(first["information_per_executed_point_nats"]))
        absolute_se = math.sqrt(
            math.fsum((item - absolute_center) ** 2 for item in expected_blocks)
            / (value["block_count"] - 1)) / math.sqrt(value["block_count"])
        per_point_se = math.sqrt(
            math.fsum((item - per_point_center) ** 2 for item in expected_per_point)
            / (value["block_count"] - 1)) / math.sqrt(value["block_count"])
        joined = {
            "delta_information_nats": absolute_center,
            "delta_information_standard_error_nats": absolute_se,
            "delta_information_per_executed_point_nats": per_point_center,
            "delta_information_per_executed_point_standard_error_nats": per_point_se,
        }
        if any(not math.isclose(float(comparison[name]), expected_value,
                                rel_tol=1e-12, abs_tol=1e-12)
               for name, expected_value in joined.items()):
            raise ValueError("pairwise comparison values do not join candidate scores")
    payload = dict(value)
    payload.pop("score_set_fingerprint")
    payload.pop("diagnostics")
    payload["candidate_scores"] = [
        {key: item for key, item in score.items() if key != "diagnostics"} for score in scores]
    expected_fp = _fingerprint("isar.scan_window_eig_score_set_fingerprint/o36.4", payload)
    if value["score_set_fingerprint"] != expected_fp:
        raise ValueError("score_set_fingerprint is invalid")


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
    authority_scopes: tuple[tuple[str, str], ...] = _INELIGIBLE_AUTHORITY
    diagnostics: Mapping[str, Any] = field(default_factory=dict, compare=False)
    contract: str = field(default=SHADOW_VARIANT_CONTRACT, init=False)
    variant_evidence_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if (not isinstance(self.policy_id, str) or not self.policy_id
                or not isinstance(self.variant_id, str) or not self.variant_id
                or not _valid_fingerprint(self.policy_fingerprint)
                or not _valid_fingerprint(self.variant_fingerprint)
                or self.status not in {STATUS_COMPLETE, STATUS_UNAVAILABLE}):
            raise ValueError("shadow variant identity is invalid")
        codes = _codes(self.reason_codes)
        partition = None if self.compiled_partition is None else _freeze(self.compiled_partition)
        score_set = None if self.score_set is None else _freeze(self.score_set)
        delta = (None if self.target_core_minus_legacy is None
                 else _freeze(self.target_core_minus_legacy))
        expected_authority = (_DIAGNOSTIC_AUTHORITY if self.status == STATUS_COMPLETE
                              else _INELIGIBLE_AUTHORITY)
        if tuple(self.authority_scopes) != expected_authority:
            raise ValueError("shadow variant authority is invalid")
        if self.status == STATUS_COMPLETE:
            if codes or partition is None or score_set is None or delta is None:
                raise ValueError("complete shadow variant evidence is incomplete")
            _validate_score_set_mapping(_thaw(score_set))
            try:
                reconstructed_partition = ScalarQoIDiscretization(
                    qoi_id=partition["qoi_id"], qoi_unit=partition["qoi_unit"],
                    origin=partition["origin"], bin_width=partition["bin_width"],
                    offsets=tuple(partition["offsets"]),
                )
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("compiled shadow partition is invalid") from error
            if reconstructed_partition.to_dict() != _thaw(partition):
                raise ValueError("compiled shadow partition fingerprint is invalid")
            if dict(score_set).get("status") != "complete":
                raise ValueError("complete shadow variant requires a complete Slice 4 score set")
            expected_delta_keys = {
                "delta_information_nats", "delta_information_standard_error_nats",
                "delta_information_per_executed_point_nats",
                "delta_information_per_executed_point_standard_error_nats",
            }
            if set(delta) != expected_delta_keys or any(
                    not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(float(value)) for value in delta.values()):
                raise ValueError("normalized shadow comparison is invalid")
        elif not codes or score_set is not None or delta is not None:
            raise ValueError("unavailable shadow variant carries partial numerical evidence")
        diagnostics = _freeze(self.diagnostics)
        if not isinstance(diagnostics, Mapping):
            raise ValueError("shadow variant diagnostics are invalid")
        if _contains_forbidden_field({
                "compiled_partition": partition,
                "score_set": score_set,
                "target_core_minus_legacy": delta,
                "diagnostics": diagnostics,
        }):
            raise ValueError("shadow variant contains an authoritative decision field")
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "compiled_partition", partition)
        object.__setattr__(self, "score_set", score_set)
        object.__setattr__(self, "target_core_minus_legacy", delta)
        object.__setattr__(self, "diagnostics", diagnostics)
        object.__setattr__(self, "variant_evidence_fingerprint", _fingerprint(
            "isar.scan_design_shadow_variant_fingerprint/o36.6",
            _without_diagnostics(self._payload())))

    def _payload(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "policy_id": self.policy_id,
            "policy_fingerprint": self.policy_fingerprint,
            "variant_id": self.variant_id,
            "variant_fingerprint": self.variant_fingerprint,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "compiled_partition": _thaw(self.compiled_partition),
            "score_set": _thaw(self.score_set),
            "target_core_minus_legacy": _thaw(self.target_core_minus_legacy),
            "authority_scopes": dict(self.authority_scopes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._payload(),
            "variant_evidence_fingerprint": self.variant_evidence_fingerprint,
            "diagnostics": _thaw(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignShadowVariant":
        _exact_keys(value, {
            "contract", "policy_id", "policy_fingerprint", "variant_id",
            "variant_fingerprint", "status", "reason_codes", "compiled_partition",
            "score_set", "target_core_minus_legacy", "authority_scopes",
            "variant_evidence_fingerprint", "diagnostics",
        }, "shadow variant evidence")
        if value["contract"] != SHADOW_VARIANT_CONTRACT:
            raise ValueError("shadow variant evidence contract is unsupported")
        item = cls(
            policy_id=value["policy_id"], policy_fingerprint=value["policy_fingerprint"],
            variant_id=value["variant_id"], variant_fingerprint=value["variant_fingerprint"],
            status=value["status"], reason_codes=tuple(value["reason_codes"]),
            compiled_partition=value["compiled_partition"], score_set=value["score_set"],
            target_core_minus_legacy=value["target_core_minus_legacy"],
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
    authority_scopes: tuple[tuple[str, str], ...] = _INELIGIBLE_AUTHORITY
    diagnostics: Mapping[str, Any] = field(default_factory=dict, compare=False)
    contract: str = field(default=SHADOW_SCORING_CONTRACT, init=False)
    shadow_scoring_fingerprint: str = field(default="", init=False)

    def __post_init__(self) -> None:
        if self.status not in {
                STATUS_NOT_REQUESTED, STATUS_COMPLETE, STATUS_PARTIAL, STATUS_UNAVAILABLE}:
            raise ValueError("shadow scoring status is invalid")
        codes = _codes(self.reason_codes)
        variants = tuple(self.variants)
        keys = tuple((item.policy_id, item.variant_id) for item in variants)
        if (any(not isinstance(item, ScanDesignShadowVariant) for item in variants)
                or keys != tuple(sorted(keys)) or len(keys) != len(set(keys))):
            raise ValueError("shadow scoring variants must be ordered and unique")
        if variants and len({(item.policy_id, item.policy_fingerprint) for item in variants}) != 1:
            raise ValueError("shadow scoring variants must join one matched policy")
        complete = sum(item.status == STATUS_COMPLETE for item in variants)
        unavailable = len(variants) - complete
        valid_status = (
            self.status == STATUS_NOT_REQUESTED and not variants and bool(codes)
            or self.status == STATUS_COMPLETE and complete > 0 and unavailable == 0 and not codes
            or self.status == STATUS_PARTIAL and complete > 0 and unavailable > 0 and not codes
            or self.status == STATUS_UNAVAILABLE and complete == 0 and (bool(codes) or bool(variants))
        )
        if not valid_status:
            raise ValueError("shadow scoring status is inconsistent with variant evidence")
        expected_authority = (_DIAGNOSTIC_AUTHORITY if complete else _INELIGIBLE_AUTHORITY)
        if tuple(self.authority_scopes) != expected_authority:
            raise ValueError("shadow scoring authority is invalid")
        for name in ("scoring_config_fingerprint", "policy_set_fingerprint"):
            value = getattr(self, name)
            if value is not None and not _valid_fingerprint(value):
                raise ValueError(f"{name} is invalid")
        if (_contains_forbidden_field(self.to_unfingerprinted_dict())
                or _contains_forbidden_field(self.diagnostics)):
            raise ValueError("shadow scoring contains an authoritative decision field")
        object.__setattr__(self, "reason_codes", codes)
        object.__setattr__(self, "variants", variants)
        for name in ("scoring_config", "policy_set", "policy_match", "eligibility",
                     "design_measure", "candidate_pair", "diagnostics"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, Mapping):
                    raise ValueError(f"shadow scoring {name} must be a mapping")
                object.__setattr__(self, name, _freeze(value))
        authenticated_config = None
        if self.scoring_config is not None:
            authenticated_config = ScanWindowShadowScoringConfig.from_dict(
                _thaw(self.scoring_config))
            if authenticated_config.scoring_config_fingerprint != self.scoring_config_fingerprint:
                raise ValueError("shadow scoring configuration fingerprint is invalid")
        elif self.scoring_config_fingerprint is not None:
            raise ValueError("shadow scoring configuration content is missing")
        authenticated_policies = None
        if self.policy_set is not None:
            authenticated_policies = QoIDistinctionPolicySet.from_dict(_thaw(self.policy_set))
            if authenticated_policies.policy_set_fingerprint != self.policy_set_fingerprint:
                raise ValueError("shadow policy-set fingerprint is invalid")
            if len(authenticated_policies.policies) > MAX_QOI_POLICIES:
                raise ValueError("shadow policy set exceeds the authenticated runtime bound")
        elif self.policy_set_fingerprint is not None:
            raise ValueError("shadow policy-set content is missing")
        if authenticated_config is not None and authenticated_policies is not None:
            if any(len(policy.variants) > authenticated_config.max_runtime_variants
                   for policy in authenticated_policies.policies):
                raise ValueError("shadow policy variants exceed the authenticated runtime bound")
        matched_policy = None
        if self.policy_match is not None:
            match = _thaw(self.policy_match)
            if set(match) != {"qoi_id", "qoi_unit", "scope_kind", "scope_fingerprint",
                              "matching_policy_ids"}:
                raise ValueError("shadow policy-match evidence is invalid")
            expected_matches = (() if authenticated_policies is None else matching_qoi_policies(
                authenticated_policies, qoi_id=match["qoi_id"], qoi_unit=match["qoi_unit"],
                scope_kind=match["scope_kind"], scope_fingerprint=match["scope_fingerprint"]))
            if [policy.policy_id for policy in expected_matches] != match["matching_policy_ids"]:
                raise ValueError("shadow policy-match evidence does not reproduce")
            if len(expected_matches) == 1:
                matched_policy = expected_matches[0]
        if self.variants:
            if matched_policy is None or authenticated_config is None:
                raise ValueError("shadow variants lack an authenticated unique policy/configuration")
            if len(matched_policy.variants) > authenticated_config.max_runtime_variants:
                raise ValueError("shadow policy variants exceed the authenticated runtime bound")
            expected_variants = {
                (variant.variant_id, variant.variant_fingerprint): variant
                for variant in matched_policy.variants}
            actual_variants = {(variant.variant_id, variant.variant_fingerprint)
                               for variant in self.variants}
            if actual_variants != set(expected_variants):
                raise ValueError("shadow variant evidence does not cover the frozen policy")
            for evidence in self.variants:
                if (evidence.policy_id != matched_policy.policy_id
                        or evidence.policy_fingerprint != matched_policy.policy_fingerprint):
                    raise ValueError("shadow variant policy identity is invalid")
                variant = expected_variants[(evidence.variant_id, evidence.variant_fingerprint)]
                try:
                    partition = compile_qoi_distinction_policy(matched_policy, variant)
                except ValueError as error:
                    if str(error) != "qoi_policy_unsupported":
                        raise
                    if (evidence.status != STATUS_UNAVAILABLE
                            or evidence.reason_codes != ("qoi_policy_unsupported",)
                            or evidence.compiled_partition is not None):
                        raise ValueError("unsupported shadow variant evidence is invalid") from error
                else:
                    if (_thaw(evidence.compiled_partition) != partition.to_dict()
                            or evidence.reason_codes == ("qoi_policy_unsupported",)):
                        raise ValueError("compiled shadow variant does not match frozen policy")
        if self.eligibility is not None:
            _validate_eligibility_summary(_thaw(self.eligibility))
        if self.design_measure is not None:
            measure = _thaw(self.design_measure)
            if (set(measure) != {
                    "contract", "status", "reason_codes", "construction_codes",
                    "eligibility", "qoi", "source_identities",
                    "source_draw_identity_fingerprint", "design_measure_fingerprint",
                    "authority_scopes",
            } or measure["eligibility"] != _thaw(self.eligibility)
                    or not _valid_fingerprint(measure["design_measure_fingerprint"])
                    or (measure["source_draw_identity_fingerprint"] is not None
                        and not _valid_fingerprint(
                            measure["source_draw_identity_fingerprint"]))):
                raise ValueError("shadow design-measure summary is invalid")
        if self.seed_evidence is not None and not isinstance(self.seed_evidence, ShadowSeedEvidence):
            raise TypeError("shadow scoring seed evidence is invalid")
        if self.candidate_pair is not None:
            pair = _thaw(self.candidate_pair)
            if (set(pair) != {"contract", "legacy", "target_core",
                              "candidate_pair_fingerprint"}
                    or pair["contract"] != "isar.scan_design_candidate_pair/o36.6"):
                raise ValueError("shadow scoring candidate pair is invalid")
            stored_pair_fingerprint = pair.pop("candidate_pair_fingerprint")
            if stored_pair_fingerprint != _fingerprint(
                    "isar.scan_design_candidate_pair_fingerprint/o36.6", pair):
                raise ValueError("shadow candidate-pair fingerprint is invalid")
            pair["candidate_pair_fingerprint"] = stored_pair_fingerprint
        if self.seed_evidence is not None:
            if (self.scoring_config_fingerprint
                    != self.seed_evidence.scoring_config_fingerprint
                    or not isinstance(self.design_measure, Mapping)
                    or self.design_measure.get("design_measure_fingerprint")
                    != self.seed_evidence.design_measure_fingerprint
                    or not isinstance(self.candidate_pair, Mapping)
                    or self.candidate_pair.get("candidate_pair_fingerprint")
                    != self.seed_evidence.candidate_pair_fingerprint):
                raise ValueError("shadow seed evidence does not join its scoring inputs")
        if complete and self.seed_evidence is None:
            raise ValueError("scored shadow evidence requires an authenticated seed")
        if complete:
            if (self.scoring_config_fingerprint is None
                    or self.policy_set_fingerprint is None
                    or not isinstance(self.eligibility, Mapping)
                    or not isinstance(self.design_measure, Mapping)
                    or self.design_measure.get("status") != "available"
                    or not isinstance(self.candidate_pair, Mapping)):
                raise ValueError("scored shadow evidence is missing authenticated inputs")
            legacy_id = self.candidate_pair["legacy"]["candidate_id"]
            target_id = self.candidate_pair["target_core"]["candidate_id"]
            expected_scores = {
                legacy_id: self.candidate_pair["legacy"],
                target_id: self.candidate_pair["target_core"],
            }
            numerical_shapes: set[tuple[int, int]] = set()
            for variant in self.variants:
                if variant.status != STATUS_COMPLETE:
                    continue
                score_set = variant.score_set
                numerical_shapes.add((score_set["simulation_count"], score_set["block_count"]))
                if (score_set["seed"] != self.seed_evidence.seed
                        or score_set["design_measure_fingerprint"]
                        != self.design_measure["design_measure_fingerprint"]
                        or score_set["source_draw_identity_fingerprint"]
                        != self.design_measure["source_draw_identity_fingerprint"]
                        or score_set["qoi_discretization_fingerprint"]
                        != variant.compiled_partition["discretization_fingerprint"]):
                    raise ValueError("shadow score set does not join its scoring inputs")
                for candidate_score in score_set["candidate_scores"]:
                    expected = expected_scores.get(candidate_score["candidate_id"])
                    if (expected is None
                            or candidate_score["energy_meV"] != expected["energy_meV"]
                            or candidate_score["executed_point_count"]
                            != expected["executed_point_count"]
                            or candidate_score["exposure_per_point"]
                            != expected["exposure_per_point"]):
                        raise ValueError("shadow candidate score does not join candidate pair")
                comparisons = variant.score_set["pairwise_comparisons"]
                comparison = comparisons[0]
                sign = 1.0 if comparison["candidate_b_id"] == target_id else -1.0
                if {comparison["candidate_a_id"], comparison["candidate_b_id"]} != {
                        legacy_id, target_id}:
                    raise ValueError("shadow score comparison does not join its candidate pair")
                expected_delta = {
                    "delta_information_nats": sign * comparison["delta_information_nats"],
                    "delta_information_standard_error_nats":
                        comparison["delta_information_standard_error_nats"],
                    "delta_information_per_executed_point_nats": sign
                        * comparison["delta_information_per_executed_point_nats"],
                    "delta_information_per_executed_point_standard_error_nats":
                        comparison[
                            "delta_information_per_executed_point_standard_error_nats"],
                }
                if any(not math.isclose(
                        float(variant.target_core_minus_legacy[name]), float(expected),
                        rel_tol=1e-12, abs_tol=1e-12)
                        for name, expected in expected_delta.items()):
                    raise ValueError("normalized shadow delta does not join Slice 4 evidence")
            expected_shape = (
                authenticated_config.simulation_count,
                authenticated_config.block_count,
            )
            if numerical_shapes != {expected_shape}:
                raise ValueError("shadow variants use inconsistent numerical configurations")
        object.__setattr__(self, "shadow_scoring_fingerprint", _fingerprint(
            "isar.scan_design_shadow_scoring_fingerprint/o36.6",
            _without_diagnostics(self.to_unfingerprinted_dict())))

    def to_unfingerprinted_dict(self) -> dict[str, Any]:
        return {
            "contract": self.contract,
            "status": self.status,
            "reason_codes": list(self.reason_codes),
            "scoring_config_fingerprint": self.scoring_config_fingerprint,
            "policy_set_fingerprint": self.policy_set_fingerprint,
            "scoring_config": _thaw(self.scoring_config),
            "policy_set": _thaw(self.policy_set),
            "policy_match": _thaw(self.policy_match),
            "eligibility": _thaw(self.eligibility),
            "design_measure": _thaw(self.design_measure),
            "candidate_pair": _thaw(self.candidate_pair),
            "seed_evidence": None if self.seed_evidence is None else self.seed_evidence.to_dict(),
            "variants": [item.to_dict() for item in self.variants],
            "authority_scopes": dict(self.authority_scopes),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_unfingerprinted_dict(),
            "shadow_scoring_fingerprint": self.shadow_scoring_fingerprint,
            "diagnostics": _thaw(self.diagnostics),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ScanDesignShadowScoring":
        if _contains_forbidden_field(value):
            raise ValueError("shadow scoring contains an authoritative decision field")
        _exact_keys(value, {
            "contract", "status", "reason_codes", "scoring_config_fingerprint",
            "policy_set_fingerprint", "scoring_config", "policy_set", "policy_match",
            "eligibility", "design_measure", "candidate_pair",
            "seed_evidence", "variants", "authority_scopes", "shadow_scoring_fingerprint",
            "diagnostics",
        }, "shadow scoring")
        if value["contract"] != SHADOW_SCORING_CONTRACT:
            raise ValueError("shadow scoring contract is unsupported")
        item = cls(
            status=value["status"], reason_codes=tuple(value["reason_codes"]),
            scoring_config_fingerprint=value["scoring_config_fingerprint"],
            policy_set_fingerprint=value["policy_set_fingerprint"],
            scoring_config=value["scoring_config"], policy_set=value["policy_set"],
            policy_match=value["policy_match"],
            eligibility=value["eligibility"], design_measure=value["design_measure"],
            candidate_pair=value["candidate_pair"],
            seed_evidence=(None if value["seed_evidence"] is None
                           else ShadowSeedEvidence.from_dict(value["seed_evidence"])),
            variants=tuple(ScanDesignShadowVariant.from_dict(item)
                           for item in value["variants"]),
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
        """Construct fail-open evidence without claiming unauthenticated source identities."""
        return cls(status=STATUS_UNAVAILABLE, reason_codes=(reason_code,),
                   diagnostics={} if diagnostics is None else diagnostics)


def validate_scan_design_shadow_scoring(value: Any) -> ScanDesignShadowScoring:
    if not isinstance(value, ScanDesignShadowScoring):
        raise TypeError("value must be a ScanDesignShadowScoring")
    return ScanDesignShadowScoring.from_dict(value.to_dict())


def _candidate_summary(candidate: Any, *, context: Any,
                       generator_config_fingerprint: str) -> dict[str, Any]:
    return {
        "candidate_id": candidate.candidate_id,
        "candidate_family": candidate.candidate_family,
        "generator_id": candidate.generator_id,
        "generator_version": int(candidate.generator_version),
        "generator_config_fingerprint": generator_config_fingerprint,
        "context_fingerprint": context.fingerprint,
        "scientific_action_fingerprint": context.scientific_action_fingerprint,
        "hkl": [float(value) for value in candidate.hkl],
        "q": float(candidate.q),
        "kind": candidate.kind,
        "reason": candidate.reason,
        "energy_meV": [float(value) for value in candidate.energies],
        "grid_fingerprint": candidate.grid.fingerprint(candidate.ncount),
        "executed_point_count": int(candidate.n_points),
        "exposure_per_point": int(candidate.ncount),
        "request_unit": "number_neutrons",
        "exposure_interpretation": "uniform_per_executed_point",
        "nominal_cost": int(candidate.nominal_cost),
    }


def _candidate_pair(inventory: Any) -> tuple[Any, Any, dict[str, Any]]:
    legacy = [item for item in inventory.candidates if item.candidate_family == _FAMILY_LEGACY]
    target = [item for item in inventory.candidates if item.candidate_family == _FAMILY_TARGET]
    if len(legacy) != 1 or len(target) != 1:
        raise LookupError("scan_design_candidate_pair_missing" if not legacy or not target
                          else "scan_design_candidate_pair_ambiguous")
    first, second = legacy[0], target[0]
    context = inventory.context
    generator_config_fingerprint = inventory.generator_config.fingerprint
    if (first.candidate_id == second.candidate_id or first.energies == second.energies
            or first.ncount != second.ncount
            or first.hkl != second.hkl or first.hkl != context.hkl
            or float(first.q) != float(second.q) or float(first.q) != float(context.q)
            or first.kind != second.kind or first.kind != context.kind
            or first.reason != second.reason or first.reason != context.reason
            or first.generator_id != second.generator_id
            or first.generator_version != second.generator_version):
        raise ValueError("scan_design_candidate_pair_invalid")
    summaries = {
        "legacy": _candidate_summary(
            first, context=context,
            generator_config_fingerprint=generator_config_fingerprint),
        "target_core": _candidate_summary(
            second, context=context,
            generator_config_fingerprint=generator_config_fingerprint),
    }
    pair_payload = {
        "contract": "isar.scan_design_candidate_pair/o36.6",
        **summaries,
    }
    pair_payload["candidate_pair_fingerprint"] = _fingerprint(
        "isar.scan_design_candidate_pair_fingerprint/o36.6", pair_payload)
    return first, second, pair_payload


def validate_shadow_candidate_pair(inventory: Any, value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Recompute the Slice 6 pair from the inventory and require exact persisted parity."""
    if not isinstance(value, Mapping):
        raise TypeError("shadow candidate pair must be a mapping")
    _legacy, _target, expected = _candidate_pair(inventory)
    if _thaw(value) != expected:
        raise ValueError("shadow candidate pair does not authenticate inventory candidates")
    return value


def _measure_summary(measure: Any) -> dict[str, Any]:
    return {
        "contract": measure.contract,
        "status": measure.status,
        "reason_codes": list(measure.reason_codes),
        "construction_codes": list(measure.construction_codes),
        "eligibility": _thaw(dict(measure.eligibility)),
        "qoi": _thaw(dict(measure.qoi)) if measure.qoi else None,
        "source_identities": _thaw(dict(measure.source_identities))
        if measure.source_identities else None,
        "source_draw_identity_fingerprint": measure.source_draw_identity_fingerprint,
        "design_measure_fingerprint": measure.design_measure_fingerprint,
        "authority_scopes": dict(measure.authority_scopes),
    }


def _normalized_comparison(score_set: Any, legacy_id: str, target_id: str
                           ) -> dict[str, float] | None:
    comparison = next((item for item in score_set.pairwise_comparisons
                       if {item["candidate_a_id"], item["candidate_b_id"]}
                       == {legacy_id, target_id}), None)
    if comparison is None:
        return None
    sign = 1.0 if comparison["candidate_b_id"] == target_id else -1.0
    return {
        "delta_information_nats": sign * float(comparison["delta_information_nats"]),
        "delta_information_standard_error_nats": float(
            comparison["delta_information_standard_error_nats"]),
        "delta_information_per_executed_point_nats": sign * float(
            comparison["delta_information_per_executed_point_nats"]),
        "delta_information_per_executed_point_standard_error_nats": float(
            comparison["delta_information_per_executed_point_standard_error_nats"]),
    }


def score_shadow_inventory(
        inventory: Any,
        *,
        predictive_artifact: Mapping[str, Any] | None,
        eligibility_input: EligibilityInput,
        policy_set: QoIDistinctionPolicySet | None,
        scoring_config: Any,
        scope_kind: str,
        scope_fingerprint: str,
        deadline_check: Callable[[], None] | None = None,
        predictive_evaluator: Callable[..., Any] = evaluate_master_predictive,
        scorer: Callable[..., Any] = score_scan_window_candidates,
) -> ScanDesignShadowScoring:
    """Score exactly legacy versus target-core without selecting either candidate."""
    check = deadline_check or (lambda: None)
    if getattr(getattr(inventory, "context", None), "scan_window_eig_mode", None) != "shadow":
        return ScanDesignShadowScoring.not_requested()
    try:
        # Provider-neutral reauthentication: the inventory owns its frozen reader, so this pure
        # module need not import acquisition generation or any campaign layer.
        inventory = type(inventory).from_dict(inventory.to_full_dict())
    except (AttributeError, TypeError, ValueError):
        return ScanDesignShadowScoring.unavailable("scan_design_inventory_invalid")
    scoring_config = validate_scan_window_shadow_scoring_config(scoring_config)
    eligibility = None
    try:
        measure = validate_scan_window_design_measure(
            build_scan_window_design_measure(predictive_artifact, eligibility_input))
        eligibility = _thaw(dict(measure.eligibility))
    except (TypeError, ValueError) as error:
        log.warning("shadow design-measure validation failed: %s", error)
        return ScanDesignShadowScoring.unavailable("design_measure_invalid")
    common: dict[str, Any] = {
        "scoring_config_fingerprint": scoring_config.scoring_config_fingerprint,
        "policy_set_fingerprint": (None if policy_set is None
                                   else policy_set.policy_set_fingerprint),
        "scoring_config": scoring_config.to_dict(),
        "policy_set": None if policy_set is None else policy_set.to_dict(),
        "eligibility": eligibility,
        "design_measure": _measure_summary(measure),
    }
    if measure.status != "available":
        reason = (measure.reason_codes or measure.construction_codes
                  or ("design_measure_unavailable",))[0]
        return ScanDesignShadowScoring.unavailable(str(reason), **common)
    try:
        legacy, target, pair = _candidate_pair(inventory)
    except LookupError as error:
        log.info("shadow candidate pair unavailable: %s", error)
        return ScanDesignShadowScoring.unavailable(str(error), **common)
    except ValueError as error:
        log.warning("shadow candidate pair refused: %s", error)
        return ScanDesignShadowScoring.unavailable(str(error), **common)
    common["candidate_pair"] = pair
    qoi = dict(measure.qoi)
    match_evidence = {
        "qoi_id": qoi["id"], "qoi_unit": qoi["unit"],
        "scope_kind": scope_kind, "scope_fingerprint": scope_fingerprint,
        "matching_policy_ids": [],
    }
    common["policy_match"] = match_evidence
    if policy_set is None:
        return ScanDesignShadowScoring.unavailable("qoi_policy_missing", **common)
    try:
        policies = validate_qoi_distinction_policy_set(policy_set)
        if len(policies.policies) > MAX_QOI_POLICIES:
            raise ValueError("QoI policy count exceeds the runtime bound")
        matches = matching_qoi_policies(
            policies, qoi_id=qoi["id"], qoi_unit=qoi["unit"],
            scope_kind=scope_kind, scope_fingerprint=scope_fingerprint)
    except (KeyError, TypeError, ValueError) as error:
        log.warning("shadow QoI policy validation failed: %s", error)
        return ScanDesignShadowScoring.unavailable("qoi_policy_invalid", **common)
    match_evidence["matching_policy_ids"] = [policy.policy_id for policy in matches]
    if not matches:
        return ScanDesignShadowScoring.unavailable("qoi_policy_missing", **common)
    if len(matches) != 1:
        return ScanDesignShadowScoring.unavailable("qoi_policy_ambiguous", **common)
    policy = matches[0]
    if len(policy.variants) > scoring_config.max_runtime_variants:
        return ScanDesignShadowScoring.unavailable(
            "qoi_policy_variant_limit_exceeded", **common)
    compiled: list[tuple[Any, Any]] = []
    variants: list[ScanDesignShadowVariant] = []
    for variant in policy.variants:
        try:
            partition = compile_qoi_distinction_policy(policy, variant)
        except ValueError as error:
            if str(error) != "qoi_policy_unsupported":
                raise
            log.info("shadow QoI policy variant unsupported: %s/%s",
                     policy.policy_id, variant.variant_id)
            variants.append(ScanDesignShadowVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status=STATUS_UNAVAILABLE, reason_codes=("qoi_policy_unsupported",),
            ))
            continue
        compiled.append((variant, partition))
    if not compiled:
        return ScanDesignShadowScoring(
            status=STATUS_UNAVAILABLE, reason_codes=(), variants=tuple(variants), **common)
    seed_evidence = derive_shadow_seed(
        scoring_config_fingerprint=scoring_config.scoring_config_fingerprint,
        design_measure_fingerprint=measure.design_measure_fingerprint,
        candidate_pair_fingerprint=pair["candidate_pair_fingerprint"],
        seed_derivation_revision=scoring_config.seed_derivation_revision,
    )
    check()
    legacy_evaluation = predictive_evaluator(
        predictive_artifact, legacy.energies, legacy.ncount)
    check()
    target_evaluation = predictive_evaluator(
        predictive_artifact, target.energies, target.ncount)
    candidate_inputs = (
        CandidateEvaluationInput(legacy.candidate_id, legacy_evaluation),
        CandidateEvaluationInput(target.candidate_id, target_evaluation),
    )
    for variant_index, (variant, partition) in enumerate(compiled):
        try:
            check()
            score_set = validate_scan_window_score_set(scorer(
                measure, candidate_inputs, qoi_discretization=partition,
                seed=seed_evidence.seed,
                simulation_count=scoring_config.simulation_count,
                block_count=scoring_config.block_count,
            ))
        except Exception as error:  # noqa: BLE001 - per-variant fail-open evidence
            deadline = type(error).__name__ == "AnalysisLimitExceeded"
            reason_code = "shadow_scoring_deadline" if deadline else "shadow_scoring_failed"
            log.warning("shadow scoring variant %s/%s failed (%s): %s",
                        policy.policy_id, variant.variant_id, type(error).__name__, error)
            variants.append(ScanDesignShadowVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status=STATUS_UNAVAILABLE, reason_codes=(reason_code,),
                compiled_partition=partition.to_dict(),
                diagnostics={"error_type": type(error).__name__, "message": str(error)},
            ))
            if deadline:
                for pending_variant, pending_partition in compiled[variant_index + 1:]:
                    variants.append(ScanDesignShadowVariant(
                        policy_id=policy.policy_id,
                        policy_fingerprint=policy.policy_fingerprint,
                        variant_id=pending_variant.variant_id,
                        variant_fingerprint=pending_variant.variant_fingerprint,
                        status=STATUS_UNAVAILABLE,
                        reason_codes=("shadow_scoring_deadline",),
                        compiled_partition=pending_partition.to_dict(),
                    ))
                break
            continue
        delta = (_normalized_comparison(score_set, legacy.candidate_id, target.candidate_id)
                 if score_set.status == "complete" else None)
        if delta is None:
            codes = tuple(score_set.reason_codes) or ("score_set_incomplete",)
            variants.append(ScanDesignShadowVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status=STATUS_UNAVAILABLE, reason_codes=codes,
                compiled_partition=partition.to_dict(),
            ))
        else:
            variants.append(ScanDesignShadowVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status=STATUS_COMPLETE, reason_codes=(),
                compiled_partition=partition.to_dict(), score_set=score_set.to_dict(),
                target_core_minus_legacy=delta, authority_scopes=_DIAGNOSTIC_AUTHORITY,
            ))
    variants.sort(key=lambda item: (item.policy_id, item.variant_id))
    complete = sum(item.status == STATUS_COMPLETE for item in variants)
    status = (STATUS_COMPLETE if complete == len(variants) else STATUS_PARTIAL
              if complete else STATUS_UNAVAILABLE)
    return ScanDesignShadowScoring(
        status=status, reason_codes=(), variants=tuple(variants),
        seed_evidence=seed_evidence,
        authority_scopes=_DIAGNOSTIC_AUTHORITY if complete else _INELIGIBLE_AUTHORITY,
        **common,
    )


__all__ = [
    "SHADOW_SCORING_CONTRACT", "SHADOW_SEED_DOMAIN", "SHADOW_VARIANT_CONTRACT",
    "ScanDesignShadowScoring", "ScanDesignShadowVariant", "ShadowSeedEvidence",
    "derive_shadow_seed", "score_shadow_inventory", "validate_scan_design_shadow_scoring",
    "validate_shadow_candidate_pair",
]
