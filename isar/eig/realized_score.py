"""Pure paired rescoring for exact Slice 7 realized requests."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from isar.predictive import evaluate_master_predictive
from isar.schema.eig_policy import (
    MAX_QOI_POLICIES,
    matching_qoi_policies,
    validate_qoi_distinction_policy_set,
    validate_scan_window_shadow_scoring_config,
)
from isar.schema.eig_realized import (
    RealizedCandidatePair,
    RealizedScanRequest,
    RealizedScoreVariant,
    RealizedSeedEvidence,
    derive_realized_seed,
)
from isar.schema.eig_shadow import DIAGNOSTIC_AUTHORITY, INELIGIBLE_AUTHORITY

from .design_measure import build_scan_window_design_measure, validate_scan_window_design_measure
from .eligibility import EligibilityInput
from .policy import compile_qoi_distinction_policy
from .scorer import CandidateEvaluationInput, score_scan_window_candidates, validate_scan_window_score_set


class RealizedScoringUnavailable(ValueError):
    """A stable global refusal before per-variant paired scoring can start."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def _comparison(score_set: Any, legacy_id: str, target_id: str) -> dict[str, float] | None:
    record = next((item for item in score_set.pairwise_comparisons
                   if {item["candidate_a_id"], item["candidate_b_id"]}
                   == {legacy_id, target_id}), None)
    if record is None:
        return None
    sign = 1.0 if record["candidate_b_id"] == target_id else -1.0
    return {
        "delta_information_nats": sign * float(record["delta_information_nats"]),
        "delta_information_standard_error_nats": float(
            record["delta_information_standard_error_nats"]),
        "delta_information_per_executed_point_nats": sign * float(
            record["delta_information_per_executed_point_nats"]),
        "delta_information_per_executed_point_standard_error_nats": float(
            record["delta_information_per_executed_point_standard_error_nats"]),
    }


