"""Persistence and runtime adaptation for restricted O41 scan-window authority."""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from isar.drive.acquisition.grid_adapters import TAVI_GRID_ADAPTER
from isar.drive.acquisition.inventory import canonical_json
from isar.drive.acquisition.realized_comparison import (
    load_scan_design_realized_comparison,
)
from isar.drive.acquisition.scan_design import (
    load_scan_design_inventory,
    validate_scan_design_inventory,
)
from isar.drive.acquisition.scan_grid import UniformGrid, resolve_point_count
from isar.eig.o41_authority import (
    O41AuthoritativeProfile,
    O41CandidateEvidence,
    O41PartitionOffsetEvidence,
    O41ScoreEvidence,
    O41SelectorInput,
    OUTCOME_UNAVAILABLE,
    PRIMARY_PARAMETERS,
    PRIMARY_POLICY_ID,
    PRIMARY_VARIANT_ID,
    ROLE_LEGACY,
    ROLE_TARGET,
    select_o41_authority,
)
from isar.eig.shadow_inventory import _score_set_from_mapping
from isar.schema.eig_authority import (
    ScanDesignAuthorityDecision,
    validate_scan_design_authority_decision,
)
from isar.schema.eig_realized import RealizedScanRequest, ScanDesignRealizedComparison
from isar.schema.eig_shadow import thaw


AUTHORITY_DECISION_REF_KIND = "scan_design_authority_decision"


def _candidate_evidence(request: RealizedScanRequest, role: str, *, validation_status: str,
                        scored_grid_join_status: str = "matched") -> O41CandidateEvidence:
    return O41CandidateEvidence(
        role=role,
        candidate_id=request.source_candidate_id,
        request_fingerprint=request.request_fingerprint,
        grid_fingerprint=request.grid_fingerprint,
        e_lo=request.e_lo,
        e_hi=request.e_hi,
        executed_energies=request.executed_energies,
        ncount=request.ncount,
        scan_command=request.scan_command,
        validated_request_status=validation_status,
        scored_grid_join_status=scored_grid_join_status,
    )


def _refused_target_evidence(inventory: Any, refusal: Any) -> O41CandidateEvidence:
    """Retain the authenticated planned target identity when realization refused it."""
    candidate = next((item for item in inventory.candidates
                      if item.candidate_family == "target_core/1"
                      and (refusal.source_candidate_id is None
                           or item.candidate_id == refusal.source_candidate_id)), None)
    if candidate is None:
        # Some target families are refused by the generator itself, before an inventory candidate
        # can exist (for example a quicklook target clipped back to the complete legacy domain).
        # The refusal is still content-addressed evidence.  Reconstruct its attempted grid where
        # diagnostics permit it; otherwise use the authenticated legacy envelope only as the
        # schema's unavailable-grid carrier.  The refused/unavailable statuses prevent authority.
        planned_refusal = next((item for item in inventory.refusals
                                if item.candidate_family == "target_core/1"), None)
        if planned_refusal is None:
            raise ValueError("target_core refusal has no inventory refusal evidence")
        refusal_id = hashlib.sha256(
            canonical_json(planned_refusal.to_dict()).encode("utf-8")).hexdigest()
        detail = dict(planned_refusal.detail)
        legacy = inventory.selected_candidate
        try:
            grid = UniformGrid(
                float(detail["e_lo"]), float(detail["e_hi"]), float(detail["e_step"]),
                int(detail["n_points"]), adapter=TAVI_GRID_ADAPTER)
        except (KeyError, TypeError, ValueError):
            grid = legacy.grid
        return O41CandidateEvidence(
            role=ROLE_TARGET,
            candidate_id=refusal_id,
            request_fingerprint=refusal_id,
            grid_fingerprint=grid.fingerprint(legacy.ncount),
            e_lo=grid.e_lo,
            e_hi=grid.e_hi,
            executed_energies=grid.energies,
            ncount=legacy.ncount,
            scan_command=grid.scan_command(),
            validated_request_status="refused",
            scored_grid_join_status="unavailable",
        )
    grid = candidate.grid
    if (refusal.source_grid_fingerprint is not None
            and refusal.source_grid_fingerprint != grid.fingerprint(candidate.ncount)):
        raise ValueError("target_core refusal grid does not join its inventory candidate")
    return O41CandidateEvidence(
        role=ROLE_TARGET,
        candidate_id=candidate.candidate_id,
        # No instrument-validated request exists.  The immutable candidate id is used as the
        # planned-request identity; the refused validation status prevents it granting authority.
        request_fingerprint=candidate.candidate_id,
        grid_fingerprint=grid.fingerprint(candidate.ncount),
        e_lo=candidate.e_lo,
        e_hi=candidate.e_hi,
        executed_energies=grid.energies,
        ncount=candidate.ncount,
        scan_command=grid.scan_command(),
        validated_request_status="refused",
        scored_grid_join_status="unavailable",
    )


