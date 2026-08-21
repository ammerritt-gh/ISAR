"""Pure deep validation and scoring for scan-design inventories.

Module and field names here ("shadow") are historical: the diagnostic-only "shadow" mode this was
written for was retired in SCENE-4 PR 4.4 (D3). ``score_shadow_inventory`` and
``validate_scan_design_shadow_scoring`` now serve the sole remaining scoring regime,
``scan_window_eig_mode == "authoritative"``; kept unrenamed given ~12 import sites across the
module and its persisted field/event names (``shadow_scoring``, ``ScanDesignShadowScoring``) that
must not change (H1 forbids renaming a persisted contract).
"""
from __future__ import annotations

import json
import logging
import math
from typing import Any, Callable, Mapping

from isar.predictive import evaluate_master_predictive
from isar.schema.eig_policy import (
    MAX_QOI_POLICIES,
    QoIDistinctionPolicySet,
    ScanWindowShadowScoringConfig,
    matching_qoi_policies,
    validate_qoi_distinction_policy_set,
    validate_scan_window_shadow_scoring_config,
)
from isar.schema.eig_shadow import (
    DIAGNOSTIC_AUTHORITY,
    INELIGIBLE_AUTHORITY,
    SHADOW_SCORING_CONTRACT,
    SHADOW_SEED_DOMAIN,
    SHADOW_VARIANT_CONTRACT,
    STATUS_COMPLETE,
    STATUS_NOT_REQUESTED,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    ScanDesignShadowScoring,
    ScanDesignShadowVariant,
    ScoreRefusalEvidence,
    ShadowSeedEvidence,
    derive_shadow_seed,
    thaw,
    valid_fingerprint,
    validate_scan_design_shadow_scoring as validate_shadow_structure,
)
from isar.schema.statistics import semantics_fingerprint

from .design_measure import build_scan_window_design_measure, validate_scan_window_design_measure
from .eligibility import EligibilityInput, validate_eligibility_summary
from .policy import compile_qoi_distinction_policy
from .scorer import (
    CandidateEvaluationInput,
    ScalarQoIDiscretization,
    ScanWindowScoreSet,
    _CandidateScore,
    _PairwiseComparison,
    _energy_identity,
    _grid_fingerprint,
    score_scan_window_candidates,
    validate_scan_window_score_set,
)


_DIAGNOSTIC_AUTHORITY = DIAGNOSTIC_AUTHORITY
_INELIGIBLE_AUTHORITY = INELIGIBLE_AUTHORITY
_FAMILY_LEGACY = "legacy_uniform/1"
_FAMILY_TARGET = "target_core/1"
_PAIR_FINGERPRINT_DOMAIN = "isar.scan_design_candidate_pair_fingerprint/o36.6"
_PAIR_CANDIDATE_FIELDS = {
    "candidate_id", "candidate_family", "generator_id", "generator_version",
    "generator_config_fingerprint", "context_fingerprint",
    "scientific_action_fingerprint", "hkl", "q", "kind", "reason", "energy_meV",
    "grid_fingerprint", "executed_point_count", "exposure_per_point", "request_unit",
    "exposure_interpretation", "nominal_cost",
}
log = logging.getLogger(__name__)


def _canonical(value: Any) -> str:
    return json.dumps(thaw(value), sort_keys=True, separators=(",", ":"), allow_nan=False)


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
        "q": float(candidate.q), "kind": candidate.kind, "reason": candidate.reason,
        "energy_meV": [float(value) for value in candidate.energies],
        "grid_fingerprint": candidate.grid.fingerprint(candidate.ncount),
        "executed_point_count": int(candidate.n_points),
        "exposure_per_point": int(candidate.ncount),
        "request_unit": "number_neutrons",
        "exposure_interpretation": "uniform_per_executed_point",
        "nominal_cost": int(candidate.nominal_cost),
    }


def _target_refusal_code(inventory: Any) -> str | None:
    refusals = [item for item in getattr(inventory, "refusals", ())
                if item.candidate_family == _FAMILY_TARGET]
    return refusals[0].reason_code if len(refusals) == 1 else None


