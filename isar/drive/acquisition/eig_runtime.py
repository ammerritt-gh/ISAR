"""EIG runtime glue: the campaign-loop side of the scan-window EIG lifecycle (P2.4).

Everything here used to live inline in :mod:`isar.drive.loop`. It is the *runtime adapter* between
the campaign loop and the pure EIG machinery in :mod:`isar.drive.acquisition.eig_shadow`,
:mod:`isar.drive.acquisition.realized_comparison` and :mod:`isar.drive.acquisition.authority`:
shadow scoring, realized-comparison refresh/authentication, the O41 authoritative selection
lifecycle, and the EIG half of the submission payload.

The module-level imports below are the canonical monkeypatch points for these paths — the executing
code reads them out of *this* module's namespace, not the loop's.

It deliberately does not import :mod:`isar.drive.loop` (that would be a cycle): the one loop-owned
callable it needs, ``_finalize_proposal``, arrives as the ``finalizer`` argument, with a lazy import
as the default for direct callers (tests, fixture tools) that do not pass one.
"""
from __future__ import annotations

import copy
import logging
import time
from typing import TYPE_CHECKING

from isar.drive.acquisition.authority import (
    apply_selected_request,
    attach_authority_metadata,
    authenticate_authority_submission,
    build_authority_decision,
    persist_authority_decision,
)
from isar.drive.acquisition.eig_shadow import (
    SHADOW_SCORING_ALLOWANCE_SECONDS,
    attach_unavailable_shadow,
    score_proposal_realized_pair,
    score_proposal_shadow_inventory,
)
from isar.drive.acquisition.realized_comparison import (
    REALIZED_COMPARISON_ALLOWANCE_SECONDS,
    RealizedPairConstructionError,
    attach_realized_metadata,
    build_realized_comparison,
    construct_realized_pair,
    load_scan_design_realized_comparison,
    persist_realized_comparison,
)
from isar.drive.acquisition.scan_design import SCAN_DESIGN_INVENTORY_SCHEMA_V2
from isar.drive.instrument.request_realization import (
    _feasible_point_count,
    _target_supporting_feasible_points,
    _validate_body,
    requested_point_count,
)
from isar.drive.session_log import AnalysisLimitExceeded, analysis_operation, log_event, timed
from isar.eig.o41_authority import validate_o41_authoritative_profile
from isar.eig.realized_score import RealizedScoringUnavailable
from isar.schema.eig_realized import RealizedScanRequest
from isar.workspace_api.workspace import WorkspaceNotFound

if TYPE_CHECKING:  # pragma: no cover - typing only, never a runtime import (cycle)
    from isar.drive.loop import CampaignConfig

_LOG = logging.getLogger(__name__)


_REALIZED_SUBMISSION_FIELDS = (
    "scan_design_realized_comparison_ref",
    "scan_design_realized_comparison_fingerprint",
    "scan_design_realized_variant_fingerprints",
    "scan_design_realized_selected_request_fingerprint",
    "scan_design_realized_selected_grid_fingerprint",
)

_AUTHORITY_SUBMISSION_FIELDS = (
    "scan_design_authority_decision_ref",
    "scan_design_authority_decision_fingerprint",
    "scan_design_authority_outcome",
    "scan_design_authority_status",
    "scan_design_authority_selected_role",
    "scan_design_authority_reason_codes",
    "scan_design_authority_legacy_point_count",
    "scan_design_authority_target_point_count",
    "scan_design_authority_delta",
    "scan_design_authority_standard_error",
    "scan_design_authority_lower_margin",
    "scan_design_selected_role",
    "scan_design_selected_candidate_family",
    "scan_design_selected_candidate_id",
    # The executed request identity the authority decision selected, kept distinct from the
    # ``scan_design_realized_selected_*`` keys the realized comparison's own accessor defines.
    "scan_design_authority_selected_request_fingerprint",
    "scan_design_authority_selected_grid_fingerprint",
)


class ScanDesignInvariantViolation(RuntimeError):
    """A scan-design invariant the controller guarantees was violated.

    Raised where the shadow scorer's fail-open handling deliberately does not apply: an invariant
    break means the controller is wrong, not that an optional evidence path is unavailable. It is a
    distinct type so :func:`_next_finalized_proposal` can turn it into a classified controller-fault
    terminal — a bare ``RuntimeError`` escaped every handler in ``run_campaign`` and aborted the
    campaign with no ``stop`` event, leaving every consumer (GUI, ``isar status``, resume) seeing a
    campaign that was still running.
    """


