"""Live, exact-source adapter for diagnostic scan-window EIG scoring.

This is the sole seam allowed to combine read-only workspace artifacts with the pure
``isar.eig`` scoring modules.  It resolves a completed master only through the atomic session key
and detector frozen on the proposal; it never searches by Q and never substitutes a newer master.
"""
from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from isar.drive.acquisition.scan_design import load_scan_design_inventory
from isar.drive.objective import current_objective_comparison_scope
from isar.drive.session_log import AnalysisLimitExceeded, read_log
from isar.eig.eligibility import EligibilityInput
from isar.eig.qoi import adapt_omega0_target_projection
from isar.eig.realized_score import score_realized_pair
from isar.eig.shadow_inventory import ScanDesignShadowScoring, score_shadow_inventory
from isar.schema.eig_realized import RealizedCandidatePair


SHADOW_SCORING_ALLOWANCE_SECONDS = 5.0


def _intent_mapping(source: Any) -> dict[str, Any] | None:
    intent = getattr(source, "action_intent", None)
    if intent is None or not dataclasses.is_dataclass(intent):
        return None
    value = dataclasses.asdict(intent)
    value["scientific_action_fingerprint"] = getattr(
        source, "scientific_action_fingerprint", None)
    return value


def _exact_master(workspace: Any, source: Any) -> tuple[Any, Mapping[str, Any] | None]:
    """Return the exact ledger entry/outcome named by the proposal, or ``(None, None)``."""
    key = getattr(source, "master_session_key", None)
    detector = getattr(source, "detector_id", None)
    snapshot = workspace.snapshot
    if snapshot is None or key is None or detector is None:
        return None, None
    entry = snapshot.scans.get(str(key))
    if entry is None:
        return None, None
    outcome = (entry.detectors or {}).get(str(detector))
    if not isinstance(outcome, Mapping):
        return entry, None
    expected_revision = getattr(source, "master_revision", None)
    actual_revision = outcome.get("master_revision")
    if (expected_revision is None or actual_revision is None
            or str(expected_revision) != str(actual_revision)):
        return entry, None
    return entry, outcome


def _exact_reference(source_value: Any, outcome: Mapping[str, Any] | None,
                     field: str) -> str | None:
    """Accept a side-artifact reference only when source and exact ledger agree."""
    if not isinstance(source_value, str) or not source_value or outcome is None:
        return None
    ledger_value = outcome.get(field)
    if not isinstance(ledger_value, str) or ledger_value != source_value:
        return None
    return source_value