def _candidate_pair(inventory: Any) -> tuple[Any, Any, dict[str, Any]]:
    """Resolve the pair with a frozen refusal precedence and no numerical work."""
    legacy = [item for item in inventory.candidates if item.candidate_family == _FAMILY_LEGACY]
    target = [item for item in inventory.candidates if item.candidate_family == _FAMILY_TARGET]

    # 1. Alias/equal-grid degeneracy is stronger than every other diagnosis.
    if ((len(legacy) == 1 and _FAMILY_TARGET in legacy[0].alias_families)
            or (legacy and target and any(
                left.candidate_id == right.candidate_id or left.energies == right.energies
                for left in legacy for right in target))):
        raise ValueError("candidate_pair_degenerate")
    # 2. A generator-produced target refusal retains its exact reason code.
    refusal = _target_refusal_code(inventory)
    if not target and refusal is not None:
        raise ValueError(refusal)
    # 3--4. Missing and ambiguous families are distinct.
    if not legacy or not target:
        raise ValueError("candidate_family_missing")
    if len(legacy) != 1 or len(target) != 1:
        raise ValueError("candidate_family_ambiguous")
    first, second = legacy[0], target[0]
    # 5. Equal per-point exposure is its own diagnosis.
    if first.ncount != second.ncount:
        raise ValueError("candidate_exposure_mismatch")
    # 6. Remaining action/context/generator differences are structural.
    context = inventory.context
    if (first.hkl != second.hkl or first.hkl != context.hkl
            or float(first.q) != float(second.q) or float(first.q) != float(context.q)
            or first.kind != second.kind or first.kind != context.kind
            or first.reason != second.reason or first.reason != context.reason
            or first.generator_id != second.generator_id
            or first.generator_version != second.generator_version):
        raise ValueError("candidate_pair_invalid")
    config_fingerprint = inventory.generator_config.fingerprint
    pair = {
        "contract": "isar.scan_design_candidate_pair/o36.6",
        "legacy": _candidate_summary(
            first, context=context, generator_config_fingerprint=config_fingerprint),
        "target_core": _candidate_summary(
            second, context=context, generator_config_fingerprint=config_fingerprint),
    }
    pair["candidate_pair_fingerprint"] = semantics_fingerprint(
        _PAIR_FINGERPRINT_DOMAIN, pair)
    return first, second, pair


