"""Drive-side construction and persistence for Slice 7 realized EIG comparisons."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from isar.drive.acquisition.acquire import ScanProposal
from isar.drive.acquisition.grid_adapters import DEFAULT_GRID_ADAPTER
from isar.drive.acquisition.inventory import canonical_json
from isar.drive.acquisition.scan_design import (
    FAMILY_LEGACY_UNIFORM,
    FAMILY_TARGET_CORE,
    ScanDesignInventory,
    load_scan_design_inventory,
    validate_scan_design_inventory,
)
from isar.drive.acquisition.scan_grid import UniformGrid
from isar.eig.shadow_inventory import (
    _normalized_comparison,
    _partition_from_mapping,
    _score_set_from_mapping,
    validate_scan_design_shadow_scoring,
)
from isar.eig.policy import compile_qoi_distinction_policy
from isar.schema.eig_policy import QoIDistinctionPolicySet
from isar.schema.eig_realized import (
    RealizedCandidatePair,
    RealizedCandidateRefusal,
    RealizedScanRequest,
    RealizedScoreVariant,
    RealizedSeedEvidence,
    ScanDesignRealizedComparison,
    derive_realized_seed,
    validate_scan_design_realized_comparison,
)
from isar.schema.eig_shadow import DIAGNOSTIC_AUTHORITY, INELIGIBLE_AUTHORITY, thaw
from isar.schema.statistics import semantics_fingerprint


REALIZED_COMPARISON_ALLOWANCE_SECONDS = 5.0
REALIZED_COMPARISON_REF_KIND = "scan_design_realized_comparison"

Finalizer = Callable[..., None]
Rescorer = Callable[[RealizedCandidatePair, float], tuple[
    tuple[RealizedScoreVariant, ...], RealizedSeedEvidence | None]]


def _source_candidate(inventory: ScanDesignInventory, family: str):
    matches = [item for item in inventory.candidates if item.candidate_family == family]
    return matches[0] if len(matches) == 1 else None


def _planned_pair_fingerprint(inventory: ScanDesignInventory) -> str:
    scoring = inventory.shadow_scoring
    pair = getattr(scoring, "candidate_pair", None)
    if isinstance(pair, Mapping) and isinstance(pair.get("candidate_pair_fingerprint"), str):
        return str(pair["candidate_pair_fingerprint"])
    return semantics_fingerprint("isar.scan_design_planned_pair_or_refusal/o36.7", {
        "context_fingerprint": inventory.context.fingerprint,
        "candidates": [{"candidate_id": item.candidate_id,
                        "candidate_family": item.candidate_family,
                        "grid_fingerprint": item.grid.fingerprint(item.ncount)}
                       for item in inventory.candidates],
        "refusals": [item.to_dict() for item in inventory.refusals],
    })


def _request(candidate: Any, proposal: ScanProposal, *, validation_status: str,
             adjustments: Sequence[str]) -> RealizedScanRequest:
    grid = UniformGrid(
        e_lo=float(proposal.e_lo), e_hi=float(proposal.e_hi), e_step=float(proposal.e_step),
        n_points=int(proposal.n_points), adapter=DEFAULT_GRID_ADAPTER)
    fingerprint = (proposal.meta or {}).get("proposal_fingerprint")
    if not isinstance(fingerprint, str):
        raise ValueError("realized_candidate_invalid")
    return RealizedScanRequest(
        source_candidate_id=candidate.candidate_id,
        candidate_family=candidate.candidate_family,
        source_grid_fingerprint=candidate.grid.fingerprint(candidate.ncount),
        validation_status=validation_status,
        hkl=(float(proposal.h), float(proposal.k), float(proposal.l)),
        e_lo=float(proposal.e_lo), e_hi=float(proposal.e_hi), e_step=float(proposal.e_step),
        scan_command=proposal.scan_command(), grid_adapter=grid.adapter.identity(),
        executed_energies=grid.energies, grid_fingerprint=grid.fingerprint(proposal.ncount),
        executed_point_count=grid.n_points, ncount=int(proposal.ncount),
        point_neutron_cost=grid.n_points * int(proposal.ncount),
        request_fingerprint=fingerprint, adjustments=tuple(adjustments))


def _target_meta(proposal: ScanProposal, candidate: Any) -> dict[str, Any]:
    source = proposal.meta if isinstance(proposal.meta, dict) else {}
    meta = {key: copy.deepcopy(source.get(key)) for key in (
        "acquisition_action_role", "purpose", "target_branch_role", "rival_branch_role",
        "authoritative_evidence_revision", "scientific_action_fingerprint", "target_band",
    ) if key in source}
    diagnostics = dict(candidate.count_diagnostics)
    diagnostics.pop("runtime_ncount_limit", None)
    diagnostics.pop("limit_clamp_logged", None)
    meta["count_diagnostics"] = diagnostics
    return meta


def construct_realized_pair(
        proposal: ScanProposal,
        inventory: ScanDesignInventory,
        objective: Any,
        cfg: Any,
        budget_state: Mapping[str, Any],
        *,
        it: int,
        finalizer: Finalizer,
        legacy_adjustments: Sequence[str] = (),
) -> RealizedCandidatePair:
    """Construct the exact selected request and equal-exposure counterfactual in isolation."""
    inventory = validate_scan_design_inventory(inventory)
    legacy_source = _source_candidate(inventory, FAMILY_LEGACY_UNIFORM)
    if legacy_source is None:
        raise ValueError("realized_pair_unavailable")
    legacy_adjustment_list = list(legacy_adjustments)
    if ((float(proposal.e_lo), float(proposal.e_hi), float(proposal.e_step), int(proposal.ncount))
            != (float(legacy_source.e_lo), float(legacy_source.e_hi),
                float(legacy_source.e_step), int(legacy_source.ncount))
            and "runtime_finalization" not in legacy_adjustment_list):
        legacy_adjustment_list.append("runtime_finalization")
    legacy = _request(
        legacy_source, proposal, validation_status="submitted_legacy",
        adjustments=legacy_adjustment_list)

    target_source = _source_candidate(inventory, FAMILY_TARGET_CORE)
    if target_source is None:
        refusals = [item for item in inventory.refusals
                    if item.candidate_family == FAMILY_TARGET_CORE]
        reason = refusals[0].reason_code if len(refusals) == 1 else "realized_pair_unavailable"
        target: RealizedScanRequest | RealizedCandidateRefusal = RealizedCandidateRefusal(
            source_candidate_id=None, candidate_family=FAMILY_TARGET_CORE,
            source_grid_fingerprint=None, reason_codes=(reason,))
        return RealizedCandidatePair(_planned_pair_fingerprint(inventory), legacy, target)

    target_proposal = ScanProposal(
        h=float(proposal.h), k=float(proposal.k), l=float(proposal.l),
        e_lo=float(target_source.e_lo), e_hi=float(target_source.e_hi),
        e_step=float(target_source.e_step), ncount=int(legacy.ncount),
        kind=str(proposal.kind), reason=str(proposal.reason), e_pred=float(proposal.e_pred),
        fwhm_dE=float(proposal.fwhm_dE), n_points=int(target_source.n_points),
        acquisition_id=proposal.acquisition_id, meta=_target_meta(proposal, target_source),
        score_recompute=None, intent=proposal.intent)
    isolated_budget = copy.deepcopy(dict(budget_state or {}))
    try:
        finalizer(
            target_proposal, objective, cfg, isolated_budget, it=it, event_sink=None)
    except Exception:  # noqa: BLE001 - counterfactual refusal must not suppress the legacy action
        target = RealizedCandidateRefusal(
            source_candidate_id=target_source.candidate_id,
            candidate_family=FAMILY_TARGET_CORE,
            source_grid_fingerprint=target_source.grid.fingerprint(target_source.ncount),
            reason_codes=("realized_candidate_invalid",))
        return RealizedCandidatePair(_planned_pair_fingerprint(inventory), legacy, target)
    if int(target_proposal.ncount) != int(legacy.ncount):
        target = RealizedCandidateRefusal(
            source_candidate_id=target_source.candidate_id,
            candidate_family=FAMILY_TARGET_CORE,
            source_grid_fingerprint=target_source.grid.fingerprint(target_source.ncount),
            reason_codes=("realized_exposure_mismatch",))
    else:
        adjustments = []
        if ((target_proposal.e_lo, target_proposal.e_hi, target_proposal.e_step,
             target_proposal.n_points) != (target_source.e_lo, target_source.e_hi,
                                           target_source.e_step, target_source.n_points)):
            adjustments.append("runtime_finalization")
        target = _request(
            target_source, target_proposal,
            validation_status="not_instrument_validated_counterfactual",
            adjustments=adjustments)
    return RealizedCandidatePair(_planned_pair_fingerprint(inventory), legacy, target)


def _pair_matches_source(inventory: ScanDesignInventory,
                         pair: RealizedCandidatePair) -> bool:
    source = inventory.shadow_scoring.candidate_pair
    if not isinstance(source, Mapping) or not isinstance(pair.target_core, RealizedScanRequest):
        return False
    for name, realized in (("legacy", pair.legacy), ("target_core", pair.target_core)):
        planned = source.get(name)
        if (not isinstance(planned, Mapping)
                or planned.get("candidate_id") != realized.source_candidate_id
                or planned.get("candidate_family") != realized.candidate_family
                or tuple(planned.get("energy_meV") or ()) != realized.executed_energies
                or int(planned.get("exposure_per_point") or 0) != realized.ncount):
            return False
    return True


def _source_fields(inventory: ScanDesignInventory, reference: Mapping[str, Any]) -> dict[str, Any]:
    scoring = validate_scan_design_shadow_scoring(
        inventory.shadow_scoring, inventory=inventory)
    design = thaw(scoring.design_measure) if scoring.design_measure is not None else None
    eligibility = thaw(scoring.eligibility) if scoring.eligibility is not None else None
    return {
        "source_inventory_ref": dict(reference),
        "source_inventory_fingerprint": inventory.content_hash(),
        "source_shadow_scoring_fingerprint": scoring.shadow_scoring_fingerprint,
        "source_shadow_variant_fingerprints": tuple(
            item.variant_evidence_fingerprint for item in scoring.variants),
        "source_scoring_config_fingerprint": scoring.scoring_config_fingerprint,
        "source_policy_set_fingerprint": scoring.policy_set_fingerprint,
        "source_eligibility_fingerprint": (None if eligibility is None else semantics_fingerprint(
            "isar.scan_design_realized_source_eligibility/o36.7", eligibility)),
        "source_design_measure_fingerprint": (
            design.get("design_measure_fingerprint") if isinstance(design, Mapping) else None),
        "source_identities": (
            design.get("source_identities") if isinstance(design, Mapping) else None),
    }


def _reused_variants(inventory: ScanDesignInventory) -> tuple[RealizedScoreVariant, ...]:
    variants = []
    for source in inventory.shadow_scoring.variants:
        if source.status == "complete":
            score_set = thaw(source.score_set)
            variants.append(RealizedScoreVariant(
                policy_id=source.policy_id, policy_fingerprint=source.policy_fingerprint,
                variant_id=source.variant_id, variant_fingerprint=source.variant_fingerprint,
                status="complete", score_disposition="reused",
                source_variant_evidence_fingerprint=source.variant_evidence_fingerprint,
                source_score_set_fingerprint=score_set["score_set_fingerprint"],
                compiled_partition=thaw(source.compiled_partition),
                target_core_minus_legacy=thaw(source.target_core_minus_legacy),
                authority_scopes=DIAGNOSTIC_AUTHORITY))
        else:
            variants.append(RealizedScoreVariant(
                policy_id=source.policy_id, policy_fingerprint=source.policy_fingerprint,
                variant_id=source.variant_id, variant_fingerprint=source.variant_fingerprint,
                status="unavailable", score_disposition="unavailable",
                reason_codes=tuple(source.reason_codes),
                source_variant_evidence_fingerprint=source.variant_evidence_fingerprint,
                compiled_partition=thaw(source.compiled_partition),
                authority_scopes=INELIGIBLE_AUTHORITY))
    return tuple(variants)


def build_realized_comparison(
        inventory: ScanDesignInventory,
        inventory_ref: Mapping[str, Any],
        pair: RealizedCandidatePair,
        *,
        remaining_seconds: float,
        rescorer: Rescorer | None,
        unavailable_reason: str | None = None,
) -> ScanDesignRealizedComparison:
    source = _source_fields(inventory, inventory_ref)
    if _pair_matches_source(inventory, pair) and inventory.shadow_scoring.variants:
        variants = _reused_variants(inventory)
        complete = any(item.status == "complete" for item in variants)
        reasons = tuple(dict.fromkeys(
            code for variant in variants for code in variant.reason_codes))
        return ScanDesignRealizedComparison(
            **source, realized_pair=pair,
            score_disposition="reused" if complete else "unavailable", variants=variants,
            reason_codes=() if complete else reasons or ("realized_score_incomplete",),
            authority_scopes=DIAGNOSTIC_AUTHORITY if complete else INELIGIBLE_AUTHORITY)
    if not isinstance(pair.target_core, RealizedScanRequest):
        return ScanDesignRealizedComparison(
            **source, realized_pair=pair, score_disposition="unavailable", variants=(),
            reason_codes=tuple(pair.target_core.reason_codes), authority_scopes=INELIGIBLE_AUTHORITY)
    if rescorer is None or remaining_seconds <= 0.0:
        reason = (unavailable_reason or "realized_scoring_deadline"
                  if remaining_seconds <= 0.0 else unavailable_reason
                  or "realized_predictive_evaluation_unavailable")
        return ScanDesignRealizedComparison(
            **source, realized_pair=pair, score_disposition="unavailable", variants=(),
            reason_codes=(reason,), authority_scopes=INELIGIBLE_AUTHORITY)
    variants, seed = rescorer(pair, remaining_seconds)
    if (seed is None
            or seed.scoring_config_fingerprint != source["source_scoring_config_fingerprint"]
            or seed.design_measure_fingerprint != source["source_design_measure_fingerprint"]
            or seed.realized_pair_fingerprint != pair.realized_pair_fingerprint):
        raise ValueError("realized scoring did not preserve authenticated source identities")
    complete = any(item.status == "complete" for item in variants)
    if not variants:
        return ScanDesignRealizedComparison(
            **source, realized_pair=pair, score_disposition="unavailable", variants=(),
            seed_evidence=seed, reason_codes=("realized_score_incomplete",),
            authority_scopes=INELIGIBLE_AUTHORITY)
    if not complete:
        reasons = tuple(dict.fromkeys(
            code for variant in variants for code in variant.reason_codes))
        return ScanDesignRealizedComparison(
            **source, realized_pair=pair, score_disposition="unavailable", variants=variants,
            seed_evidence=seed, reason_codes=reasons or ("realized_score_incomplete",),
            authority_scopes=INELIGIBLE_AUTHORITY)
    return ScanDesignRealizedComparison(
        **source, realized_pair=pair, score_disposition="rescored", variants=variants,
        seed_evidence=seed, reason_codes=(),
        authority_scopes=DIAGNOSTIC_AUTHORITY if complete else INELIGIBLE_AUTHORITY)


def persist_realized_comparison(workspace: str | Path,
                                comparison: ScanDesignRealizedComparison) -> dict[str, Any]:
    comparison = validate_scan_design_realized_comparison(comparison)
    digest = comparison.content_hash()
    root = Path(workspace)
    path = root / "report" / "scan-design-realizations" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json(comparison.to_dict()).encode("utf-8")
    if path.exists() and path.read_bytes() != encoded:
        raise ValueError("stored realized comparison does not match its content address")
    if not path.exists():
        temporary = path.with_suffix(".json.tmp")
        with open(temporary, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    return {"kind": REALIZED_COMPARISON_REF_KIND, "sha256": digest,
            "path": f"report/scan-design-realizations/{digest}.json"}


def load_scan_design_realized_comparison(
        reference: Mapping[str, Any], workspace: str | Path) -> ScanDesignRealizedComparison:
    if (not isinstance(reference, Mapping) or set(reference) != {"kind", "sha256", "path"}
            or reference.get("kind") != REALIZED_COMPARISON_REF_KIND):
        raise ValueError("realized-comparison reference is invalid")
    digest = reference.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or not all(
            item in "0123456789abcdef" for item in digest):
        raise ValueError("realized-comparison digest is invalid")
    relative = f"report/scan-design-realizations/{digest}.json"
    if str(reference.get("path", "")).replace("\\", "/") != relative:
        raise ValueError("realized-comparison path is invalid")
    root = Path(workspace).resolve()
    allowed = (root / "report" / "scan-design-realizations").resolve()
    path = (root / Path(reference["path"])).resolve()
    if path.parent != allowed or path.name != f"{digest}.json":
        raise ValueError("realized-comparison reference escapes its directory")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("realized-comparison artifact is unreadable") from error
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("realized-comparison raw-byte address is invalid")
    if hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest() != digest:
        raise ValueError("realized-comparison semantic address is invalid")
    comparison = ScanDesignRealizedComparison.from_dict(payload)
    if comparison.content_hash() != digest:
        raise ValueError("reconstructed realized-comparison address is invalid")
    inventory = load_scan_design_inventory(comparison.source_inventory_ref, root)
    if inventory.content_hash() != comparison.source_inventory_fingerprint:
        raise ValueError("realized-comparison source inventory is invalid")
    validate_scan_design_shadow_scoring(inventory.shadow_scoring, inventory=inventory)
    if inventory.shadow_scoring.shadow_scoring_fingerprint != (
            comparison.source_shadow_scoring_fingerprint):
        raise ValueError("realized-comparison source shadow evidence is invalid")
    if comparison.realized_pair.planned_pair_fingerprint != _planned_pair_fingerprint(inventory):
        raise ValueError("realized-comparison planned pair identity is invalid")
    source_candidates = {item.candidate_id: item for item in inventory.candidates}
    realized_requests = [comparison.realized_pair.legacy]
    if isinstance(comparison.realized_pair.target_core, RealizedScanRequest):
        realized_requests.append(comparison.realized_pair.target_core)
    else:
        refusal = comparison.realized_pair.target_core
        if refusal.source_candidate_id is not None:
            source_candidate = source_candidates.get(refusal.source_candidate_id)
            if (source_candidate is None
                    or source_candidate.candidate_family != refusal.candidate_family
                    or source_candidate.grid.fingerprint(source_candidate.ncount)
                    != refusal.source_grid_fingerprint):
                raise ValueError("realized refusal source candidate is invalid")
    for request in realized_requests:
        source_candidate = source_candidates.get(request.source_candidate_id)
        if (source_candidate is None
                or source_candidate.candidate_family != request.candidate_family
                or source_candidate.grid.fingerprint(source_candidate.ncount)
                != request.source_grid_fingerprint):
            raise ValueError("realized request source candidate is invalid")
        grid = UniformGrid(
            request.e_lo, request.e_hi, request.e_step, request.executed_point_count,
            adapter=DEFAULT_GRID_ADAPTER)
        if (canonical_json(grid.adapter.identity()) != canonical_json(thaw(request.grid_adapter))
                or request.scan_command != grid.scan_command()
                or request.executed_energies != grid.energies
                or request.grid_fingerprint != grid.fingerprint(request.ncount)):
            raise ValueError("realized request grid does not reproduce")
    expected_source = _source_fields(inventory, comparison.source_inventory_ref)
    for name, expected in expected_source.items():
        actual = getattr(comparison, name)
        if canonical_json(thaw(actual)) != canonical_json(thaw(expected)):
            raise ValueError(f"realized-comparison {name} is invalid")
    policy_variants = {}
    if comparison.variants:
        if inventory.shadow_scoring.policy_set is None:
            raise ValueError("realized variants have no authenticated policy set")
        policies = QoIDistinctionPolicySet.from_dict(thaw(inventory.shadow_scoring.policy_set))
        for policy in policies.policies:
            for policy_variant in policy.variants:
                policy_variants[(policy.policy_id, policy_variant.variant_id)] = (
                    policy, policy_variant)
    if comparison.score_disposition == "reused" or (
            comparison.variants and all(
                item.source_variant_evidence_fingerprint is not None
                for item in comparison.variants)):
        expected_variants = _reused_variants(inventory)
        if canonical_json([item.to_dict() for item in comparison.variants]) != canonical_json(
                [item.to_dict() for item in expected_variants]):
            raise ValueError("reused realized variants do not reproduce from source evidence")
    if comparison.seed_evidence is not None:
        if (comparison.source_scoring_config_fingerprint is None
                or comparison.source_design_measure_fingerprint is None):
            raise ValueError("realized seed lacks authenticated source inputs")
        expected_seed = derive_realized_seed(
            scoring_config_fingerprint=comparison.source_scoring_config_fingerprint,
            design_measure_fingerprint=comparison.source_design_measure_fingerprint,
            realized_pair_fingerprint=comparison.realized_pair.realized_pair_fingerprint)
        if comparison.seed_evidence.to_dict() != expected_seed.to_dict():
            raise ValueError("realized seed evidence does not reproduce")
    for variant in comparison.variants:
        source_policy = policy_variants.get((variant.policy_id, variant.variant_id))
        if (source_policy is None
                or variant.policy_fingerprint != source_policy[0].policy_fingerprint
                or variant.variant_fingerprint != source_policy[1].variant_fingerprint):
            raise ValueError("realized variant policy identity is invalid")
        if variant.compiled_partition is not None:
            persisted_partition = _partition_from_mapping(variant.compiled_partition)
            try:
                expected_partition = compile_qoi_distinction_policy(*source_policy)
            except ValueError as error:
                raise ValueError("realized compiled partition is not policy-derived") from error
            if persisted_partition.to_dict() != expected_partition.to_dict():
                raise ValueError("realized compiled partition is invalid")
        if variant.score_set is None:
            continue
        score_set = _score_set_from_mapping(variant.score_set)
        if (score_set.design_measure_fingerprint
                != comparison.source_design_measure_fingerprint):
            raise ValueError("realized score-set design measure is invalid")
        if comparison.seed_evidence is None or score_set.seed != comparison.seed_evidence.seed:
            raise ValueError("realized score-set seed is invalid")
        candidate_ids = {item["candidate_id"] for item in score_set.to_dict()["candidate_scores"]}
        expected_ids = {
            comparison.realized_pair.legacy.realized_request_fingerprint,
            comparison.realized_pair.target_core.realized_request_fingerprint,
        }
        if candidate_ids != expected_ids:
            raise ValueError("realized score-set candidate identities are invalid")
        normalized = _normalized_comparison(
            score_set,
            comparison.realized_pair.legacy.realized_request_fingerprint,
            comparison.realized_pair.target_core.realized_request_fingerprint)
        if canonical_json(normalized) != canonical_json(thaw(variant.target_core_minus_legacy)):
            raise ValueError("realized score-set comparison is invalid")
    return comparison


def attach_realized_metadata(proposal: ScanProposal, reference: Mapping[str, Any],
                             comparison: ScanDesignRealizedComparison) -> None:
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    meta.update({
        "scan_design_realized_comparison_ref": dict(reference),
        "scan_design_realized_comparison_fingerprint": comparison.realized_comparison_fingerprint,
        "scan_design_realized_variant_fingerprints": list(
            comparison.realized_variant_fingerprints),
        "scan_design_realized_selected_request_fingerprint": (
            comparison.selected_request_fingerprint),
        "scan_design_realized_selected_grid_fingerprint": comparison.selected_grid_fingerprint,
        "scan_design_eig_applicability": "applicable",
    })


__all__ = [
    "REALIZED_COMPARISON_ALLOWANCE_SECONDS", "attach_realized_metadata",
    "build_realized_comparison", "construct_realized_pair",
    "load_scan_design_realized_comparison", "persist_realized_comparison",
]