def _score_shadow_proposal(proposal, objective, cfg, *, it: int) -> None:
    """Run diagnostic scan-window scoring after selection and before realization clamps."""
    inventory = getattr(proposal, "scan_design_inventory", None)
    if inventory is None or inventory.context.scan_window_eig_mode != "authoritative":
        return
    selected_id = inventory.selected_candidate_id
    selected = inventory.selected_candidate
    if selected.candidate_family != "legacy_uniform/1":
        raise ScanDesignInvariantViolation(
            "shadow scoring requires the legacy control to remain selected")
    operation_id = ":".join((
        "scan-window-shadow",
        str(cfg.run_nonce),
        str(it),
        str(inventory.context.fingerprint),
        str(selected_id),
    ))
    deadline = time.monotonic() + SHADOW_SCORING_ALLOWANCE_SECONDS
    try:
        with analysis_operation(
                cfg.log_path,
                operation_id,
                "scan_window_shadow_scoring",
                limit_seconds=objective.analysis_limit_seconds,
                checkpoint_seconds=1.0,
                iteration=it,
                scientific_action_fingerprint=(proposal.meta or {}).get(
                    "scientific_action_fingerprint")):
            score_proposal_shadow_inventory(
                proposal, objective, cfg.workspace, deadline_monotonic=deadline)
    except AnalysisLimitExceeded as error:
        attach_unavailable_shadow(
            proposal, "shadow_scoring_deadline", error=str(error))
        log_event(cfg.log_path, "exception", {
            "action": "shadow_scoring_deadline",
            "severity": "high",
            "error": str(error),
            "iter": it,
        })
    except WorkspaceNotFound as error:
        # Cold-start: the workspace is auto-initialised after the first scan, so iteration 1
        # has no workspace yet. No core candidate exists to score against — legacy control is
        # the correct fallback, not a fault — so this logs at info with no traceback.
        _LOG.info(
            "scan-window shadow scoring skipped: workspace not yet initialized at %s",
            cfg.workspace)
        attach_unavailable_shadow(
            proposal, "workspace_not_initialized", error=str(error))
        log_event(cfg.log_path, "exception", {
            "action": "shadow_scoring_workspace_not_initialized",
            "severity": "low",
            "error": str(error),
            "iter": it,
        })
    except Exception as error:  # noqa: BLE001 — Slice 6 is deliberately fail-open
        _LOG.exception("scan-window shadow scoring failed; continuing with legacy control")
        attach_unavailable_shadow(
            proposal, "shadow_scoring_failed", error=str(error))
        log_event(cfg.log_path, "exception", {
            "action": "shadow_scoring_failed",
            "severity": "high",
            "error": str(error),
            "iter": it,
        })
    scored = proposal.scan_design_inventory
    if (scored.selected_candidate_id != selected_id
            or scored.selected_candidate.candidate_family != "legacy_uniform/1"):
        raise ScanDesignInvariantViolation(
            "shadow scoring changed the legacy scan-design selection")


def _proposal_for_realized_request(proposal, request: RealizedScanRequest):
    candidate = _clone_proposal(proposal)
    candidate.h, candidate.k, candidate.l = request.hkl
    candidate.e_lo = request.e_lo
    candidate.e_hi = request.e_hi
    candidate.e_step = request.e_step
    candidate.n_points = request.executed_point_count
    candidate.ncount = request.ncount
    meta = candidate.meta if isinstance(candidate.meta, dict) else {}
    candidate.meta = meta
    meta["proposal_fingerprint"] = request.request_fingerprint
    meta["scan_design_candidate_id"] = request.source_candidate_id
    return candidate


def _clone_proposal(proposal):
    """Clone mutable proposal state without pickling immutable mapping-proxy evidence."""
    candidate = copy.copy(proposal)
    candidate.meta = copy.deepcopy(proposal.meta)
    return candidate


def _restore_proposal(proposal, snapshot) -> None:
    restored = _clone_proposal(snapshot)
    proposal.__dict__.clear()
    proposal.__dict__.update(restored.__dict__)