def validate_shadow_candidate_pair(inventory: Any, value: Mapping[str, Any]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("shadow candidate pair must be a mapping")
    _legacy, _target, expected = _candidate_pair(inventory)
    if thaw(value) != expected:
        raise ValueError("shadow candidate pair does not authenticate inventory candidates")
    return value


def _measure_summary(measure: Any) -> dict[str, Any]:
    return {
        "contract": measure.contract, "status": measure.status,
        "reason_codes": list(measure.reason_codes),
        "construction_codes": list(measure.construction_codes),
        "eligibility": thaw(dict(measure.eligibility)),
        "qoi": thaw(dict(measure.qoi)) if measure.qoi else None,
        "source_identities": thaw(dict(measure.source_identities))
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


def _score_set_from_mapping(value: Mapping[str, Any]) -> ScanWindowScoreSet:
    """Rehydrate private Slice 4 records solely for the published deep validator."""
    payload = thaw(value)
    candidates = []
    for record in payload["candidate_scores"]:
        item = dict(record)
        item["reason_codes"] = tuple(item["reason_codes"])
        item["energy_meV"] = tuple(item["energy_meV"])
        item["partition_information_nats"] = tuple(item["partition_information_nats"])
        item["block_information_nats"] = tuple(item["block_information_nats"])
        item["authority_scopes"] = tuple(sorted(item["authority_scopes"].items()))
        item["diagnostics"] = tuple(sorted(item["diagnostics"].items()))
        candidates.append(_CandidateScore(**item))
    comparisons = []
    for record in payload["pairwise_comparisons"]:
        item = dict(record)
        item["paired_block_deltas"] = tuple(item["paired_block_deltas"])
        item["paired_block_deltas_per_executed_point_nats"] = tuple(
            item["paired_block_deltas_per_executed_point_nats"])
        comparisons.append(_PairwiseComparison(**item))
    score_set = ScanWindowScoreSet(
        contract=payload["contract"], scorer_revision=payload["scorer_revision"],
        likelihood_revision=payload["likelihood_revision"], crn_revision=payload["crn_revision"],
        status=payload["status"], reason_codes=tuple(payload["reason_codes"]),
        design_measure_fingerprint=payload["design_measure_fingerprint"],
        source_draw_identity_fingerprint=payload["source_draw_identity_fingerprint"],
        qoi_discretization_fingerprint=payload["qoi_discretization_fingerprint"],
        seed=payload["seed"], simulation_count=payload["simulation_count"],
        block_count=payload["block_count"],
        numerical_backend=tuple(sorted(payload["numerical_backend"].items())),
        authority_scopes=tuple(sorted(payload["authority_scopes"].items())),
        score_set_fingerprint=payload["score_set_fingerprint"],
        diagnostics=tuple(sorted(payload["diagnostics"].items())),
        _candidate_scores=tuple(candidates), _pairwise_comparisons=tuple(comparisons),
    )
    validate_scan_window_score_set(score_set)
    if _canonical(score_set.to_dict()) != _canonical(payload):
        raise ValueError("Slice 4 score set does not reproduce from persisted content")
    return score_set


def _partition_from_mapping(value: Mapping[str, Any]) -> ScalarQoIDiscretization:
    partition = thaw(value)
    item = ScalarQoIDiscretization(
        qoi_id=partition["qoi_id"], qoi_unit=partition["qoi_unit"],
        origin=partition["origin"], bin_width=partition["bin_width"],
        offsets=tuple(partition["offsets"]),
    )
    if item.to_dict() != partition:
        raise ValueError("compiled shadow partition fingerprint is invalid")
    return item


def _aggregate_variant_codes(variants: list[ScanDesignShadowVariant]) -> tuple[str, ...]:
    result: list[str] = []
    for variant in variants:
        if variant.status == STATUS_UNAVAILABLE:
            for code in variant.reason_codes:
                if code not in result:
                    result.append(code)
    return tuple(result)


def validate_scan_design_shadow_scoring(
        value: Any, *, inventory: Any | None = None) -> ScanDesignShadowScoring:
    """Deeply authenticate structural records, policy joins, and every retained numeric."""
    scoring = validate_shadow_structure(value)
    if scoring.status == STATUS_NOT_REQUESTED:
        return scoring
    config = None
    if scoring.scoring_config is not None:
        config = ScanWindowShadowScoringConfig.from_dict(thaw(scoring.scoring_config))
        if config.scoring_config_fingerprint != scoring.scoring_config_fingerprint:
            raise ValueError("shadow scoring configuration fingerprint is invalid")
    elif scoring.scoring_config_fingerprint is not None:
        raise ValueError("shadow scoring configuration content is missing")
    policies = None
    if scoring.policy_set is not None:
        policies = QoIDistinctionPolicySet.from_dict(thaw(scoring.policy_set))
        if policies.policy_set_fingerprint != scoring.policy_set_fingerprint:
            raise ValueError("shadow policy-set fingerprint is invalid")
        if len(policies.policies) > MAX_QOI_POLICIES:
            raise ValueError("shadow policy set exceeds the runtime bound")
    elif scoring.policy_set_fingerprint is not None:
        raise ValueError("shadow policy-set content is missing")
    if config is not None and policies is not None and any(
            len(policy.variants) > config.max_runtime_variants for policy in policies.policies):
        raise ValueError("shadow policy variants exceed the authenticated runtime bound")

    matched = None
    if scoring.policy_match is not None:
        match = thaw(scoring.policy_match)
        if set(match) != {"qoi_id", "qoi_unit", "scope_kind", "scope_fingerprint",
                          "matching_policy_ids"}:
            raise ValueError("shadow policy-match evidence is invalid")
        expected = (() if policies is None else matching_qoi_policies(
            policies, qoi_id=match["qoi_id"], qoi_unit=match["qoi_unit"],
            scope_kind=match["scope_kind"], scope_fingerprint=match["scope_fingerprint"]))
        if [policy.policy_id for policy in expected] != match["matching_policy_ids"]:
            raise ValueError("shadow policy-match evidence does not reproduce")
        matched = expected[0] if len(expected) == 1 else None
    if scoring.eligibility is not None:
        validate_eligibility_summary(thaw(scoring.eligibility))
    if scoring.design_measure is not None:
        measure = thaw(scoring.design_measure)
        expected_fields = {
            "contract", "status", "reason_codes", "construction_codes", "eligibility", "qoi",
            "source_identities", "source_draw_identity_fingerprint",
            "design_measure_fingerprint", "authority_scopes"}
        if (set(measure) != expected_fields or measure["eligibility"] != thaw(scoring.eligibility)
                or not valid_fingerprint(measure["design_measure_fingerprint"])):
            raise ValueError("shadow design-measure summary is invalid")
    if scoring.candidate_pair is not None:
        pair = thaw(scoring.candidate_pair)
        if (set(pair) != {"contract", "legacy", "target_core", "candidate_pair_fingerprint"}
                or pair["contract"] != "isar.scan_design_candidate_pair/o36.6"
                or not all(isinstance(pair.get(role), Mapping)
                           and set(pair[role]) == _PAIR_CANDIDATE_FIELDS
                           for role in ("legacy", "target_core"))
                or pair["legacy"]["candidate_family"] != _FAMILY_LEGACY
                or pair["target_core"]["candidate_family"] != _FAMILY_TARGET
                or not valid_fingerprint(pair["candidate_pair_fingerprint"])):
            raise ValueError("shadow scoring candidate pair is invalid")
        pair_payload = {key: pair[key] for key in ("contract", "legacy", "target_core")}
        if pair["candidate_pair_fingerprint"] != semantics_fingerprint(
                _PAIR_FINGERPRINT_DOMAIN, pair_payload):
            raise ValueError("shadow scoring candidate-pair fingerprint is invalid")
        if inventory is not None:
            validate_shadow_candidate_pair(inventory, pair)
    if scoring.seed_evidence is not None:
        if (config is None or scoring.design_measure is None or scoring.candidate_pair is None
                or scoring.seed_evidence.scoring_config_fingerprint
                != config.scoring_config_fingerprint
                or scoring.seed_evidence.design_measure_fingerprint
                != scoring.design_measure["design_measure_fingerprint"]
                or scoring.seed_evidence.candidate_pair_fingerprint
                != scoring.candidate_pair["candidate_pair_fingerprint"]):
            raise ValueError("shadow seed evidence does not join its scoring inputs")

    if scoring.variants:
        if matched is None or config is None:
            raise ValueError("shadow variants lack an authenticated unique policy/configuration")
        expected_variants = {(item.variant_id, item.variant_fingerprint): item
                             for item in matched.variants}
        if {(item.variant_id, item.variant_fingerprint) for item in scoring.variants} != set(
                expected_variants):
            raise ValueError("shadow variant evidence does not cover the frozen policy")
        for evidence in scoring.variants:
            if (evidence.policy_id != matched.policy_id
                    or evidence.policy_fingerprint != matched.policy_fingerprint):
                raise ValueError("shadow variant policy identity is invalid")
            variant = expected_variants[(evidence.variant_id, evidence.variant_fingerprint)]
            try:
                expected_partition = compile_qoi_distinction_policy(matched, variant)
            except ValueError as error:
                if str(error) != "qoi_policy_unsupported":
                    raise
                if (evidence.status != STATUS_UNAVAILABLE
                        or evidence.reason_codes != ("qoi_policy_unsupported",)
                        or evidence.compiled_partition is not None
                        or evidence.score_refusal_evidence is not None):
                    raise ValueError("unsupported shadow variant evidence is invalid") from error
                continue
            if thaw(evidence.compiled_partition) != expected_partition.to_dict():
                raise ValueError("compiled shadow variant does not match frozen policy")
            _partition_from_mapping(evidence.compiled_partition)
            if evidence.status == STATUS_COMPLETE:
                score_set = _score_set_from_mapping(evidence.score_set)
                if (scoring.seed_evidence is None
                        or score_set.seed != scoring.seed_evidence.seed
                        or score_set.simulation_count != config.simulation_count
                        or score_set.block_count != config.block_count
                        or score_set.design_measure_fingerprint
                        != scoring.design_measure["design_measure_fingerprint"]
                        or score_set.qoi_discretization_fingerprint
                        != expected_partition.discretization_fingerprint):
                    raise ValueError("shadow score set does not join its scoring inputs")
                source_draw = scoring.design_measure["source_draw_identity_fingerprint"]
                if score_set.source_draw_identity_fingerprint != source_draw:
                    raise ValueError("shadow score set source draws do not join design measure")
                pair_by_id = {
                    scoring.candidate_pair["legacy"]["candidate_id"]:
                        scoring.candidate_pair["legacy"],
                    scoring.candidate_pair["target_core"]["candidate_id"]:
                        scoring.candidate_pair["target_core"],
                }
                if tuple(item["candidate_id"] for item in score_set.candidate_scores) != tuple(
                        sorted(pair_by_id)):
                    raise ValueError("shadow candidate scores do not join candidate pair")
                for candidate_score in score_set.candidate_scores:
                    pair_candidate = pair_by_id[candidate_score["candidate_id"]]
                    expected_grid = _grid_fingerprint(
                        tuple(_energy_identity(item) for item in pair_candidate["energy_meV"]),
                        float(pair_candidate["exposure_per_point"]))
                    if (list(candidate_score["energy_meV"])
                            != list(pair_candidate["energy_meV"])
                            or candidate_score["executed_grid_fingerprint"]
                            != expected_grid
                            or candidate_score["executed_point_count"]
                            != pair_candidate["executed_point_count"]
                            or candidate_score["exposure_per_point"]
                            != pair_candidate["exposure_per_point"]
                            or candidate_score["source_draw_identity_fingerprint"] != source_draw):
                        raise ValueError("shadow candidate score does not join candidate pair/design")
                expected_delta = _normalized_comparison(
                    score_set, scoring.candidate_pair["legacy"]["candidate_id"],
                    scoring.candidate_pair["target_core"]["candidate_id"])
                if expected_delta is None or any(not math.isclose(
                        float(evidence.target_core_minus_legacy[name]), float(expected),
                        rel_tol=1e-12, abs_tol=1e-12)
                        for name, expected in expected_delta.items()):
                    raise ValueError("normalized shadow delta does not join Slice 4 evidence")
            refusal = evidence.score_refusal_evidence
            if refusal is not None:
                if evidence.status != STATUS_UNAVAILABLE or scoring.candidate_pair is None:
                    raise ValueError("score refusal evidence is attached to an invalid variant")
                if set(evidence.reason_codes) & {
                        "shadow_scoring_deadline", "shadow_scoring_failed",
                        "qoi_policy_unsupported"}:
                    raise ValueError("score refusal evidence is not an incomplete Slice 4 result")
                expected_candidates = tuple(sorted((
                    (scoring.candidate_pair["legacy"]["candidate_id"], _FAMILY_LEGACY),
                    (scoring.candidate_pair["target_core"]["candidate_id"], _FAMILY_TARGET),
                )))
                actual_candidates = tuple(
                    (item["candidate_id"], item["candidate_family"])
                    for item in refusal.candidate_refusals)
                if actual_candidates != expected_candidates:
                    raise ValueError("score refusal candidates do not join candidate pair")
                aggregate: list[str] = list(refusal.score_set_reason_codes)
                for item in refusal.candidate_refusals:
                    for code in item["reason_codes"]:
                        if code not in aggregate:
                            aggregate.append(code)
                if tuple(aggregate or ["score_set_incomplete"]) != evidence.reason_codes:
                    raise ValueError("score refusal reasons do not join variant evidence")
    return scoring


def _unavailable(inventory: Any, reason_code: str, common: Mapping[str, Any], *,
                  stage: str) -> ScanDesignShadowScoring:
    del inventory, stage
    return ScanDesignShadowScoring.unavailable(reason_code, **common)


def score_shadow_inventory(
        inventory: Any, *, predictive_artifact: Mapping[str, Any] | None,
        eligibility_input: EligibilityInput, policy_set: QoIDistinctionPolicySet | None,
        scoring_config: Any, scope_kind: str, scope_fingerprint: str,
        deadline_check: Callable[[], None] | None = None,
        predictive_evaluator: Callable[..., Any] = evaluate_master_predictive,
        scorer: Callable[..., Any] = score_scan_window_candidates,
) -> ScanDesignShadowScoring:
    """Score exactly legacy versus target-core without selecting either candidate."""
    check = deadline_check or (lambda: None)
    if getattr(getattr(inventory, "context", None), "scan_window_eig_mode", None) != "authoritative":
        return ScanDesignShadowScoring.not_requested()
    try:
        inventory = type(inventory).from_dict(inventory.to_full_dict())
    except (AttributeError, TypeError, ValueError):
        return ScanDesignShadowScoring.unavailable("scan_design_inventory_invalid")
    scoring_config = validate_scan_window_shadow_scoring_config(scoring_config)
    try:
        measure = validate_scan_window_design_measure(
            build_scan_window_design_measure(predictive_artifact, eligibility_input))
    except (TypeError, ValueError) as error:
        log.warning("shadow design-measure validation failed: %s", error)
        return ScanDesignShadowScoring.unavailable("design_measure_invalid")
    common: dict[str, Any] = {
        "scoring_config_fingerprint": scoring_config.scoring_config_fingerprint,
        "policy_set_fingerprint": None if policy_set is None else policy_set.policy_set_fingerprint,
        "scoring_config": scoring_config.to_dict(),
        "policy_set": None if policy_set is None else policy_set.to_dict(),
        "eligibility": thaw(dict(measure.eligibility)), "design_measure": _measure_summary(measure),
    }
    if measure.status != "available":
        reason = str((measure.reason_codes or measure.construction_codes
                      or ("design_measure_unavailable",))[0])
        return _unavailable(inventory, reason, common, stage="design_measure")
    try:
        legacy, target, pair = _candidate_pair(inventory)
    except ValueError as error:
        reason = str(error)
        log.info("shadow candidate pair refused: %s", reason)
        return _unavailable(inventory, reason, common, stage="candidate_pair")
    common["candidate_pair"] = pair
    qoi = dict(measure.qoi)
    match = {"qoi_id": qoi["id"], "qoi_unit": qoi["unit"], "scope_kind": scope_kind,
             "scope_fingerprint": scope_fingerprint, "matching_policy_ids": []}
    common["policy_match"] = match
    if policy_set is None:
        return _unavailable(inventory, "qoi_policy_missing", common, stage="policy_match")
    try:
        policies = validate_qoi_distinction_policy_set(policy_set)
        if len(policies.policies) > MAX_QOI_POLICIES:
            raise ValueError("QoI policy count exceeds the runtime bound")
        matches = matching_qoi_policies(
            policies, qoi_id=qoi["id"], qoi_unit=qoi["unit"],
            scope_kind=scope_kind, scope_fingerprint=scope_fingerprint)
    except (KeyError, TypeError, ValueError) as error:
        log.warning("shadow QoI policy validation failed: %s", error)
        return _unavailable(inventory, "qoi_policy_invalid", common, stage="policy_match")
    match["matching_policy_ids"] = [item.policy_id for item in matches]
    if not matches:
        # Name the mismatch: a policy that exists but binds a different campaign scope is the
        # common cause, and it is indistinguishable from "no policy at all" in the reason code.
        log.warning(
            "shadow QoI policy did not match: qoi=%s/%s scope_kind=%s scope_fingerprint=%s "
            "declared=%s", qoi["id"], qoi["unit"], scope_kind, scope_fingerprint,
            [(item.policy_id, dict(item.source_qoi), item.scope["kind"],
              item.scope["fingerprint"]) for item in policies.policies])
        return _unavailable(inventory, "qoi_policy_missing", common, stage="policy_match")
    if len(matches) != 1:
        return _unavailable(inventory, "qoi_policy_ambiguous", common, stage="policy_match")
    policy = matches[0]
    if len(policy.variants) > scoring_config.max_runtime_variants:
        return _unavailable(
            inventory, "qoi_policy_variant_limit_exceeded", common, stage="policy_compile")

    compiled: list[tuple[Any, Any]] = []
    variants: list[ScanDesignShadowVariant] = []
    for variant in policy.variants:
        try:
            partition = compile_qoi_distinction_policy(policy, variant)
        except ValueError as error:
            if str(error) != "qoi_policy_unsupported":
                raise
            variants.append(ScanDesignShadowVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status=STATUS_UNAVAILABLE, reason_codes=("qoi_policy_unsupported",)))
            continue
        compiled.append((variant, partition))
    if not compiled:
        variants.sort(key=lambda item: (item.policy_id, item.variant_id))
        return ScanDesignShadowScoring(
            status=STATUS_UNAVAILABLE, reason_codes=(), variants=tuple(variants),
            **common)

    seed = derive_shadow_seed(
        scoring_config_fingerprint=scoring_config.scoring_config_fingerprint,
        design_measure_fingerprint=measure.design_measure_fingerprint,
        candidate_pair_fingerprint=pair["candidate_pair_fingerprint"],
        seed_derivation_revision=scoring_config.seed_derivation_revision)
    check()
    legacy_evaluation = predictive_evaluator(predictive_artifact, legacy.energies, legacy.ncount)
    check()
    target_evaluation = predictive_evaluator(predictive_artifact, target.energies, target.ncount)
    candidate_inputs = (
        CandidateEvaluationInput(legacy.candidate_id, legacy_evaluation),
        CandidateEvaluationInput(target.candidate_id, target_evaluation))
    for index, (variant, partition) in enumerate(compiled):
        try:
            check()
            raw_score_set = scorer(
                measure, candidate_inputs, qoi_discretization=partition, seed=seed.seed,
                simulation_count=scoring_config.simulation_count,
                block_count=scoring_config.block_count)
            check()  # soft allowance also applies to a scorer that returns after the deadline
            score_set = validate_scan_window_score_set(raw_score_set)
        except Exception as error:  # noqa: BLE001 - bounded per-variant fail-open
            deadline = type(error).__name__ == "AnalysisLimitExceeded"
            reason = "shadow_scoring_deadline" if deadline else "shadow_scoring_failed"
            log.warning("shadow scoring variant %s/%s failed (%s): %s",
                        policy.policy_id, variant.variant_id, type(error).__name__, error)
            variants.append(ScanDesignShadowVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status=STATUS_UNAVAILABLE, reason_codes=(reason,),
                compiled_partition=partition.to_dict(),
                diagnostics={"error_type": type(error).__name__, "message": str(error)}))
            if deadline:
                for pending, pending_partition in compiled[index + 1:]:
                    variants.append(ScanDesignShadowVariant(
                        policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                        variant_id=pending.variant_id, variant_fingerprint=pending.variant_fingerprint,
                        status=STATUS_UNAVAILABLE, reason_codes=(reason,),
                        compiled_partition=pending_partition.to_dict()))
                break
            continue
        delta = (_normalized_comparison(score_set, legacy.candidate_id, target.candidate_id)
                 if score_set.status == STATUS_COMPLETE else None)
        if delta is None:
            codes = tuple(score_set.reason_codes)
            candidate_records = []
            family_by_id = {
                legacy.candidate_id: _FAMILY_LEGACY,
                target.candidate_id: _FAMILY_TARGET,
            }
            for item in score_set.candidate_scores:
                record = {
                    "candidate_id": item["candidate_id"],
                    "candidate_family": family_by_id[item["candidate_id"]],
                    "status": item["status"], "reason_codes": list(item["reason_codes"]),
                    "source_reason_code": item.get("source_reason_code"),
                }
                candidate_records.append(record)
                for code in item["reason_codes"]:
                    if code not in codes:
                        codes += (code,)
            codes = codes or ("score_set_incomplete",)
            refusal = ScoreRefusalEvidence(
                score_set_status=score_set.status,
                score_set_reason_codes=tuple(score_set.reason_codes),
                candidate_refusals=tuple(candidate_records))
            variants.append(ScanDesignShadowVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status=STATUS_UNAVAILABLE, reason_codes=codes,
                compiled_partition=partition.to_dict(), score_refusal_evidence=refusal))
        else:
            variants.append(ScanDesignShadowVariant(
                policy_id=policy.policy_id, policy_fingerprint=policy.policy_fingerprint,
                variant_id=variant.variant_id, variant_fingerprint=variant.variant_fingerprint,
                status=STATUS_COMPLETE, reason_codes=(), compiled_partition=partition.to_dict(),
                score_set=score_set.to_dict(), target_core_minus_legacy=delta,
                authority_scopes=_DIAGNOSTIC_AUTHORITY))
    variants.sort(key=lambda item: (item.policy_id, item.variant_id))
    complete = sum(item.status == STATUS_COMPLETE for item in variants)
    status = STATUS_COMPLETE if complete == len(variants) else STATUS_PARTIAL if complete else STATUS_UNAVAILABLE
    return ScanDesignShadowScoring(
        status=status, reason_codes=(), variants=tuple(variants), seed_evidence=seed,
        authority_scopes=_DIAGNOSTIC_AUTHORITY if complete else _INELIGIBLE_AUTHORITY,
        **common)


__all__ = [
    "SHADOW_SCORING_CONTRACT", "SHADOW_SEED_DOMAIN", "SHADOW_VARIANT_CONTRACT",
    "STATUS_COMPLETE", "STATUS_NOT_REQUESTED", "STATUS_PARTIAL", "STATUS_UNAVAILABLE",
    "ScanDesignShadowScoring", "ScanDesignShadowVariant", "ScoreRefusalEvidence",
    "ShadowSeedEvidence", "derive_shadow_seed", "score_shadow_inventory",
    "validate_scan_design_shadow_scoring", "validate_shadow_candidate_pair",
]
