"""Live, exact-source adapter for scan-window EIG scoring.

This is the sole seam allowed to combine read-only workspace artifacts with the pure
``isar.eig`` scoring modules.  It resolves a completed master only through the atomic session key
and detector frozen on the proposal; it never searches by Q and never substitutes a newer master.

Module name is historical: the diagnostic-only "shadow" mode this was written for was retired in
SCENE-4 PR 4.4 (D3) — everything below now serves the sole remaining scoring regime,
``scan_window_eig_mode == "authoritative"``. Kept unrenamed (rather than split across a rename)
because the import churn across the module's ~4 call sites did not clearly buy back clarity; a
future touch that already has reason to move these functions may rename the module then.
"""
from __future__ import annotations

import dataclasses
import logging
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


_LOG = logging.getLogger(__name__)

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


# A target projection is bound to a NINE-field authority basis (``isar.eig.qoi``
# ``_AUTHORITY_FIELDS``), and that basis is assembled from two sources at publication time
# (``isar.session.driver._publish_target_projections`` →
# ``isar.session.predictive_artifact.build_target_projection``):
#
#   * the four fields frozen on the ledger entry at fit time, under
#     ``provenance["target_projection_authority"]`` — the fit is the only scope that holds the
#     calibration and policy fingerprints; and
#   * the five tracker fields, read from the pooled master's outcome, because branch triage and
#     the connected tracker can still move the target after the fit.
#
# The currentness check needs the same nine fields as they stand NOW, so the frozen half alone is
# not an expected authority: it names no track or candidate, and every field the consumer omits
# makes the projection's currentness unknown rather than current.
_AUTHORITY_BASIS_FIELDS = ("action_role", "master_revision",
                           "calibration_fingerprint", "policy_fingerprint")
_AUTHORITY_TRACK_FIELDS = ("track_revision", "track_state", "candidate_id",
                           "candidate_state", "selected_candidate_index")


def _authority_basis(entry: Any, source: Any) -> Mapping[str, Any] | None:
    """The frozen basis from the exact ledger entry and/or the proposal, or ``None``.

    Either provenance alone is accepted; when both are present they must agree exactly, because
    disagreeing provenance is no provenance and must not authenticate a currentness verdict.
    """
    recorded = ((getattr(entry, "provenance", None) or {}).get("target_projection_authority")
                if entry is not None else None)
    ledger = dict(recorded) if isinstance(recorded, Mapping) else None
    declared = getattr(source, "target_projection_authority", None)
    declared = dict(declared) if isinstance(declared, Mapping) else None
    if ledger is not None and declared is not None and ledger != declared:
        return None
    return ledger if ledger is not None else declared


def _expected_authority(entry: Any, outcome: Mapping[str, Any] | None,
                        source: Any) -> dict[str, Any] | None:
    """The authority basis a projection published from this exact master NOW would be bound to.

    Returns ``None`` — leaving the adapter to fail closed on unknown currentness — when the frozen
    basis is unavailable or self-contradictory, or when there is no exact ledger outcome to read
    the current tracker state from.  Every field is resolved exactly as the publisher resolves it,
    so a genuinely stale projection (a superseded master revision, or a track or candidate the
    tracker has since moved) still differs field-wise and is still reported stale.

    The two ``or`` fallbacks below are not defaults chosen here; they mirror
    ``_publish_target_projections`` verbatim, which resolves ``master_revision=authority.get(
    "master_revision") or outcome.get("master_revision")`` and ``action_role=authority.get(
    "action_role") or "master"``.  Resolving either field differently from the publisher would
    manufacture a mismatch on an unmoved master and report a current projection stale.  Neither
    fallback can hide staleness: both sides read the ledger outcome, so a master that has since
    been refit carries a new revision here and still differs from the frozen projection.
    """
    basis = _authority_basis(entry, source)
    if basis is None or outcome is None:
        return None
    expected = {
        "action_role": basis.get("action_role") or "master",
        "master_revision": basis.get("master_revision") or outcome.get("master_revision"),
        "calibration_fingerprint": basis.get("calibration_fingerprint"),
        "policy_fingerprint": basis.get("policy_fingerprint"),
    }
    expected.update({name: outcome.get(name) for name in _AUTHORITY_TRACK_FIELDS})
    return expected


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
        except (TypeError, ValueError) as error:
            _LOG.warning("scan_design_provenance_invalid: acquisition %s inventory %s could not be "
                         "authenticated: %s", acquisition_id, reference.get("sha256"), error)
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
    authority = _expected_authority(entry, outcome, source)
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
    if inventory.context.scan_window_eig_mode != "authoritative":
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
    if inventory is None or inventory.context.scan_window_eig_mode != "authoritative":
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