def _clear_realized_claims(proposal, *, applicability: str = "unavailable",
                           preserve_parent: bool = False) -> None:
    """Remove Slice 7 claims that cannot authenticate the request which may be submitted."""
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    start = 2 if preserve_parent else 0
    for key in _REALIZED_SUBMISSION_FIELDS[start:]:
        meta.pop(key, None)
    # Diagnostic only, never authenticated: drop it with the claims it explained.
    meta.pop("scan_design_realized_reason_codes", None)
    meta["scan_design_eig_applicability"] = applicability
    if applicability != "invalidated":
        meta.pop("scan_design_eig_invalidation_reason", None)


def _refresh_realized_comparison(
        cfg: CampaignConfig,
        objective,
        proposal,
        budget_state: dict,
        *,
        it: int,
        legacy_adjustments: tuple[str, ...] = (),
        force: bool = False,
        finalizer=None,
) -> dict | None:
    """Attach exact current Slice 7 evidence, replacing any stale realized comparison.

    ``finalizer`` is the loop's ``_finalize_proposal``, passed in rather than imported so this
    module stays free of an ``isar.drive.loop`` cycle. It defaults to a lazy import of that same
    callable for direct callers (tests, fixture tools) that do not supply one.
    """
    if finalizer is None:
        from isar.drive.loop import _finalize_proposal as finalizer  # noqa: PLC0415 - cycle guard
    inventory = getattr(proposal, "scan_design_inventory", None)
    if inventory is None or inventory.context.scan_window_eig_mode != "authoritative":
        return None
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    if meta.get("scan_design_eig_applicability") == "invalidated":
        return meta.get("scan_design_realized_comparison_ref")
    current_request = meta.get("proposal_fingerprint")
    if (not force and meta.get("scan_design_eig_applicability") == "applicable"
            and meta.get("scan_design_realized_selected_request_fingerprint") == current_request
            and isinstance(meta.get("scan_design_realized_comparison_ref"), dict)):
        return meta["scan_design_realized_comparison_ref"]
    _clear_realized_claims(proposal)
    if inventory.inventory_version == SCAN_DESIGN_INVENTORY_SCHEMA_V2:
        return None
    if proposal.realized_scoring_remaining_seconds is None:
        proposal.realized_scoring_remaining_seconds = REALIZED_COMPARISON_ALLOWANCE_SECONDS

    def rescore(realized_pair, remaining_seconds):
        allowance = min(float(remaining_seconds),
                        float(proposal.realized_scoring_remaining_seconds or 0.0))
        if allowance <= 0.0:
            raise AnalysisLimitExceeded("realized scoring allowance exhausted")
        started = time.monotonic()
        operation_id = ":".join((
            "scan-window-realized", str(cfg.run_nonce), str(it),
            str(inventory.context.fingerprint), str(inventory.selected_candidate_id)))
        try:
            with analysis_operation(
                    cfg.log_path, operation_id, "scan_window_realized_scoring",
                    limit_seconds=objective.analysis_limit_seconds, checkpoint_seconds=1.0,
                    iteration=it,
                    scientific_action_fingerprint=meta.get("scientific_action_fingerprint")):
                return score_proposal_realized_pair(
                    proposal, objective, cfg.workspace, realized_pair,
                    deadline_monotonic=time.monotonic() + allowance)
        finally:
            elapsed = max(time.monotonic() - started, 0.0)
            proposal.realized_scoring_remaining_seconds = max(allowance - elapsed, 0.0)

    try:
        inventory_ref = meta.get("scan_design_inventory_ref")
        if not isinstance(inventory_ref, dict):
            raise RuntimeError("realized comparison requires a persisted scan-design inventory")
        try:
            pair = construct_realized_pair(
                proposal, inventory, objective, cfg, budget_state, it=it,
                finalizer=finalizer, legacy_adjustments=legacy_adjustments)
        except RealizedPairConstructionError as error:
            pair = error.unavailable_pair
            _LOG.exception("realized counterfactual construction failed; retaining exact legacy")
            log_event(cfg.log_path, "exception", {
                "action": "realized_construction_failed", "severity": "high",
                "error": str(error), "iter": it})
            comparison = build_realized_comparison(
                inventory, inventory_ref, pair,
                remaining_seconds=float(proposal.realized_scoring_remaining_seconds or 0.0),
                rescorer=None, unavailable_reason="realized_construction_failed")
        else:
            try:
                comparison = build_realized_comparison(
                    inventory, inventory_ref, pair,
                    remaining_seconds=float(proposal.realized_scoring_remaining_seconds),
                    rescorer=rescore)
            except AnalysisLimitExceeded as error:
                comparison = build_realized_comparison(
                    inventory, inventory_ref, pair, remaining_seconds=0.0, rescorer=None,
                    unavailable_reason="realized_scoring_deadline")
                log_event(cfg.log_path, "exception", {
                    "action": "realized_scoring_deadline", "severity": "high",
                    "error": str(error), "iter": it})
            except RealizedScoringUnavailable as error:
                # Named, expected refusal (e.g. predictive_insufficient_bootstrap_mass,
                # shadow_disagrees, modality_fork) — legacy control is the correct fallback, not
                # a fault, so this logs at info with no traceback.
                reason = str(getattr(error, "reason_code", "realized_scoring_failed"))
                comparison = build_realized_comparison(
                    inventory, inventory_ref, pair,
                    remaining_seconds=float(proposal.realized_scoring_remaining_seconds or 0.0),
                    rescorer=None, unavailable_reason=reason)
                _LOG.info(
                    "realized scan-window scoring unavailable (%s); continuing with legacy control",
                    reason)
                log_event(cfg.log_path, "exception", {
                    "action": reason, "severity": "low", "error": str(error),
                    "reason": str(error), "iter": it})
            except Exception as error:  # noqa: BLE001 - fail open at the authority boundary
                reason = str(getattr(error, "reason_code", "realized_scoring_failed"))
                comparison = build_realized_comparison(
                    inventory, inventory_ref, pair,
                    remaining_seconds=float(proposal.realized_scoring_remaining_seconds or 0.0),
                    rescorer=None, unavailable_reason=reason)
                _LOG.exception("realized scan-window scoring failed; continuing with legacy control")
                log_event(cfg.log_path, "exception", {
                    "action": reason, "severity": "high", "error": str(error), "iter": it})
        reference = persist_realized_comparison(cfg.workspace, comparison)
        attach_realized_metadata(proposal, reference, comparison)
        log_event(cfg.log_path, "scan_design_realized_comparison", {
            "iter": it, "comparison_ref": reference,
            "realized_comparison_fingerprint": comparison.realized_comparison_fingerprint,
            "realized_pair_fingerprint": comparison.realized_pair.realized_pair_fingerprint,
            "score_disposition": comparison.score_disposition,
            "reason_codes": list(comparison.reason_codes),
            "request_fingerprint": comparison.selected_request_fingerprint})
        return reference
    except Exception as error:  # noqa: BLE001 - Slice 7 evidence never blocks legacy control
        _clear_realized_claims(proposal)
        _LOG.exception("realized comparison refresh failed; continuing without Slice 7 claims")
        try:
            log_event(cfg.log_path, "exception", {
                "action": "realized_comparison_refresh_failed", "severity": "high",
                "error": str(error), "iter": it})
        except Exception:  # noqa: BLE001 - the process logger remains the final visible channel
            _LOG.exception("failed to persist the realized-comparison refresh failure event")
        return None