def _partition_offset_evidence(
        score_set: Any, partition: Mapping[str, Any], legacy_ids: set[str], target_ids: set[str],
) -> tuple[O41PartitionOffsetEvidence, ...] | None:
    """Pair each compiled offset's retained per-candidate information for the selector.

    ``partition_information_nats`` is the mean information the scoring run already computed for
    each offset of the compiled partition, in the partition's own offset order.  Anything that
    does not join exactly returns ``None`` so the caller fails closed instead of granting on the
    pooled margin alone.
    """
    offsets = [float(value) for value in partition["offsets"]]
    if not offsets:
        return None
    scores = {str(item.get("candidate_id")): item for item in score_set.candidate_scores}
    legacy = next((scores[key] for key in scores if key in legacy_ids), None)
    target = next((scores[key] for key in scores if key in target_ids), None)
    if legacy is None or target is None:
        return None
    legacy_information = [float(value) for value in legacy.get("partition_information_nats") or ()]
    target_information = [float(value) for value in target.get("partition_information_nats") or ()]
    if len(legacy_information) != len(offsets) or len(target_information) != len(offsets):
        return None
    try:
        return tuple(
            O41PartitionOffsetEvidence(
                offset=offset,
                legacy_information_nats=legacy_value,
                target_core_information_nats=target_value)
            for offset, legacy_value, target_value
            in zip(offsets, legacy_information, target_information))
    except ValueError:
        return None


def _primary_score_evidence(inventory: Any, comparison: ScanDesignRealizedComparison,
                            legacy: RealizedScanRequest,
                            target: RealizedScanRequest) -> O41ScoreEvidence:
    primary = next((item for item in comparison.variants
                    if item.policy_id == PRIMARY_POLICY_ID
                    and item.variant_id == PRIMARY_VARIANT_ID), None)
    if primary is None or primary.status != "complete":
        return O41ScoreEvidence("unavailable", "target_core_minus_legacy", ())
    partition = thaw(primary.compiled_partition)
    if (not isinstance(partition, Mapping)
            or partition.get("qoi_id") != "excitation.energy"
            or partition.get("qoi_unit") != "meV"
            or partition.get("origin") != PRIMARY_PARAMETERS["origin"]
            or partition.get("bin_width") != PRIMARY_PARAMETERS["bin_width"]
            or partition.get("offsets") != PRIMARY_PARAMETERS["offsets"]):
        return O41ScoreEvidence(
            "unavailable", "target_core_minus_legacy", (), evidence_join_status="mismatch")
    score_payload = thaw(primary.score_set)
    if score_payload is None:
        source = next((item for item in inventory.shadow_scoring.variants
                       if item.policy_id == PRIMARY_POLICY_ID
                       and item.variant_id == PRIMARY_VARIANT_ID), None)
        score_payload = None if source is None else thaw(source.score_set)
    if not isinstance(score_payload, Mapping):
        return O41ScoreEvidence("unavailable", "target_core_minus_legacy", ())
    try:
        score_set = _score_set_from_mapping(score_payload)
    except (TypeError, ValueError, KeyError):
        return O41ScoreEvidence(
            "unavailable", "target_core_minus_legacy", (), evidence_join_status="mismatch")
    legacy_ids = {legacy.realized_request_fingerprint, legacy.source_candidate_id}
    target_ids = {target.realized_request_fingerprint, target.source_candidate_id}
    pair = next((item for item in score_set.pairwise_comparisons
                 if ((item.get("candidate_a_id") in legacy_ids
                      and item.get("candidate_b_id") in target_ids)
                     or (item.get("candidate_b_id") in legacy_ids
                         and item.get("candidate_a_id") in target_ids))), None)
    if pair is None:
        return O41ScoreEvidence("unavailable", "target_core_minus_legacy", ())
    sign = 1.0 if pair["candidate_b_id"] in target_ids else -1.0
    blocks = tuple(sign * float(value) for value in
                   pair["paired_block_deltas_per_executed_point_nats"])
    offsets = _partition_offset_evidence(score_set, partition, legacy_ids, target_ids)
    if offsets is None:
        return O41ScoreEvidence(
            "unavailable", "target_core_minus_legacy", (), evidence_join_status="mismatch")
    return O41ScoreEvidence(
        "complete", "target_core_minus_legacy", blocks, partition_offsets=offsets)