def score_realized_pair(
        pair: RealizedCandidatePair,
        *,
        predictive_artifact: Mapping[str, Any] | None,
        eligibility_input: EligibilityInput,
        policy_set: Any,
        scoring_config: Any,
        scope_kind: str,
        scope_fingerprint: str,
        deadline_check: Callable[[], None] | None = None,
        predictive_evaluator: Callable[..., Any] = evaluate_master_predictive,
        scorer: Callable[..., Any] = score_scan_window_candidates,
) -> tuple[tuple[RealizedScoreVariant, ...], RealizedSeedEvidence]:
    """Score one exact equal-exposure pair together without selection authority."""
    if not isinstance(pair.target_core, RealizedScanRequest):
        raise RealizedScoringUnavailable("realized_pair_unavailable")
    check = deadline_check or (lambda: None)
    config = validate_scan_window_shadow_scoring_config(scoring_config)
    try:
        measure = validate_scan_window_design_measure(
            build_scan_window_design_measure(predictive_artifact, eligibility_input))
    except (TypeError, ValueError) as error:
        raise RealizedScoringUnavailable("realized_design_measure_invalid") from error
    if measure.status != "available":
        reason = str((measure.reason_codes or measure.construction_codes
                      or ("realized_pair_unavailable",))[0])
        raise RealizedScoringUnavailable(reason)
    if policy_set is None:
        raise RealizedScoringUnavailable("qoi_policy_missing")
    try:
        policies = validate_qoi_distinction_policy_set(policy_set)
        if len(policies.policies) > MAX_QOI_POLICIES:
            raise ValueError("QoI policy count exceeds the runtime bound")
        qoi = dict(measure.qoi)
        matches = matching_qoi_policies(
            policies, qoi_id=qoi["id"], qoi_unit=qoi["unit"],
            scope_kind=scope_kind, scope_fingerprint=scope_fingerprint)
    except (KeyError, TypeError, ValueError) as error:
        raise RealizedScoringUnavailable("qoi_policy_invalid") from error
    if not matches:
        raise RealizedScoringUnavailable("qoi_policy_missing")
    if len(matches) != 1:
        raise RealizedScoringUnavailable("qoi_policy_ambiguous")
    policy = matches[0]
    if len(policy.variants) > config.max_runtime_variants:
        raise RealizedScoringUnavailable("qoi_policy_variant_limit_exceeded")

    compiled: list[tuple[Any, Any]] = []
    variants: list[RealizedScoreVariant] = []
    for variant in policy.variants:
        try:
            partition = compile_qoi_distinction_policy(policy, variant)
        except ValueError as error:
            if str(error) != "qoi_policy_unsupported":
                raise
            variants.append(RealizedScoreVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status="unavailable", score_disposition="unavailable",
                reason_codes=("qoi_policy_unsupported",), authority_scopes=INELIGIBLE_AUTHORITY))
            continue
        compiled.append((variant, partition))
    if not compiled and variants:
        return tuple(sorted(variants, key=lambda item: (item.policy_id, item.variant_id))), (
            derive_realized_seed(
                scoring_config_fingerprint=config.scoring_config_fingerprint,
                design_measure_fingerprint=measure.design_measure_fingerprint,
                realized_pair_fingerprint=pair.realized_pair_fingerprint))

    seed = derive_realized_seed(
        scoring_config_fingerprint=config.scoring_config_fingerprint,
        design_measure_fingerprint=measure.design_measure_fingerprint,
        realized_pair_fingerprint=pair.realized_pair_fingerprint)
    legacy, target = pair.legacy, pair.target_core
    check()
    legacy_evaluation = predictive_evaluator(
        predictive_artifact, legacy.executed_energies, legacy.ncount)
    check()
    target_evaluation = predictive_evaluator(
        predictive_artifact, target.executed_energies, target.ncount)
    inputs = (
        CandidateEvaluationInput(legacy.realized_request_fingerprint, legacy_evaluation),
        CandidateEvaluationInput(target.realized_request_fingerprint, target_evaluation),
    )
    for index, (variant, partition) in enumerate(compiled):
        try:
            check()
            score_set = validate_scan_window_score_set(scorer(
                measure, inputs, qoi_discretization=partition, seed=seed.seed,
                simulation_count=config.simulation_count, block_count=config.block_count))
            check()
        except Exception as error:  # noqa: BLE001 - recognize only the injected deadline type
            deadline = type(error).__name__ == "AnalysisLimitExceeded"
            if not deadline:
                raise
            reason = "realized_scoring_deadline"
            variants.append(RealizedScoreVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status="unavailable", score_disposition="unavailable", reason_codes=(reason,),
                compiled_partition=partition.to_dict(), authority_scopes=INELIGIBLE_AUTHORITY))
            for pending, pending_partition in compiled[index + 1:]:
                variants.append(RealizedScoreVariant(
                    policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                    variant_id=pending.variant_id,
                    variant_fingerprint=pending.variant_fingerprint,
                    status="unavailable", score_disposition="unavailable",
                    reason_codes=(reason,), compiled_partition=pending_partition.to_dict(),
                    authority_scopes=INELIGIBLE_AUTHORITY))
            break
        delta = _comparison(
            score_set, legacy.realized_request_fingerprint,
            target.realized_request_fingerprint) if score_set.status == "complete" else None
        if delta is None:
            codes = tuple(score_set.reason_codes) or ("realized_score_incomplete",)
            variants.append(RealizedScoreVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status="unavailable", score_disposition="unavailable", reason_codes=codes,
                compiled_partition=partition.to_dict(), authority_scopes=INELIGIBLE_AUTHORITY))
        else:
            variants.append(RealizedScoreVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status="complete", score_disposition="rescored",
                compiled_partition=partition.to_dict(), score_set=score_set.to_dict(),
                target_core_minus_legacy=delta, authority_scopes=DIAGNOSTIC_AUTHORITY))
    variants.sort(key=lambda item: (item.policy_id, item.variant_id))
    return tuple(variants), seed


__all__ = ["RealizedScoringUnavailable", "score_realized_pair"]