def _invalidate_realized_comparison_for_recovery(proposal) -> None:
    """Retain parent design lineage while removing claims about a non-EIG recovery request."""
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    _clear_realized_claims(proposal, applicability="invalidated", preserve_parent=True)
    meta["scan_design_eig_invalidation_reason"] = "eig_realization_invalidated_by_recovery"


def _prepare_authoritative_submission(
        cfg: CampaignConfig, objective, proposal, budget_state: dict,
        legacy_validation: dict, *, it: int, finalizer=None,
) -> dict:
    """Run the bounded O41 validation/selection lifecycle and return selected validation."""
    inventory = getattr(proposal, "scan_design_inventory", None)
    if inventory is None or inventory.context.scan_window_eig_mode != "authoritative":
        return legacy_validation
    profile = validate_o41_authoritative_profile(objective.scan_window_eig)
    authenticated_legacy_snapshot = _clone_proposal(proposal)
    authenticated_legacy_validation = copy.deepcopy(legacy_validation)
    legacy_snapshot = _clone_proposal(proposal)
    refresh_count = 0
    selector_calls = 0

    while True:
        meta = proposal.meta if isinstance(proposal.meta, dict) else {}
        inventory_ref = meta.get("scan_design_inventory_ref")
        comparison_ref = meta.get("scan_design_realized_comparison_ref")
        try:
            if refresh_count:
                with timed(cfg.log_path, "validate", iteration=it,
                           candidate_role="legacy_uniform", authority_refresh=refresh_count):
                    legacy_validation = cfg.client.validate(_validate_body(
                        proposal, sample=cfg.sample,
                        background=cfg.sample_background_frozen))
                log_event(cfg.log_path, "validate", {
                    "candidate_role": "legacy_uniform",
                    "would_queue": legacy_validation.get("would_queue", False),
                    "blockers": legacy_validation.get("blockers"),
                    "requested_points": legacy_validation.get("requested_points"),
                    "feasible_points": _feasible_point_count(
                        legacy_validation, proposal.n_points or 1),
                    "request_fingerprint": (proposal.meta or {}).get("proposal_fingerprint"),
                    "authority_refresh": refresh_count,
                    "iter": it,
                })
                if not legacy_validation.get("would_queue", False):
                    raise RuntimeError(
                        "refreshed legacy request failed adapter validation")
            comparison = load_scan_design_realized_comparison(comparison_ref, cfg.workspace)
            target_request = comparison.realized_pair.target_core
            if isinstance(target_request, RealizedScanRequest):
                target_proposal = _proposal_for_realized_request(legacy_snapshot, target_request)
                with timed(cfg.log_path, "validate", iteration=it,
                           candidate_role="target_core", authority_refresh=refresh_count):
                    target_validation = cfg.client.validate(_validate_body(
                        target_proposal, sample=cfg.sample,
                        background=cfg.sample_background_frozen))
                target_support = _target_supporting_feasible_points(
                    target_validation, (target_proposal.meta or {}).get("target_band"))
                min_support = int((getattr(objective, "steering", None) or {}).get(
                    "minimum_target_support_points", 0))
                target_valid = bool(target_validation.get("would_queue", False))
                target_refusal_reason = None
                if min_support > 0 and target_support is not None and target_support < min_support:
                    target_valid = False
                    target_refusal_reason = "target_core_below_minimum_support"
                target_request_fingerprint = target_request.request_fingerprint
                target_blockers = target_validation.get("blockers")
                # The echoed count, or the manifest length when the adapter omits it — without that
                # fallback a partially-infeasible target hides from the check below.
                target_requested_points = requested_point_count(target_validation)
                target_feasible_points = _feasible_point_count(
                    target_validation, target_proposal.n_points or 1)
                # A partially-infeasible target core is unsubmittable (TAVI 400s the grid), and the
                # authority has no way to trim it — the target window is the candidate's identity.
                # Refuse it here so the existing refused-target path submits the legacy request,
                # rather than selecting a window that would abort the campaign at submit.
                if (target_requested_points is not None
                        and 0 < target_feasible_points < int(target_requested_points)):
                    target_valid = False
                    target_refusal_reason = "target_core_partially_infeasible"
            else:
                target_validation = {}
                target_support = None
                target_valid = False
                target_refusal_reason = "target_core_refused_by_generator"
                target_request_fingerprint = None
                target_blockers = list(target_request.reason_codes)
                target_requested_points = None
                target_feasible_points = 0
            log_event(cfg.log_path, "validate", {
                "candidate_role": "target_core",
                "would_queue": target_validation.get("would_queue", False),
                "blockers": target_blockers,
                "requested_points": target_requested_points,
                "feasible_points": target_feasible_points,
                "target_supporting_feasible_points": target_support,
                "request_fingerprint": target_request_fingerprint,
                "refusal_reason": target_refusal_reason,
                "authority_refresh": refresh_count,
                "iter": it,
            })
            # A refused target gets the one shared refresh before any selector call.  The second
            # refusal is final and becomes an unavailable-authority legacy decision.
            if not target_valid and refresh_count == 0:
                refresh_count = 1
                _restore_proposal(proposal, legacy_snapshot)
                _refresh_realized_comparison(
                    cfg, objective, proposal, budget_state, it=it,
                    legacy_adjustments=("authoritative_target_validation_refresh",), force=True,
                    finalizer=finalizer)
                legacy_snapshot = _clone_proposal(proposal)
                continue

            decision = build_authority_decision(
                inventory=proposal.scan_design_inventory,
                inventory_ref=inventory_ref,
                comparison=comparison,
                comparison_ref=comparison_ref,
                profile=profile,
                legacy_validation_status=(
                    "validated" if legacy_validation.get("would_queue", False) else "refused"),
                target_validation_status="validated" if target_valid else "refused",
                applicability_status=(proposal.meta or {}).get(
                    "scan_design_eig_applicability", "unavailable"),
                refresh_count=refresh_count,
            )
            selector_calls += 1
            reference = persist_authority_decision(cfg.workspace, decision)
            attach_authority_metadata(proposal, reference, decision)
            apply_selected_request(proposal, decision)
            if decision.physical_role != "none":
                authenticate_authority_submission(proposal, cfg.workspace)
            # The executed identity under its OWN key. ``apply_selected_request`` overwrites the
            # ``scan_design_realized_selected_*`` keys with the request the authority decision
            # selected, which on a grant is the target_core request — NOT what
            # ``ScanDesignRealizedComparison.selected_request_fingerprint`` returns (always the
            # legacy request). An auditor reading the persisted record beside the comparison
            # artifact would otherwise see two different answers to "which request was selected"
            # with nothing naming the decision that chose between them. Additive: the realized keys
            # keep their current value (the submission-identity authentication above is defined
            # against them), and the decision ref plus these keys make the record unambiguous.
            selected = decision.selected_request
            if selected is not None:
                meta = proposal.meta if isinstance(proposal.meta, dict) else {}
                proposal.meta = meta
                meta["scan_design_authority_selected_request_fingerprint"] = selected[
                    "request_fingerprint"]
                meta["scan_design_authority_selected_grid_fingerprint"] = selected[
                    "grid_fingerprint"]
            log_event(cfg.log_path, "scan_design_authority_decision", {
                "iter": it,
                "mode": "authoritative",
                "decision_outcome": decision.decision_outcome,
                "authority_status": decision.authority_status,
                "selected_role": decision.physical_role,
                "candidate_ids": {
                    "legacy": decision.legacy_validation["candidate_id"],
                    "target_core": decision.target_core_validation["candidate_id"],
                },
                "comparison_fingerprint": decision.realized_comparison_fingerprint,
                "decision_fingerprint": decision.authority_decision_fingerprint,
                "delta": decision.delta_information_per_executed_point_nats,
                "standard_error": decision.paired_standard_error_nats,
                "lower_margin": decision.lower_margin_nats,
                "point_counts": {
                    "legacy": decision.legacy_validation["executed_point_count"],
                    "target_core": decision.target_core_validation["executed_point_count"],
                },
                "reason_codes": list(decision.reason_codes),
                "refresh_count": refresh_count,
                "selector_calls": selector_calls,
            })
            return target_validation if decision.physical_role == "target_core" else legacy_validation
        except Exception as error:  # noqa: BLE001 - one shared refresh, then safe legacy
            if refresh_count == 0:
                refresh_count = 1
                # Recorded even though the retry may succeed: without it a corrupt or truncated
                # realized-comparison artifact leaves no trace but ``refresh_count: 1`` on the
                # eventual decision event, with nothing naming the cause.
                _LOG.warning("authoritative selection failed; taking its one shared refresh: %s",
                             error)
                log_event(cfg.log_path, "exception", {
                    "action": "scan_design_authority_refresh", "severity": "medium",
                    "error": str(error), "iter": it,
                    "refresh_count": refresh_count, "selector_calls": selector_calls,
                })
                _restore_proposal(proposal, legacy_snapshot)
                _refresh_realized_comparison(
                    cfg, objective, proposal, budget_state, it=it,
                    legacy_adjustments=("authoritative_authentication_refresh",), force=True,
                    finalizer=finalizer)
                legacy_snapshot = _clone_proposal(proposal)
                continue
            _restore_proposal(proposal, authenticated_legacy_snapshot)
            legacy_validation = authenticated_legacy_validation
            for field in _AUTHORITY_SUBMISSION_FIELDS:
                (proposal.meta or {}).pop(field, None)
            _LOG.exception(
                "authoritative selection failed after its bounded refresh; submitting legacy")
            log_event(cfg.log_path, "exception", {
                "action": "scan_design_authority_unavailable",
                "severity": "high", "error": str(error), "iter": it,
                "refresh_count": refresh_count, "selector_calls": selector_calls,
            })
            return legacy_validation