def _validation_mapping(evidence: O41CandidateEvidence, adjustments: tuple[str, ...]) -> dict:
    return {
        "role": evidence.role,
        "candidate_id": evidence.candidate_id,
        "request_fingerprint": evidence.request_fingerprint,
        "grid_fingerprint": evidence.grid_fingerprint,
        "validated_request_status": evidence.validated_request_status,
        "scored_grid_join_status": evidence.scored_grid_join_status,
        "executed_point_count": evidence.point_count,
        "ncount": evidence.ncount,
        "adjustments": list(adjustments),
    }


def _selected_request(request: RealizedScanRequest, role: str) -> dict[str, Any]:
    return {
        "role": role,
        "candidate_family": request.candidate_family,
        "candidate_id": request.source_candidate_id,
        "request_fingerprint": request.request_fingerprint,
        "grid_fingerprint": request.grid_fingerprint,
        "hkl": list(request.hkl),
        "energy_window": {"e_lo": request.e_lo, "e_hi": request.e_hi,
                          "e_step": request.e_step},
        "executed_energies": list(request.executed_energies),
        "executed_point_count": request.executed_point_count,
        "ncount": request.ncount,
        "point_neutron_cost": request.point_neutron_cost,
        "scan_command": request.scan_command,
    }


def build_authority_decision(
        *, inventory: Any, inventory_ref: Mapping[str, Any],
        comparison: ScanDesignRealizedComparison, comparison_ref: Mapping[str, Any],
        profile: O41AuthoritativeProfile, legacy_validation_status: str,
        target_validation_status: str, applicability_status: str = "applicable",
        refresh_count: int = 0,
) -> ScanDesignAuthorityDecision:
    """Authenticate the exact parents, apply O41, and construct the durable decision."""
    inventory = validate_scan_design_inventory(inventory)
    if inventory.content_hash() != comparison.source_inventory_fingerprint:
        raise ValueError("authority inventory does not match realized comparison")
    legacy_request = comparison.realized_pair.legacy
    target_request = comparison.realized_pair.target_core
    legacy = _candidate_evidence(
        legacy_request, ROLE_LEGACY, validation_status=legacy_validation_status)
    target = (_candidate_evidence(
        target_request, ROLE_TARGET, validation_status=target_validation_status)
        if isinstance(target_request, RealizedScanRequest)
        else _refused_target_evidence(inventory, target_request))
    eligibility = thaw(inventory.shadow_scoring.eligibility)
    eligibility_status = (
        str(eligibility.get("status")) if isinstance(eligibility, Mapping) else "unassessable")
    score = (_primary_score_evidence(inventory, comparison, legacy_request, target_request)
             if isinstance(target_request, RealizedScanRequest)
             else O41ScoreEvidence("unavailable", "target_core_minus_legacy", (),
                                   evidence_join_status="unavailable"))
    result = select_o41_authority(O41SelectorInput(
        profile=profile, eligibility_status=eligibility_status,
        applicability_status=applicability_status, legacy=legacy, target_core=target, score=score))
    selected = (
        target_request if result.physical_role == ROLE_TARGET else legacy_request
        if result.physical_role == ROLE_LEGACY else None)
    primary_variant = next((item for item in comparison.variants
                            if item.policy_id == PRIMARY_POLICY_ID
                            and item.variant_id == PRIMARY_VARIANT_ID), None)
    return ScanDesignAuthorityDecision(
        configured_mode="authoritative",
        scientific_action_fingerprint=inventory.context.scientific_action_fingerprint,
        eligibility_result_fingerprint=comparison.source_eligibility_fingerprint,
        primary_evidence_fingerprint=(
            None if primary_variant is None else primary_variant.variant_evidence_fingerprint),
        authority_scopes=(("physical_scan_window_selection", "restricted_o41"),)
        if result.authority_status == "granted" else (),
        source_inventory_ref=inventory_ref,
        source_inventory_fingerprint=inventory.content_hash(),
        realized_comparison_ref=comparison_ref,
        realized_comparison_fingerprint=comparison.realized_comparison_fingerprint,
        profile=profile.to_dict(),
        legacy_validation=_validation_mapping(legacy, legacy_request.adjustments),
        target_core_validation=_validation_mapping(
            target,
            (target_request.adjustments if isinstance(target_request, RealizedScanRequest)
             else tuple(target_request.reason_codes))),
        decision_outcome=result.decision_outcome,
        physical_role=result.physical_role,
        authority_status=result.authority_status,
        reason_codes=result.reason_codes,
        structural_conditions=result.structural_conditions,
        paired_block_deltas_per_executed_point_nats=(
            result.paired_block_deltas_per_executed_point_nats),
        delta_information_per_executed_point_nats=(
            result.delta_information_per_executed_point_nats),
        paired_standard_error_nats=result.paired_standard_error_nats,
        selection_margin_multiplier=result.margin_multiplier,
        lower_margin_nats=result.lower_margin_nats,
        refresh_count=refresh_count,
        selected_request=(None if selected is None else _selected_request(
            selected, result.physical_role)),
        partition_offset_selections=tuple(
            item.to_dict() for item in result.partition_offset_selections),
        partition_offsets_agree=result.partition_offsets_agree,
        margin_standard_error_multiple=(
            None if result.partition_offsets_agree is None
            else result.margin_standard_error_multiple),
    )