def _scan_design_source(workspace: Path, acquisition_id: str | None,
                        request_fingerprint: str | None
                        ) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Authenticate the executed inventory joined to one exact logical acquisition."""
    if acquisition_id is None or request_fingerprint is None:
        return None, ("scan_design_provenance_missing",)
    events = read_log(workspace / "report" / "session_log.jsonl")
    submissions = [
        dict(event.get("payload") or {})
        for event in events
        if event.get("kind") == "physical_submission"
        and str((event.get("payload") or {}).get("acquisition_id")) == acquisition_id
        and str((event.get("payload") or {}).get("proposal_fingerprint"))
        == request_fingerprint
    ]
    if not submissions:
        return None, ("scan_design_provenance_missing",)
    identities: list[tuple[Any, Any, Any]] = []
    for payload in submissions:
        reference = payload.get("scan_design_inventory_ref")
        candidate_id = payload.get("scan_design_candidate_id")
        context_fingerprint = payload.get("scan_design_context_fingerprint")
        if not isinstance(reference, Mapping) or candidate_id is None or context_fingerprint is None:
            return None, ("scan_design_provenance_invalid",)
        try:
            inventory = load_scan_design_inventory(reference, workspace)
        except (TypeError, ValueError):
            return None, ("scan_design_provenance_invalid",)
        if (inventory.selected_candidate_id != str(candidate_id)
                or inventory.context.fingerprint != str(context_fingerprint)):
            return None, ("scan_design_provenance_invalid",)
        identities.append((dict(reference), str(candidate_id), str(context_fingerprint)))
    first = identities[0]
    if any(identity != first for identity in identities[1:]):
        return None, ("scan_design_provenance_invalid",)
    reference, candidate_id, context_fingerprint = first
    source_events: list[str] = []
    if len(submissions) > 1:
        source_events.append("missed_window_recenter")
    if any(
            event.get("kind") == "recovery_completed"
            and str((event.get("payload") or {}).get("acquisition_id")) == acquisition_id
            for event in events):
        source_events.append("recovery_completed")
    return {
        "inventory_fingerprint": reference["sha256"],
        "context_fingerprint": context_fingerprint,
        "selected_candidate_id": candidate_id,
        "status": "completed",
        "recovery_state": "none" if not source_events else "present",
    }, tuple(sorted(source_events))


def _deadline_check(deadline_monotonic: float, monotonic: Callable[[], float]) -> None:
    if monotonic() >= deadline_monotonic:
        raise AnalysisLimitExceeded("scan-window shadow scoring exceeded its five-second allowance")


def _resolved_scoring_inputs(proposal: Any, objective: Any, workspace_path: str | Path,
                             check: Callable[[], None]) -> tuple[Any, EligibilityInput, Any, dict]:
    """Resolve the exact source once for both planned and realized scoring paths."""
    inventory = getattr(proposal, "scan_design_inventory", None)
    source = getattr(proposal, "shadow_scoring_source", None)
    if inventory is None or source is None:
        raise ValueError("shadow_source_missing")
    source_action = getattr(source, "scientific_action_fingerprint", None)
    if (source_action != inventory.context.scientific_action_fingerprint
            or source_action != (proposal.meta or {}).get("scientific_action_fingerprint")):
        raise ValueError("shadow_source_action_mismatch")
    check()
    from isar.workspace_api import Workspace

    workspace = Workspace.open(workspace_path)
    entry, outcome = _exact_master(workspace, source)
    predictive_ref = _exact_reference(
        getattr(source, "predictive_ref", None), outcome, "predictive_ref")
    projection_ref = _exact_reference(
        getattr(source, "target_projection_ref", None), outcome, "target_projection_ref")
    predictive = workspace.master_predictive(predictive_ref) if predictive_ref else None
    projection = workspace.target_projection(projection_ref) if projection_ref else None
    authority = None
    if entry is not None:
        ledger_authority = (entry.provenance or {}).get("target_projection_authority")
        source_authority = getattr(source, "target_projection_authority", None)
        if isinstance(ledger_authority, Mapping):
            if source_authority is None or dict(source_authority) == dict(ledger_authority):
                authority = dict(ledger_authority)
    qoi_evidence = adapt_omega0_target_projection(
        predictive, projection, expected_authority=authority)
    source_acquisition = getattr(source, "source_acquisition_id", None)
    source_request = getattr(source, "source_submission_request_fingerprint", None)
    scan_design, scan_events = _scan_design_source(
        Path(workspace_path),
        str(source_acquisition) if source_acquisition is not None else None,
        str(source_request) if source_request is not None else None)
    eligibility = EligibilityInput.from_current_contract(
        action_intent=_intent_mapping(source),
        action_provenance=getattr(source, "action_provenance", None),
        target_outcome=outcome,
        selection_record=(outcome.get("fit_selection_semantics")
                          if outcome is not None else None),
        qoi_evidence=qoi_evidence,
        shadow_record=(outcome.get("shadow_model_selection")
                       if outcome is not None else None),
        scan_design_provenance=scan_design,
        scan_design_events=scan_events)
    return predictive, eligibility, objective.scan_window_eig, current_objective_comparison_scope(
        objective)


def score_proposal_shadow_inventory(
        proposal: Any,
        objective: Any,
        workspace_path: str | Path,
        *,
        deadline_monotonic: float | None = None,
        monotonic: Callable[[], float] = time.monotonic,
) -> Any:
    """Immutably attach diagnostic scoring to a proposal's pre-finalization inventory."""
    inventory = getattr(proposal, "scan_design_inventory", None)
    if inventory is None:
        return proposal
    if inventory.context.scan_window_eig_mode not in {"shadow", "authoritative"}:
        proposal.scan_design_inventory = inventory.with_shadow_scoring(
            ScanDesignShadowScoring.not_requested())
        return proposal
    deadline = (monotonic() + SHADOW_SCORING_ALLOWANCE_SECONDS
                if deadline_monotonic is None else float(deadline_monotonic))
    check = lambda: _deadline_check(deadline, monotonic)
    try:
        predictive, eligibility, config, scope = _resolved_scoring_inputs(
            proposal, objective, workspace_path, check)
    except ValueError as error:
        reason = str(error)
        if reason not in {"shadow_source_missing", "shadow_source_action_mismatch"}:
            raise
        proposal.scan_design_inventory = inventory.with_shadow_scoring(
            ScanDesignShadowScoring.unavailable_internal(reason))
        return proposal
    scoring = score_shadow_inventory(
        inventory,
        predictive_artifact=predictive,
        eligibility_input=eligibility,
        policy_set=config.policy_set,
        scoring_config=config.scoring_config,
        scope_kind="campaign_configuration",
        scope_fingerprint=scope["fingerprint"],
        deadline_check=check,
    )
    proposal.scan_design_inventory = inventory.with_shadow_scoring(scoring)
    return proposal


def score_proposal_realized_pair(
        proposal: Any,
        objective: Any,
        workspace_path: str | Path,
        pair: RealizedCandidatePair,
        *,
        deadline_monotonic: float,
        monotonic: Callable[[], float] = time.monotonic,
):
    """Score one changed realized pair against the proposal's exact authenticated source."""
    check = lambda: _deadline_check(float(deadline_monotonic), monotonic)
    predictive, eligibility, config, scope = _resolved_scoring_inputs(
        proposal, objective, workspace_path, check)
    return score_realized_pair(
        pair, predictive_artifact=predictive, eligibility_input=eligibility,
        policy_set=config.policy_set, scoring_config=config.scoring_config,
        scope_kind="campaign_configuration", scope_fingerprint=scope["fingerprint"],
        deadline_check=check)


def attach_unavailable_shadow(proposal: Any, reason_code: str, *, error: str | None = None) -> Any:
    """Fail open without minting source identities or changing the selected candidate."""
    inventory = getattr(proposal, "scan_design_inventory", None)
    if inventory is None or inventory.context.scan_window_eig_mode not in {
            "shadow", "authoritative"}:
        return proposal
    diagnostics = {} if error is None else {"error": str(error)}
    proposal.scan_design_inventory = inventory.with_shadow_scoring(
        ScanDesignShadowScoring.unavailable_internal(reason_code, diagnostics=diagnostics))
    return proposal


__all__ = [
    "SHADOW_SCORING_ALLOWANCE_SECONDS",
    "attach_unavailable_shadow",
    "score_proposal_realized_pair",
    "score_proposal_shadow_inventory",
]