def eig_submission_metadata(proposal) -> dict:
    """The EIG half of the submission payload, in wire order.

    Spliced into ``submission_payload`` at exactly the position these keys occupied inline, so the
    persisted record stays byte-identical.
    """
    return {
        "scan_design_inventory_ref": (proposal.meta or {}).get(
            "scan_design_inventory_ref"),
        "scan_design_candidate_id": (proposal.meta or {}).get(
            "scan_design_candidate_id"),
        "scan_design_context_fingerprint": (proposal.meta or {}).get(
            "scan_design_context_fingerprint"),
        "scan_design_selection_basis": (proposal.meta or {}).get(
            "scan_design_selection_basis"),
        "scan_design_shadow_status": (proposal.meta or {}).get(
            "scan_design_shadow_status"),
        "scan_design_shadow_fingerprint": (proposal.meta or {}).get(
            "scan_design_shadow_fingerprint"),
        "scan_design_shadow_variant_fingerprints": (proposal.meta or {}).get(
            "scan_design_shadow_variant_fingerprints"),
        "scan_design_realized_comparison_ref": (proposal.meta or {}).get(
            "scan_design_realized_comparison_ref"),
        "scan_design_realized_comparison_fingerprint": (proposal.meta or {}).get(
            "scan_design_realized_comparison_fingerprint"),
        "scan_design_realized_variant_fingerprints": (proposal.meta or {}).get(
            "scan_design_realized_variant_fingerprints"),
        "scan_design_realized_selected_request_fingerprint": (proposal.meta or {}).get(
            "scan_design_realized_selected_request_fingerprint"),
        "scan_design_realized_selected_grid_fingerprint": (proposal.meta or {}).get(
            "scan_design_realized_selected_grid_fingerprint"),
        "scan_design_eig_applicability": (proposal.meta or {}).get(
            "scan_design_eig_applicability"),
        "scan_design_eig_invalidation_reason": (proposal.meta or {}).get(
            "scan_design_eig_invalidation_reason"),
        "scan_design_authority_decision_ref": (proposal.meta or {}).get(
            "scan_design_authority_decision_ref"),
        "scan_design_authority_decision_fingerprint": (proposal.meta or {}).get(
            "scan_design_authority_decision_fingerprint"),
        "scan_design_authority_outcome": (proposal.meta or {}).get(
            "scan_design_authority_outcome"),
        "scan_design_authority_status": (proposal.meta or {}).get(
            "scan_design_authority_status"),
        "scan_design_authority_selected_role": (proposal.meta or {}).get(
            "scan_design_authority_selected_role"),
        "scan_design_authority_reason_codes": (proposal.meta or {}).get(
            "scan_design_authority_reason_codes"),
        "scan_design_authority_legacy_point_count": (proposal.meta or {}).get(
            "scan_design_authority_legacy_point_count"),
        "scan_design_authority_target_point_count": (proposal.meta or {}).get(
            "scan_design_authority_target_point_count"),
        "scan_design_authority_delta": (proposal.meta or {}).get(
            "scan_design_authority_delta"),
        "scan_design_authority_standard_error": (proposal.meta or {}).get(
            "scan_design_authority_standard_error"),
        "scan_design_authority_lower_margin": (proposal.meta or {}).get(
            "scan_design_authority_lower_margin"),
        "scan_design_selected_role": (proposal.meta or {}).get(
            "scan_design_selected_role"),
        "scan_design_selected_candidate_family": (proposal.meta or {}).get(
            "scan_design_selected_candidate_family"),
        "scan_design_selected_candidate_id": (proposal.meta or {}).get(
            "scan_design_selected_candidate_id"),
        "scan_design_authority_selected_request_fingerprint": (proposal.meta or {}).get(
            "scan_design_authority_selected_request_fingerprint"),
        "scan_design_authority_selected_grid_fingerprint": (proposal.meta or {}).get(
            "scan_design_authority_selected_grid_fingerprint"),
    }