def persist_authority_decision(workspace: str | Path,
                               decision: ScanDesignAuthorityDecision) -> dict[str, Any]:
    decision = validate_scan_design_authority_decision(decision)
    digest = decision.content_hash()
    root = Path(workspace)
    path = root / "report" / "scan-design-authority" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json(decision.to_dict()).encode("utf-8")
    if path.exists() and path.read_bytes() != encoded:
        raise ValueError("stored authority decision does not match its content address")
    if not path.exists():
        temporary = path.with_suffix(".json.tmp")
        with open(temporary, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    return {"kind": AUTHORITY_DECISION_REF_KIND, "sha256": digest,
            "path": f"report/scan-design-authority/{digest}.json"}


def load_authority_decision(reference: Mapping[str, Any],
                            workspace: str | Path) -> ScanDesignAuthorityDecision:
    if (not isinstance(reference, Mapping)
            or set(reference) != {"kind", "sha256", "path"}
            or reference.get("kind") != AUTHORITY_DECISION_REF_KIND):
        raise ValueError("authority-decision reference is invalid")
    digest = reference.get("sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest):
        raise ValueError("authority-decision digest is invalid")
    relative = f"report/scan-design-authority/{digest}.json"
    if str(reference.get("path", "")).replace("\\", "/") != relative:
        raise ValueError("authority-decision path is invalid")
    root = Path(workspace).resolve()
    allowed = (root / "report" / "scan-design-authority").resolve()
    path = (root / Path(reference["path"])).resolve()
    if path.parent != allowed or path.name != f"{digest}.json":
        raise ValueError("authority-decision reference escapes its directory")
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("authority-decision artifact is unreadable") from error
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("authority-decision raw-byte address is invalid")
    if hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest() != digest:
        raise ValueError("authority-decision semantic address is invalid")
    decision = ScanDesignAuthorityDecision.from_dict(payload)
    if decision.content_hash() != digest:
        raise ValueError("reconstructed authority-decision address is invalid")
    inventory = load_scan_design_inventory(decision.source_inventory_ref, root)
    comparison = load_scan_design_realized_comparison(decision.realized_comparison_ref, root)
    if (inventory.content_hash() != decision.source_inventory_fingerprint
            or comparison.realized_comparison_fingerprint
            != decision.realized_comparison_fingerprint
            or comparison.source_inventory_fingerprint != inventory.content_hash()):
        raise ValueError("authority-decision parent join is invalid")
    primary = next((item for item in comparison.variants
                    if item.policy_id == PRIMARY_POLICY_ID
                    and item.variant_id == PRIMARY_VARIANT_ID), None)
    if (decision.scientific_action_fingerprint
            != inventory.context.scientific_action_fingerprint
            or decision.eligibility_result_fingerprint
            != comparison.source_eligibility_fingerprint
            or decision.primary_evidence_fingerprint
            != (None if primary is None else primary.variant_evidence_fingerprint)):
        raise ValueError("authority-decision evidence identity join is invalid")
    return decision


def apply_selected_request(proposal: Any, decision: ScanDesignAuthorityDecision) -> None:
    """Materialize the authenticated selected request onto the mutable acquisition proposal."""
    request = thaw(decision.selected_request)
    if request is None:
        return
    window = request["energy_window"]
    proposal.h, proposal.k, proposal.l = map(float, request["hkl"])
    proposal.e_lo = float(window["e_lo"])
    proposal.e_hi = float(window["e_hi"])
    proposal.e_step = float(window["e_step"])
    proposal.n_points = int(request["executed_point_count"])
    proposal.ncount = int(request["ncount"])
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    meta["proposal_fingerprint"] = request["request_fingerprint"]
    meta["scan_design_candidate_id"] = request["candidate_id"]
    meta["scan_design_realized_selected_request_fingerprint"] = request[
        "request_fingerprint"]
    meta["scan_design_realized_selected_grid_fingerprint"] = request["grid_fingerprint"]


def attach_authority_metadata(proposal: Any, reference: Mapping[str, Any],
                              decision: ScanDesignAuthorityDecision) -> None:
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    meta.update({
        "scan_design_authority_decision_ref": dict(reference),
        "scan_design_authority_decision_fingerprint": decision.authority_decision_fingerprint,
        "scan_design_authority_outcome": decision.decision_outcome,
        "scan_design_authority_status": decision.authority_status,
        "scan_design_authority_selected_role": decision.physical_role,
        "scan_design_authority_reason_codes": list(decision.reason_codes),
        "scan_design_authority_legacy_point_count": int(
            decision.legacy_validation["executed_point_count"]),
        "scan_design_authority_target_point_count": int(
            decision.target_core_validation["executed_point_count"]),
        "scan_design_authority_delta": decision.delta_information_per_executed_point_nats,
        "scan_design_authority_standard_error": decision.paired_standard_error_nats,
        "scan_design_authority_lower_margin": decision.lower_margin_nats,
        "scan_design_selected_role": decision.physical_role,
        "scan_design_selected_candidate_family": (
            None if decision.selected_request is None
            else decision.selected_request["candidate_family"]),
        "scan_design_selected_candidate_id": (
            None if decision.selected_request is None
            else decision.selected_request["candidate_id"]),
    })


def authenticate_authority_submission(proposal: Any, workspace: str | Path
                                      ) -> ScanDesignAuthorityDecision:
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    decision = load_authority_decision(meta.get("scan_design_authority_decision_ref"), workspace)
    request = thaw(decision.selected_request)
    if request is None:
        raise ValueError("authority decision has no physical selected request")
    window = request["energy_window"]
    expected_count, step = resolve_point_count(
        proposal.e_lo, proposal.e_hi, proposal.e_step, adapter=TAVI_GRID_ADAPTER)
    grid = UniformGrid(proposal.e_lo, proposal.e_hi, step, expected_count,
                       adapter=TAVI_GRID_ADAPTER)
    if (
        meta.get("scan_design_authority_decision_fingerprint")
        != decision.authority_decision_fingerprint
        or meta.get("scan_design_authority_selected_role") != decision.physical_role
        or meta.get("scan_design_selected_role") != decision.physical_role
        or meta.get("scan_design_selected_candidate_family") != request["candidate_family"]
        or meta.get("scan_design_selected_candidate_id") != request["candidate_id"]
        or meta.get("scan_design_realized_selected_request_fingerprint")
        != request["request_fingerprint"]
        or meta.get("scan_design_realized_selected_grid_fingerprint")
        != request["grid_fingerprint"]
        or meta.get("proposal_fingerprint") != request["request_fingerprint"]
        or tuple(map(float, (proposal.h, proposal.k, proposal.l))) != tuple(request["hkl"])
        or float(proposal.e_lo) != float(window["e_lo"])
        or float(proposal.e_hi) != float(window["e_hi"])
        or float(proposal.e_step) != float(window["e_step"])
        or proposal.scan_command() != request["scan_command"]
        or int(proposal.n_points or 0) != request["executed_point_count"]
        or int(proposal.ncount) != request["ncount"]
        or grid.energies != tuple(request["executed_energies"])
        or grid.fingerprint(proposal.ncount) != request["grid_fingerprint"]
    ):
        raise ValueError("authority-decision submission identity is inconsistent")
    return decision


__all__ = [
    "AUTHORITY_DECISION_REF_KIND", "apply_selected_request", "attach_authority_metadata",
    "authenticate_authority_submission", "build_authority_decision",
    "load_authority_decision", "persist_authority_decision",
]
