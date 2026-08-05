"""``run_campaign`` — the adaptive-loop orchestrator (LLM_HARNESS_DESIGN §8, the worked MVP loop).

Wires the three parts into the §8 loop:

    objective spec → (σ-calibration is milestone 0, loaded here) → adaptive loop {
        acquisition proposes a scan → POST /validate → POST /scan (isolated, seeded) → wait →
        save scan JSON → `isar run` (subprocess) → read results.csv / card.state.json →
        fold into the ω(q) model → stopping check + exception branches
    } → stop (met / incomplete / infeasible) → campaign summary

The loop is **decision-only**: numbers come only from ISAR's ``results.csv`` (read through the file
contract), never from a fit imported here. It LOOKS at raw counts solely to re-center a missed window
(§1). Every decision is written to the session log (:mod:`isar.drive.session_log`).

Seams are injectable so the loop is unit-testable with no network and no real ISAR: pass ``client`` (a
:class:`~isar.drive.instrument.tavi_client.TaviClient`-shaped object), ``run_isar`` (a callable that
writes ``report/results.csv``), and ``resolution`` (a float / dict / callable). Defaults build the real
TAVI client and shell out to ``isar run``.
"""
from __future__ import annotations

from collections.abc import Mapping
import copy
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

import json
import hashlib
import inspect
import itertools
import logging
import math
import os
import subprocess
import sys
import time
import uuid

from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition.acquire import (
    ScanProposal,
    _candidate_precision,
    normalize_proposal_score,
    hkl_key,
    plan_actions,
    propose_action,
)
from isar.drive.acquisition.scan_design import (
    MAX_POINTS,
    BudgetInsufficientForMinimumBatch,
    FAMILY_LEGACY_UNIFORM,
    FAMILY_TARGET_CORE,
    InstrumentLimitBelowMinimumBatch,
    InstrumentPointLimitExceeded,
    SCAN_DESIGN_INVENTORY_SCHEMA_V2,
    points_in_window,
)
from isar.drive.acquisition.scan_grid import (
    COARSEN_FROM_DESIGNED_SPAN,
    resolve_point_count,
    resolve_uniform_grid,
)
from isar.drive.acquisition.grid_adapters import TAVI_GRID_ADAPTER
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
    authenticate_realized_submission,
    build_realized_comparison,
    construct_realized_pair,
    load_scan_design_realized_comparison,
    persist_realized_comparison,
)
from isar.drive.acquisition.authority import (
    apply_selected_request,
    attach_authority_metadata,
    authenticate_authority_submission,
    build_authority_decision,
    load_authority_decision,
    persist_authority_decision,
)
from isar.drive.acquisition.inventory import (
    GENERATOR_REGISTRY, GENERATOR_REGISTRY_VERSION, TERMINAL_ACTION_INVENTORY_EXHAUSTED,
    TERMINAL_CONTROLLER_FAULT, CampaignSliceKey, FinalizedProposal, RejectionClass,
    ValidatedAttempt, canonical_json)
from isar.drive.acquisition.calibration import Calibration, load_calibration
from isar.drive.instrument.tavi_client import TaviClient, TaviError
from isar.drive.isar_io.readers import read_card_state, read_results_csv
from isar.drive.isar_io.runner import SubprocessRunner, as_runner
from isar.drive.isar_io.runner import init_isar as _default_init_isar
from isar.drive.isar_io.worker_client import WorkerRunner
from isar.drive.isar_io.scan_writer import save_scan_json, scan_json_path
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import (AnalysisLimitExceeded, activity, analysis_clock_state,
                                    analysis_operation, close_interrupted_activities,
                                    close_interrupted_analysis, log_activity_progress,
                                    log_event, read_log, timed)
from isar.drive.supervisor import exceptions as exc
from isar.drive.supervisor.stopping import (
    FIRST_CROSSING_MARGIN,
    PRODUCTION_KINDS,
    PRODUCTION_NCOUNT,
    QUICKLOOK_NCOUNT,
    StopDecision,
    USABLE_DISPOSITIONS as _USABLE,
    evaluate_stop,
    local_exhaustion_status,
    model_points,
    summarize_grid,
    systematic_floor,
)
from isar.predictive import PredictiveArtifactError, validate_master_predictive_artifact
from isar.eig.o41_authority import validate_o41_authoritative_profile
from isar.schema.eig_realized import RealizedScanRequest
from isar.schema.card import Card
from isar.schema.hypotheses import MAX_NCOUNT_PER_SCAN
from isar.schema.steering import (ANALYSIS_UNUSABLE, AUXILIARY_TRACK_NODE, CANDIDATE_NOT_DETECTED,
                                  CANONICAL_MASTER, realized_outcome_for)

DEFAULT_WAIT_TIMEOUT = 600.0
# The instrument point-count ceiling (TAVI enforces max_points=200 on a scan command). An over-limit
# scan latches TAVI's validator state and blocks all later API calls until restart (see
# docs/campaigns/2026-07_MVP_ZONE_RETEST.md), so ISAR must never submit/validate one. Used only as the
# fallback when the live limit cannot be read from the instrument once per campaign.
DEFAULT_MAX_POINTS = 200
# Smallest energy window a feasibility trim is allowed to produce. A one-point window is a degenerate
# window (``isar/drive/acquisition/physical_windows.py`` rejects it as ``degenerate_window``), and the
# instrument itself refuses a single-point scan command with an HTTP 400 — so a feasible run shorter
# than this is not a scan worth salvaging and the request is declined instead of trimmed.
FEASIBLE_TRIM_MIN_POINTS = 2
# Candidate keys a TAVI /state or /schema payload might carry the point-count limit under.
_MAX_POINTS_KEYS = ("max_points", "maxPoints", "scan_max_points", "max_scan_points", "points_max")
_LIMIT_KEYS = {
    "max_points": _MAX_POINTS_KEYS,
    "max_neutrons_per_point": ("max_neutrons_per_point", "maxNeutronsPerPoint",
                                "max_ncount", "max_ncount_per_point"),
    "queue_neutron_budget": ("queue_neutron_budget", "queueNeutronBudget",
                              "max_queue_neutrons", "queue_neutrons"),
    "max_queued": ("max_queued", "maxQueued", "max_queue_size", "queue_max_scans"),
}
_DEFAULT_CALIBRATION = (Path(__file__).resolve().parents[2] / "calibration"
                        / "phonon_dft_deterministic.json")
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
)


def _extract_limit(payload: Any, keys: tuple[str, ...]) -> int | None:
    """Extract a positive integral limit from a top-level or nested TAVI limits document."""
    if not isinstance(payload, dict):
        return None
    scopes = [payload]
    for key in ("limits", "instrument_limits", "queue_limits"):
        if isinstance(payload.get(key), dict):
            scopes.append(payload[key])
    for scope in scopes:
        for key in keys:
            value = scope.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 1:
                return int(value)
            if (isinstance(value, float) and math.isfinite(value)
                    and value >= 1.0 and value.is_integer()):
                return int(value)
    return None


def _extract_max_points(payload: Any) -> int | None:
    """Pull a positive integer point-count limit out of a TAVI /state or /schema body, checking the
    known key names at the top level and inside a nested ``limits`` dict. Returns ``None`` when no
    recognizable limit is present (the caller then falls back to :data:`DEFAULT_MAX_POINTS`)."""
    return _extract_limit(payload, _MAX_POINTS_KEYS)


def _preflight_instrument_limits(client) -> dict[str, Any]:
    """Resolve the additive TAVI limit snapshot once, preferring /state field by field."""
    values: dict[str, int | None] = {name: None for name in _LIMIT_KEYS}
    sources: dict[str, str] = {name: "unavailable" for name in _LIMIT_KEYS}
    for method in ("get_state", "get_schema"):
        fn = getattr(client, method, None)
        if not callable(fn):
            continue
        try:
            payload = fn()
        except Exception as error:   # noqa: BLE001 — discovery is best-effort and fallback-safe
            _LOG.warning("TAVI instrument-limit discovery via %s failed: %s", method, error)
            continue
        for name, keys in _LIMIT_KEYS.items():
            if values[name] is not None:
                continue
            value = _extract_limit(payload, keys)
            if value is not None:
                values[name] = value
                sources[name] = method
    if values["max_points"] is None:
        values["max_points"] = DEFAULT_MAX_POINTS
        sources["max_points"] = "default"
    return {**values, "sources": sources}


def _preflight_max_points(client) -> tuple[int, str]:
    """Read the instrument's max-points limit ONCE per campaign, returning ``(limit, source)``.

    Tries ``GET /state`` then ``GET /schema`` (whichever first carries a recognizable limit). A
    preflight failure — endpoint unreachable, method absent on the client seam, or no limit present —
    must never break the campaign: it falls back silently to :data:`DEFAULT_MAX_POINTS`, recording the
    fallback via the ``source`` so the eventual clamp event states where the limit came from."""
    snapshot = _preflight_instrument_limits(client)
    return int(snapshot["max_points"]), str(snapshot["sources"]["max_points"])


def _runtime_limits(cfg, objective, n_points: int | None = None) -> dict[str, Any]:
    policy = dict(getattr(objective, "steering", None) or {})
    instrument = dict(getattr(cfg, "instrument_limits", None) or {})
    instrument.setdefault("max_points", int(getattr(cfg, "max_points", DEFAULT_MAX_POINTS)))
    instrument.setdefault("sources", {"max_points": getattr(cfg, "max_points_source", "default")})
    result = dict(instrument)
    result["instrument_max_neutrons_per_point"] = instrument.get("max_neutrons_per_point")
    result["max_points"] = min(int(policy.get("max_scan_points", MAX_POINTS)),
                               int(instrument["max_points"]))
    policy_ncount = int(policy.get("max_ncount_per_scan", MAX_NCOUNT_PER_SCAN))
    candidates = [(policy_ncount, "policy")]
    if instrument.get("max_neutrons_per_point") is not None:
        candidates.append((int(instrument["max_neutrons_per_point"]),
                           instrument["sources"].get("max_neutrons_per_point", "instrument")))
    if n_points and instrument.get("queue_neutron_budget") is not None:
        candidates.append((int(math.floor(float(instrument["queue_neutron_budget"]) /
                                          max(int(n_points), 1))),
                           instrument["sources"].get("queue_neutron_budget", "instrument")))
    result["max_neutrons_per_point"] = min(value for value, _source in candidates)
    result["effective_ncount_sources"] = [source for value, source in candidates
                                           if value == result["max_neutrons_per_point"]]
    return result


def _analysis_runner_kwargs(cfg, objective, *, finish_initial_wave: bool = False) -> dict:
    """Deadline allowance supported by the runner seam, without imposing it on old test doubles."""
    limit = objective.analysis_limit_seconds
    if limit is None:
        return {}
    spent = float(analysis_clock_state(cfg.log_path)["spent_seconds"])
    remaining = max(float(limit) - spent, 0.0)
    parameters = inspect.signature(cfg.runner.run).parameters
    accepts_kwargs = any(item.kind == inspect.Parameter.VAR_KEYWORD
                         for item in parameters.values())
    result = {}
    if "analysis_allowance_seconds" in parameters or accepts_kwargs:
        result["analysis_allowance_seconds"] = remaining
    if "finish_initial_wave" in parameters or accepts_kwargs:
        result["finish_initial_wave"] = bool(finish_initial_wave)
    return result


def _require_complete_analysis(result, objective) -> None:
    """Turn a soft runner checkpoint stop into the campaign's explicit incomplete outcome."""
    if getattr(result, "completed", True):
        return
    limit = float(objective.analysis_limit_seconds or 0.0)
    raise AnalysisLimitExceeded(
        f"analysis limit exhausted; active fit wave checkpointed (limit {limit:.1f}s)")


def _emitted_point_count(e_lo: float, e_hi: float, e_step: float) -> int:
    """The TRUE (unclamped) number of points TAVI computes from a ``deltaE lo hi step`` command.

    :func:`~isar.drive.acquisition.acquire.points_in_window` clamps to the acquisition budget
    (``MAX_POINTS``); TAVI does not — it counts the command literally. A missed-window recenter widens
    ``e_lo``/``e_hi`` without recomputing the (tight) ``e_step``, so the emitted command can carry far
    more points than the clamped ledger shows (the 594-pt over-limit scan). This is what the guard
    checks, so a wide-window/tight-step proposal cannot slip past the ledger clamp to TAVI."""
    count, _step = resolve_point_count(e_lo, e_hi, e_step)
    return count


def _guard_point_count(proposal, max_points: int, source: str, log_path, it: int, *,
                       event_sink=log_event) -> bool:
    """Clamp ``proposal`` to the instrument's ``max_points`` limit BEFORE any validate/submit call.

    When the emitted point count exceeds the limit, COARSEN ``e_step`` to keep the FULL energy window
    (never truncate it) so the scan fits, then recompute ``n_points`` via the acquisition helper. A
    greppable ``proposal_clamped`` exception event records the old/new step + point counts, the hkl,
    the limit, and where the limit came from. No-op (returns ``False``) when the proposal already fits.
    Returns ``True`` when it clamped."""
    n_emitted = _emitted_point_count(proposal.e_lo, proposal.e_hi, proposal.e_step)
    if n_emitted <= max_points:
        proposal.n_points = n_emitted
        return False
    old_step = float(proposal.e_step)
    grid = resolve_uniform_grid(
        proposal.e_lo, proposal.e_hi, proposal.e_step,
        max_points=max_points, coarsen_span=COARSEN_FROM_DESIGNED_SPAN)
    if grid.n_points > int(max_points):
        raise InstrumentPointLimitExceeded(
            "instrument_point_limit_exceeded: historical designed-span coarsening resolves to "
            f"{grid.n_points} adapter-exact points above the live limit {int(max_points)} "
            f"from {source}")
    proposal.e_step = grid.e_step
    proposal.n_points = grid.n_points
    if event_sink is not None:
        event_sink(log_path, "exception", {
            "action": "proposal_clamped",
            "hkl": [proposal.h, proposal.k, proposal.l],
            "limit": int(max_points), "limit_source": source,
            "old_step": old_step, "new_step": grid.e_step,
            "old_n_points": int(n_emitted),
            "new_n_points": grid.n_points,
            "iter": it,
        })
    return True


def _request_fingerprint_for(proposal, *, e_lo: float, e_hi: float, e_step: float,
                             ncount: int) -> str:
    """Hash one exact request without including additive audit references."""
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    action_role = meta.get("acquisition_action_role")
    if action_role is None:
        action_role = ("classification" if meta.get("purpose") == "differentiate" else
                       "discovery" if proposal.kind == "quicklook" else "production")
        meta["acquisition_action_role"] = action_role
    basis = {
        "hkl": [float(proposal.h), float(proposal.k), float(proposal.l)],
        "energy_window": [float(e_lo), float(e_hi), float(e_step)],
        "ncount": int(ncount), "kind": str(proposal.kind),
        "action_role": str(action_role),
        "target_branch_role": meta.get("target_branch_role"),
        "rival_branch_role": meta.get("rival_branch_role"),
        "authoritative_evidence_revision": meta.get("authoritative_evidence_revision"),
    }
    encoded = json.dumps(basis, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _stamp_proposal_provenance(proposal) -> None:
    """Freeze the additive decision identity after all window/count clamps are known."""
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    meta["proposal_fingerprint"] = _request_fingerprint_for(
        proposal,
        e_lo=proposal.e_lo,
        e_hi=proposal.e_hi,
        e_step=proposal.e_step,
        ncount=proposal.ncount,
    )


def finalized_proposal(proposal) -> FinalizedProposal | None:
    """Stage 2 of the action lifecycle: the exact request, after every clamp, before validation.

    Built here rather than in ``acquire._build_proposal`` because the request is not final there —
    :func:`_finalize_proposal` still applies the instrument point-count ceiling and the neutron
    affordability clamp afterwards. ``request_fingerprint`` reuses the provenance hash
    :func:`_stamp_proposal_provenance` already computes over the *finalized* window and count, so the
    two can never disagree about what was actually asked for; re-finalizing after a step shrink or a
    re-center moves it while the intent's scientific fingerprint stays put.

    Returns ``None`` for a proposal that never carried an intent (older unit-level callers), so the
    lifecycle is additive rather than a new precondition on every proposal.
    """
    intent = getattr(proposal, "intent", None)
    fingerprint = (proposal.meta or {}).get("proposal_fingerprint")
    if intent is None or not fingerprint:
        return None
    return FinalizedProposal(
        intent=intent, request_fingerprint=str(fingerprint),
        e_lo=float(proposal.e_lo), e_hi=float(proposal.e_hi), e_step=float(proposal.e_step),
        n_points=int(proposal.n_points or 0), ncount=int(proposal.ncount))


def _feasibility_manifest_fingerprint(validation: dict) -> str:
    """Hash TAVI's frozen feasibility verdict — the geometry an attempt was judged under.

    Deliberately covers only the instrument's answer (queueability, blockers, the planned/feasible
    point manifest), never the request that produced it: a q that is reachable today and blocked
    tomorrow must be distinguishable *at the same* request fingerprint, or a geometry rejection gets
    charged to the scientific action and suppresses it permanently.

    List order is preserved rather than normalized, except for ``blockers`` (an unordered set of
    reasons). ``planned_feasible_mask`` and ``feasible_segments`` are positional — sorting them would
    make two genuinely different geometries hash the same, and a false *match* is the dangerous
    direction here: it would let a suppression formed under one geometry silently apply to another.
    A false *difference* only costs a redundant retry.

    Canonicalized with the same strict encoder as the inventory artifacts, so an unexpected type
    raises instead of being silently stringified into a stable-looking but meaningless address.
    """
    manifest = {
        "would_queue": bool(validation.get("would_queue", False)),
        "blockers": sorted(str(b) for b in (validation.get("blockers") or [])),
        "requested_points": validation.get("requested_points"),
        "feasible_points": validation.get("feasible_points"),
        "planned_feasible_mask": validation.get("planned_feasible_mask"),
        "feasible_segments": validation.get("feasible_segments"),
        "infeasible": validation.get("infeasible"),
    }
    return hashlib.sha256(canonical_json(manifest).encode("utf-8")).hexdigest()


def validated_attempt(finalized: FinalizedProposal | None, validation: dict, *,
                      status: str) -> ValidatedAttempt | None:
    """Stage 3: TAVI's verdict on one finalized request, under one instrument geometry."""
    if finalized is None:
        return None
    return ValidatedAttempt(
        proposal=finalized,
        feasibility_manifest_fingerprint=_feasibility_manifest_fingerprint(validation),
        feasible_points=_feasible_point_count(validation, finalized.n_points or 1),
        status=str(status))


def _finalize_proposal(proposal, objective, cfg, budget_state: dict, *, it: int,
                       event_sink=log_event) -> None:
    """Normalize one proposal immediately before validation/submission.

    The frozen steering ceiling and the live instrument ceiling are both authoritative.  Once the
    true emitted point count is known, neutron affordability is recalculated against it; hour budgets
    remain scan-boundary-only because ISAR has no invented ncount-to-seconds conversion.
    """
    policy = dict(getattr(objective, "steering", None) or {})
    limits = _runtime_limits(cfg, objective)
    cap = int(limits["max_points"])
    source = f"policy+{limits['sources'].get('max_points', 'default')}"
    _guard_point_count(
        proposal, cap, source, cfg.log_path, it, event_sink=event_sink)
    n_points = int(proposal.n_points or 0)
    limits = _runtime_limits(cfg, objective, n_points)
    minimum = int(policy.get(
        "quicklook_ncount" if proposal.kind == "quicklook" else "production_batch_ncount",
        QUICKLOOK_NCOUNT if proposal.kind == "quicklook" else PRODUCTION_NCOUNT))
    instrument_cap = int(limits["max_neutrons_per_point"])
    if instrument_cap < minimum:
        raise InstrumentLimitBelowMinimumBatch(
            f"instrument_limit_below_minimum_batch: {instrument_cap:g} neutrons/point "
            f"is below the {minimum:g} {proposal.kind} floor")
    diagnostics = (proposal.meta or {}).get("count_diagnostics")
    preclamp_requested = (diagnostics.get("requested_before_runtime_limit")
                          if isinstance(diagnostics, dict) else None)
    if int(proposal.ncount) > instrument_cap or (
            preclamp_requested is not None and not diagnostics.get("limit_clamp_logged")):
        requested = max(int(proposal.ncount), int(preclamp_requested or 0))
        proposal.ncount = instrument_cap
        if isinstance(diagnostics, dict):
            diagnostics["runtime_ncount_limit"] = instrument_cap
            diagnostics["limit_clamp_logged"] = True
        if event_sink is not None:
            event_sink(cfg.log_path, "exception", {
                "action": "proposal_ncount_clamped_to_limit",
                "hkl": [proposal.h, proposal.k, proposal.l],
                "requested_ncount": requested, "effective_ncount": instrument_cap,
                "policy_limit": int(policy.get("max_ncount_per_scan", MAX_NCOUNT_PER_SCAN)),
                "instrument_limits": {
                    "max_neutrons_per_point": limits.get("instrument_max_neutrons_per_point"),
                    "queue_neutron_budget": limits.get("queue_neutron_budget")},
                "limit_sources": limits.get("effective_ncount_sources", []),
                "n_points": n_points, "iter": it})
    budget = dict(budget_state or {})
    if budget.get("kind") != "neutrons" or budget.get("amount") is None:
        normalize_proposal_score(proposal, float(getattr(objective, "tolerance_meV", 0.0)))
        _stamp_proposal_provenance(proposal)
        return
    if bool(policy.get("dynamic_ncount", False)) and int(proposal.ncount) < minimum:
        old_ncount = int(proposal.ncount)
        proposal.ncount = minimum
        if event_sink is not None:
            event_sink(cfg.log_path, "exception", {
                "action": "proposal_ncount_raised_to_floor",
                "hkl": [proposal.h, proposal.k, proposal.l],
                "old_ncount": old_ncount, "new_ncount": minimum,
                "n_points": n_points, "iter": it})
    remaining = max(float(budget["amount"]) - float(budget.get("spent", 0.0)), 0.0)
    affordable = int(math.floor(remaining / max(n_points, 1)))
    if affordable < minimum:
        raise BudgetInsufficientForMinimumBatch(
            f"budget_insufficient_for_minimum_batch: need {minimum * max(n_points, 1):g} "
            f"neutrons, have {remaining:g}")
    if int(proposal.ncount) > affordable:
        old_ncount = int(proposal.ncount)
        proposal.ncount = affordable
        if event_sink is not None:
            event_sink(cfg.log_path, "exception", {
                "action": "proposal_ncount_clamped", "hkl": [proposal.h, proposal.k, proposal.l],
                "old_ncount": old_ncount, "new_ncount": affordable, "n_points": n_points,
                "remaining_neutrons": remaining, "iter": it})
    normalize_proposal_score(proposal, float(getattr(objective, "tolerance_meV", 0.0)))
    _stamp_proposal_provenance(proposal)


def _resolve_calibration(cfg_value) -> Calibration:
    if isinstance(cfg_value, Calibration):
        return cfg_value
    if cfg_value:
        return load_calibration(cfg_value)
    return load_calibration(_DEFAULT_CALIBRATION)


def _make_resolution_cb(client, override):
    if override is not None:
        return override
    def cb(h, k, l, deltaE):
        try:
            return client.get_resolution(h, k, l, deltaE)
        except Exception:   # noqa: BLE001  (any transport/refusal → advisory default FWHM)
            return {"ok": False}
    return cb


def _belief_slices(card_state: dict, *, sample: str) -> dict[float, list[dict]]:
    """Belief-model rows for the active sample, grouped by temperature bin."""
    slices: dict[float, list[dict]] = {}
    for row in (card_state.get("branch_belief_models") or []):
        if not isinstance(row, dict) or str(row.get("sample", "")) != str(sample):
            continue
        try:
            temperature = float(row.get("t_bin"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(temperature):
            slices.setdefault(temperature, []).append(dict(row))
    return slices


def _complete_slice_temperatures(slices: dict[float, list[dict]]) -> list[float]:
    """Temperatures whose slice holds both branches — the only ones that can classify anything."""
    return sorted(temperature for temperature, group in slices.items()
                  if len({row.get("branch_id") for row in group if row.get("branch_id")}) >= 2)


def resolve_campaign_slice(card_state: dict, objective, *, sample: str,
                           binning_policy_fingerprint: str | None = None
                           ) -> tuple[CampaignSliceKey | None, str | None]:
    """Resolve which belief slice this campaign is acting on, or refuse to guess.

    Returns ``(key, None)`` when the slice is determined and ``(None, reason)`` when the evidence is
    ambiguous. The previous rule took ``max(complete)`` — the newest temperature with both branches
    present — which silently classified a 100 K campaign against 200 K beliefs whenever a newer
    complete slice existed, with nothing in the run record to say so.

    Order: the temperature the objective explicitly declared; else the single complete slice when
    exactly one qualifies; else refuse. No complete slice at all is the ordinary cold start, not an
    ambiguity: the campaign runs on the common prior with ``temperature_bin=None``.

    The caller turns a refusal into an incomplete inventory, so an unattended run stops resumably with
    a structured reason instead of either crashing or guessing.
    """
    slices = _belief_slices(card_state, sample=sample)
    complete = _complete_slice_temperatures(slices)
    key = lambda temperature: CampaignSliceKey(
        sample=str(sample), temperature_bin=temperature,
        binning_policy_fingerprint=binning_policy_fingerprint)

    declared = getattr(objective, "target_temperature_K", None)
    if declared is not None:
        declared = float(declared)
        match = next((t for t in complete if math.isclose(t, declared, rel_tol=0.0, abs_tol=1e-6)),
                     None)
        if match is not None:
            return key(match), None
        if not complete:
            return key(None), None
        return None, (
            f"campaign_slice_unresolved: the objective declares {declared:g} K but the complete "
            f"belief slices for sample {sample!r} are at "
            f"{', '.join(f'{t:g} K' for t in complete)}; classifying against a slice the campaign "
            "did not declare would silently answer with the wrong temperature's evidence")
    if len(complete) == 1:
        return key(complete[0]), None
    if not complete:
        return key(None), None
    return None, (
        f"campaign_slice_unresolved: sample {sample!r} has complete belief slices at "
        f"{', '.join(f'{t:g} K' for t in complete)} and the objective declares no target "
        "temperature; declare target_temperature_K rather than letting the controller guess")


def _temperature_binning_fingerprint(workspace: Path) -> str | None:
    """Hash the scientist-owned rules that decide which temperature bin evidence lands in.

    Two policy blocks decide that, and both belong in the identity:

    * ``collation.t_round`` is the binning granularity itself — the bin is
      ``round(T / t_round) * t_round`` (:mod:`isar.session.branch_model`,
      :mod:`isar.session.driver`). Change it and rows regroup into different slices.
    * ``temperature_rule`` decides which field ``T`` is *read from* (setpoint vs sensors, and the
      divergence tolerance), so it changes the value that then gets binned.

    Either can silently reroute evidence while the sample and the nominal temperature both look
    unchanged, which is exactly the kind of move a slice fingerprint exists to make visible.
    """
    policy_path = workspace / "card.policy.toml"
    if not policy_path.exists():
        return None
    try:
        card = Card.load(policy_path, workspace / "card.state.json")
        rule = getattr(card.policy, "temperature_rule", None) or {}
        t_round = (getattr(card.policy, "collation", None) or {}).get("t_round")
    except (OSError, ValueError, TypeError) as exc:
        logging.getLogger(__name__).warning(
            "could not read the temperature binning rules from %s: %s", policy_path, exc)
        return None
    if not rule and t_round is None:
        return None
    payload = canonical_json({"temperature_rule": rule, "t_round": t_round})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _make_classification_providers(card_state: dict, objective, resolution_cb, *, sample: str,
                                   temperature_bin: float | None = None):
    """Build MI/rival providers from the resolved belief slice.

    ``temperature_bin`` is the slice :func:`resolve_campaign_slice` settled on; ``None`` means the
    cold start, where no measured slice exists and the common prior is used.
    """
    from isar.drive.acquisition.acquire import _resolve_fwhm
    from isar.drive.acquisition.classify_eig import assignment_mi, belief_predict

    slices = _belief_slices(card_state, sample=sample)
    selected = list(slices.get(temperature_bin) or []) if temperature_bin is not None else []
    if not selected:
        # A common prior-only slice is safe across samples/T; unrelated measured slices are not.
        selected = [{"branch_id": h.get("name"), "anchors": [], "n_anchors": 0,
                     "parameter_prior": h, "period": None}
                    for h in (getattr(objective, "hypotheses", []) or [])
                    if h.get("name") not in (None, "null")]
    if len({row.get("branch_id") for row in selected if row.get("branch_id")}) < 2:
        return None, None
    target = (getattr(objective, "target_hypothesis", None)
              or getattr(objective, "target_branch", None))
    policy = dict(getattr(objective, "steering", {}) or {})
    entropy_stop = float(policy.get("assignment_entropy_stop_nats", 0.2))
    target_branch = getattr(objective, "target_branch", None) or target
    rival_branch = "acoustic" if target_branch == "optic" else "optic"
    rival_resolved_hkls = []
    for assignment in ((card_state.get("branch_tracks") or {}).get("assignments") or []):
        candidates = assignment.get("candidates") or []
        candidate_index = assignment.get(f"{rival_branch}_candidate_index")
        if not isinstance(candidate_index, int) or not 0 <= candidate_index < len(candidates):
            continue
        probabilities = (candidates[candidate_index].get("probabilities") or {})
        acoustic = float(probabilities.get("acoustic", 0.0))
        optic = float(probabilities.get("optic", 0.0))
        known_total = acoustic + optic
        known = ([acoustic / known_total, optic / known_total] if known_total > 0.0 else [])
        identity_entropy = -sum(value * math.log(value) for value in known if value > 0.0)
        if (float(probabilities.get(rival_branch, 0.0)) >= 0.85
                and identity_entropy <= entropy_stop and assignment.get("q")):
            rival_resolved_hkls.append(tuple(float(value) for value in assignment["q"]))

    def reduced(hkl, period):
        if not period:
            return None
        folded = [((float(v) + 0.5 * period) % period) - 0.5 * period for v in hkl]
        return math.sqrt(sum(v * v for v in folded))

    def components(g):
        out = []
        for row in selected:
            u = reduced(g.hkl, row.get("period"))
            mu, sigma = belief_predict(row, g.q if u is None else u)
            out.append({"branch_id": row.get("branch_id"), "mean": mu, "sigma": sigma,
                        "probability": float(row.get("n_anchors", 0)) + 1.0})
        total = sum(item["probability"] for item in out) or 1.0
        for item in out:
            item["probability"] /= total
        return out

    def classify(g):
        if any(math.dist(g.hkl, hkl) <= 1e-5 for hkl in rival_resolved_hkls):
            return 0.0  # labeled rival context has no target-precision acquisition value
        parts = components(g)
        target_part = next((part for part in parts if part["branch_id"] == target), parts[0])
        fwhm = _resolve_fwhm(resolution_cb, *g.hkl, max(target_part["mean"], 0.0))
        tolerance = policy.get("differentiation_tolerance_meV")
        if tolerance is None:
            tolerance = max(3.0 * float(objective.tolerance_meV), fwhm / 2.3548)
        return assignment_mi(parts, meas_sigma=fwhm / 2.3548,
                             differentiation_tolerance_meV=float(tolerance))

    def rival_band(g):
        parts = [part for part in components(g) if part["branch_id"] != target]
        if not parts:
            return None
        rival = max(parts, key=lambda part: part["probability"])
        return rival["mean"], rival["sigma"]

    return classify, rival_band


def _validate_body(proposal, *, sample: str, background: dict | None = None) -> dict[str, Any]:
    """The ``POST /validate`` body — ``parameters`` only (engine/seed/isolated are scan-only fields).

    ``background`` is the campaign's frozen catalog-backed background profile (see
    :func:`_freeze_sample_background`), stamped as a per-scan override. It rides on the validate body
    too, because the instrument validates the background block (catalog version, source IDs, scales):
    without it a scan could validate clean and then be refused at submit. ``None`` happens only for a
    campaign that opted out of freezing via ``sample_background="instrument_config"`` → the key is
    absent, the body is byte-identical to the pre-background one, and the instrument's live
    configuration decides the background. An undeclared campaign stamps a frozen DISABLED profile
    instead of omitting the key (see :func:`_freeze_sample_background`).
    """
    body: dict[str, Any] = {
        "parameters": {
            "H": proposal.h, "K": proposal.k, "L": proposal.l,
            "scan_command1": proposal.scan_command(),
            # A stale scan_command2 on the instrument (e.g. TAVI GUI residue)
            # multiplies the point grid and can scan the same axis twice; the
            # driver owns the scan definition, so clear it explicitly. The
            # instrument-side bleed is fixed (TAVI GUI-independence change); this
            # stays as belt-and-braces.
            "scan_command2": "",
            "number_neutrons": int(proposal.ncount),
            # TAVI's API is now GUI-independent: POST /scan|/validate build state from
            # instrument defaults (Al_bragg) + this request's parameters, no longer
            # inheriting the operator's GUI sample selection. ISAR measures phonons, so
            # the campaign's sample must be stamped explicitly.
            "sample": sample,
        },
    }
    if background is not None:
        # Transport only: the loop never chooses, tunes, or re-resolves this — it stamps the profile
        # frozen at campaign setup. The instrument derives ``source="per_scan_override"`` from the
        # key's presence, so no companion ``background_source`` field is sent.
        body["background"] = background
    return body


def _scan_body(proposal, engine: str, seed: int, *, sample: str,
               background: dict | None = None) -> dict[str, Any]:
    """The ``POST /scan`` body — the validate body plus the isolated/seeded engine selection."""
    body = _validate_body(proposal, sample=sample, background=background)
    body.update({"engine": engine, "seed": int(seed), "isolated": True})
    return body


def _feasible_point_count(validation: dict, fallback: int) -> int:
    """Executable point count from TAVI's frozen feasibility manifest."""
    value = validation.get("feasible_points") if isinstance(validation, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
        return int(value)
    mask = validation.get("planned_feasible_mask") if isinstance(validation, dict) else None
    if isinstance(mask, list):
        return sum(bool(item) for item in mask)
    return max(int(fallback), 0)


def _feasible_runs(validation: dict, n_points: int) -> list[tuple[int, int]] | None:
    """Every maximal contiguous feasible run in TAVI's frozen manifest, in positional order.

    Returns inclusive ``(first_index, last_index)`` pairs, or ``None`` when the manifest does not
    localize the infeasibility against the exact grid that was requested. ``planned_feasible_mask``
    and ``feasible_segments`` are positional against the requested point list, so a mask whose
    length disagrees with the requested count describes a *different* grid: indexing into it would
    trim to the wrong energies, which is far worse than not trimming at all. That mismatch is logged
    and reported as "no localization".
    """
    if not isinstance(validation, dict) or int(n_points) <= 0:
        return None
    mask = validation.get("planned_feasible_mask")
    flags: list[bool] | None = None
    if isinstance(mask, list):
        if len(mask) != int(n_points):
            _LOG.warning(
                "planned_feasible_mask length %d does not match the %d requested points; "
                "refusing to localize the infeasible region", len(mask), int(n_points))
            return None
        flags = [bool(item) for item in mask]
    else:
        segments = validation.get("feasible_segments")
        if isinstance(segments, list) and segments:
            flags = [False] * int(n_points)
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                start, end = segment.get("start_index"), segment.get("end_index")
                if not isinstance(start, int) or not isinstance(end, int):
                    continue
                if start < 0 or end >= int(n_points) or end < start:
                    _LOG.warning(
                        "feasible_segments entry [%r, %r] falls outside the %d requested points; "
                        "refusing to localize the infeasible region", start, end, int(n_points))
                    return None
                for index in range(start, end + 1):
                    flags[index] = True
    if flags is None or not any(flags):
        return None
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for index, feasible in enumerate([*flags, False]):
        if feasible and run_start is None:
            run_start = index
        elif not feasible and run_start is not None:
            runs.append((run_start, index - 1))
            run_start = None
    return runs


def _largest_feasible_run(validation: dict, n_points: int) -> tuple[int, int] | None:
    """The longest contiguous feasible run, or ``None`` without localization.

    Ties go to the first run encountered, which keeps the choice deterministic for a given manifest.
    """
    runs = _feasible_runs(validation, n_points)
    if not runs:
        return None
    return max(runs, key=lambda run: run[1] - run[0])


def _select_feasible_run(validation: dict, energies: Sequence[float], *,
                         target_energy: float | None,
                         acceptance: tuple[float, float] | None,
                         ) -> tuple[tuple[int, int] | None, str]:
    """Choose which feasible run a trimmed window should keep, and say why.

    Returns ``(run, decision)``. ``decision`` is one of:

    * ``"no_localization"`` — the manifest cannot be indexed against this grid (``run`` is ``None``);
    * ``"contains_target"`` — the kept run spans the energy this scan is *about*;
    * ``"longest"`` — no run contains the target (or none is declared), so the longest run wins;
    * ``"target_excluded"`` — a target is declared, no run contains it, and the longest run does not
      even overlap its acceptance interval (``run`` is ``None``).

    The last case is the point of this function. Maximizing run length alone can keep a long stretch
    of reachable-but-irrelevant energies while dropping the excitation the scan exists to measure —
    a scan that is submittable, cheap, and scientifically empty. Declining is strictly better than
    submitting a blind window.
    """
    runs = _feasible_runs(validation, len(energies))
    if not runs:
        return None, "no_localization"
    longest = max(runs, key=lambda run: run[1] - run[0])
    if target_energy is None:
        return longest, "longest"

    def span(run: tuple[int, int]) -> tuple[float, float]:
        return float(energies[run[0]]), float(energies[run[1]])

    for run in runs:
        low, high = span(run)
        if low - 1e-9 <= float(target_energy) <= high + 1e-9:
            return run, "contains_target"
    if acceptance is not None:
        low, high = span(longest)
        if not (low <= float(acceptance[1]) + 1e-9 and high >= float(acceptance[0]) - 1e-9):
            return None, "target_excluded"
    return longest, "longest"


# Blocker vocabulary is TAVI's, not ISAR's, so the trim recognises infeasibility by marker rather
# than by an enumerated list it cannot keep in sync. Anything unrecognised (queue limits, budget
# refusals, transport complaints) fails CLOSED: the window is left alone and the ordinary decline
# path runs, because mutating a request in response to a refusal that had nothing to do with
# geometry both loses the original request and answers the wrong question.
_INFEASIBILITY_BLOCKER_MARKERS = (
    "infeasible", "unreachable", "out_of_range", "outofrange", "out of range",
    "triangle", "geometry", "not_reachable",
)


def _is_feasibility_shaped_refusal(validation: dict) -> bool:
    """Whether a ``would_queue=False`` verdict is about geometry and nothing else."""
    blockers = validation.get("blockers") if isinstance(validation, dict) else None
    if not isinstance(blockers, list) or not blockers:
        return False
    return all(
        any(marker in str(blocker).lower() for marker in _INFEASIBILITY_BLOCKER_MARKERS)
        for blocker in blockers)


def _executed_point_count(data: dict, fallback: int) -> int:
    result = data.get("result") if isinstance(data, dict) else None
    if not isinstance(result, dict):
        return max(int(fallback), 0)
    mask = result.get("executed_feasible_mask")
    if isinstance(mask, list) and mask:
        return sum(bool(item) for item in mask)
    return max(int(fallback), 0)


def _target_supporting_feasible_points(validation: dict, target_band) -> int | None:
    """Feasible bins inside the target's 95%-credible energy support; None without a manifest."""
    manifest = validation.get("point_manifest") if isinstance(validation, dict) else None
    if not isinstance(manifest, list) or target_band is None:
        return None
    mean, sigma = map(float, target_band)
    lo, hi = mean - 2.0 * sigma, mean + 2.0 * sigma
    count = 0
    for point in manifest:
        if not isinstance(point, dict) or not point.get("feasible", False):
            continue
        values = point.get("values") or {}
        energy = values.get("deltaE") if isinstance(values, dict) else None
        if (isinstance(energy, (int, float)) and not isinstance(energy, bool)
                and math.isfinite(float(energy)) and lo <= float(energy) <= hi):
            count += 1
    return count


def _tavi_error_code(error: TaviError) -> str | None:
    """Best-effort structured code from a TAVI error envelope."""
    try:
        payload = json.loads(error.body)
    except (TypeError, ValueError):
        return None
    detail = payload.get("error") if isinstance(payload, dict) else None
    return detail.get("code") if isinstance(detail, dict) else None


def _row_hkl(row: dict):
    h, k, l = row.get("h"), row.get("k"), row.get("l")
    if h is None or k is None or l is None:
        return None
    return (float(h), float(k), float(l))


def _pick_interval(row: dict) -> tuple:
    """The (lo, hi) the convergence test should use, and whether it is trustworthy.

    Precedence, most-authoritative first: the parametric-bootstrap ``interval_lo/hi`` (full tier)
    when ISAR does not flag it unreliable; else the exact ω₀ **profile** interval (full tier, robust
    in the Γ→0 corner where the bootstrap collapses to a spurious spike); else the delta-method
    **HESSE** interval (fast tier) when ISAR marks it ``hesse_trusted`` — the asymptotic regime where
    HESSE σ ≈ the bootstrap half-width, so the loop can converge without paying for a bootstrap.
    Returns ``(lo, hi, ok)``; ``ok`` is False when none is available (→ no calibrated half-width, q
    not converged: an untrusted HESSE fit escalates to the full tier rather than certifying).
    """
    lo, hi = row.get("interval_lo"), row.get("interval_hi")
    plo, phi = row.get("omega0_profile_lo"), row.get("omega0_profile_hi")
    hlo, hhi = row.get("hesse_lo"), row.get("hesse_hi")
    reliable = row.get("interval_reliable")            # bool | None (None = older ledger → trust it)
    if reliable is not False and lo is not None and hi is not None:
        return lo, hi, True                            # bootstrap (well-constrained, full tier)
    if plo is not None and phi is not None:            # bootstrap flagged → the profile interval
        return plo, phi, True                          # profile (Γ-at-bound robust, full tier)
    if hlo is not None and hhi is not None and row.get("hesse_trusted"):
        return hlo, hhi, True                          # trusted HESSE (fast tier)
    return None, None, False


def _is_certifiable(row: dict, ok: bool) -> bool:
    """Whether ``row``'s ω₀ interval may SEAT convergence (S3 / DECISIONS S3).

    A calibrated interval exists (``ok`` from :func:`_pick_interval`) AND it is not a flagged one: an
    ``interval_reliable=False`` fit (γ-at-bound — the bootstrap/profile is degenerate, zero-width is
    degeneracy not precision) is uncertifiable EVEN when a profile fallback makes ``_pick_interval``
    return ``ok`` — the flag is authoritative. The sole carve-out is the fast tier's *trusted* HESSE
    interval (``hesse_trusted``), which is calibrated-equivalent by construction and is allowed to
    certify without paying for a bootstrap. ``interval_reliable`` absent (older ledger → None) is
    treated as reliable (additive contract)."""
    if not ok:
        return False
    if row.get("interval_reliable") is False and not row.get("hesse_trusted"):
        return False
    return True


def _row_certifiable(row: dict) -> bool:
    """The S3 certifiability predicate a folded row must pass to SEAT convergence — exactly what
    :func:`_observation_from_row` stamps into its ``certifiable`` field, factored out so the
    ``escalate_fit`` outcome classifier and the observation builder cannot drift apart."""
    return _is_certifiable(row, _pick_interval(row)[2])


def _uncertifiable_kind(row: dict) -> str:
    """Why a usable-but-uncertifiable delivery has no certifiable interval (S3 stop-state taxonomy):
    ``"fast_tier"`` when the only interval is an untrusted fast-tier HESSE (no bootstrap/profile ever
    ran — the q never left the fast tier); ``"gamma_at_bound"`` otherwise (a full-tier bootstrap/
    profile ran but flagged ``interval_reliable=False``)."""
    has_boot = row.get("interval_lo") is not None
    has_prof = row.get("omega0_profile_lo") is not None
    has_hesse = row.get("hesse_lo") is not None
    if has_hesse and not row.get("hesse_trusted") and not has_boot and not has_prof:
        return "fast_tier"
    return "gamma_at_bound"


def _calibrated_hw(row: dict, calibration: Calibration, bucket: str) -> float | None:
    """Shared interval→half-width core of the two observation builders below.

    Takes the reliable ``(lo, hi)`` from :func:`_pick_interval` (bootstrap → profile → trusted HESSE)
    and returns its calibrated half-width at the given count ``bucket``, or ``None`` when there is no
    trustworthy interval (``ok`` False) *or* the calibration rejects the interval. Callers keep their
    distinct output shapes; the quicklook ``omega0_err`` fallback (a different interval source) stays
    inline in :func:`_observation_from_row`."""
    lo, hi, ok = _pick_interval(row)
    if not ok:
        return None
    try:
        return calibration.calibrated_halfwidth(lo, hi, bucket=bucket)
    except Exception:   # noqa: BLE001  (bad interval -> treat as un-calibratable, not a crash)
        return None


def _diagnostic_interval(row: dict) -> tuple[Any, Any, str | None]:
    """Best finite interval available before the certifiability gate, with method provenance."""
    for lo_key, hi_key, method in (
            ("interval_lo", "interval_hi", "bootstrap"),
            ("omega0_profile_lo", "omega0_profile_hi", "profile"),
            ("hesse_lo", "hesse_hi", "hesse"),
            ("bayes_lo", "bayes_hi", "bayesian")):
        lo, hi = row.get(lo_key), row.get(hi_key)
        if lo is not None and hi is not None:
            try:
                if math.isfinite(float(lo)) and math.isfinite(float(hi)) and float(hi) >= float(lo):
                    return lo, hi, method
            except (TypeError, ValueError):
                continue
    return None, None, None


def _calibration_version(calibration: Calibration) -> str:
    raw = calibration.raw or {}
    return str(raw.get("version") or raw.get("schema") or calibration.truth_source or "unversioned")


def _diagnostic_halfwidth(row: dict, calibration: Calibration,
                          bucket: str) -> tuple[float | None, str | None]:
    lo, hi, method = _diagnostic_interval(row)
    if method is None:
        return None, None
    try:
        value = calibration.calibrated_halfwidth(lo, hi, bucket=bucket)
    except Exception:  # noqa: BLE001 - diagnostic absence must not become a delivery interval
        return None, method
    return (float(value) if math.isfinite(float(value)) else None), method


def _effective_exposure_bucket(row: dict, submitted_ncount: int) -> tuple[str, float, str]:
    """Deterministic calibration/steering exposure key with a submitted-count fallback."""
    value = row.get("effective_exposure")
    if value is None:
        fallback = float(submitted_ncount)
        return str(int(round(fallback))), fallback, "submitted_ncount"
    try:
        exposure = float(value)
    except (TypeError, ValueError):
        return "invalid", 0.0, "invalid_effective_exposure"
    if math.isfinite(exposure) and exposure > 0.0:
        return str(int(round(exposure))), exposure, "effective_exposure"
    return "invalid", exposure, "invalid_effective_exposure"


def _calibration_bucket(calibration: Calibration, exposure_key: str) -> tuple[str, str | None]:
    """Record the bucket actually used; unmatched exposures use the global calibration."""
    if exposure_key in calibration.buckets:
        return exposure_key, exposure_key
    return "global", None


def _observation_from_row(row: dict, kind: str, ncount: int, calibration: Calibration,
                          proposal_cost: float = 0.0, *, driver_inserted: bool = False,
                          certification_eligible: bool = True,
                          acquisition_id: str | None = None,
                          count_diagnostics: dict | None = None,
                          acquisition_action_role: str | None = None,
                          target_branch_role: str | None = None,
                          rival_branch_role: str | None = None,
                          authoritative_evidence_revision: str | None = None,
                          proposal_fingerprint: str | None = None,
                          evidence_sink: str = CANONICAL_MASTER) -> dict:
    disp = row.get("disposition")
    verdict = row.get("verdict")
    # `flagged` (model_incomplete) still carries a usable ω₀ — for an ω₀-tolerance objective the flag
    # documents peak-shape imperfection, not an ω₀ error (see stopping.USABLE_DISPOSITIONS).
    usable = disp in _USABLE
    hw = None
    ok = _pick_interval(row)[2]
    certifiable = _row_certifiable(row)
    # S3 third state: a usable delivery with no certifiable interval. Classify WHY (γ-at-bound vs
    # fast-tier-only) so the stop taxonomy can name it; only meaningful for a usable, uncertifiable fit.
    uncertifiable_kind = _uncertifiable_kind(row) if (usable and not certifiable) else None
    exposure_key, effective_exposure, exposure_source = _effective_exposure_bucket(row, ncount)
    bucket, calibration_arg = _calibration_bucket(calibration, exposure_key)
    if usable and ok:
        hw = _calibrated_hw(row, calibration, calibration_arg)
    elif usable and kind == "quicklook":
        # Cheap-tier quicklooks carry only a HESSE omega0_err (no bootstrap interval). Good enough to
        # SEED the ω(q) acquisition model (steering only); it never reaches the stop rule, which
        # consumes the master (pooled) half-width built from a real interval.
        err, w0 = row.get("omega0_err"), row.get("omega0")
        if err is not None and w0 is not None and float(err) > 0:
            try:
                hw = calibration.calibrated_halfwidth(float(w0) - float(err), float(w0) + float(err),
                                                      bucket=calibration_arg)
            except Exception:   # noqa: BLE001
                hw = None
    count_diagnostics = dict(count_diagnostics or {})
    return {
        "hkl": _row_hkl(row), "kind": kind, "calibrated_halfwidth": hw,
        "resolved": usable, "disposition": disp, "verdict": verdict,
        # S3: certifiable gates convergence seating; uncertifiable_kind names the third-state reason.
        "certifiable": certifiable, "uncertifiable_kind": uncertifiable_kind,
        "omega0": row.get("omega0"), "scan": row.get("scan"), "detector": row.get("detector"),
        "proposal_cost": float(proposal_cost), "fit_kind": row.get("fit_kind"),
        "submitted_ncount": int(ncount), "acquisition_id": acquisition_id,
        "acquisition_action_role": acquisition_action_role,
        "target_branch_role": target_branch_role,
        "rival_branch_role": rival_branch_role,
        "authoritative_evidence_revision": authoritative_evidence_revision,
        "proposal_fingerprint": proposal_fingerprint,
        "evidence_sink": evidence_sink,
        # Only for probes. A canonical delivery is already fully described by disposition/certifiable,
        # and giving it a second, coarser status would invite the two to disagree. A probe needs it
        # because its clean null is a *result* that must not read as a failed scan.
        "realized_outcome": (
            None if evidence_sink == CANONICAL_MASTER
            else realized_outcome_for(disp, has_candidate=bool(row.get("branch_candidates")))),
        "requested_ncount": count_diagnostics.get("requested_before_runtime_limit", ncount),
        "runtime_ncount_limit": count_diagnostics.get("runtime_ncount_limit"),
        "ncount_clamped": bool(count_diagnostics.get("requested_before_runtime_limit") is not None),
        "effective_exposure": effective_exposure, "exposure_source": exposure_source,
        "exposure_bucket": bucket,
        "branch_id": row.get("branch_id"),   # Phase A: only acoustic (or null) rows steer ω(q)
        "branch_candidates": row.get("branch_candidates"),
        "selected_candidate_index": row.get("selected_candidate_index"),
        "track_state": row.get("track_state"), "track_revision": row.get("track_revision"),
        "target_probability": row.get("target_probability"),
        "identity_entropy": row.get("identity_entropy"),
        "local_identity_clear": row.get("local_identity_clear"),
        "modality": row.get("modality"),
        "gamma_at_bound": bool(row.get("gamma_at_bound", False)),
        "linewidth_state": row.get("linewidth_state"),
        "pool_consistent": row.get("pool_consistent"),
        "consistency_pvalue": row.get("consistency_pvalue"),
        "consistency_deviance": row.get("consistency_deviance"),
        "consistency_alpha": row.get("consistency_alpha"),
        "spectrum_ref": row.get("spectrum_ref"),
        "predictive_ref": row.get("predictive_ref"),
        "content_hash": row.get("content_hash"),
        "master_revision": row.get("master_revision"),
        "fit_kind": row.get("fit_kind"),
        # Driver-inserted probes inform branch classification but never occupy a requested grid point
        # or contribute to its production/certification counters.  Persisted run-event provenance
        # restores these flags after a crash rather than relying on an hkl-nearness inference.
        "driver_inserted": bool(driver_inserted),
        "certification_eligible": bool(certification_eligible),
    }


def _master_obs_from_row(row: dict, calibration: Calibration) -> dict:
    """A pooled fit → its sole decision-bearing, model-robust master assessment.

    ``target_delivery_eligible=False`` withholds every delivery this row could supply. Clearing the
    scalar ``omega0`` alone is not enough: a model-robust row takes its ω₀ from
    ``model_interval_med``, so it would keep certifying from the model-averaged predictive interval
    without ever reading the scalar. The row remains present, fit and reported — it is a real
    measurement — it simply cannot stand as the target's delivered number.
    """
    exposure_key, effective_exposure, exposure_source = _effective_exposure_bucket(
        row, PRODUCTION_NCOUNT)
    bucket, calibration_arg = _calibration_bucket(calibration, exposure_key)
    deliverable = row.get("target_delivery_eligible") is not False
    model_lo, model_med, model_hi = (row.get("model_interval_lo"),
                                         row.get("model_interval_med"),
                                         row.get("model_interval_hi"))
    model_robust = bool(deliverable and row.get("model_robust") is True and model_lo is not None
                        and model_hi is not None)
    hw = None
    if model_robust:
        try:
            hw = calibration.calibrated_halfwidth(model_lo, model_hi, bucket=calibration_arg)
        except Exception:  # noqa: BLE001 — an invalid robust interval cannot certify
            model_robust = False
    diagnostic_hw, diagnostic_method = _diagnostic_halfwidth(row, calibration, calibration_arg)
    if model_robust:
        diagnostic_hw, diagnostic_method = hw, "model_averaged_predictive"
    disp = row.get("disposition")
    return {
        "hkl": _row_hkl(row),
        "omega0": ((model_med if model_robust and model_med is not None else row.get("omega0"))
                   if deliverable else None),
        "halfwidth": hw,
        "reliable": model_robust, "certifiable": model_robust,
        "model_robust": model_robust, "model_entropy": row.get("model_entropy"),
        "model_interval": ([model_lo, model_med, model_hi] if model_robust else None),
        # ``usable`` is what model_points and the signal-seen check read, so a withheld delivery has
        # to clear it too or the ω(q) model would still be fitted through the auxiliary energy.
        "usable": (deliverable and disp in _USABLE and row.get("omega0") is not None),
        "target_delivery_eligible": deliverable,
        "disposition": disp, "n_members": row.get("n_members"),
        "effective_exposure": effective_exposure, "exposure_source": exposure_source,
        "exposure_bucket": bucket,
        "diagnostic_halfwidth": diagnostic_hw, "interval_method": diagnostic_method,
        "calibration_version": _calibration_version(calibration),
        "branch_id": row.get("branch_id"), "branch_posterior": row.get("branch_posterior"),
        "assignment_entropy": row.get("assignment_entropy"),
        "target_branch": row.get("target_branch"),
        "candidate_id": row.get("candidate_id"), "candidate_state": row.get("candidate_state"),
        "branch_candidates": row.get("branch_candidates"),
        "selected_candidate_index": row.get("selected_candidate_index"),
        "track_state": row.get("track_state"), "track_revision": row.get("track_revision"),
        "target_probability": row.get("target_probability"),
        "identity_entropy": row.get("identity_entropy"),
        "local_identity_clear": row.get("local_identity_clear"),
        "modality": row.get("modality"),
        "gamma_at_bound": bool(row.get("gamma_at_bound", False)),
        "linewidth_state": row.get("linewidth_state"),
        "pool_consistent": row.get("pool_consistent"),
        "consistency_statistic": row.get("consistency_statistic"),
        "consistency_pvalue": row.get("consistency_pvalue"),
        "consistency_alpha": row.get("consistency_alpha"),
        "spectrum_ref": row.get("spectrum_ref"),
        "predictive_ref": row.get("predictive_ref"),
        "target_projection_ref": row.get("target_projection_ref"),
        "fit_selection_semantics": row.get("fit_selection_semantics"),
        "shadow_model_selection": row.get("shadow_model_selection"),
        "master_session_key": row.get("scan_id"),
        "detector_id": row.get("detector"),
        "acquisition_id": row.get("acquisition_id"),
        "content_hash": row.get("content_hash"),
        "master_revision": row.get("master_revision"),
        "fit_kind": row.get("fit_kind"),
        # Chain of custody: the member scan labels behind this pooled value (readers coerce the CSV
        # cell to a list). members_field_present distinguishes "old results.csv lacked the column"
        # (guard skipped) from "column present but empty" (guard fires).
        "members": row.get("members"), "members_field_present": ("members" in row),
    }


def _candidate_evidence_state(observations: list[dict], update: dict) -> tuple[str, dict]:
    """Apply the two-acquisition + neighboring-q Bayesian rejection rule."""
    candidate_id = update.get("candidate_id")
    evidence = [item for item in observations
                if item.get("kind") == "pooled_master_update"
                and item.get("candidate_id") == candidate_id]
    evidence.append(update)
    acquisitions = {item.get("acquisition_id") for item in evidence
                    if item.get("acquisition_id") is not None}
    positions = {tuple(round(float(value), 8) for value in item.get("hkl", ()))
                 for item in evidence if item.get("hkl") is not None}
    posterior = update.get("branch_posterior")
    target_branch = update.get("target_branch")
    state = str(update.get("candidate_state") or "provisional")
    p_target = competitor = None
    if isinstance(posterior, dict) and target_branch:
        p_target = float(posterior.get(target_branch, 0.0))
        competitor = max(
            (float(value) for branch, value in posterior.items()
             if branch != target_branch),
            default=0.0,
        )
        if (state == "provisional" and p_target <= 0.05 and competitor >= 0.6
                and len(acquisitions) >= 2 and len(positions) >= 2):
            state = "rejected"
    diagnostics = {"n_logical_acquisitions": len(acquisitions),
                   "n_q_positions": len(positions), "p_target": p_target,
                   "p_competitor": competitor}
    return state, diagnostics


def _target_selection_rank(row: dict) -> int | None:
    """Which of the modes in play this row selects as the target, by energy order.

    A *rank* rather than the candidate id or index: ``candidate_ref`` is re-minted when a master is
    re-pooled and the stored index follows the table's order, so either would report a changed
    selection on an unchanged one. Rank is stable under refit jitter and re-pooling, and changes
    exactly when the target moves to a different mode.
    """
    index = row.get("selected_candidate_index")
    candidates = row.get("branch_candidates")
    if index is None or not candidates:
        return None
    try:
        selected = candidates[int(index)]
        energies = sorted(float(c.get("energy_meV", c.get("omega0"))) for c in candidates)
        return energies.index(float(selected.get("energy_meV", selected.get("omega0"))))
    except (TypeError, ValueError, IndexError, AttributeError):
        return None


def _identity_epoch(row: dict, observations: list[dict]) -> int:
    """A counter that advances whenever the target selection moves to a different mode.

    Saturation compares identity metrics across revisions that answer the same question. A selection
    flip is a changed *answer*, not a changed candidate set, and it must still reset that window —
    but as its own named thing, so "the candidate basis changed" keeps meaning only what it says.
    Monotone by construction, so equal values always mean "same selection era".
    """
    previous = None
    for update in reversed(observations):
        if (update.get("kind") == "pooled_master_update"
                and _hkl_close(update.get("hkl"), row.get("hkl"))
                and update.get("identity_epoch") is not None):
            previous = update
            break
    if previous is None:
        return 0
    epoch = int(previous["identity_epoch"])
    if _target_selection_rank(previous) == _target_selection_rank(row):
        return epoch
    return epoch + 1


def _record_pooled_master_update(workspace, calibration: Calibration, proposal, *,
                                 target_branch: str, log_path,
                                 observations: list[dict]) -> dict | None:
    """Persist the current pooled master for the submitted q as chronological replay evidence."""
    matching = [row for row in _read_master_rows(workspace, calibration)
                if _hkl_close(row.get("hkl"), (proposal.h, proposal.k, proposal.l))]
    if not matching:
        return None
    # Prefer confirmed target delivery when present. Otherwise keep the strongest pooled fit as
    # evidence: wrong-branch/ambiguous spectra still prove q was measured and carry candidates.
    master = max(
        matching,
        key=lambda row: (
            row.get("branch_id") == target_branch and row.get("omega0") is not None,
            int(row.get("n_members") or 0),
        ),
    )
    payload = {**master, "kind": "pooled_master_update",
               "acquisition_id": proposal.acquisition_id,
               "acquisition_target_energy": float(proposal.e_pred),
               "acquisition_action_role": (proposal.meta or {}).get(
                   "acquisition_action_role"),
               "target_branch_role": (proposal.meta or {}).get("target_branch_role"),
               "rival_branch_role": (proposal.meta or {}).get("rival_branch_role"),
               "proposal_fingerprint": (proposal.meta or {}).get("proposal_fingerprint"),
               # The identity-comparability context (stopping._identity_context). Declared in the
               # saturation basis from the start but never written by any producer, so those clauses
               # guaranteed nothing: they travel from the action's own intent, via the proposal meta.
               "hypotheses_fingerprint": (proposal.meta or {}).get("hypotheses_fingerprint"),
               "campaign_slice_fingerprint": (proposal.meta or {}).get(
                   "campaign_slice_fingerprint")}
    payload["identity_epoch"] = _identity_epoch(payload, observations)
    payload["candidate_state"], payload["candidate_evidence"] = _candidate_evidence_state(
        observations, payload)
    observations.append(payload)
    log_event(log_path, "pooled_master_update", payload)
    if payload.get("branch_id") != target_branch or payload.get("omega0") is None:
        log_event(log_path, "evidence_only_master", {
            "hkl": payload.get("hkl"), "target_branch": target_branch,
            "branch_id": payload.get("branch_id"),
            "branch_posterior": payload.get("branch_posterior"),
            "assignment_entropy": payload.get("assignment_entropy"),
            "candidate_state": payload.get("candidate_state"),
            "branch_candidates": payload.get("branch_candidates"),
            "acquisition_id": proposal.acquisition_id,
        })
    if payload["candidate_state"] == "rejected":
        log_event(log_path, "candidate_update", {
            "candidate_id": payload.get("candidate_id"), "state": "rejected",
            "target_branch": payload.get("target_branch"), "hkl": payload.get("hkl"),
            "branch_posterior": payload.get("branch_posterior"),
            "assignment_entropy": payload.get("assignment_entropy"),
            "evidence": payload.get("candidate_evidence")})
    return payload


def _exact_pooled_master_sources(workspace) -> dict[tuple[str, str, str], dict[str, str]]:
    """Join pooled revisions to their originating physical request by durable identities only.

    ``results.csv`` deliberately identifies a pooled scientific result with ``pooled:<hash>``.  That
    is not the acquisition whose executed scan-design inventory supplied the data.  The session log
    records the missing join on ``pooled_master_update``: exact master key/detector/revision plus the
    physical acquisition and submitted request fingerprint.  Ambiguous, incomplete, or orphaned
    joins are omitted so callers fail closed instead of substituting a same-Q or newer master.
    """
    events = read_log(Path(workspace) / "report" / "session_log.jsonl")
    submitted: set[tuple[str, str]] = set()
    for event in events:
        if event.get("kind") != "physical_submission":
            continue
        payload = event.get("payload") or {}
        acquisition_id = payload.get("acquisition_id")
        request_fingerprint = payload.get("proposal_fingerprint")
        if acquisition_id is not None and request_fingerprint is not None:
            submitted.add((str(acquisition_id), str(request_fingerprint)))

    candidates: dict[tuple[str, str, str], set[tuple[str, str]]] = {}
    for event in events:
        if event.get("kind") != "pooled_master_update":
            continue
        payload = event.get("payload") or {}
        identity = (
            payload.get("master_session_key"),
            payload.get("detector_id"),
            payload.get("master_revision"),
        )
        source = (payload.get("acquisition_id"), payload.get("proposal_fingerprint"))
        if any(value is None for value in identity) or any(value is None for value in source):
            continue
        exact_identity = tuple(str(value) for value in identity)
        exact_source = tuple(str(value) for value in source)
        if exact_source in submitted:
            candidates.setdefault(exact_identity, set()).add(exact_source)

    result: dict[tuple[str, str, str], dict[str, str]] = {}
    for identity, sources in candidates.items():
        if len(sources) != 1:
            continue
        acquisition_id, request_fingerprint = next(iter(sources))
        result[identity] = {
            "source_acquisition_id": acquisition_id,
            "source_submission_request_fingerprint": request_fingerprint,
        }
    return result


def _read_master_rows(workspace, calibration: Calibration) -> list[dict]:
    """All current master (pooled=True) rows in results.csv → convergence observations. Read FRESH
    every iteration (not consume-once): a master's interval shrinks as scans accumulate into it."""
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return []
    source_index = _exact_pooled_master_sources(workspace)
    out = []
    for r in rows:
        if r.get("pooled") and _row_hkl(r) is not None:
            master = _master_obs_from_row(r, calibration)
            session_key = r.get("scan_id")
            detector_id = r.get("detector")
            if session_key is not None:
                master["master_session_key"] = str(session_key)
            if detector_id is not None:
                master["detector_id"] = str(detector_id)
            revision = master.get("master_revision")
            if session_key is not None and detector_id is not None and revision is not None:
                exact_source = source_index.get(
                    (str(session_key), str(detector_id), str(revision)))
                if exact_source is not None:
                    master.update(exact_source)
            ref = r.get("predictive_ref")
            if ref:
                candidate = (Path(workspace) / str(ref)).resolve()
                root = (Path(workspace) / "data" / "master_predictive").resolve()
                try:
                    candidate.relative_to(root)
                    predictive = json.loads(candidate.read_text(encoding="utf-8"))
                    # ADR-020: the SAME strict validator the workspace facade and the arbitrary-grid
                    # evaluator run — a schema check alone let a malformed or stale-fingerprinted
                    # basis reach the loop that the other two readers would have refused.
                    validate_master_predictive_artifact(predictive)
                    master["predictive"] = predictive
                    master["expected_next_information"] = predictive.get("diagnostics")
                except PredictiveArtifactError as error:
                    # An unusable predictive artifact is no predictive basis at all (ADR-006): the
                    # master keeps every other property and the loop takes its ordinary
                    # no-predictive path, refit or legacy, never a campaign abort.
                    #
                    # The declared schema is read defensively because the refusal being handled is
                    # exactly the one that fires when the payload is not an object at all: a JSON
                    # array, string, number or ``null`` validates to ``artifact_schema_unsupported``,
                    # and ``predictive.get`` on it raised an AttributeError that escaped this whole
                    # function — turning a degrade-and-continue into a campaign abort.
                    log_event(Path(workspace) / "report" / "session_log.jsonl", "exception", {
                        "action": "predictive_artifact_unsupported", "reference": str(ref),
                        "reason": error.reason_code, "message": error.message,
                        "schema": (predictive.get("schema")
                                   if isinstance(predictive, Mapping) else None)})
                except (ValueError, OSError, json.JSONDecodeError) as error:
                    log_event(Path(workspace) / "report" / "session_log.jsonl", "exception", {
                        "action": "predictive_artifact_unavailable", "reference": str(ref),
                        "error": str(error)})
            out.append(master)
    return out


def _grid_state(objective, observations: list[dict], workspace, calibration: Calibration):
    """The ``(master_rows, grid)`` pair the loop rebuilds at three points (stop eval, model update,
    final re-eval). Reads the pooled master rows FRESH (their intervals shrink as scans accumulate),
    then folds observations + masters into the per-q grid. The three call sites were verified to be
    identical calls; the caller wraps this in whatever ``timed`` span (or none) belongs at that site."""
    master_rows = _read_master_rows(workspace, calibration)
    grid = summarize_grid(
        objective, observations, master_rows, branch_track=_read_branch_tracks(Path(workspace)))
    return master_rows, grid


#: Draw bases whose particles are a simulation-grade resample of the fitted likelihood/posterior —
#: one replicate (or one resampled nested-sampling point) per equal-weight particle. ``laplace_hesse``
#: is deliberately absent: its "particles" are i.i.d. Gaussian draws about the HESSE covariance, a
#: quadratic stand-in that carries no information the covariance did not already contain, so an EIG
#: design measure built on it re-scores its own approximation rather than the evidence.
#: (isar/session/predictive_artifact.py: ``bounded_parameter_draws`` / ``DRAW_WEIGHTING``.)
EIG_SIMULATION_GRADE_DRAW_BASES = frozenset({"parametric_bootstrap", "nested_sampling"})


def _predictive_draw_bases(predictive) -> list[str]:
    """The distinct draw bases a published predictive artifact was built from."""
    if not isinstance(predictive, Mapping):
        return []
    provenance = predictive.get("uncertainty_provenance")
    declared = provenance.get("draw_basis") if isinstance(provenance, Mapping) else None
    if isinstance(declared, str):
        declared = [declared]
    if not isinstance(declared, (list, tuple)):
        # Pre-provenance payloads: fall back to the per-model declaration the artifact must carry.
        models = predictive.get("models")
        declared = [model.get("draw_basis") for model in models
                    if isinstance(model, Mapping)] if isinstance(models, list) else []
    return sorted({str(value) for value in declared if value})


def _predictive_design_measure_ready(master) -> bool:
    """Whether this master's predictive artifact can serve as the EIG design measure.

    "Has a predictive artifact" and "is ready to be designed against" are different questions, and
    conflating them is what previously suppressed the promotion EIG needs: a cheap Laplace/HESSE
    ensemble is a perfectly valid artifact — the workspace facade publishes it, the loop scores
    expected counts from it — while being exactly the basis the design measure refuses. Readiness
    therefore mirrors what the design measure itself requires of the particle basis:

    - a validated artifact is present (the loader already refused an incomplete one — ADR-014's
      ``predictive_complete_for_eig`` — and it is re-checked here defensively);
    - its particle basis is **homogeneous**: one draw basis across every retained model, so the
      particles are exchangeable and can be pooled into a single design measure;
    - and that basis is **simulation-grade** (:data:`EIG_SIMULATION_GRADE_DRAW_BASES`); a
      ``laplace_hesse`` ensemble is rejected exactly as a non-bootstrap particle set is.
    """
    predictive = (master or {}).get("predictive")
    if not isinstance(predictive, Mapping):
        return False
    completeness = predictive.get("completeness")
    if isinstance(completeness, Mapping) \
            and completeness.get("predictive_complete_for_eig") is not True:
        return False
    bases = _predictive_draw_bases(predictive)
    return len(bases) == 1 and bases[0] in EIG_SIMULATION_GRADE_DRAW_BASES


def _master_revision_key(master) -> str:
    """The identity of the exact published master revision a promotion is charged against."""
    return ":".join(str(value or "unknown") for value in (
        (master or {}).get("content_hash") or (master or {}).get("production_batch_fingerprint"),
        (master or {}).get("master_revision") or (master or {}).get("track_revision")))


def _promoted_master_revisions(log_path) -> set[str]:
    """Master revisions a full-tier predictive promotion has already been spent on.

    Read from the durable log, so a resume never re-charges a promotion the previous invocation
    already paid for, and a *failed* attempt stays visible without trapping the campaign in an
    analysis-only retry loop.
    """
    return {
        str((record.get("payload") or {}).get("revision_key"))
        for record in read_log(log_path)
        if record.get("kind") == "escalate_fit"
        and (record.get("payload") or {}).get("reason") == "master_predictive_basis_missing"
        and (record.get("payload") or {}).get("phase") == "outcome"
    }


def _predictive_promotion_target(proposal, grid, log_path):
    """The one master a full-tier predictive promotion is owed, or ``None``.

    Scoped to the source master of the *selected* proposal rather than swept across the grid: a
    promotion costs a charged full-tier reanalysis, and the only master whose design measure the
    campaign is about to consume is the one the planned canonical production/confirmation request
    scores against. A global sweep spent that analysis on q points nothing had asked about.

    Convergence is deliberately not a disqualifier. Whether a q has stopped needing counts and
    whether its predictive basis can carry an EIG design measure are different questions; a
    converged master still supplies the basis for the requests that follow it.
    """
    if proposal is None or str(proposal.kind) not in ("production", "confirm"):
        return None
    intent = getattr(proposal, "intent", None)
    sink = getattr(intent, "evidence_sink", CANONICAL_MASTER) if intent is not None \
        else CANONICAL_MASTER
    if sink != CANONICAL_MASTER:
        return None
    hkl = (float(proposal.h), float(proposal.k), float(proposal.l))
    for item in grid:
        if not _hkl_close(tuple(item.hkl), hkl):
            continue
        master = item.master or {}
        if not master.get("usable") or _predictive_design_measure_ready(master):
            return None
        revision_key = _master_revision_key(master)
        if revision_key in _promoted_master_revisions(log_path):
            return None
        return item, revision_key
    return None


def _promote_master_predictive_basis(cfg, objective, item, revision_key: str, *, it: int):
    """Run one charged full-tier reanalysis so ``item``'s master publishes an EIG-grade basis.

    Returns ``(outcome, error)`` where outcome is ``"completed"``, ``"failed"``, or
    ``"analysis_limit"``. Both terminal outcomes are recorded as paired ``escalate_fit``
    promote/outcome events, which is also what stops the same master revision being promoted twice
    (see :func:`_promoted_master_revisions`).
    """
    full_tier = _full_tier(cfg)
    log_event(cfg.log_path, "escalate_fit", {
        "phase": "promote", "iter": it, "hkl": list(item.hkl),
        "from_tier": (item.master or {}).get("fit_kind"),
        "to_tier": full_tier, "reason": "master_predictive_basis_missing",
        "revision_key": revision_key})
    try:
        with activity(cfg.log_path, "full_fit", q=list(item.hkl),
                      fit_tier=full_tier, iteration=it):
            with analysis_operation(
                    cfg.log_path, f"predictive-escalation:{revision_key}",
                    "master_predictive_escalation",
                    limit_seconds=objective.analysis_limit_seconds,
                    iteration=it, q=list(item.hkl)):
                run_result = cfg.runner.run(
                    cfg.workspace, [cfg.data_dir], errors_tier=full_tier,
                    scan_errors_tier="fast", master_errors_tier=full_tier,
                    force=True, render=False,
                    **_analysis_runner_kwargs(cfg, objective))
                _require_complete_analysis(run_result, objective)
    except AnalysisLimitExceeded as error:
        # The analysis clock is a campaign terminal, not a promotion failure: it is NOT recorded as
        # an outcome, so the revision stays promotable after a resume that grants more analysis time.
        _LOG.warning("predictive promotion for %s stopped on the analysis limit: %s",
                     revision_key, error)
        return "analysis_limit", error
    except Exception as error:  # noqa: BLE001 — visible failure, then measure again
        log_event(cfg.log_path, "escalate_fit", {
            "phase": "outcome", "iter": it, "hkl": list(item.hkl),
            "to_tier": full_tier, "reason": "master_predictive_basis_missing",
            "revision_key": revision_key, "outcome": "failed", "error": str(error)})
        _LOG.warning("predictive promotion for %s failed: %s: %s",
                     revision_key, type(error).__name__, error)
        return "failed", error
    log_event(cfg.log_path, "escalate_fit", {
        "phase": "outcome", "iter": it, "hkl": list(item.hkl),
        "to_tier": full_tier, "reason": "master_predictive_basis_missing",
        "revision_key": revision_key, "outcome": "completed"})
    return "completed", None


def _record_eligible_crossings(grid, observations: list[dict], log_path=None) -> int:
    """Record each initial/re-crossing chronologically; unchanged pools never duplicate."""
    def eligible(update, tolerance) -> bool:
        halfwidth = update.get("halfwidth")
        certifiable = update.get("certifiable", update.get("reliable", False))
        try:
            return bool(update.get("usable") and certifiable and halfwidth is not None
                        and FIRST_CROSSING_MARGIN * float(halfwidth) <= float(tolerance))
        except (TypeError, ValueError):
            return False

    added = 0
    for g in grid:
        if not g.requires_crossing_guard:
            continue
        master = g.master or {}
        fingerprint = master.get("production_batch_fingerprint")
        halfwidth = g._master_hw
        if (not fingerprint or halfwidth is None
                or FIRST_CROSSING_MARGIN * halfwidth > g.tolerance):
            continue
        previous = g.master_updates[-2] if len(g.master_updates) >= 2 else None
        if previous is not None and eligible(previous, g.tolerance):
            continue
        if previous is None and g.crossing_fingerprints \
                and g.crossing_fingerprints[-1] == fingerprint:
            continue
        marker = {"hkl": g.hkl, "kind": "certification_crossing",
                  "production_batch_fingerprint": fingerprint,
                  "calibrated_halfwidth": halfwidth,
                  "n_members": master.get("n_members")}
        observations.append(marker)
        if log_path is not None:
            log_event(log_path, "certification_crossing", marker)
        added += 1
    return added


def _hkl_close(a, b, tol: float = 1e-4) -> bool:
    return a is not None and b is not None and all(abs(x - y) <= tol for x, y in zip(a, b))


def _q_needs_full_errors(workspace, hkl) -> bool:
    """Whether this production q needs the full (bootstrap+profile) tier rather than fast (HESSE).

    True when the q already has a usable fit whose only error is an UNtrusted HESSE interval and no
    trustworthy interval yet — the Γ-at-bound / low-count corner where the fast HESSE σ cannot be
    trusted and only the exact profile interval certifies. False when the q has a trusted HESSE (or an
    existing reliable bootstrap/profile), so it stays on the cheap fast tier."""
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return False
    saw_untrusted = False
    for r in rows:
        if not _hkl_close(_row_hkl(r), hkl):
            continue
        if not r.get("pooled"):
            continue
        if r.get("disposition") not in _USABLE or r.get("omega0") is None:
            continue
        if _pick_interval(r)[2]:
            return False                              # already have a trustworthy interval → fast is fine
        if r.get("hesse_lo") is not None and not r.get("hesse_trusted"):
            saw_untrusted = True
    return saw_untrusted


def _full_tier(cfg) -> str:
    """The tier standing in for "full" (bootstrap+profile) errors: ``bayes`` (superset of ``full`` —
    adds MINOS + posterior as CSV diagnostics, bootstrap stays the authoritative interval) when the
    campaign opted in via ``cfg.bayes_errors``, else ``full`` itself. ``cfg`` may be a
    :class:`CampaignConfig` or anything exposing a ``bayes_errors`` attribute (falls back to ``full``
    when absent, e.g. ``None``)."""
    return "bayes" if getattr(cfg, "bayes_errors", False) else "full"


def _errors_tier_for(proposal, workspace, cfg=None) -> str:
    """The ``isar run --errors`` tier for this proposal (LLM_HARNESS_DESIGN §8 escalation ladder):

    - quicklook → ``cheap`` (HESSE only, no interval — steers the ω(q) model);
    - production → ``fast`` (HESSE interval + trust flag, no bootstrap) by default, escalating to
      ``full`` (bootstrap+profile) — or ``bayes`` when ``cfg.bayes_errors`` is set, see
      :func:`_full_tier` — once the q shows an untrusted-HESSE fit that needs the reliable interval.
      The bootstrap thus fires only where the fast HESSE approximation cannot be trusted.

    Returns the bare tier string (``cheap`` | ``fast`` | ``full`` | ``bayes``) — the runner seam takes
    it as a structured ``errors_tier`` kwarg.
    """
    if proposal.kind == "quicklook":
        return "cheap"
    if _q_needs_full_errors(workspace, (proposal.h, proposal.k, proposal.l)):
        return _full_tier(cfg)
    return "fast"


def _errors_tiers_for(proposal, workspace, cfg=None) -> tuple[str, str]:
    """Return ``(scan, master)`` tiers for one drive analysis.

    Physical production scans remain provisional HESSE fits even when the authoritative pooled
    master at this q needs promotion. Omitting role tiers at lower layers preserves the historical
    single-tier contract for non-drive callers.
    """
    master_tier = _errors_tier_for(proposal, workspace, cfg)
    if proposal.kind == "quicklook":
        return "cheap", "cheap"
    return "fast", master_tier


def _errors_arg_for(proposal, workspace, cfg=None) -> list[str]:
    """Back-compat wrapper: the tier from :func:`_errors_tier_for` as ``["--errors", <tier>]`` argv."""
    return ["--errors", _errors_tier_for(proposal, workspace, cfg)]


def _unresolved_current_promotions(workspace) -> list[dict]:
    """Reconcile promotion manifests against the frozen results contract at a loop boundary."""
    from isar.promotion_contract import PromotionStore

    store = PromotionStore(workspace)
    if not store.root.exists():
        return []
    revisions = {}
    try:
        rows = read_results_csv(workspace)
    except (OSError, UnicodeError, ValueError) as error:
        _LOG.warning("could not read current master revisions for promotion gate: %s: %s",
                     type(error).__name__, error)
        return [record for record in store.records()
                if record.get("status") in ("pending", "completed")]
    for row in rows:
        if not row.get("pooled"):
            continue
        master_id = row.get("scan_id")
        revision = row.get("master_revision")
        if master_id and revision:
            revisions[str(master_id)] = str(revision)
    try:
        return store.reconcile(revisions)
    except RuntimeError as error:
        _LOG.warning("promotion reconciliation failed closed: %s", error)
        return [{"promotion_id": "unreadable-promotion-record", "status": "pending"}]


def _recertified_observations(workspace, calibration: Calibration, scan_kind: dict) -> list[dict]:
    """Rebuild the per-scan observations from the (now bootstrap-certified) results.csv so the campaign
    summary reports certified intervals. Uses the recorded ``(kind, ncount)`` per (scan, detector);
    pooled/master rows are excluded (read separately via :func:`_read_master_rows`)."""
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return []
    out = []
    for r in rows:
        if r.get("pooled") or _row_hkl(r) is None:
            continue
        submitted = scan_kind.get((r.get("scan"), r.get("detector")),
                                  ("production", PRODUCTION_NCOUNT, None))
        kind, ncount = submitted[:2]
        acquisition_id = submitted[2] if len(submitted) > 2 else None
        out.append(_observation_from_row(
            r, kind, ncount, calibration, acquisition_id=acquisition_id))
    return out


def _proposal_from_submission(payload: dict) -> ScanProposal:
    """Rebuild the analysis-facing proposal fields from a durable physical submission."""
    hkl = payload.get("hkl") or (0.0, 0.0, 0.0)
    meta = {
        "driver_inserted": bool(payload.get("driver_inserted", False)),
        "certification_eligible": bool(payload.get("certification_eligible", True)),
        "count_diagnostics": dict(payload.get("count_diagnostics") or {}),
        "acquisition_action_role": payload.get("acquisition_action_role"),
        "target_branch_role": payload.get("target_branch_role"),
        "rival_branch_role": payload.get("rival_branch_role"),
        "authoritative_evidence_revision": payload.get("authoritative_evidence_revision"),
        "proposal_fingerprint": payload.get("proposal_fingerprint"),
        "scan_design_inventory_ref": payload.get("scan_design_inventory_ref"),
        "scan_design_candidate_id": payload.get("scan_design_candidate_id"),
        "scan_design_context_fingerprint": payload.get("scan_design_context_fingerprint"),
        "scan_design_selection_basis": payload.get("scan_design_selection_basis"),
        "scan_design_shadow_status": payload.get("scan_design_shadow_status"),
        "scan_design_shadow_fingerprint": payload.get("scan_design_shadow_fingerprint"),
        "scan_design_shadow_variant_fingerprints": payload.get(
            "scan_design_shadow_variant_fingerprints"),
        "scan_design_realized_comparison_ref": payload.get(
            "scan_design_realized_comparison_ref"),
        "scan_design_realized_comparison_fingerprint": payload.get(
            "scan_design_realized_comparison_fingerprint"),
        "scan_design_realized_variant_fingerprints": payload.get(
            "scan_design_realized_variant_fingerprints"),
        "scan_design_realized_selected_request_fingerprint": payload.get(
            "scan_design_realized_selected_request_fingerprint"),
        "scan_design_realized_selected_grid_fingerprint": payload.get(
            "scan_design_realized_selected_grid_fingerprint"),
        "scan_design_eig_applicability": payload.get("scan_design_eig_applicability"),
        "scan_design_eig_invalidation_reason": payload.get(
            "scan_design_eig_invalidation_reason"),
        # The action identity a resumed scan must not lose. Without the sink a recovered auxiliary
        # probe becomes canonical by default and pools into the certified master; the scientific and
        # feasibility fingerprints are its audit identity. All three are already persisted on the
        # physical_submission event, so recovery only has to read them back.
        "evidence_sink": payload.get("evidence_sink") or CANONICAL_MASTER,
        "scientific_action_fingerprint": payload.get("scientific_action_fingerprint"),
        "feasibility_manifest_fingerprint": payload.get("feasibility_manifest_fingerprint"),
    }
    for field in _AUTHORITY_SUBMISSION_FIELDS:
        meta[field] = copy.deepcopy(payload.get(field))
    if payload.get("insertion_purpose") is not None:
        meta["insertion_purpose"] = payload.get("insertion_purpose")
    return ScanProposal(
        h=float(hkl[0]), k=float(hkl[1]), l=float(hkl[2]),
        e_lo=float(payload.get("search_energy_lo") or 0.0),
        e_hi=float(payload.get("search_energy_hi") or 0.0),
        e_step=float(payload.get("search_energy_step") or 1.0),
        ncount=int(payload.get("submitted_ncount") or payload.get("ncount")
                   or PRODUCTION_NCOUNT),
        kind=str(payload.get("kind") or "production"),
        reason="recovered physical submission",
        e_pred=float(payload.get("acquisition_target_energy") or 0.0),
        n_points=int(payload.get("n_points") or payload.get("requested_points") or 0),
        acquisition_id=(str(payload["acquisition_id"])
                        if payload.get("acquisition_id") is not None else None),
        meta=meta,
    )


def _resume_detailed_events(workspace, events: list[dict], calibration: Calibration) -> dict:
    """Replay additive physical-submission/master-update events in chronological order."""
    def hkl_key(hkl) -> tuple:
        return tuple(round(float(value), 8) for value in hkl)

    physical_job_ids = {
        str((event.get("payload") or {}).get("job_id"))
        for event in events if event.get("kind") == "physical_submission"
        and (event.get("payload") or {}).get("job_id") is not None
    }
    legacy_clamps = {}
    for event in events:
        clamp = event.get("payload") or {}
        if (event.get("kind") == "exception"
                and clamp.get("action") == "proposal_ncount_clamped_to_limit"
                and clamp.get("hkl") is not None):
            legacy_clamps[(clamp.get("iter"), hkl_key(clamp["hkl"]))] = clamp
    submissions = []
    by_job: dict[str, dict] = {}
    for order, event in enumerate(events):
        kind = event.get("kind")
        payload = dict(event.get("payload") or {})
        is_detailed = kind == "physical_submission"
        is_legacy_prefix = (kind == "run" and
                            (payload.get("job_id") is None
                             or str(payload.get("job_id")) not in physical_job_ids))
        if not (is_detailed or is_legacy_prefix):
            continue
        clamp = (legacy_clamps.get((payload.get("iter"), hkl_key(payload["hkl"])))
                 if payload.get("hkl") is not None else None)
        if clamp is not None and not payload.get("count_diagnostics"):
            payload["count_diagnostics"] = {
                "requested_before_runtime_limit": clamp.get("requested_ncount"),
                "runtime_ncount_limit": clamp.get("effective_ncount"),
                "limit_clamp_logged": True,
            }
        record = {"order": order, "payload": payload, "detailed": is_detailed,
                  "cost": float(payload.get("cost") or 0.0), "seconds_delta": 0.0,
                  "reconciled": False, "has_result_rows": False}
        submissions.append(record)
        if payload.get("job_id") is not None:
            by_job[str(payload["job_id"])] = record
    for event in events:
        payload = event.get("payload") or {}
        if (event.get("kind") != "exception"
                or payload.get("action") != "feasibility_reconciled"):
            continue
        record = by_job.get(str(payload.get("job_id")))
        if record is not None:
            record["cost"] += float(payload.get("neutron_delta") or 0.0)
            record["seconds_delta"] += float(payload.get("seconds_delta") or 0.0)
            record["reconciled"] = True

    analyzed_by_job: dict[str, tuple[int, dict]] = {}
    terminal_by_job: dict[str, tuple[int, dict]] = {}
    for order, event in enumerate(events):
        payload = dict(event.get("payload") or {})
        job_id = payload.get("job_id")
        if job_id is None:
            continue
        if event.get("kind") == "analyze":
            analyzed_by_job[str(job_id)] = (order, payload)
        elif (event.get("kind") == "exception"
              and payload.get("action") == "scan_not_done"):
            terminal_by_job[str(job_id)] = (order, payload)

    neutrons = sum(record["cost"] for record in submissions)
    seconds = 0.0
    submissions_by_hkl: dict[tuple, list[dict]] = {}
    for record in submissions:
        payload = record["payload"]
        eta = payload.get("eta")
        if isinstance(eta, dict) and eta.get("estimated_seconds") is not None:
            seconds += float(eta["estimated_seconds"])
        seconds += record["seconds_delta"]
        if payload.get("hkl") is not None:
            submissions_by_hkl.setdefault(hkl_key(payload["hkl"]), []).append(record)

    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        rows = []
    result_rows = [row for row in rows if not row.get("pooled") and _row_hkl(row) is not None]
    scan_groups: dict[tuple, list[dict]] = {}
    scan_group_order: list[tuple] = []
    for row in result_rows:
        key = (hkl_key(_row_hkl(row)), str(row.get("scan")))
        if key not in scan_groups:
            scan_groups[key] = []
            scan_group_order.append(key)
        scan_groups[key].append(row)
    groups_by_hkl: dict[tuple, list[tuple]] = {}
    for key in scan_group_order:
        groups_by_hkl.setdefault(key[0], []).append(key)

    ordered_observations: list[tuple[float, int, dict]] = []
    consumed: set = set()
    scan_kind: dict = {}
    sequence = 0
    for key, group_keys in groups_by_hkl.items():
        ledger = submissions_by_hkl.get(key, [])
        explicitly_analyzed = [
            record for record in ledger
            if ((analysis := analyzed_by_job.get(str(record["payload"].get("job_id"))))
                is not None and int(analysis[1].get("n_rows") or 0) > 0)]
        unexplained = [
            record for record in ledger
            if str(record["payload"].get("job_id")) not in analyzed_by_job
            and str(record["payload"].get("job_id")) not in terminal_by_job]
        remaining = max(len(group_keys) - len(explicitly_analyzed), 0)
        row_ledger = sorted(
            [*explicitly_analyzed, *unexplained[:remaining]], key=lambda record: record["order"])
        for group_index, group_key in enumerate(group_keys):
            record = row_ledger[group_index] if group_index < len(row_ledger) else None
            if record is not None:
                record["has_result_rows"] = True
            payload = record["payload"] if record is not None else {}
            kind = payload.get("kind") or "production"
            ncount = int(payload.get("submitted_ncount") or payload.get("ncount")
                         or PRODUCTION_NCOUNT)
            acquisition_id = payload.get("acquisition_id")
            provenance = {
                "driver_inserted": bool(payload.get("driver_inserted", False)),
                "certification_eligible": bool(payload.get("certification_eligible", True)),
                "count_diagnostics": payload.get("count_diagnostics") or {},
                "acquisition_action_role": payload.get("acquisition_action_role"),
                "target_branch_role": payload.get("target_branch_role"),
                "rival_branch_role": payload.get("rival_branch_role"),
                "authoritative_evidence_revision": payload.get(
                    "authoritative_evidence_revision"),
                "proposal_fingerprint": payload.get("proposal_fingerprint"),
            }
            cost = record["cost"] if record is not None else 0.0
            order = float(record["order"]) if record is not None else -1.0 + sequence * 1e-6
            for detector_index, row in enumerate(scan_groups[group_key]):
                observation = _observation_from_row(
                    row, kind, ncount, calibration,
                    proposal_cost=cost if detector_index == 0 else 0.0,
                    acquisition_id=acquisition_id, **provenance)
                ordered_observations.append((order, sequence, observation))
                sequence += 1
                scan_detector = (row.get("scan"), row.get("detector"))
                consumed.add(scan_detector)
                scan_kind[scan_detector] = (kind, ncount, acquisition_id)

    unmatched_submissions: list[dict] = []
    for record in submissions:
        if not record["detailed"] or record["has_result_rows"]:
            continue
        payload = record["payload"]
        job_key = str(payload.get("job_id"))
        analysis = analyzed_by_job.get(job_key)
        terminal = terminal_by_job.get(job_key)
        if analysis is not None and int(analysis[1].get("n_rows") or 0) == 0:
            ordered_observations.append((float(analysis[0]), sequence,
                                         _declined_observation(
                                             _proposal_from_submission(payload),
                                             "analysis_no_result")))
            sequence += 1
            continue
        if terminal is not None:
            verdict = str(terminal[1].get("state") or "scan_not_done")
            ordered_observations.append((float(terminal[0]), sequence,
                                         _declined_observation(
                                             _proposal_from_submission(payload), verdict)))
            sequence += 1
            continue
        unmatched_submissions.append({
            "order": record["order"], "payload": dict(payload),
            "reconciled": bool(record["reconciled"]),
        })

    for order, event in enumerate(events):
        if event.get("kind") == "pooled_master_update":
            marker = dict(event.get("payload") or {})
            marker["kind"] = "pooled_master_update"
            ordered_observations.append((float(order), sequence, marker))
            sequence += 1
        elif event.get("kind") == "certification_crossing":
            marker = dict(event.get("payload") or {})
            if marker.get("hkl") is not None and marker.get("production_batch_fingerprint"):
                marker["kind"] = "certification_crossing"
                ordered_observations.append((float(order), sequence, marker))
                sequence += 1
    ordered_observations.sort(key=lambda item: (item[0], item[1]))
    return {"observations": [item[2] for item in ordered_observations],
            "consumed": consumed, "scan_kind": scan_kind,
            "scan_counter": len(submissions), "neutrons": neutrons, "seconds": seconds,
            "proposal_counter": max(len(submissions),
                                    sum(1 for event in events if event.get("kind") == "propose")),
            "unmatched_submissions": unmatched_submissions}


def _resume_state(workspace, log_path, calibration: Calibration) -> dict:
    """Rehydrate the loop's in-memory state from the durable files after a crash/restart.

    Convergence needs no resume (the pooled master rows are read fresh every iteration); this recovers
    what ``run_campaign`` would otherwise reset to zero — the **spend ledger** (``scan_counter`` +
    ``budget`` neutrons/seconds, exact from the ``run`` events, so a restart neither re-spends beamtime
    nor reuses a seed / collides an idempotency key), the **folded-row set** (``consumed`` = every
    ``(scan, detector)`` already in ``results.csv``, so old rows are never re-folded as new), and
    **best-effort per-scan observations**. Each observation carries its OWN submission's kind (not an
    hkl-wide lock): a results.csv row cannot be joined back to its run event other than by ``q`` +
    order, so a q's rows are tail-aligned against its per-q kind ledger — surviving rows take their
    most-recent matching submission (so a resumed quicklook stays a quicklook and does not inflate
    ``n_production``); a q whose ledger is shorter than its rows falls back to ``"production"`` for the
    leading rows, as before. Each q's total cost is
    lumped onto one representative row so ``counts_spent`` sums to the true per-q spend — a PRODUCTION
    row when the q has one (``summarize_grid`` reads cost>0 rows to set ``last_prod_cost``), else the
    first row (quicklook-only q). A cold workspace (no log / no results.csv) returns the fresh-start
    tuple."""
    events = read_log(log_path)
    if any(event.get("kind") == "physical_submission" for event in events):
        return _resume_detailed_events(workspace, events, calibration)

    def resume_hkl_key(hkl) -> tuple:
        # Preserve enough precision to keep nearby diagnostic probes distinct while absorbing only
        # serialization-level float noise.  Four decimals merged legitimate sub-1e-4 scan points.
        return tuple(round(float(x), 8) for x in hkl)

    runs = [e.get("payload", {}) for e in events if e.get("kind") == "run"]
    scan_counter = len(runs)                        # one run event per submitted scan
    neutrons = seconds = 0.0
    cost_by_hkl: dict = {}
    kinds_by_hkl: dict = {}                         # per-q kind ledger, in submission order
    provenance_by_hkl: dict = {}                    # aligned driver-insertion/certification ledger
    ncount_by_hkl: dict = {}
    for r in runs:
        cost = float(r.get("cost")) if r.get("cost") is not None else 0.0
        neutrons += cost
        eta = r.get("eta")
        if isinstance(eta, dict) and eta.get("estimated_seconds") is not None:
            seconds += float(eta["estimated_seconds"])
        hkl = r.get("hkl")
        if hkl is None:
            continue
        key = resume_hkl_key(hkl)
        cost_by_hkl[key] = cost_by_hkl.get(key, 0.0) + cost
        kinds_by_hkl.setdefault(key, []).append(r.get("kind") or "production")
        provenance_by_hkl.setdefault(key, []).append({
            "driver_inserted": bool(r.get("driver_inserted", False)),
            "certification_eligible": bool(r.get("certification_eligible", True)),
            "acquisition_action_role": r.get("acquisition_action_role"),
            "target_branch_role": r.get("target_branch_role"),
            "rival_branch_role": r.get("rival_branch_role"),
            "authoritative_evidence_revision": r.get("authoritative_evidence_revision"),
            "proposal_fingerprint": r.get("proposal_fingerprint"),
        })
        ncount_by_hkl[key] = r.get("ncount") or ncount_by_hkl.get(key)

    reconciliations = [e.get("payload", {}) for e in events
                       if e.get("kind") == "exception"
                       and (e.get("payload") or {}).get("action") == "feasibility_reconciled"]
    for reconciliation in reconciliations:
        neutron_delta = float(reconciliation.get("neutron_delta") or 0.0)
        seconds_delta = float(reconciliation.get("seconds_delta") or 0.0)
        neutrons += neutron_delta
        seconds += seconds_delta
        hkl = reconciliation.get("hkl")
        if hkl is not None:
            key = resume_hkl_key(hkl)
            cost_by_hkl[key] = cost_by_hkl.get(key, 0.0) + neutron_delta

    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        rows = []
    result_rows = [row for row in rows if not row.get("pooled") and _row_hkl(row) is not None]
    # Attribute each result row its own submission's kind. In steady single-detector operation a q has
    # one persisted row per submission, so rows align 1:1 with its ledger; align at the TAIL so when
    # fewer rows survived than submissions (an older/unpersisted quicklook left no row), each surviving
    # row takes its most-recent matching submission (a q's rows are its latest results) rather than an
    # old quicklook. A ledger shorter than the rows defaults the leading rows to "production", as before.
    count_by_hkl: dict = {}
    for row in result_rows:
        key = resume_hkl_key(_row_hkl(row))
        count_by_hkl[key] = count_by_hkl.get(key, 0) + 1
    row_kinds: list = []
    row_provenance: list[dict] = []
    cursor: dict = {}
    for row in result_rows:
        key = resume_hkl_key(_row_hkl(row))
        ledger = kinds_by_hkl.get(key, [])
        i = cursor.get(key, 0)
        li = (len(ledger) - count_by_hkl[key]) + i     # tail-align rows against the per-q ledger
        row_kinds.append(ledger[li] if 0 <= li < len(ledger) else "production")
        provenance = provenance_by_hkl.get(key, [])
        row_provenance.append(provenance[li] if 0 <= li < len(provenance) else {
            "driver_inserted": False, "certification_eligible": True,
        })
        cursor[key] = i + 1
    # Choose the row each q's summed cost lands on: the first PRODUCTION row if the q has one, else its
    # first row (stamping the full sum onto a quicklook row would mis-set last_prod_cost).
    cost_target: dict = {}
    for idx, (row, kind, provenance) in enumerate(zip(result_rows, row_kinds, row_provenance)):
        key = resume_hkl_key(_row_hkl(row))
        if key not in cost_target:
            cost_target[key] = idx
        elif kind in PRODUCTION_KINDS and row_kinds[cost_target[key]] not in PRODUCTION_KINDS:
            cost_target[key] = idx                  # prefer a production row over an earlier quicklook

    observations: list[dict] = []
    consumed: set = set()
    scan_kind: dict = {}
    for idx, (row, kind, provenance) in enumerate(zip(result_rows, row_kinds, row_provenance)):
        key = resume_hkl_key(_row_hkl(row))
        ncount = int(ncount_by_hkl.get(key) or PRODUCTION_NCOUNT)
        pc = cost_by_hkl.get(key, 0.0) if cost_target.get(key) == idx else 0.0
        observations.append(_observation_from_row(
            row, kind, ncount, calibration, proposal_cost=pc, **provenance))
        sd = (row.get("scan"), row.get("detector"))
        consumed.add(sd)
        scan_kind[sd] = (kind, ncount)

    for event in events:
        if event.get("kind") == "certification_crossing":
            marker = dict(event.get("payload") or {})
            if marker.get("hkl") is not None and marker.get("production_batch_fingerprint"):
                marker["kind"] = "certification_crossing"
                observations.append(marker)

    return {"observations": observations, "consumed": consumed, "scan_kind": scan_kind,
            "scan_counter": scan_counter, "neutrons": neutrons, "seconds": seconds,
            "proposal_counter": max(scan_counter,
                                    sum(1 for event in events if event.get("kind") == "propose")),
            "unmatched_submissions": []}


def _read_run_timings(workspace) -> dict | None:
    """Best-effort read of the subprocess stage-timing file ``isar run`` drops per invocation
    (``workspace/report/run_timings.json``). Returns ``None`` when the file is absent or unparseable —
    profiling is advisory and must never fail (or alter) the campaign. Read right after ``run_isar``
    returns (the subprocess has exited), so it snapshots that invocation's own timings."""
    p = Path(workspace) / "report" / "run_timings.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:   # noqa: BLE001  (missing / malformed timing file → no embedded timings, never a crash)
        return None


def _new_rows(workspace, consumed: set) -> list[dict]:
    """Rows in results.csv not yet consumed, keyed by (scan, detector); skips status-only rows."""
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return []
    out = []
    for r in rows:
        if r.get("pooled"):            # master (pooled) rows drive convergence, read fresh elsewhere
            continue
        key = (r.get("scan"), r.get("detector"))
        if key in consumed or _row_hkl(r) is None:
            continue
        consumed.add(key)
        out.append(r)
    return out


def _rows_missed(rows: list[dict]) -> bool:
    """True when a scan produced no usable detector (peak outside the window / no signal). A
    ``branch_continuation`` (Phase A: the near-Γ acoustic, confidently identified but not resolvable
    by position) is NOT a miss — widening the window would only chase the higher optic branch, never
    resolve a peak sitting at the elastic origin — so it does not trigger the recenter/widen."""
    if not rows:
        return True
    return not any(r.get("disposition") in _USABLE or r.get("disposition") == "branch_continuation"
                   for r in rows)


def _auxiliary_clean_null(proposal, rows: list[dict]) -> bool:
    """True when an auxiliary identity probe returned a *trustworthy clean null* — a result, not a miss.

    The probe searched where a rival was predicted and cleanly found no mode; recentring/widening would
    chase a feature the scan was fired to rule out, turning a focused discriminator into a survey for
    something that may genuinely be absent (Blocker #2). Guarded on both sides: only auxiliary probes
    qualify (a canonical delivery is fully described by its disposition), and a *broken* analysis is not
    a clean null — with no ``candidate_not_detected`` row, or any ``analysis_unusable`` one, recovery
    still runs (fail-open, so a failed scan never masquerades as a settled question)."""
    if (proposal.meta or {}).get("evidence_sink") != AUXILIARY_TRACK_NODE:
        return False
    if not rows:
        return False
    outcomes = [realized_outcome_for(r.get("disposition"),
                                     has_candidate=bool(r.get("branch_candidates")))
                for r in rows]
    return CANDIDATE_NOT_DETECTED in outcomes and ANALYSIS_UNUSABLE not in outcomes


def _pooled_target_delivered(observations: list[dict], proposal, target_branch: str) -> bool:
    """Whether analysis found the requested mode even if an individual row favored the rival.

    The pooled master is the identity authority.  A two-mode fit with a usable selected optic must
    never be called a missed window merely because the individual fit's dominant scalar feature was
    the much stronger acoustic peak.
    """
    for update in reversed(observations):
        if update.get("kind") != "pooled_master_update":
            continue
        if update.get("acquisition_id") != proposal.acquisition_id:
            continue
        if not _hkl_close(update.get("hkl"), (proposal.h, proposal.k, proposal.l)):
            continue
        return bool(update.get("usable") and update.get("branch_id") == target_branch
                    and update.get("selected_candidate_index") is not None
                    and update.get("omega0") is not None)
    return False


def _pooled_recenter_inputs(workspace, hkl) -> tuple[list, list] | None:
    """Combined counts/energy for the latest master at ``hkl``, with a validated local reference."""
    try:
        rows = [row for row in read_results_csv(workspace)
                if row.get("pooled") and _hkl_close(_row_hkl(row), hkl)
                and row.get("spectrum_ref")]
        if not rows:
            return None
        row = max(rows, key=lambda item: int(item.get("n_members") or 0))
        candidate = (Path(workspace) / str(row["spectrum_ref"])).resolve()
        root = (Path(workspace) / "data" / "pooled").resolve()
        candidate.relative_to(root)
        payload = json.loads(candidate.read_text(encoding="utf-8"))
        if payload.get("schema") != "isar.pooled_spectrum/1":
            raise ValueError("unsupported pooled spectrum schema")
        return list(payload.get("counts") or []), list(payload.get("energy") or [])
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError) as error:
        log_event(Path(workspace) / "report" / "session_log.jsonl", "exception", {
            "action": "pooled_recenter_unavailable", "hkl": list(hkl), "error": str(error)})
        return None


@dataclass(frozen=True)
class CampaignConfig:
    """The resolved configuration for one :func:`run_campaign` invocation — every ``config`` key
    turned into the concrete seam / scalar the loop consumes, resolved once by :func:`_campaign_config`
    so ``run_campaign`` reads only resolved fields.

    Fields (config key → resolved field; type; default; when used):

    - ``workspace`` (:class:`Path`, config key ``workspace``, **required**): the workspace directory;
      root of all file I/O. If it has no ``card.policy.toml`` the loop runs ``isar init`` after the
      first scan lands (init needs a data file).
    - ``data_dir`` (:class:`Path`, config key ``data_dir``, default ``workspace/"data"``): where each
      scan's JSON is saved before ``isar run`` reads it.
    - ``log_path`` (:class:`Path`, config key ``session_log``, default
      ``workspace/"report"/"session_log.jsonl"``): the durable decision/timing log every event lands in.
    - ``calibration`` (:class:`Calibration`, config key ``calibration`` accepting a ``Calibration`` /
      path / ``None``, default the repo artifact): σ-inflation + systematic floor source; drives every
      calibrated half-width.
    - ``client`` (TAVI-client-shaped, config keys ``client`` then ``tavi_url``, default
      :class:`TaviClient` at ``tavi_url`` or its own default): the validate/submit/wait/fetch seam.
    - ``runner`` (runner object, config key ``run_isar`` — a bare callable/runner adapted via
      :func:`as_runner`): runs the ISAR analysis; reused across the campaign and closed in its
      ``finally``. Resolution: an explicit ``config["run_isar"]`` wins; else the persistent
      :class:`WorkerRunner` (default, config key ``worker`` — one warm process per campaign, cold
      :class:`SubprocessRunner` fallback); else a fresh-subprocess :class:`SubprocessRunner` when
      ``worker`` is off.
    - ``worker`` (:class:`bool`, config key ``worker``, default ``True``): use the persistent analysis
      worker (``isar drive --no-worker`` disables it). Ignored when ``run_isar`` is supplied.
    - ``run_timeout`` (:class:`float`, config key ``run_timeout``, default 900.0): the worker's per-job
      wall-clock ceiling (seconds) before it falls back cold. Ignored when ``run_isar`` is supplied.
    - ``init_isar`` (:class:`Callable` ``(workspace, probe) -> Any``, config key ``init_isar``, default
      the real ``isar init`` seam): scaffolds the instrument card lazily after the first scan lands.
    - ``resolution_cb`` (:class:`Callable`, built from config key ``resolution`` — a float / dict /
      callable, default wraps ``GET /resolution``): the energy-resolution FWHM source for acquisition
      and the Fisher screen.
    - ``engine`` (:class:`str`, ``= objective.tier`` — NOT a config key, derived from the objective):
      the scan engine tier stamped into every ``POST /scan`` body.
    - ``base_seed`` (:class:`int`, config key ``seed``, default 0): scan ``n`` uses ``base_seed + n``.
    - ``wait_timeout`` (:class:`float`, config key ``wait_timeout``, default
      :data:`DEFAULT_WAIT_TIMEOUT`): the per-scan ``wait_scan`` timeout in seconds.
    - ``floor`` (:class:`float`, ``= systematic_floor(calibration, config["floor"])`` — config key
      ``floor`` overrides): the systematic ω₀ floor added to every convergence gate.
    - ``sample`` (:class:`str`, config key ``sample``, default ``"Al_phonon_DFT"``): stamped into every
      validate/scan body's ``parameters`` because the instrument API no longer inherits the GUI's
      sample selection.
    - ``sample_background`` (:class:`dict` | :class:`str` | ``None``, config key
      ``sample_background``, default ``None``): the background spec bundled with the sample
      declaration — ``{catalog_version, enabled, sources: {id: {enabled, scale}}}``.
      Normalized ONCE at campaign setup into ``sample_background_frozen``
      (see :func:`_freeze_sample_background`) and stamped on every validate/scan body. ``None``
      (undeclared) freezes to a complete, DISABLED v2 profile — an
      undeclared campaign must not inherit TAVI's mutable configured profile. The sentinel string
      ``"instrument_config"`` (:data:`INSTRUMENT_CONFIG_BACKGROUND`) is the deliberate opt-out: no
      freeze, no stamping, no drift check, inherit the instrument's live config.
    - ``sample_background_frozen`` (:class:`dict` | ``None``, NOT a config key): the normalized,
      catalog-versioned profile derived from ``sample_background`` by :func:`run_campaign` at setup.
    - ``bayes_errors`` (:class:`bool`, config key ``bayes_errors``, default ``False``): campaign-level
      opt-in for the ``bayes`` errors tier (superset of ``full``: bootstrap + MINOS + profile +
      posterior). When set, every place that would otherwise use the ``full`` tier (escalation in
      :func:`_errors_tier_for`, the certify pass) uses ``bayes`` instead — see :func:`_full_tier`. The
      bootstrap/profile intervals ``full`` produces are unchanged; the posterior rides along as CSV
      diagnostics only and never feeds ``_pick_interval``/``_is_certifiable``/certification.
    """
    workspace: Path
    data_dir: Path
    log_path: Path
    calibration: Calibration
    client: Any
    runner: Any
    init_isar: Callable
    resolution_cb: Callable
    engine: str
    base_seed: int
    wait_timeout: float
    floor: float
    worker: bool = True
    run_timeout: float = 900.0
    run_nonce: str = ""
    # Instrument point-count ceiling, read once per campaign (GET /state|/schema), else DEFAULT_MAX_POINTS.
    max_points: int = DEFAULT_MAX_POINTS
    max_points_source: str = "default"
    # Complete live limit envelope; additive and independent of the frozen objective policy.
    instrument_limits: dict | None = None
    sample: str = "Al_phonon_DFT"
    # The campaign's declared background (spec as given, or the "instrument_config" sentinel) and its
    # compile-time frozen, catalog-versioned form.
    sample_background: dict | str | None = None
    sample_background_frozen: dict | None = None
    bayes_errors: bool = False
    # Cooperative operator stop (GUI subprocess control). When set, each iteration's top checks for
    # ``<workspace>/report/control/stop-<control_token>.json`` and, if present, breaks with an
    # ``operator_stopped`` terminal state. ``None`` (default) → behaviour byte-identical to today.
    control_token: str | None = None
    # Campaign provenance fingerprint stamped into the campaign_start event, plumbed from the CLI:
    # ``{"kind": "goal"|"spec"|"resume", "sha256": <hex>}``. ``None`` → field omitted (back-compat).
    source: dict | None = None


def _run_nonce(workspace: Path) -> str:
    """The campaign's idempotency nonce — minted once per workspace and persisted, so a crash-resume
    replays the SAME ``Idempotency-Key`` per scan (dedupe still works) while a fresh campaign can
    never collide with a previous run's instrument-side idempotency cache (the TAVI cache-poisoning
    replay: a poisoned/truncated cached result served to every later run that reuses the key)."""
    path = workspace / ".run_nonce"
    try:
        nonce = path.read_text(encoding="utf-8").strip()
        if nonce:
            return nonce
    except OSError:
        pass
    nonce = uuid.uuid4().hex[:12]
    workspace.mkdir(parents=True, exist_ok=True)
    path.write_text(nonce + "\n", encoding="utf-8")
    return nonce


def _campaign_config(objective: ObjectiveSpec, config: dict | None) -> CampaignConfig:
    """Resolve the raw ``config`` dict (plus ``objective``) into a frozen :class:`CampaignConfig`.

    Centralizes every default and every seam construction (calibration, client, runner, resolution
    callback, systematic floor). See :class:`CampaignConfig` for the per-key documentation."""
    config = dict(config or {})
    workspace = Path(config["workspace"])
    log_path = Path(config.get("session_log", workspace / "report" / "session_log.jsonl"))
    client = config.get("client")
    if client is None:
        client = TaviClient(config["tavi_url"]) if config.get("tavi_url") else TaviClient()
    # Runner seam: an explicit ``config["run_isar"]`` (callable hook or runner object) is adapted via
    # ``as_runner`` and always wins; otherwise the persistent WorkerRunner (one warm process per
    # campaign, cold SubprocessRunner fallback) unless ``worker`` is off, in which case a
    # fresh-subprocess SubprocessRunner per job.
    worker = bool(config.get("worker", True))
    run_timeout = float(config.get("run_timeout", 900.0))
    if config.get("run_isar") is not None:
        runner = as_runner(config["run_isar"])
    elif worker:
        runner = WorkerRunner(
            workspace, job_timeout=run_timeout, fallback=SubprocessRunner(),
            on_degraded=lambda: log_event(
                log_path, "exception",
                {"action": "worker_degraded",
                 "note": "persistent analysis worker failed repeatedly; permanent cold-subprocess fallback"}))
    else:
        runner = SubprocessRunner()
    calibration = _resolve_calibration(config.get("calibration"))
    # Read the instrument's point-count ceiling once per campaign (falls back silently to
    # DEFAULT_MAX_POINTS if the endpoint is unreachable / carries no limit).
    instrument_limits = _preflight_instrument_limits(client)
    max_points = int(instrument_limits["max_points"])
    max_points_source = str(instrument_limits["sources"]["max_points"])
    return CampaignConfig(
        workspace=workspace,
        data_dir=Path(config.get("data_dir", workspace / "data")),
        log_path=log_path,
        calibration=calibration,
        client=client,
        runner=runner,
        init_isar=config.get("init_isar", _default_init_isar),
        resolution_cb=_make_resolution_cb(client, config.get("resolution")),
        engine=objective.tier,
        base_seed=int(config.get("seed", 0)),
        wait_timeout=float(config.get("wait_timeout", DEFAULT_WAIT_TIMEOUT)),
        floor=systematic_floor(calibration, config.get("floor")),
        worker=worker,
        run_timeout=run_timeout,
        run_nonce=_run_nonce(workspace),
        max_points=max_points,
        max_points_source=max_points_source,
        instrument_limits=instrument_limits,
        sample=str(config.get("sample", "Al_phonon_DFT")),
        # Copied (a dict spec defensively, the "instrument_config" sentinel string as-is), not
        # validated here: a malformed spec is refused with a clear message by the compile-time freeze
        # in run_campaign, not silently coerced at config time.
        sample_background=(dict(config["sample_background"])
                           if isinstance(config.get("sample_background"), dict)
                           else config.get("sample_background")),
        bayes_errors=bool(config.get("bayes_errors", False)),
        control_token=(str(config["control_token"]) if config.get("control_token") else None),
        source=(dict(config["source"]) if config.get("source") else None),
    )


BACKGROUND_DRIFT = "background_fingerprint_drift"

# The one ``sample_background`` value that opts OUT of freezing: the campaign inherits TAVI's live,
# mutable background configuration and stamps nothing. See :func:`_freeze_sample_background`.
INSTRUMENT_CONFIG_BACKGROUND = "instrument_config"


def _background_catalog(schema, *, where: str) -> tuple[int, tuple[str, ...]]:
    """Return TAVI's advertised v2 catalog version and source IDs, failing closed."""
    block = (schema or {}).get("background") if isinstance(schema, dict) else None
    if not isinstance(block, dict):
        raise ValueError(f"{where}: GET /schema did not advertise a background catalog")
    background_schema = block.get("background_schema")
    if background_schema != "tavi.background/2":
        raise ValueError(f"{where}: instrument advertises unsupported background schema "
                         f"{background_schema!r}; tavi.background/2 is required")
    version = block.get("catalog_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise ValueError(f"{where}: GET /schema background.catalog_version must be an integer")
    sources = block.get("sources")
    if not isinstance(sources, dict) or not sources:
        raise ValueError(f"{where}: GET /schema background.sources must be a non-empty object")
    if any(not isinstance(source_id, str) or not source_id for source_id in sources):
        raise ValueError(f"{where}: GET /schema contains an invalid background source ID")
    return version, tuple(sources)


def _normalized_background_v2(spec: dict, *, catalog_version: int,
                              source_ids: tuple[str, ...], where: str) -> dict:
    """Strictly validate and normalize a sparse ``tavi.background/2`` request."""
    expected = {"catalog_version", "enabled", "sources"}
    unknown = set(spec) - expected
    missing = expected - set(spec)
    if unknown:
        raise ValueError(f"{where}: unknown field(s) {sorted(unknown)}")
    if missing:
        raise ValueError(f"{where}: missing required field(s) {sorted(missing)}")
    declared_version = spec["catalog_version"]
    if isinstance(declared_version, bool) or not isinstance(declared_version, int):
        raise ValueError(f"{where}: 'catalog_version' must be an integer")
    if declared_version != catalog_version:
        raise ValueError(
            f"{where}: catalog version mismatch: request declares {declared_version}, "
            f"instrument advertises {catalog_version}")
    enabled = spec["enabled"]
    if not isinstance(enabled, bool):
        raise ValueError(f"{where}: 'enabled' must be a boolean, got {enabled!r}")
    sources = spec["sources"]
    if not isinstance(sources, dict):
        raise ValueError(f"{where}: 'sources' must be an object keyed by source ID")
    unknown_sources = set(sources) - set(source_ids)
    if unknown_sources:
        raise ValueError(f"{where}: unknown background source(s) {sorted(unknown_sources)}; "
                         f"the instrument advertises {list(source_ids)}")

    normalized: dict[str, dict[str, Any]] = {}
    for source_id in source_ids:
        source = sources.get(source_id)
        if source is None:
            normalized[source_id] = {"enabled": False, "scale": 1.0}
            continue
        source_where = f"{where}.sources[{source_id!r}]"
        if not isinstance(source, dict):
            raise ValueError(f"{source_where} must be an object")
        source_unknown = set(source) - {"enabled", "scale"}
        source_missing = {"enabled", "scale"} - set(source)
        if source_unknown:
            raise ValueError(f"{source_where}: unknown field(s) {sorted(source_unknown)}")
        if source_missing:
            raise ValueError(f"{source_where}: missing required field(s) {sorted(source_missing)}")
        source_enabled = source["enabled"]
        if not isinstance(source_enabled, bool):
            raise ValueError(f"{source_where}: 'enabled' must be a boolean")
        scale = source["scale"]
        if (isinstance(scale, bool) or not isinstance(scale, (int, float))
                or not math.isfinite(float(scale)) or float(scale) < 0.0):
            raise ValueError(f"{source_where}: 'scale' must be a finite number >= 0")
        normalized[source_id] = {"enabled": source_enabled, "scale": float(scale)}
    return {"catalog_version": catalog_version, "enabled": enabled, "sources": normalized}


def _prior_frozen_background(log_path) -> dict | None:
    """The frozen background this workspace last ran under, from the durable session log (or None)."""
    prior = None
    for event in read_log(log_path):
        payload = event.get("payload") or {}
        if event.get("kind") == "decide" and payload.get("event") == "sample_background_frozen":
            frozen = payload.get("frozen")
            if isinstance(frozen, dict):
                prior = frozen
    return prior


def _freeze_sample_background(cfg: CampaignConfig) -> tuple[dict | None, dict]:
    """Normalize ``cfg.sample_background`` against TAVI's v2 catalog ONCE at campaign setup.

    A campaign's background is part of its SAMPLE DECLARATION, not a steering choice: it is resolved
    here and then stamped verbatim onto every validate/scan body as a per-scan override. Nothing in
    the loop may re-resolve it, and the campaign never depends on TAVI's mutable config default —
    a GUI edit, another client, a crash leftover, or a preset retune would otherwise change the
    background truth mid-campaign without a trace. ``TaviClient.set_background`` is manual/test-only
    and is never called from here.

    The only accepted remote form is
    ``{catalog_version, enabled, sources: {id: {enabled, scale}}}``. The source map may be sparse;
    omitted catalog sources are frozen as disabled at ×1. Presets, numeric terms, raw parameter
    overrides and profile-wide scaling belong to ``tavi.background/1`` and are rejected.

    OMISSION IS A DECLARATION OF CLEAN. ``sample_background=None`` (the default) is treated exactly
    like an empty, disabled v2 request: it freezes every advertised source disabled at ×1, is
    stamped on every validate/scan body, and is subject to the drift check below. It previously
    meant "send no background key", which silently inherited whatever profile the operator had last
    configured on TAVI — an enabled persisted profile consumed a live campaign's whole iteration
    budget before anyone noticed. The frozen event carries ``"defaulted": True`` so a log still
    distinguishes declared-clean from defaulted-clean.

    Resume drift: the frozen form is durable in the session log, so a resumed campaign re-expands
    the spec against the CURRENT schema and refuses (``background_fingerprint_drift``) rather than
    appending scans generated under different background truth. Because omission now freezes too,
    that guard also catches a resume that DROPS a previously declared background — before any spend,
    instead of downstream at collation.

    The sentinel string ``sample_background="instrument_config"`` is the one deliberate escape: no
    freeze, no stamping, no drift check, and every scan inherits TAVI's live mutable configuration.
    That is a footgun by construction — the background truth can change under the campaign at any
    moment, from a GUI edit, another client, a crash leftover, or a preset retune, and nothing in
    the record will say so. Use it only when the whole point is to measure whatever the instrument
    is currently configured to produce.

    Returns ``(frozen | None, log_meta)``; only the sentinel returns ``(None, {})``. Raises
    :class:`ValueError` — an honest setup abort, never a caught-and-continued degradation — on an
    unusable spec or on drift.
    """
    spec = cfg.sample_background
    where = "sample_background"
    if spec == INSTRUMENT_CONFIG_BACKGROUND:
        return None, {}
    defaulted = spec is None
    try:
        schema = cfg.client.get_schema()
        catalog_version, source_ids = _background_catalog(schema, where=where)
    except Exception as error:  # noqa: BLE001 — setup abort; campaign must not guess the catalog
        log_event(cfg.log_path, "exception", {
            "action": "sample_background_unresolved", "error": str(error)})
        if isinstance(error, ValueError):
            raise
        raise ValueError(f"{where}: cannot read the background catalog — "
                         f"GET /schema failed ({error})") from error
    if defaulted:
        spec = {"catalog_version": catalog_version, "enabled": False, "sources": {}}
    if not isinstance(spec, dict):
        raise ValueError(
            f"{where} must be a tavi.background/2 object "
            "{catalog_version, enabled, sources}, omitted for a frozen disabled default, or "
            f"the sentinel {INSTRUMENT_CONFIG_BACKGROUND!r} to inherit the instrument's live config; "
            f"got {type(spec).__name__}")
    prior = _prior_frozen_background(cfg.log_path)
    if prior is not None and "catalog_version" not in prior:
        raise ValueError(
            f"{BACKGROUND_DRIFT}: this workspace was frozen under tavi.background/1. "
            "Its numeric-term profile cannot be resumed under the tavi.background/2 source catalog; "
            "start a new campaign workspace.")
    if prior is not None and prior.get("catalog_version") != catalog_version:
        raise ValueError(
            f"{BACKGROUND_DRIFT}: this workspace was frozen under background catalog "
            f"{prior.get('catalog_version')}, but the instrument now advertises catalog "
            f"{catalog_version}. Catalog source definitions changed; start a new campaign workspace.")
    try:
        frozen = _normalized_background_v2(
            spec, catalog_version=catalog_version, source_ids=source_ids, where=where)
    except ValueError as error:
        if prior is not None and "catalog version mismatch" in str(error):
            raise ValueError(
                f"{BACKGROUND_DRIFT}: {error}. The instrument catalog changed since this workspace "
                "was frozen; start a new campaign workspace.") from error
        raise
    if prior is not None and canonical_json(prior) != canonical_json(frozen):
        raise ValueError(
            f"{BACKGROUND_DRIFT}: this workspace's scans were taken under a different background "
            f"profile. Stored: {canonical_json(prior)} — re-expanded now: {canonical_json(frozen)}. "
            "Resuming would append scans generated under different truth; start a new campaign or "
            "restore the original background.")
    meta: dict[str, Any] = {"catalog_version": catalog_version}
    if defaulted:
        meta["defaulted"] = True
    return frozen, meta


@dataclass(frozen=True)
class ActionRejection:
    """How a refused attempt is charged: to the scientific question, to the request, or to nobody.

    ``scope`` is the load-bearing field, and it is why the lifecycle has more than one identity:

    * ``"geometry"`` — the instrument cannot reach that q at all. Charged to the **hkl**, not to the
      scientific action fingerprint: unreachability is a property of the geometry, not of the
      question being asked there. The scientific fingerprint includes the evidence basis, so keying
      a geometry refusal to it would let the suppression evaporate the moment any evidence advanced
      and the controller would re-propose the same unreachable q for the rest of the campaign.
    * ``"request"`` — this *particular* request was refused. The widen / re-center / step-shrink
      ladder answering the same question with a different window is a real recovery and must stay
      open, so only the request is charged and the ladder keeps it.
    * ``"none"`` — transient. Suppress nothing; the question and the request both remain live.
    """

    verdict: str
    rejection_class: RejectionClass
    scope: str          # "geometry" | "request" | "none"


# Verdict → durable consequence. Anything not listed is TRANSIENT/"none" on purpose: an unrecognised
# verdict must not become a permanent suppression on the strength of a string nobody has mapped yet,
# so classification fails open.
_REJECTION_RULES: dict[str, tuple[RejectionClass, str]] = {
    "geometry_unreachable": (RejectionClass.STRUCTURAL, "geometry"),
    "all_points_infeasible": (RejectionClass.STRUCTURAL, "geometry"),
    # Partial infeasibility refused at submit: this exact grid is unsubmittable, but the q is
    # reachable — scoping it to "geometry" would suppress an HKL the instrument can still measure.
    "infeasible_points_at_submit": (RejectionClass.STRUCTURAL, "request"),
    "validation_rejected": (RejectionClass.STRUCTURAL, "request"),
    "insufficient_target_support": (RejectionClass.STRUCTURAL, "request"),
    "instrument_point_limit_exceeded": (RejectionClass.STRUCTURAL, "request"),
    "budget_insufficient_for_minimum_batch": (RejectionClass.BUDGET, "request"),
}


def classify_rejection(verdict: str) -> ActionRejection:
    """Class one refusal verdict into its durable consequence. See :class:`ActionRejection`."""
    rejection_class, scope = _REJECTION_RULES.get(
        str(verdict), (RejectionClass.TRANSIENT, "none"))
    return ActionRejection(verdict=str(verdict), rejection_class=rejection_class, scope=scope)


def suppressed_actions(log_path) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
    """Rebuild the durable suppression sets from the session log.

    Suppression is derived state, never a second durable store: a resume that could not rebuild it
    would re-propose something the instrument has already refused, every iteration, for the rest of
    the campaign. Returns ``(by_scientific_action, by_hkl, by_request)``.

    ``by_request`` is keyed by ``request_fingerprint`` and holds requests that were refused *as that
    exact request* (``validation_rejected`` / ``insufficient_target_support``). It deliberately does
    NOT feed ``by_action``: the widen / re-center / step-shrink ladder answering the same scientific
    question with a *different* window keys off the scientific action fingerprint and must stay open —
    only the identical request is blocked from re-emitting. Transient entries are recorded for
    forensics and narrow nothing.
    """
    by_action: dict[str, dict] = {}
    by_hkl: dict[str, dict] = {}
    by_request: dict[str, dict] = {}
    for event in read_log(log_path):
        if event.get("kind") != "action_suppressed":
            continue
        payload = event.get("payload") or {}
        scope = payload.get("scope")
        entry = {
            "class": str(payload.get("rejection_class") or RejectionClass.STRUCTURAL.value),
            "reason": str(payload.get("reason") or "previously refused"),
        }
        if scope == "geometry" and payload.get("hkl"):
            by_hkl[hkl_key(payload["hkl"])] = entry
        elif scope == "action" and payload.get("scientific_action_fingerprint"):
            by_action[str(payload["scientific_action_fingerprint"])] = entry
        elif scope == "request" and payload.get("request_fingerprint"):
            by_request[str(payload["request_fingerprint"])] = entry
            # Requests the proposal was adjusted away from before it was refused are charged too, so
            # a resume does not re-emit the un-adjusted original and repeat the adjustment.
            for value in (payload.get("superseded_request_fingerprints") or []):
                if value:
                    by_request[str(value)] = entry
    return by_action, by_hkl, by_request


# How many actions one iteration may try before giving the loop back its turn. A refused request
# should not cost the whole iteration, but an inventory that refuses everything must not spin either.
MAX_ACTION_ATTEMPTS_PER_ITERATION = 3


def _record_action_rejection(cfg, plan, proposal, verdict: str,
                             action_suppressions: dict, unreachable_hkl: dict,
                             request_suppressions: dict, *, it: int):
    """Record one refusal at its correct scope and return the next action to try, if any.

    Always logs, because a refusal is evidence about the action space even when it suppresses
    nothing. Only a durable scope narrows the space *and* justifies moving on to another action this
    iteration:

    * ``geometry`` charges the **hkl** (``unreachable_hkl``);
    * ``action`` charges the **scientific action fingerprint** (``action_suppressions``);
    * ``request`` charges only the **request fingerprint** (``request_suppressions``) so the exact
      request is not re-emitted, while the scientific action — and with it the widen / re-center /
      step-shrink ladder that answers the same question with a different window — stays untouched;
    * a transient refusal is left entirely alone and does not advance to another action.

    The logged ``action_suppressed`` event carries the scope and the request fingerprint, so a resume
    rebuilds every set (including ``request_suppressions``) from the durable log.
    """
    rejection = classify_rejection(verdict)
    meta = proposal.meta or {}
    fingerprint = meta.get("scientific_action_fingerprint")
    request_fingerprint = meta.get("proposal_fingerprint")
    # Requests this proposal was adjusted away from in flight. They are charged alongside the final
    # one, or the un-adjusted original re-emits next iteration and repeats the same decline.
    superseded = [str(value) for value in (meta.get("superseded_request_fingerprints") or [])
                  if value and str(value) != str(request_fingerprint)]
    hkl = [proposal.h, proposal.k, proposal.l]
    reason = f"{verdict} at hkl={hkl}"
    log_event(cfg.log_path, "action_suppressed", {
        "scope": rejection.scope,
        "verdict": rejection.verdict,
        "rejection_class": rejection.rejection_class.value,
        "reason": reason,
        "scientific_action_fingerprint": fingerprint,
        "request_fingerprint": request_fingerprint,
        "superseded_request_fingerprints": superseded,
        "hkl": hkl,
        "acquisition_id": proposal.acquisition_id,
        "iter": it})
    entry = {"class": rejection.rejection_class.value, "reason": reason}
    if rejection.scope == "geometry":
        unreachable_hkl[hkl_key(hkl)] = entry
    elif rejection.scope == "action" and fingerprint:
        action_suppressions[str(fingerprint)] = entry
    elif rejection.scope == "request" and request_fingerprint:
        request_suppressions[str(request_fingerprint)] = entry
        for value in superseded:
            request_suppressions[value] = entry
    else:
        return None
    return plan.next_proposal() if plan is not None else None


def _score_shadow_proposal(proposal, objective, cfg, *, it: int) -> None:
    """Run diagnostic scan-window scoring after selection and before realization clamps."""
    inventory = getattr(proposal, "scan_design_inventory", None)
    if inventory is None or inventory.context.scan_window_eig_mode not in {
            "shadow", "authoritative"}:
        return
    selected_id = inventory.selected_candidate_id
    selected = inventory.selected_candidate
    if selected.candidate_family != "legacy_uniform/1":
        raise RuntimeError("shadow scoring requires the legacy control to remain selected")
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
        raise RuntimeError("shadow scoring changed the legacy scan-design selection")


def _next_finalized_proposal(plan, objective, cfg, grid, budget_state: dict,
                             request_suppressions: dict, *, it: int):
    """Select the best not-yet-tried action, finalize it, and hand back a ready proposal (O29).

    Selection and finalization are one bounded step so that a first action which cannot fund its
    minimum batch — or which the instrument cannot deliver — no longer terminates a campaign while a
    cheaper eligible action is still affordable. A budget / instrument refusal raised during
    finalization is caught here, logged, and the next candidate is tried instead; only when *no*
    remaining candidate can be finalized does a terminal fire — ``infeasible`` if any instrument limit
    was hit, otherwise ``incomplete``.

    A finalized proposal whose ``request_fingerprint`` is already in ``request_suppressions`` (an
    unchanged request the log says was refused) is skipped in favour of a fresh action; a different
    window at the same q keeps its own fingerprint and stays allowed, so the widen / re-center /
    step-shrink ladder is untouched.

    The identical request is **never** re-emitted. Only ``validation_rejected``,
    ``insufficient_target_support`` and ``budget_insufficient_for_minimum_batch`` ever reach
    ``request_suppressions`` — transient verdicts are scoped ``"none"`` and recorded for forensics only
    — so every entry there is a structural or budget refusal, and re-submitting it would repeat a
    refused request once per iteration for the rest of the campaign.

    When the only realizable action is such a request, the honest report is a **resumable
    controller fault** naming the refusal. Note what is deliberately *not* done: the scientific intent
    stays eligible in the inventory. One refused finalized request does not prove its
    ``MeasurementIntent`` is scientifically exhausted, and rejecting the parent action to manufacture
    an ``action_inventory_exhausted`` would both collapse the request/action distinction the
    three-stage lifecycle exists to draw and mis-class a BUDGET refusal as an empty action space. The
    honest terminal for complete *realization* exhaustion belongs to a realization inventory beneath
    the scientific one, once the widen/re-center/step ladder is explicitly enumerated.

    Returns ``(proposal, None)`` for a ready proposal, ``(None, StopDecision)`` when a budget /
    instrument / unrealizable-request terminal is the honest outcome, or ``(None, None)`` when the plan
    genuinely ran out of candidates — the caller then falls through to the blocked-frontier /
    exhausted-action-space path, which reads the same canonical inventory for its terminal proof.
    """
    instrument_error: Exception | None = None
    budget_error: Exception | None = None
    suppressed_refusal: dict | None = None   # the sole realizable action was an already-refused request
    while True:
        # Both the realization (``next_proposal`` → ``_build_proposal``) and the finalize clamp can
        # raise the affordability/instrument-limit exceptions, so both live inside one handler — the
        # refusal is charged and the loop tries the next candidate rather than terminating (O29).
        try:
            proposal = plan.next_proposal()
            if proposal is not None:
                _score_shadow_proposal(proposal, objective, cfg, it=it)
                _finalize_proposal(proposal, objective, cfg, budget_state, it=it)
        except (InstrumentLimitBelowMinimumBatch, InstrumentPointLimitExceeded) as error:
            log_event(cfg.log_path, "exception", {
                "action": ("instrument_point_limit_exceeded"
                           if isinstance(error, InstrumentPointLimitExceeded)
                           else "instrument_limit_below_minimum_batch"),
                "error": str(error), "iter": it})
            instrument_error = error
            continue
        except BudgetInsufficientForMinimumBatch as error:
            log_event(cfg.log_path, "exception", {
                "action": "budget_insufficient_for_minimum_batch",
                "error": str(error), "iter": it})
            budget_error = error
            continue
        if proposal is None:
            # A named resource limit is a better answer than a generic fault, so an instrument or
            # budget terminal established during the sweep outranks the unrealizable-request one.
            if instrument_error is None and budget_error is None and suppressed_refusal is not None:
                # The recorded class decides the terminal. A BUDGET-scoped refusal from an *earlier*
                # iteration reaches here with no live ``budget_error``, and reporting it as a software
                # fault would tell the operator to repair the controller when the answer is to add
                # budget. Only a structural refusal is a controller-side fault.
                budget_refusal = suppressed_refusal["class"] == RejectionClass.BUDGET.value
                if budget_refusal:
                    reason = (
                        "incomplete: the remaining budget cannot fund any realization of an eligible "
                        f"action at hkl={suppressed_refusal['hkl']} — its only emittable request was "
                        f"already refused ({suppressed_refusal['reason']}). More budget, or a looser "
                        "minimum batch, is what unblocks it.")
                else:
                    reason = (
                        "controller_fault: an eligible action has no unsuppressed realization — the "
                        f"only request it can emit at hkl={suppressed_refusal['hkl']} was already "
                        f"refused ({suppressed_refusal['reason']}). The scientific action remains "
                        "eligible; re-emitting the identical refused request would repeat it every "
                        "iteration. Checkpoints retained and resume is supported after repair.")
                log_event(cfg.log_path, "decide", {
                    "event": "request_has_no_unsuppressed_realization",
                    "action": "request_has_no_unsuppressed_realization",
                    "request_fingerprint": suppressed_refusal["request_fingerprint"],
                    "rejection_class": suppressed_refusal["class"],
                    "hkl": suppressed_refusal["hkl"], "iter": it})
                return None, StopDecision(
                    "incomplete", [reason],
                    evaluate_stop(objective, grid, budget_state,
                                  calibration=cfg.calibration, floor=cfg.floor).per_q,
                    terminal_cause=(None if budget_refusal else TERMINAL_CONTROLLER_FAULT))
            if instrument_error is not None:
                return None, StopDecision(
                    "infeasible", [str(instrument_error)],
                    evaluate_stop(objective, grid, budget_state,
                                  calibration=cfg.calibration, floor=cfg.floor).per_q)
            if budget_error is not None:
                return None, StopDecision(
                    "incomplete", [str(budget_error)],
                    evaluate_stop(objective, grid, budget_state,
                                  calibration=cfg.calibration, floor=cfg.floor).per_q)
            return None, None
        finalized = finalized_proposal(proposal)
        if finalized is not None and finalized.request_fingerprint in request_suppressions:
            log_event(cfg.log_path, "decide", {
                "event": "request_suppressed_skip", "action": "request_suppressed_skip",
                "request_fingerprint": finalized.request_fingerprint,
                "hkl": [proposal.h, proposal.k, proposal.l], "iter": it})
            entry = request_suppressions.get(finalized.request_fingerprint) or {}
            suppressed_refusal = suppressed_refusal or {
                "request_fingerprint": finalized.request_fingerprint,
                "hkl": [proposal.h, proposal.k, proposal.l],
                "class": str(entry.get("class") or "structural"),
                "reason": str(entry.get("reason") or "previously refused")}
            continue
        _persist_scan_design_inventory(cfg, proposal, it=it)
        _refresh_realized_comparison(
            cfg, objective, proposal, budget_state, it=it,
            legacy_adjustments=("initial_finalization",))
        return proposal, None


def _mark_request_superseded(proposal, fingerprint: str | None) -> None:
    """Remember a request fingerprint this proposal has already moved on from.

    An in-flight adjustment (today: the feasibility trim) re-fingerprints the proposal, so a decline
    afterwards charges only the *adjusted* request. The original is then still unsuppressed and the
    selector re-emits it on the next iteration, which adjusts it into the same decline again — churn
    that costs an iteration every time and never terminates. Recording the superseded fingerprint
    here lets :func:`_record_action_rejection` charge both.
    """
    if not fingerprint:
        return
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    superseded = meta.setdefault("superseded_request_fingerprints", [])
    if str(fingerprint) not in superseded:
        superseded.append(str(fingerprint))


def _declined_observation(proposal, verdict: str) -> dict:
    """The observation appended when a proposed scan yields no fit — validation rejected it, or the
    scan job never reached ``done``. Identical shape at both sites; only ``verdict`` differs
    (``"validation_rejected"`` vs the terminal job state)."""
    meta = proposal.meta or {}
    return {"hkl": (proposal.h, proposal.k, proposal.l), "kind": proposal.kind,
            "calibrated_halfwidth": None, "resolved": False, "disposition": "declined",
            "verdict": verdict, "omega0": None,
            "submitted_ncount": int(proposal.ncount),
            "acquisition_id": proposal.acquisition_id,
            "acquisition_action_role": meta.get("acquisition_action_role"),
            "target_branch_role": meta.get("target_branch_role"),
            "rival_branch_role": meta.get("rival_branch_role"),
            "authoritative_evidence_revision": meta.get("authoritative_evidence_revision"),
            "proposal_fingerprint": meta.get("proposal_fingerprint"),
            "driver_inserted": bool(meta.get("driver_inserted", False)),
            "certification_eligible": bool(meta.get("certification_eligible", True))}


def _persist_campaign_target(workspace: Path, objective) -> None:
    """Copy the frozen objective's active analysis target into machine-owned card state."""
    policy_path = workspace / "card.policy.toml"
    state_path = workspace / "card.state.json"
    # Runner seams used by dry-run/notebook consumers may deliberately provide a no-op initializer.
    # There is then no machine state to extend; the real runner creates the policy immediately above.
    if not policy_path.exists():
        return
    card = Card.load(policy_path, state_path)
    target = {"branch": getattr(objective, "target_branch", "acoustic"),
              "label": getattr(objective, "target_label", "acoustic"),
              "selector": getattr(objective, "target_selector", "lowest_energy"),
              "target_energy_meV": getattr(objective, "target_energy_meV", None),
              "energy_search_meV": list(getattr(objective, "energy_search_meV", (-2.0, 7.0))),
              # The session identity tracker needs the frozen scientific path order; sorting fitted
              # q after the fact would silently turn a looped path into a different experiment.
              "path": [list(q) for q in getattr(objective, "path", [])]}
    identity_search = getattr(objective, "identity_search_meV", None)
    if identity_search is not None:
        target["identity_search_meV"] = list(identity_search)
    snapshot = ({"hypotheses": list(getattr(objective, "hypotheses", []) or []),
                 "target_hypothesis": getattr(objective, "target_hypothesis", None),
                 "source": getattr(objective, "hypothesis_source", None),
                 "fingerprint": getattr(objective, "hypothesis_fingerprint", None),
                 "steering": dict(getattr(objective, "steering", {}) or {})}
                if getattr(objective, "hypotheses", None) else {})
    if card.state.campaign_target != target or card.state.hypothesis_snapshot != snapshot:
        card.state.campaign_target = target
        card.state.hypothesis_snapshot = snapshot
        card.save_state(state_path)


def _persist_acquisition_provenance(workspace: Path, payload: dict) -> None:
    """Append the TAVI job→logical acquisition map before analysis consumes the raw scan."""
    policy_path = workspace / "card.policy.toml"
    state_path = workspace / "card.state.json"
    if not policy_path.exists():
        return
    job_id = payload.get("job_id")
    acquisition_id = payload.get("acquisition_id")
    if job_id is None or acquisition_id is None:
        return
    card = Card.load(policy_path, state_path)
    existing = getattr(card.state, "acquisition_provenance", None) or []
    if any(str(item.get("job_id")) == str(job_id) for item in existing):
        return
    card.state.acquisition_provenance.append({
        "job_id": str(job_id),
        "acquisition_id": str(acquisition_id),
        "acquisition_target_energy": float(payload["acquisition_target_energy"]),
        # The searched window, so a clean null from an auxiliary probe reaches the tracker as negative
        # evidence at analysis time (the session reads this record, not the driver's in-memory proposal).
        "search_energy_lo": payload.get("search_energy_lo"),
        "search_energy_hi": payload.get("search_energy_hi"),
        "search_energy_step": payload.get("search_energy_step"),
        "requested_points": payload.get("requested_points"),
        "submitted_ncount": int(payload["submitted_ncount"]),
        "scan_kind": str(payload["kind"]),
        "acquisition_action_role": payload.get("acquisition_action_role"),
        "target_branch_role": payload.get("target_branch_role"),
        "rival_branch_role": payload.get("rival_branch_role"),
        "authoritative_evidence_revision": payload.get("authoritative_evidence_revision"),
        "proposal_fingerprint": payload.get("proposal_fingerprint"),
        "scan_design_inventory_ref": payload.get("scan_design_inventory_ref"),
        "scan_design_candidate_id": payload.get("scan_design_candidate_id"),
        "scan_design_context_fingerprint": payload.get("scan_design_context_fingerprint"),
        "scan_design_selection_basis": payload.get("scan_design_selection_basis"),
        "scan_design_shadow_status": payload.get("scan_design_shadow_status"),
        "scan_design_shadow_fingerprint": payload.get("scan_design_shadow_fingerprint"),
        "scan_design_shadow_variant_fingerprints": payload.get(
            "scan_design_shadow_variant_fingerprints"),
        "scan_design_realized_comparison_ref": payload.get(
            "scan_design_realized_comparison_ref"),
        "scan_design_realized_comparison_fingerprint": payload.get(
            "scan_design_realized_comparison_fingerprint"),
        "scan_design_realized_variant_fingerprints": payload.get(
            "scan_design_realized_variant_fingerprints"),
        "scan_design_realized_selected_request_fingerprint": payload.get(
            "scan_design_realized_selected_request_fingerprint"),
        "scan_design_realized_selected_grid_fingerprint": payload.get(
            "scan_design_realized_selected_grid_fingerprint"),
        "scan_design_eig_applicability": payload.get("scan_design_eig_applicability"),
        "scan_design_eig_invalidation_reason": payload.get(
            "scan_design_eig_invalidation_reason"),
        "scan_design_executed_grid_status": payload.get("scan_design_executed_grid_status"),
        "scan_design_executed_grid_reason": payload.get("scan_design_executed_grid_reason"),
        # The one channel by which analysis learns this scan was not ordinary evidence. Written here,
        # before the raw scan is consumed, because reduction decides whether to pool it and the session
        # decides how the tracker may read it — both from this record, neither from the driver.
        "evidence_sink": payload.get("evidence_sink") or CANONICAL_MASTER,
    })
    card.save_state(state_path)


def _read_branch_tracks(workspace: Path) -> dict:
    policy_path = workspace / "card.policy.toml"
    state_path = workspace / "card.state.json"
    if not policy_path.exists():
        return {}
    try:
        return dict(Card.load(policy_path, state_path).state.branch_tracks or {})
    except (OSError, ValueError, TypeError) as exc:
        logging.getLogger(__name__).warning("could not read branch tracks from %s: %s", state_path, exc)
        return {}


def _workspace_zone_period(workspace: Path) -> float | None:
    """Read the scientist-owned reduced-zone period without mutating either card file."""
    policy_path = workspace / "card.policy.toml"
    if not policy_path.exists():
        return None
    try:
        card = Card.load(policy_path, workspace / "card.state.json")
        value = (card.policy.dispersion or {}).get("zone_period")
        return float(value) if value is not None else None
    except (OSError, ValueError, TypeError) as exc:
        logging.getLogger(__name__).warning("could not read zone period from %s: %s", policy_path, exc)
        return None


class ControllerInvariantError(RuntimeError):
    """Acquisition abandoned a frontier it was still authorized to act on.

    This is a controller defect, never a scientific result, so it is raised rather than reported.
    The loop breaks unconditionally after :func:`_blocked_frontier_decision`, so a softened verdict
    would end the campaign anyway — and OP10 showed what that costs: six q reported as
    ``branch_ambiguity`` while holding 11 of 12 authorized productions and an untouched per-scan
    ceiling three orders of magnitude above the largest scan actually submitted.
    """


def _invariant_payload(point) -> dict:
    """Everything needed to reconstruct why acquisition declined to act on this point."""
    return {
        "path_index": point.index,
        "hkl": list(point.hkl),
        "track_state": point.track_state,
        "target_probability": point.target_probability,
        "identity_entropy": point.identity_entropy,
        "local_identity_clear": point.local_identity_clear,
        "candidate_precision_meV": _candidate_precision(
            point.evidence_master or point.master or {}),
        "n_production": point.n_production,
        "max_production_batches": point.max_production_batches,
        "same_q_ceiling_reached": point.same_q_ceiling_reached,
        "exposure_saturated": point.exposure_saturated,
        "identity_saturated": point.identity_saturated,
        "n_quicklook": point.n_quicklook,
        "production_sequences": list(point.production_sequences),
        "identity_revisit_sequences": list(point.identity_revisit_sequences),
        "evidence_update_sequences": list(point.evidence_update_sequences),
    }


def _operator_authorization_hint(inventory) -> str | None:
    """The operator-authorizable reason an exhausted inventory holds, if it holds one (DECISIONS O31).

    An exhaustion terminal whose *only* remaining action is forbidden by declared policy is a
    legitimate terminal, but it is escaped by authorizing the action — not by more beam, not by a
    repair. That escape has to reach the operator reading the stop reason; leaving it in the persisted
    inventory alone reports a resolvable campaign as a scientific dead end (O28's strict-historical
    AP6: the rival that would settle the identity sat above the declared search band).

    Two shapes carry the signal, and both are read here because a generator may take either route:
    an action that was generated and then rejected as ``OPERATOR``, and a generator that emitted
    nothing at all and declared ``policy_forbidden`` as its reason code. Anything else — structural,
    saturation, budget, transient — is deliberately excluded: those are not the operator's to lift,
    and presenting them as authorizable would send a scientist to edit a policy that cannot help.
    """
    if inventory is None:
        return None
    hints: list[str] = []
    for evaluation in getattr(inventory, "evaluations", []):
        if (evaluation.rejection_class is RejectionClass.OPERATOR
                and evaluation.rejection_reason):
            hints.append(str(evaluation.rejection_reason))
    for report in getattr(inventory, "reports", []):
        reason = report.reason_code
        if reason and str(reason).startswith("policy_forbidden"):
            hints.append(str(reason))
    if not hints:
        return None
    unique = list(dict.fromkeys(hints))          # first-seen order; a repeated band says it once
    return "; ".join(unique[:3])


def _blocked_frontier_decision(objective, grid, decision: StopDecision,
                               inventory=None) -> StopDecision:
    """Turn a proposal-less tracked identity frontier into an honest incomplete outcome.

    ``inventory`` is the :class:`~isar.drive.acquisition.inventory.ActionInventory` acquisition just
    enumerated. Its :meth:`terminal_cause` decides *why* the action space ended — an exhaustion proof
    (``action_inventory_exhausted``) only when every applicable generator ran cleanly, otherwise a
    ``controller_fault`` — and that cause is stamped on the returned terminal. When no inventory is
    supplied (unit callers of this helper) the honest legacy meaning, ``action_inventory_exhausted``,
    is used.
    """
    if (decision.state != "continue"
            or getattr(objective, "target_selector", None) != "tracked_branch"):
        return decision
    blocked = [point for point in grid
               if point.visited and point.track_state in {
                   "ambiguous", "missing", "provisional", "blocked"}]
    if not blocked:
        return decision
    # This helper runs only after the acquisition controller returned no proposal.  That controller
    # has already checked local statistics, newer-neighbor revisit tokens, every unvisited requested
    # point ranked by branch information, and saturation.  Duplicating an approximate route test here
    # previously let the loop break while still reporting ``continue``.
    actionable = [point for point in blocked if point.same_q_identity_work_remains]
    if actionable:
        details = "; ".join(
            f"path index {point.index} {tuple(point.hkl)} "
            f"{point.n_production}/{point.max_production_batches} productions"
            for point in actionable)
        raise ControllerInvariantError(
            "acquisition returned no proposal for an actionable blocked frontier: "
            f"{details}. Authorized same-q identity work remained, so branch ambiguity here is a "
            f"controller fault rather than a result. state={[_invariant_payload(p) for p in actionable]}")
    # The cause comes straight from the canonical inventory — never coerced. A ``None`` cause means the
    # inventory still holds an eligible action while acquisition produced no proposal: that is a
    # controller bug, not an exhaustion proof, so it must raise rather than be relabelled as exhausted
    # (the ``same_q`` guard above catches the legacy-predicate form; this catches it generally).
    cause = (inventory.terminal_cause(GENERATOR_REGISTRY) if inventory is not None
             else TERMINAL_ACTION_INVENTORY_EXHAUSTED)
    if cause is None:
        eligible = [e.intent.action_class for e in inventory.eligible_actions()]
        raise ControllerInvariantError(
            "acquisition returned no proposal although the canonical inventory holds eligible "
            f"action(s) {eligible}: branch ambiguity here is a controller fault, not a result.")
    indices = ", ".join(str(point.index) for point in blocked)
    per_q = [dict(row) for row in decision.per_q]
    if cause == TERMINAL_CONTROLLER_FAULT:
        # A controller failure is not a scientific branch-ambiguity result: name it as such so a
        # GUI/report reader that ignores the additive cause cannot mistake a software fault for a
        # scientific terminal. Checkpoints are retained; the campaign resumes after repair.
        failed = ", ".join(f"{r.generator}({r.error_code or 'unknown'})"
                           for r in inventory.failed_reports()) or "unknown"
        top_reason = (f"controller_fault: the action inventory is incomplete — generator(s) {failed} "
                      "did not run to completion, so action-space exhaustion cannot be proven; "
                      "checkpoints retained and resume is supported after repair")
        per_q_reason = f"controller_fault: inventory incomplete ({failed}); resume supported after repair"
    else:
        top_reason = (f"branch_ambiguity; frontier_blocked at path index/indices {indices}: "
                      "the connected classification route and identity-revisit allowance were exhausted")
        hint = _operator_authorization_hint(inventory)
        if hint:
            top_reason += f" — an action remains that declared policy forbids: {hint}"
        per_q_reason = "branch_ambiguity; frontier_blocked after identity-route allowance was exhausted"
    for point in blocked:
        if point.index < len(per_q):
            per_q[point.index]["assessment_state"] = "incomplete"
            per_q[point.index]["reason"] = per_q_reason
    return StopDecision("incomplete", [top_reason], per_q, terminal_cause=cause)


def _exhausted_action_space_decision(decision: StopDecision, inventory=None) -> StopDecision:
    """Terminalize a still-``continue`` decision when acquisition has no action left to offer.

    :func:`_blocked_frontier_decision` speaks only for a *tracked* identity frontier; every other
    objective fell straight through it, and because the loop breaks unconditionally on a missing
    proposal the campaign then reported ``continue`` — a non-terminal masquerading as a terminal
    state, with no cause and no inventory proof attached.

    Nothing could reach that path while the selector always returned some action. Durable suppression
    (PR2.3) makes an empty action space ordinary, so the general case now needs the same honest
    terminal the frontier case gets: the cause comes from the canonical inventory, never a coercion,
    and an inventory still holding an eligible action is a controller fault rather than an outcome.
    """
    if decision.state != "continue":
        return decision
    cause = (inventory.terminal_cause(GENERATOR_REGISTRY) if inventory is not None
             else TERMINAL_ACTION_INVENTORY_EXHAUSTED)
    if cause is None:
        eligible = [e.intent.action_class for e in inventory.eligible_actions()]
        raise ControllerInvariantError(
            "acquisition returned no proposal although the canonical inventory holds eligible "
            f"action(s) {eligible}: an empty action space here is a controller fault, not a result.")
    if cause == TERMINAL_CONTROLLER_FAULT:
        failed = ", ".join(f"{r.generator}({r.error_code or 'unknown'})"
                           for r in inventory.failed_reports()) or "unknown"
        reason = (f"controller_fault: the action inventory is incomplete — generator(s) {failed} "
                  "did not run to completion, so action-space exhaustion cannot be proven; "
                  "checkpoints retained and resume is supported after repair")
    else:
        reason = ("action_inventory_exhausted: no eligible measurement action remains for this "
                  "objective under the registered generators")
        hint = _operator_authorization_hint(inventory)
        if hint:
            reason += f" — an action remains that declared policy forbids: {hint}"
    return StopDecision("incomplete", [reason], decision.per_q, terminal_cause=cause)


def _selected_action_class(proposal) -> str | None:
    """The action class the selected proposal realizes, matching ``MeasurementIntent.action_class``."""
    if proposal is None:
        return None
    meta = proposal.meta or {}
    if meta.get("purpose") == "differentiate":
        return "differentiation_probe"
    return proposal.kind


def _selected_action_record(proposal, inventory) -> dict:
    """Identify the selected action in the compact per-iteration record.

    The class alone cannot distinguish twenty production scans at twenty different q, so the record
    also carries the scientific fingerprint, the q, the generator that produced it, and the emitted
    request fingerprint — enough to follow a campaign's action sequence from the log alone.
    """
    if proposal is None:
        return {"selected_action_class": None, "selected_scientific_action_fingerprint": None,
                "selected_hkl": None, "selected_request_fingerprint": None,
                "selected_generator": None}
    meta = proposal.meta or {}
    # Match the generator to the action actually realized, by its scientific fingerprint. After a
    # fallback the selected proposal need not be the first eligible action, so ``eligible_actions()[0]``
    # would mis-attribute the record to whatever ranked first. Fall back to the first eligible only when
    # the proposal carries no fingerprint (legacy/driver-inserted).
    selected_fingerprint = meta.get("scientific_action_fingerprint")
    generator = None
    if selected_fingerprint is not None:
        generator = next(
            (e.generator for e in inventory.evaluations
             if e.intent.scientific_action_fingerprint == selected_fingerprint), None)
    if generator is None:
        eligible = inventory.eligible_actions()
        generator = eligible[0].generator if eligible else None
    return {
        "selected_action_class": meta.get("action_class") or _selected_action_class(proposal),
        "selected_scientific_action_fingerprint": selected_fingerprint,
        "selected_hkl": [proposal.h, proposal.k, proposal.l],
        "selected_request_fingerprint": meta.get("proposal_fingerprint"),
        "selected_generator": generator,
    }


def _inventory_artifact_matches(path: Path, digest: str) -> bool:
    """Whether an existing content-addressed artifact still hashes to the address it is stored under."""
    try:
        raw = path.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return False
    return (
        hashlib.sha256(raw).hexdigest() == digest
        and hashlib.sha256(canonical_json(parsed).encode("utf-8")).hexdigest() == digest
    )


def _record_scan_design_realization(
        cfg: CampaignConfig,
        proposal,
        *,
        adjustment: str,
        previous_request_fingerprint: str | None,
        it: int,
        attempt: int | None = None,
) -> bool:
    """Record how an immutable selected candidate became a different exact request."""
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    current = meta.get("proposal_fingerprint")
    inventory_ref = meta.get("scan_design_inventory_ref")
    candidate_id = meta.get("scan_design_candidate_id")
    context_fingerprint = meta.get("scan_design_context_fingerprint")
    if (not current or current == previous_request_fingerprint or inventory_ref is None
            or candidate_id is None or context_fingerprint is None):
        return False
    log_event(cfg.log_path, "scan_design_realization", {
        "schema": "isar.scan_design_realization/1",
        "iter": it,
        "attempt": attempt,
        "adjustment": str(adjustment),
        "inventory_ref": inventory_ref,
        "candidate_id": str(candidate_id),
        "context_fingerprint": str(context_fingerprint),
        "previous_request_fingerprint": previous_request_fingerprint,
        "request_fingerprint": str(current),
        "hkl": [float(proposal.h), float(proposal.k), float(proposal.l)],
        "e_lo": float(proposal.e_lo),
        "e_hi": float(proposal.e_hi),
        "e_step": float(proposal.e_step),
        "n_points": int(proposal.n_points or 0),
        "ncount": int(proposal.ncount),
    })
    return True


def _persist_scan_design_inventory(cfg: CampaignConfig, proposal, *, it: int) -> dict | None:
    """Persist and join the selected scan-design inventory to its finalized request.

    The inventory describes the planned uniform candidate before loop-level realization clamps. The
    existing ``proposal_fingerprint`` describes the exact request after those clamps. Carrying both IDs
    preserves that distinction instead of silently rewriting the design artifact to match execution.
    """
    inventory = getattr(proposal, "scan_design_inventory", None)
    if inventory is None:
        return None
    digest = inventory.content_hash()
    path = cfg.workspace / "report" / "scan-design" / f"{digest}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    corrupt = path.exists() and not _inventory_artifact_matches(path, digest)
    if corrupt:
        log_event(cfg.log_path, "exception", {
            "action": "scan_design_artifact_mismatch",
            "sha256": digest,
            "iter": it,
            "path": f"report/scan-design/{digest}.json",
            "error": "stored artifact did not match its content address; rewritten",
        })
    if corrupt or not path.exists():
        temporary = path.with_suffix(".json.tmp")
        encoded = canonical_json(inventory.to_full_dict()).encode("utf-8")
        with open(temporary, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    ref = {
        "kind": "scan_design_inventory",
        "sha256": digest,
        "path": f"report/scan-design/{digest}.json",
    }
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    meta["scan_design_inventory_ref"] = ref
    meta["scan_design_candidate_id"] = inventory.selected_candidate_id
    meta["scan_design_context_fingerprint"] = inventory.context.fingerprint
    meta["scan_design_selection_basis"] = inventory.selection_basis
    shadow = inventory.shadow_scoring
    meta["scan_design_shadow_status"] = shadow.status
    meta["scan_design_shadow_fingerprint"] = shadow.shadow_scoring_fingerprint
    meta["scan_design_shadow_variant_fingerprints"] = [
        item.variant_evidence_fingerprint for item in shadow.variants
    ]
    log_event(cfg.log_path, "scan_design", {
        "iter": it,
        "scientific_action_fingerprint": meta.get("scientific_action_fingerprint"),
        "request_fingerprint": meta.get("proposal_fingerprint"),
        "selected_candidate_id": inventory.selected_candidate_id,
        "context_fingerprint": inventory.context.fingerprint,
        "selection_basis": inventory.selection_basis,
        "shadow_status": shadow.status,
        "shadow_scoring_fingerprint": shadow.shadow_scoring_fingerprint,
        "shadow_variant_fingerprints": [
            item.variant_evidence_fingerprint for item in shadow.variants
        ],
        "inventory_ref": ref,
    })
    candidate = inventory.selected_candidate
    planned_request_fingerprint = _request_fingerprint_for(
        proposal,
        e_lo=candidate.e_lo,
        e_hi=candidate.e_hi,
        e_step=candidate.e_step,
        ncount=candidate.ncount,
    )
    _record_scan_design_realization(
        cfg,
        proposal,
        adjustment="initial_finalization_clamp",
        previous_request_fingerprint=planned_request_fingerprint,
        it=it,
    )
    return ref


def _clear_realized_claims(proposal, *, applicability: str = "unavailable",
                           preserve_parent: bool = False) -> None:
    """Remove Slice 7 claims that cannot authenticate the request which may be submitted."""
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    proposal.meta = meta
    start = 2 if preserve_parent else 0
    for key in _REALIZED_SUBMISSION_FIELDS[start:]:
        meta.pop(key, None)
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
) -> dict | None:
    """Attach exact current Slice 7 evidence, replacing any stale realized comparison."""
    inventory = getattr(proposal, "scan_design_inventory", None)
    if inventory is None or inventory.context.scan_window_eig_mode not in {
            "shadow", "authoritative"}:
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
                finalizer=_finalize_proposal, legacy_adjustments=legacy_adjustments)
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


def _authenticate_realized_before_submission(
        cfg: CampaignConfig, objective, proposal, budget_state: dict, *, it: int,
) -> None:
    """Reauthenticate once after validation, then strip claims rather than block legacy control."""
    inventory = getattr(proposal, "scan_design_inventory", None)
    if inventory is None or inventory.context.scan_window_eig_mode != "shadow":
        return
    try:
        authenticate_realized_submission(proposal, cfg.workspace)
        return
    except Exception as error:  # noqa: BLE001 - one forced refresh is the bounded recovery
        first_error = error
    _refresh_realized_comparison(
        cfg, objective, proposal, budget_state, it=it,
        legacy_adjustments=("pre_submit_reauthentication",), force=True)
    try:
        authenticate_realized_submission(proposal, cfg.workspace)
    except Exception as error:  # noqa: BLE001 - legacy submission remains safe without EIG claims
        _clear_realized_claims(proposal)
        _LOG.exception("realized submission authentication failed; submitting legacy without claims")
        try:
            log_event(cfg.log_path, "exception", {
                "action": "realized_submission_authentication_failed", "severity": "high",
                "initial_error": str(first_error), "error": str(error), "iter": it})
        except Exception:  # noqa: BLE001 - logging failure cannot block the legacy request
            _LOG.exception("failed to persist the realized-submission authentication event")


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


def _restore_authenticated_legacy_request(cfg: CampaignConfig, proposal) -> None:
    """Replace any selected target with the persisted exact legacy request after late auth loss."""
    meta = proposal.meta if isinstance(proposal.meta, dict) else {}
    comparison = load_scan_design_realized_comparison(
        meta.get("scan_design_realized_comparison_ref"), cfg.workspace)
    legacy = comparison.realized_pair.legacy
    restored = _proposal_for_realized_request(proposal, legacy)
    proposal.h, proposal.k, proposal.l = restored.h, restored.k, restored.l
    proposal.e_lo, proposal.e_hi, proposal.e_step = (
        restored.e_lo, restored.e_hi, restored.e_step)
    proposal.n_points, proposal.ncount = restored.n_points, restored.ncount
    meta["proposal_fingerprint"] = legacy.request_fingerprint
    meta["scan_design_candidate_id"] = legacy.source_candidate_id
    for field in _AUTHORITY_SUBMISSION_FIELDS:
        meta.pop(field, None)
    meta["scan_design_realized_selected_request_fingerprint"] = legacy.request_fingerprint
    meta["scan_design_realized_selected_grid_fingerprint"] = legacy.grid_fingerprint
    authenticate_realized_submission(proposal, cfg.workspace)


def _eig_eligibility(inventory) -> Mapping[str, Any] | None:
    scoring = getattr(inventory, "shadow_scoring", None)
    eligibility = getattr(scoring, "eligibility", None)
    return eligibility if isinstance(eligibility, Mapping) else None


def _eig_primary_blocker(inventory, meta: Mapping[str, Any]) -> str | None:
    """The single code that answers "why did EIG not act this iteration", most specific first.

    The authority's own reason codes win when a decision exists, because they already account for
    everything below them; eligibility codes answer the far more common case where no decision was
    ever reachable.  ``None`` means nothing recorded a blocker — an operator reads that as "ready".
    """
    codes = meta.get("scan_design_authority_reason_codes")
    if isinstance(codes, (list, tuple)) and codes:
        return str(codes[0])
    invalidation = meta.get("scan_design_eig_invalidation_reason")
    if invalidation:
        return str(invalidation)
    eligibility = _eig_eligibility(inventory)
    if eligibility is not None:
        for key in ("scientific_codes", "observability_codes"):
            values = eligibility.get(key)
            if isinstance(values, (list, tuple)) and values:
                return str(values[0])
    reasons = getattr(getattr(inventory, "shadow_scoring", None), "reason_codes", ())
    if isinstance(reasons, (list, tuple)) and reasons:
        return str(reasons[0])
    applicability = meta.get("scan_design_eig_applicability")
    if applicability and applicability != "applicable":
        return f"realized_evidence_{applicability}"
    return None


def _eig_planned_point_counts(inventory) -> tuple[int | None, int | None]:
    """Planned legacy / target-core point counts straight off the in-memory inventory."""
    legacy = target = None
    for candidate in getattr(inventory, "candidates", ()) or ():
        if candidate.candidate_family == FAMILY_LEGACY_UNIFORM and legacy is None:
            legacy = int(candidate.n_points)
        elif candidate.candidate_family == FAMILY_TARGET_CORE and target is None:
            target = int(candidate.n_points)
    return legacy, target


def _log_eig_decision(cfg: CampaignConfig, proposal, *, it: int) -> bool:
    """Emit the one operator-facing ``eig_decision`` summary for an iteration that considered EIG.

    Presentation only: every value is copied from evidence the authority/shadow path already wrote
    (``proposal.meta`` and the in-memory inventory).  Nothing is loaded, recomputed or decided here,
    and the emitter never raises — a campaign must not die because a log line could not be formed.

    ``point_count_basis`` says which counts the payload carries, because the two differ: under an
    authority decision they are the *validated realized* counts, otherwise they are the *planned*
    inventory counts (the realized pair is only content-addressed on disk).

    Returns ``True`` when this iteration's single event slot has been spent (including the failure
    case, so a failed emission is not retried and re-reported at the end of the iteration), and
    ``False`` when the iteration never considered EIG at all.
    """
    try:
        inventory = getattr(proposal, "scan_design_inventory", None)
        mode = None if inventory is None else inventory.context.scan_window_eig_mode
        if mode not in {"shadow", "authoritative"}:
            return False
        meta = proposal.meta if isinstance(proposal.meta, dict) else {}
        decision_ref = meta.get("scan_design_authority_decision_ref")
        decision_sha = (decision_ref.get("sha256") if isinstance(decision_ref, Mapping) else None)
        if decision_sha is not None:
            legacy_points = meta.get("scan_design_authority_legacy_point_count")
            target_points = meta.get("scan_design_authority_target_point_count")
            basis = "authority_decision"
        else:
            legacy_points, target_points = _eig_planned_point_counts(inventory)
            basis = "planned_inventory"
        if mode == "authoritative":
            ready = meta.get("scan_design_authority_status") == "granted"
        else:
            ready = (getattr(getattr(inventory, "shadow_scoring", None), "status", None)
                     == "complete"
                     and meta.get("scan_design_eig_applicability") == "applicable")
        blocker = None if ready else _eig_primary_blocker(inventory, meta)
        eligibility = _eig_eligibility(inventory)
        design = getattr(getattr(inventory, "shadow_scoring", None), "design_measure", None)
        identities = design.get("source_identities") if isinstance(design, Mapping) else None
        master_id = (identities.get("predictive_basis_fingerprint")
                     if isinstance(identities, Mapping) else None)
        log_event(cfg.log_path, "eig_decision", {
            "iter": it,
            "hkl": [proposal.h, proposal.k, proposal.l],
            "mode": mode,
            "master_id": master_id,
            "ready": bool(ready),
            "blocker": blocker,
            "eligibility_status": (None if eligibility is None
                                   else eligibility.get("status")),
            "point_count_basis": basis,
            "legacy_points": legacy_points,
            "target_points": target_points,
            "delta_nats_per_point": meta.get("scan_design_authority_delta"),
            "standard_error_nats": meta.get("scan_design_authority_standard_error"),
            "lower_margin_nats": meta.get("scan_design_authority_lower_margin"),
            "selected_role": meta.get("scan_design_authority_selected_role"),
            "decision_outcome": meta.get("scan_design_authority_outcome"),
            "decision_ref": decision_sha,
        })
        return True
    except Exception as error:  # noqa: BLE001 - an operator line may never break the campaign
        _LOG.exception("could not form the eig_decision operator event")
        try:
            log_event(cfg.log_path, "exception", {
                "action": "eig_decision_event_failed", "severity": "low",
                "error": str(error), "iter": it})
        except Exception:  # noqa: BLE001 - the process logger stays the final visible channel
            _LOG.exception("failed to persist the eig_decision emission failure")
        return True


def _prepare_authoritative_submission(
        cfg: CampaignConfig, objective, proposal, budget_state: dict,
        legacy_validation: dict, *, it: int,
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
                target_requested_points = target_validation.get("requested_points")
                target_feasible_points = _feasible_point_count(
                    target_validation, target_proposal.n_points or 1)
                if target_requested_points is None:
                    # ``planned_feasible_mask`` is positional against the requested point list, so
                    # its length IS the requested count. Without this fallback an adapter that omits
                    # the echoed count hides a partially-infeasible target from the check below.
                    mask = target_validation.get("planned_feasible_mask")
                    if isinstance(mask, list) and mask:
                        target_requested_points = len(mask)
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
                    legacy_adjustments=("authoritative_target_validation_refresh",), force=True)
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
                _restore_proposal(proposal, legacy_snapshot)
                _refresh_realized_comparison(
                    cfg, objective, proposal, budget_state, it=it,
                    legacy_adjustments=("authoritative_authentication_refresh",), force=True)
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


def _persist_action_inventory(cfg: CampaignConfig, inventory, *, it: int, proposal,
                              prev_fingerprint: str | None,
                              force_full: bool) -> tuple[dict | None, str | None]:
    """Log the compact inventory summary every iteration, and persist the full ranked inventory as a
    content-addressed artifact when it is worth keeping: the selected *scientific action* changed, the
    inventory is empty, or this is a forced terminal boundary.

    Returns ``(inventory_ref, selected_fingerprint)``; the ref is ``None`` when no full artifact was
    written. The artifact is content-addressed, so a resume that recomputes the same inventory from the
    same authoritative evidence re-references it rather than duplicating it. The ``inventory`` event
    kind is resume-inert (``_resume_state`` never reads it), keeping the inventory a derived,
    recomputed-on-resume view rather than load-bearing state.
    """
    selected = _selected_action_record(proposal, inventory)
    summary = inventory.summary(selected_action_class=selected["selected_action_class"],
                                registry=GENERATOR_REGISTRY)
    summary.update({key: value for key, value in selected.items()
                    if key != "selected_action_class"})
    fingerprint = selected["selected_scientific_action_fingerprint"]
    # Trigger on the scientific fingerprint, not the broad class: a run of production scans at
    # different q is a different action each time and must not collapse to one persisted artifact.
    write_full = bool(force_full
                      or not inventory.has_eligible_action
                      or fingerprint != prev_fingerprint)
    inventory_ref = None
    if write_full:
        digest = inventory.content_hash()
        path = cfg.workspace / "report" / "inventory" / f"{digest}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        corrupt = path.exists() and not _inventory_artifact_matches(path, digest)
        if corrupt:
            # Never reference a file that does not hash to its own address. The atomic write below
            # rules out partial writes from this process, so a mismatch means external damage: repair
            # it from the inventory in hand (which IS the correct content) and say so.
            log_event(cfg.log_path, "exception", {
                "action": "inventory_artifact_mismatch", "sha256": digest, "iter": it,
                "path": f"report/inventory/{digest}.json",
                "error": "stored artifact did not match its content address; rewritten"})
        if corrupt or not path.exists():
            # Atomic write (temp + fsync + os.replace), matching the certification-artifact pattern:
            # a crash mid-write must never leave a partial file at the content-addressed path, which
            # ``path.exists()`` would then trust forever and reference from the terminal proof.
            temporary = path.with_suffix(".json.tmp")
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(inventory.to_full_dict(), stream, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        inventory_ref = {"kind": "action_inventory", "sha256": digest,
                         "path": f"report/inventory/{digest}.json"}
    payload = {**summary, "iter": it}
    if inventory_ref is not None:
        payload["inventory_ref"] = inventory_ref
    log_event(cfg.log_path, "inventory", payload)
    return inventory_ref, fingerprint


def _compare_returned_realized_grid(
        cfg: CampaignConfig, proposal, submission_payload: dict,
        document: Mapping[str, Any], *, it: int | None) -> tuple[str, str | None]:
    """Authenticate Slice 7 evidence and compare TAVI's returned grid without blocking analysis."""
    authority_ref = submission_payload.get("scan_design_authority_decision_ref")
    if authority_ref is not None:
        try:
            decision = load_authority_decision(authority_ref, cfg.workspace)
            request = decision.selected_request
            if request is None and decision.physical_role == "none":
                return "not_applicable", None
            if request is None:
                raise ValueError("authority decision has no selected physical request")
            if (submission_payload.get("scan_design_authority_decision_fingerprint")
                    != decision.authority_decision_fingerprint
                    or submission_payload.get("scan_design_authority_selected_role")
                    != decision.physical_role
                    or submission_payload.get("proposal_fingerprint")
                    != request["request_fingerprint"]):
                raise ValueError("authority-decision submission identity is inconsistent")
            expected = TAVI_GRID_ADAPTER.canonicalize_returned_energies(
                request["executed_energies"])
            result = document.get("result") if isinstance(document, Mapping) else None
            returned = TAVI_GRID_ADAPTER.canonicalize_returned_energies(
                result.get("scan_values_1") if isinstance(result, Mapping) else None)
        except Exception as error:  # noqa: BLE001 - analysis continues after evidence loss
            _LOG.exception("authority evidence is unavailable for returned-grid comparison")
            log_event(cfg.log_path, "exception", {
                "action": "scan_design_authority_unavailable", "severity": "high",
                "job_id": submission_payload.get("job_id"), "error": str(error), "iter": it,
                "authority_decision_ref": authority_ref,
            })
            return "unavailable", "scan_design_authority_unavailable"
        if returned != expected:
            log_event(cfg.log_path, "exception", {
                "action": "executed_grid_mismatch", "severity": "high",
                "job_id": submission_payload.get("job_id"),
                "reason": "executed grid differs from the authority-selected request",
                "expected_energies": list(expected), "returned_energies": list(returned),
                "authority_decision_ref": authority_ref,
                "authority_decision_fingerprint": decision.authority_decision_fingerprint,
                "selected_role": decision.physical_role,
                "selected_request_fingerprint": request["request_fingerprint"], "iter": it,
            })
            return "mismatch", "executed_grid_mismatch"
        return "matched", None
    selected_grid = submission_payload.get("scan_design_realized_selected_grid_fingerprint")
    if selected_grid is None:
        return "not_applicable", None
    reference = submission_payload.get("scan_design_realized_comparison_ref")
    try:
        comparison = load_scan_design_realized_comparison(reference, cfg.workspace)
        if (submission_payload.get("scan_design_realized_comparison_fingerprint")
                != comparison.realized_comparison_fingerprint
                or submission_payload.get(
                    "scan_design_realized_selected_request_fingerprint")
                != comparison.selected_request_fingerprint
                or selected_grid != comparison.selected_grid_fingerprint
                or (proposal.meta or {}).get("proposal_fingerprint")
                != comparison.selected_request_fingerprint):
            raise ValueError("realized-comparison submission identity is inconsistent")
    except Exception as error:  # noqa: BLE001 - evidence loss must not suppress recovery/analysis
        _LOG.exception("realized-comparison evidence is unavailable for returned-grid comparison")
        log_event(cfg.log_path, "exception", {
            "action": "realized_comparison_unavailable", "severity": "high",
            "job_id": submission_payload.get("job_id"), "error": str(error), "iter": it,
            "comparison_ref": reference,
            "selected_request_fingerprint": submission_payload.get(
                "scan_design_realized_selected_request_fingerprint"),
            "selected_grid_fingerprint": selected_grid,
        })
        return "unavailable", "realized_comparison_unavailable"

    result = document.get("result") if isinstance(document, Mapping) else None
    returned_raw = result.get("scan_values_1") if isinstance(result, Mapping) else None
    try:
        expected = TAVI_GRID_ADAPTER.canonicalize_returned_energies(
            comparison.realized_pair.legacy.executed_energies)
        returned = TAVI_GRID_ADAPTER.canonicalize_returned_energies(returned_raw)
    except (TypeError, ValueError) as error:
        expected = tuple(comparison.realized_pair.legacy.executed_energies)
        returned = ()
        reason = str(error)
    else:
        reason = "executed grid differs from the selected realized grid"
    if returned != expected:
        log_event(cfg.log_path, "exception", {
            "action": "executed_grid_mismatch", "severity": "high",
            "job_id": submission_payload.get("job_id"), "reason": reason,
            "expected_energies": list(expected), "returned_energies": list(returned),
            "comparison_ref": reference,
            "realized_comparison_fingerprint": comparison.realized_comparison_fingerprint,
            "selected_request_fingerprint": comparison.selected_request_fingerprint,
            "selected_grid_fingerprint": comparison.selected_grid_fingerprint, "iter": it,
        })
        return "mismatch", "executed_grid_mismatch"
    return "matched", None


def _finalize_completed_submission(*, cfg: CampaignConfig, objective, proposal,
                                   submission_payload: dict, budget_state: dict,
                                   observations: list[dict], consumed: set, scan_kind: dict,
                                   it: int | None, attempt: int = 0,
                                   data: dict | None = None,
                                   reconciliation_applied: bool = False) -> tuple[list[dict], dict]:
    """Turn one completed physical job into the authoritative analyzed evidence.

    Live scans and crash recovery both enter here.  The function never submits or waits for a job;
    it owns the common fetch/save, executed-cost reconciliation, workspace/provenance update,
    analysis, folding, and pooled-master ledger tail.
    """
    job_id = str(submission_payload["job_id"])
    planned_points = int(submission_payload.get("n_points")
                         or submission_payload.get("requested_points") or proposal.n_points or 0)
    eta_doc = submission_payload.get("eta")
    eta = eta_doc.get("estimated_seconds") if isinstance(eta_doc, dict) else None
    with timed(cfg.log_path, "fetch_save", iteration=it):
        document = data if data is not None else cfg.client.get_scan_data(job_id)
        scan_path = save_scan_json(document, cfg.data_dir)
    grid_status, grid_reason = _compare_returned_realized_grid(
        cfg, proposal, submission_payload, document, it=it)
    submission_payload["scan_design_executed_grid_status"] = grid_status
    submission_payload["scan_design_executed_grid_reason"] = grid_reason
    executed_points = _executed_point_count(document, planned_points)
    if executed_points != planned_points and not reconciliation_applied:
        neutron_delta, seconds_delta = _reconcile_budget_points(
            budget_state, proposal.ncount, planned_points, executed_points, eta=eta)
        log_event(cfg.log_path, "exception", {
            "action": "feasibility_reconciled", "job_id": job_id,
            "hkl": [proposal.h, proposal.k, proposal.l],
            "planned_points": planned_points, "executed_points": executed_points,
            "ncount": int(proposal.ncount),
            "executed_cost": float(executed_points) * float(proposal.ncount),
            "neutron_delta": neutron_delta, "seconds_delta": seconds_delta, "iter": it,
        })

    if not (cfg.workspace / "card.policy.toml").exists():
        with timed(cfg.log_path, "isar_init", iteration=it):
            cfg.init_isar(cfg.workspace, scan_path)
        log_event(cfg.log_path, "decide", {
            "event": "workspace_init", "action": "workspace_init",
            "probe": str(scan_path), "iter": it})
    _persist_campaign_target(cfg.workspace, objective)
    _persist_acquisition_provenance(cfg.workspace, submission_payload)
    tracks_before = _read_branch_tracks(cfg.workspace)

    scan_tier, tier = _errors_tiers_for(proposal, cfg.workspace, cfg)
    promoted = proposal.kind in PRODUCTION_KINDS and tier in ("full", "bayes")
    if promoted:
        log_event(cfg.log_path, "escalate_fit", {
            "phase": "promote", "iter": it, "attempt": attempt,
            "hkl": [proposal.h, proposal.k, proposal.l], "from_tier": "fast",
            "to_tier": tier, "reason": "untrusted_hesse_no_reliable_interval"})
    stage = "full_fit" if tier in ("full", "bayes") else "fast_fit"
    with activity(cfg.log_path, stage, q=[proposal.h, proposal.k, proposal.l],
                  job_id=job_id, fit_tier=tier, iteration=it):
        with timed(cfg.log_path, "isar_run", iteration=it, errors_tier=tier, attempt=attempt):
            with analysis_operation(
                    cfg.log_path, f"reduce:{proposal.acquisition_id}:{tier}:{attempt}",
                    "pooled_reduction_and_fit",
                    limit_seconds=objective.analysis_limit_seconds,
                    allow_overrun_start=True, iteration=it, q=[proposal.h, proposal.k, proposal.l]):
                run_result = cfg.runner.run(
                    cfg.workspace, [cfg.data_dir], errors_tier=tier,
                    scan_errors_tier=scan_tier, master_errors_tier=tier, render=False,
                    **_analysis_runner_kwargs(cfg, objective, finish_initial_wave=True))
                _require_complete_analysis(run_result, objective)
    tracks_after = _read_branch_tracks(cfg.workspace)
    if tracks_after and tracks_after.get("revision") != tracks_before.get("revision"):
        log_event(cfg.log_path, "branch_track_update", {
            "iter": it, "revision": tracks_after.get("revision"),
            "anchor_index": tracks_after.get("anchor_index"),
            "acquisition_seed_index": tracks_after.get("acquisition_seed_index"),
            "reduced_q_origin_index": tracks_after.get("reduced_q_origin_index"),
            "frontiers": tracks_after.get("frontiers") or {}})
        for reassignment in tracks_after.get("reassignments") or []:
            log_event(cfg.log_path, "branch_reassignment", {
                "iter": it, "revision": tracks_after.get("revision"), **dict(reassignment)})
        if (tracks_after.get("frontiers") or {}).get("frontier_blocked"):
            log_event(cfg.log_path, "frontier_blocked", {
                "iter": it, "revision": tracks_after.get("revision"),
                "blocked_indices": (tracks_after.get("frontiers") or {}).get("blocked_indices") or []})

    with timed(cfg.log_path, "fold_rows", iteration=it) as metrics:
        rows = _new_rows(cfg.workspace, consumed)
        scan_cost = float(executed_points) * float(proposal.ncount)
        for index, row in enumerate(rows):
            observations.append(_observation_from_row(
                row, proposal.kind, proposal.ncount, cfg.calibration,
                proposal_cost=scan_cost if index == 0 else 0.0,
                driver_inserted=bool((proposal.meta or {}).get("driver_inserted", False)),
                certification_eligible=bool((proposal.meta or {}).get(
                    "certification_eligible", True)),
                acquisition_id=proposal.acquisition_id,
                count_diagnostics=(proposal.meta or {}).get("count_diagnostics"),
                acquisition_action_role=(proposal.meta or {}).get("acquisition_action_role"),
                target_branch_role=(proposal.meta or {}).get("target_branch_role"),
                rival_branch_role=(proposal.meta or {}).get("rival_branch_role"),
                authoritative_evidence_revision=(proposal.meta or {}).get(
                    "authoritative_evidence_revision"),
                proposal_fingerprint=(proposal.meta or {}).get("proposal_fingerprint"),
                evidence_sink=((proposal.meta or {}).get("evidence_sink")
                               or CANONICAL_MASTER)))
            scan_kind[(row.get("scan"), row.get("detector"))] = (
                proposal.kind, proposal.ncount, proposal.acquisition_id)
        metrics["n_rows"] = len(rows)

    timings = _read_run_timings(cfg.workspace)
    analyze_payload = {"job_id": job_id, "errors_tier": tier,
                       "scan_errors_tier": scan_tier, "master_errors_tier": tier, "iter": it,
                       "n_rows": len(rows),
                       "dispositions": [row.get("disposition") or row.get("verdict")
                                        for row in rows]}
    if timings is not None:
        analyze_payload["timings"] = timings
    log_event(cfg.log_path, "analyze", analyze_payload)
    with activity(cfg.log_path, "pooled_update", q=[proposal.h, proposal.k, proposal.l],
                  job_id=job_id, iteration=it):
        _record_pooled_master_update(
            cfg.workspace, cfg.calibration, proposal, target_branch=objective.target_branch,
            log_path=cfg.log_path, observations=observations)

    if promoted:
        outcome = ("superseded" if not rows else
                   "seated" if any(_row_certifiable(row) for row in rows) else "failed")
        log_event(cfg.log_path, "escalate_fit", {
            "phase": "outcome", "iter": it, "job_id": job_id,
            "hkl": [proposal.h, proposal.k, proposal.l], "to_tier": tier,
            "outcome": outcome, "n_rows": len(rows),
            "dispositions": [row.get("disposition") or row.get("verdict") for row in rows]})
    return rows, document


def _recover_unmatched_submissions(*, cfg: CampaignConfig, objective,
                                   unmatched: list[dict], budget_state: dict,
                                   observations: list[dict], consumed: set,
                                   scan_kind: dict) -> str | None:
    """Recover durable physical submissions without ever issuing another submission.

    Returns a human reason when one job remains non-terminal or unreachable.  The caller must stop as
    ``pending_recovery`` in that case; completed and terminal-failure jobs are finalized in order.
    """
    for item in sorted(unmatched, key=lambda entry: int(entry.get("order", 0))):
        payload = dict(item.get("payload") or {})
        job_id = str(payload.get("job_id") or "")
        proposal = _proposal_from_submission(payload)
        data = None
        raw_path = scan_json_path(job_id, cfg.data_dir)
        if raw_path.exists():
            try:
                data = json.loads(raw_path.read_text(encoding="utf-8"))
                if str(data.get("job_id")) != job_id or "result" not in data:
                    raise ValueError("saved raw scan does not match the submitted job")
            except (OSError, ValueError) as error:
                data = None
                log_event(cfg.log_path, "exception", {
                    "action": "recovery_raw_read_failed", "job_id": job_id,
                    "path": str(raw_path), "error": str(error), "iter": None})

        state = "done" if data is not None else None
        if state is None:
            try:
                snapshot = cfg.client.get_scan(job_id)
                state = str(snapshot.get("state") or "")
                if state not in {"done", "failed", "stopped", "cancelled"}:
                    snapshot = cfg.client.wait_scan(job_id, timeout=cfg.wait_timeout)
                    state = str(snapshot.get("state") or "")
            except Exception as error:  # noqa: BLE001 — uncertainty must stop, never resubmit
                reason = f"job {job_id} recovery is pending: instrument unreachable ({error})"
                log_event(cfg.log_path, "recovery_pending", {
                    "job_id": job_id, "state": state or "unknown", "decline": False,
                    "reason": reason, "acquisition_id": proposal.acquisition_id})
                return reason

        if state in {"failed", "stopped", "cancelled"}:
            observations.append(_declined_observation(proposal, state))
            log_event(cfg.log_path, "exception", {
                "action": "scan_not_done", "job_id": job_id, "state": state,
                "error": "reconstructed during recovery", "iter": None})
            continue
        if state != "done":
            reason = f"job {job_id} recovery is pending: instrument state {state or 'unknown'}"
            log_event(cfg.log_path, "recovery_pending", {
                "job_id": job_id, "state": state or "unknown", "decline": False,
                "reason": reason, "acquisition_id": proposal.acquisition_id})
            return reason

        if data is None:
            try:
                data = cfg.client.get_scan_data(job_id)
            except Exception as error:  # noqa: BLE001 — completed job data remains safely retryable
                reason = f"job {job_id} recovery is pending: scan data unavailable ({error})"
                log_event(cfg.log_path, "recovery_pending", {
                    "job_id": job_id, "state": "done", "decline": False,
                    "reason": reason, "acquisition_id": proposal.acquisition_id})
                return reason

        rows, _document = _finalize_completed_submission(
            cfg=cfg, objective=objective, proposal=proposal,
            submission_payload=payload, budget_state=budget_state,
            observations=observations, consumed=consumed, scan_kind=scan_kind,
            it=None, data=data, reconciliation_applied=bool(item.get("reconciled", False)))
        log_event(cfg.log_path, "recovery_completed", {
            "job_id": job_id, "state": "done", "n_rows": len(rows),
            "acquisition_id": proposal.acquisition_id, "resubmitted": False})
    return None


def _execute_proposal(*, cfg: CampaignConfig, objective, proposal, it: int, scan_counter: int,
                      budget_state: dict, observations: list[dict], consumed: set,
                      scan_kind: dict) -> tuple[list[dict], int, str | None]:
    """Run one proposed scan to a folded result, with bounded missed-window re-centering (a LOOK, §1).

    Owns the inner validate → submit → wait → fetch/save → (lazy init) → ``isar run`` → fold → analyze
    loop and its recenter/widen retries. Mutates ``observations`` (appends folded or declined rows),
    ``consumed`` (via :func:`_new_rows`), ``scan_kind``, ``budget_state``, and ``proposal`` (e_lo/e_hi/
    n_points on recenter) in place; returns ``(rows, scan_counter, declined_verdict)`` — the final
    folded rows (empty on a decline), the advanced scan counter, and the decline verdict (``None`` on
    success) that :func:`classify_rejection` turns into a durable consequence. Every ``timed`` span and
    ``log_event`` payload is verbatim from the original loop body (the profiling / event contract is
    part of the identity gate)."""
    client, runner = cfg.client, cfg.runner
    log_path, workspace, data_dir = cfg.log_path, cfg.workspace, cfg.data_dir
    # ---- run this proposal, with bounded missed-window re-centering (a LOOK, §1) ----------
    attempt = 0
    rows: list[dict] = []
    support_adjusted = False
    feasibility_trimmed = False
    # The request as it stood before a feasibility trim. A decline after the trim charges the
    # *trimmed* fingerprint, so without this the untrimmed original stays unsuppressed and the
    # selector re-emits it next iteration, repeating trim → decline for the rest of the campaign.
    pre_trim_request_fingerprint: str | None = None
    declined_verdict: str | None = None
    # One operator-facing EIG line per iteration, wherever this iteration's EIG story ends: right
    # after the authority path concludes when it gets that far, otherwise at the single exit below
    # (a declined or unvalidatable request still owes the operator a "why nothing happened").
    eig_event_emitted = False
    while True:
        # Single checkpoint before EVERY validate/submit, including support/recenter retries.
        previous_request_fingerprint = (proposal.meta or {}).get("proposal_fingerprint")
        try:
            _finalize_proposal(proposal, objective, cfg, budget_state, it=it)
        except InstrumentPointLimitExceeded as error:
            log_event(log_path, "exception", {
                "action": "instrument_point_limit_exceeded", "phase": "execution_finalization",
                "error": str(error), "hkl": [proposal.h, proposal.k, proposal.l], "iter": it})
            observations.append(
                _declined_observation(proposal, "instrument_point_limit_exceeded"))
            declined_verdict = "instrument_point_limit_exceeded"
            break
        _refresh_realized_comparison(
            cfg, objective, proposal, budget_state, it=it,
            legacy_adjustments=("execution_time_finalization",))
        _record_scan_design_realization(
            cfg,
            proposal,
            adjustment="execution_time_finalization_clamp",
            previous_request_fingerprint=previous_request_fingerprint,
            it=it,
            attempt=attempt,
        )
        finalized = finalized_proposal(proposal)
        with timed(log_path, "validate", iteration=it, attempt=attempt):
            val = client.validate(_validate_body(proposal, sample=cfg.sample,
                                                 background=cfg.sample_background_frozen))
        # Stage 3: freeze the geometry this exact request was judged under, before any branch below
        # decides what the verdict means. PR2.3 reads ``attempt.status`` to class the rejection.
        attempt_record = validated_attempt(
            finalized, val, status=("would_queue" if val.get("would_queue", False)
                                    else "validation_rejected"))
        manifest_fingerprint = (attempt_record.feasibility_manifest_fingerprint
                                if attempt_record is not None
                                else _feasibility_manifest_fingerprint(val))
        feasible_points = _feasible_point_count(val, proposal.n_points or 1)
        target_band = (proposal.meta or {}).get("target_band")
        min_support = int((getattr(objective, "steering", None) or {}).get(
            "minimum_target_support_points", 0))
        target_support = _target_supporting_feasible_points(val, target_band)
        log_event(log_path, "validate", {"would_queue": val.get("would_queue"),
                                          "blockers": val.get("blockers"), "eta": val.get("eta"),
                                          "requested_points": val.get("requested_points"),
                                          "feasible_points": feasible_points,
                                          "target_supporting_feasible_points": target_support,
                                          "partial": val.get("partial"),
                                          "feasible_segments": val.get("feasible_segments"),
                                          "infeasible": val.get("infeasible"),
                                          "request_fingerprint": (
                                              finalized.request_fingerprint
                                              if finalized is not None else None),
                                          "feasibility_manifest_fingerprint": manifest_fingerprint,
                                          "iter": it})
        # ---- partial infeasibility: trim the window to the instrument's reachable run -----------
        # TAVI refuses a partially-infeasible scan command outright (HTTP 400 ``infeasible_points``),
        # so a grid whose edge point cannot close the scattering triangle is unsubmittable as-is even
        # though the physics of interest is perfectly reachable. Trimming to the largest contiguous
        # feasible run keeps the campaign moving; the alternative — submitting anyway — cost a whole
        # authoritative campaign zero executed scans. This is a *request* adjustment, exactly like the
        # step-shrink below: the q itself is never charged for it.
        requested_points = int(val.get("requested_points") or proposal.n_points or 0)
        # Only a feasibility-shaped verdict may move the window. A refusal for an unrelated reason
        # (a full queue, an exceeded neutron budget) can still carry a partial mask, and trimming on
        # it would silently rewrite the request while answering none of the actual blocker.
        trim_admissible = bool(val.get("would_queue", False)) or _is_feasibility_shaped_refusal(val)
        if 0 < feasible_points < requested_points and trim_admissible:
            # The energy this scan is *about* — for a rival probe that is the rival centre, not the
            # target model's prediction, which is exactly why ``e_pred`` and not ``target_band`` is
            # the primary signal. The acceptance interval is the target band when the band actually
            # describes this scan, and a resolution width around ``e_pred`` otherwise.
            target_energy = (float(proposal.e_pred)
                             if isinstance(proposal.e_pred, (int, float))
                             and not isinstance(proposal.e_pred, bool)
                             and math.isfinite(float(proposal.e_pred)) else None)
            def _in_window(value) -> bool:
                return (value is not None
                        and float(proposal.e_lo) - 1e-9 <= float(value) <= float(proposal.e_hi)
                        + 1e-9)

            if not _in_window(target_energy):
                # A survey quicklook leaves ``e_pred`` at 0.0 and searches a whole band; the
                # hypothesis' own centre is then what the window is about. With neither, the
                # proposal declares no target and longest-run is the honest choice.
                target_energy = None
                if target_band is not None and _in_window(float(target_band[0])):
                    target_energy = float(target_band[0])
            acceptance = None
            if target_energy is not None:
                # A resolution width, deliberately NOT the ±2σ credible band. The question here is
                # "can this window still see the excitation", and a window touching the far tail of a
                # broad prior answers no — the shipped optic prior is wide enough that a 2σ test
                # accepts a window five resolution widths away from the peak.
                half_width = max(float(proposal.fwhm_dE or 0.0), float(proposal.e_step), 1e-6)
                acceptance = (target_energy - half_width, target_energy + half_width)
            # The exact point list the instrument masked, at its own execution precision — never a
            # re-derived ``e_lo + i·step``, which drifts from the wire values the mask indexes.
            grid_energies = TAVI_GRID_ADAPTER.executed_energies(
                proposal.e_lo, proposal.e_step, requested_points)
            run, run_decision = _select_feasible_run(
                val, grid_energies, target_energy=target_energy, acceptance=acceptance)
            run_length = 0 if run is None else run[1] - run[0] + 1
            # "Nothing here is a scan" is a more basic fact than "nothing here is your peak", so the
            # floor is judged against the whole manifest and reported first — otherwise a manifest
            # with no usable run at all would be filed under the target-coverage verdict.
            all_runs = _feasible_runs(val, requested_points) or []
            longest_run_length = max((end - start + 1 for start, end in all_runs), default=0)
            if (all_runs and longest_run_length < FEASIBLE_TRIM_MIN_POINTS) or (
                    run is not None and run_length < FEASIBLE_TRIM_MIN_POINTS):
                # Request scope, not geometry: this window clipped the reachable band down to
                # (nearly) nothing, but a shifted or widened window at the same Q may still be
                # fully measurable — the widen/re-center ladder must stay open. Only a
                # zero-feasible verdict condemns the geometry itself.
                log_event(log_path, "exception", {
                    "action": "feasible_run_below_minimum", "phase": "validation",
                    "hkl": [proposal.h, proposal.k, proposal.l], "blockers": val.get("blockers"),
                    "reason": "largest_feasible_run_below_minimum",
                    "feasible_points": feasible_points,
                    "requested_points": requested_points,
                    "feasible_run_points": max(run_length, longest_run_length),
                    "minimum_points": FEASIBLE_TRIM_MIN_POINTS,
                    "planned_feasible_mask": val.get("planned_feasible_mask"),
                    "iter": it})
                _mark_request_superseded(proposal, pre_trim_request_fingerprint)
                observations.append(_declined_observation(proposal, "validation_rejected"))
                declined_verdict = "validation_rejected"
                break
            if run_decision == "target_excluded":
                # Every reachable stretch misses the excitation. Submitting the longest run would buy
                # a clean, cheap, scientifically empty scan; refuse the request and let the
                # widen/re-center ladder propose a window the instrument can actually measure.
                log_event(log_path, "exception", {
                    "action": "feasible_run_excludes_target", "phase": "validation",
                    "hkl": [proposal.h, proposal.k, proposal.l], "blockers": val.get("blockers"),
                    "reason": "no_feasible_run_overlaps_the_declared_target",
                    "target_energy": target_energy,
                    "acceptance": list(acceptance) if acceptance is not None else None,
                    "feasible_points": feasible_points,
                    "requested_points": requested_points,
                    "planned_feasible_mask": val.get("planned_feasible_mask"),
                    "iter": it})
                _mark_request_superseded(proposal, pre_trim_request_fingerprint)
                observations.append(_declined_observation(proposal, "validation_rejected"))
                declined_verdict = "validation_rejected"
                break
            if run is not None and feasibility_trimmed:
                # One trim per proposal. A second partial verdict on an already-trimmed window means
                # the instrument's answer is not converging, so refuse *this request* and let the
                # ladder try a different window rather than looping on the same geometry.
                log_event(log_path, "exception", {
                    "action": "feasibility_trim_did_not_converge",
                    "hkl": [proposal.h, proposal.k, proposal.l],
                    "feasible_points": feasible_points,
                    "requested_points": requested_points,
                    "planned_feasible_mask": val.get("planned_feasible_mask"),
                    "iter": it})
                _mark_request_superseded(proposal, pre_trim_request_fingerprint)
                observations.append(_declined_observation(proposal, "validation_rejected"))
                declined_verdict = "validation_rejected"
                break
            if run is not None:
                old_e_lo, old_e_hi = float(proposal.e_lo), float(proposal.e_hi)
                e_step = float(proposal.e_step)
                previous_request_fingerprint = (proposal.meta or {}).get(
                    "proposal_fingerprint")
                pre_trim_request_fingerprint = previous_request_fingerprint
                proposal.e_lo = float(grid_energies[run[0]])
                proposal.e_hi = float(grid_energies[run[1]])
                # ``_finalize_proposal`` re-derives ``n_points`` from the mutated window through
                # ``_guard_point_count``, so the trimmed grid is re-costed and re-fingerprinted the
                # same way the step-shrink path is.
                _finalize_proposal(proposal, objective, cfg, budget_state, it=it)
                _refresh_realized_comparison(
                    cfg, objective, proposal, budget_state, it=it,
                    legacy_adjustments=("validation_feasibility_trim",), force=True)
                _record_scan_design_realization(
                    cfg,
                    proposal,
                    adjustment="validation_feasibility_trim",
                    previous_request_fingerprint=previous_request_fingerprint,
                    it=it,
                    attempt=attempt,
                )
                feasibility_trimmed = True
                log_event(log_path, "feasibility_trim", {
                    "hkl": [proposal.h, proposal.k, proposal.l],
                    "old_e_lo": old_e_lo, "old_e_hi": old_e_hi,
                    "new_e_lo": float(proposal.e_lo), "new_e_hi": float(proposal.e_hi),
                    "e_step": e_step,
                    "dropped_point_count": int(requested_points - run_length),
                    "requested_points": requested_points,
                    "feasible_points": feasible_points,
                    "feasible_run": [run[0], run[1]],
                    "run_selection": run_decision,
                    "target_energy": target_energy,
                    "previous_request_fingerprint": previous_request_fingerprint,
                    "request_fingerprint": (proposal.meta or {}).get("proposal_fingerprint"),
                    "n_points": int(proposal.n_points or 0),
                    "planned_feasible_mask": val.get("planned_feasible_mask"),
                    "iter": it, "attempt": attempt})
                continue
            # No localization: fall through. The zero-feasible branch and the submit-time
            # ``infeasible_points`` fallback below both remain in force.
            _LOG.warning(
                "validation reported %d of %d points feasible with no positional manifest; "
                "submitting unchanged", feasible_points, requested_points)
        if (min_support > 0 and target_support is not None and target_support < min_support
                and val.get("would_queue", False)):
            mean, sigma = map(float, target_band)
            desired_step = max((4.0 * sigma) / max(min_support - 1, 1), 1e-6)
            if not support_adjusted and desired_step < proposal.e_step:
                old_step = proposal.e_step
                previous_request_fingerprint = (proposal.meta or {}).get(
                    "proposal_fingerprint")
                proposal.e_step = desired_step
                _finalize_proposal(proposal, objective, cfg, budget_state, it=it)
                _refresh_realized_comparison(
                    cfg, objective, proposal, budget_state, it=it,
                    legacy_adjustments=("minimum_target_support",), force=True)
                _record_scan_design_realization(
                    cfg,
                    proposal,
                    adjustment="minimum_target_support",
                    previous_request_fingerprint=previous_request_fingerprint,
                    it=it,
                    attempt=attempt,
                )
                support_adjusted = True
                log_event(log_path, "exception", {
                    "action": "increase_target_support", "hkl": [proposal.h, proposal.k, proposal.l],
                    "old_step": old_step, "requested_step": desired_step,
                    "new_step": proposal.e_step, "finalized_step": proposal.e_step,
                    "target_supporting_feasible_points": target_support,
                    "required": min_support, "iter": it})
                continue
            log_event(log_path, "exception", {
                "action": "insufficient_target_support", "hkl": [proposal.h, proposal.k, proposal.l],
                "target_supporting_feasible_points": target_support,
                "required": min_support, "iter": it})
            observations.append(_declined_observation(proposal, "insufficient_target_support"))
            declined_verdict = "insufficient_target_support"
            break
        if not val.get("would_queue", False):
            if feasible_points <= 0 and int(val.get("requested_points") or proposal.n_points or 0) > 0:
                log_event(log_path, "exception", {
                    "action": "geometry_unreachable", "phase": "validation",
                    "hkl": [proposal.h, proposal.k, proposal.l], "blockers": val.get("blockers"),
                    "iter": it})
                observations.append(_declined_observation(proposal, "geometry_unreachable"))
                declined_verdict = "geometry_unreachable"
                break
            v_act = exc.handle_validation_rejection(val.get("blockers") or [])
            log_event(log_path, "exception", {**v_act.to_dict(), "iter": it})
            observations.append(_declined_observation(proposal, "validation_rejected"))
            declined_verdict = "validation_rejected"
            break

        legacy_validation = val
        val = _prepare_authoritative_submission(
            cfg, objective, proposal, budget_state, legacy_validation, it=it)
        if not eig_event_emitted:
            eig_event_emitted = _log_eig_decision(cfg, proposal, it=it)
        finalized = finalized_proposal(proposal)
        manifest_fingerprint = (validated_attempt(
            finalized, val, status=("would_queue" if val.get("would_queue", False)
                                    else "validation_rejected")).feasibility_manifest_fingerprint
            if finalized is not None else _feasibility_manifest_fingerprint(val))
        feasible_points = _feasible_point_count(val, proposal.n_points or 1)

        with timed(log_path, "submit", iteration=it, attempt=attempt) as _m:
            _authenticate_realized_before_submission(
                cfg, objective, proposal, budget_state, it=it)
            if (proposal.meta or {}).get("scan_design_authority_selected_role") in {
                    "legacy", "target_core"}:
                try:
                    authenticate_authority_submission(proposal, cfg.workspace)
                except Exception as error:  # noqa: BLE001 - final fail-safe is exact legacy
                    _LOG.exception(
                        "authority identity changed at the submission boundary; restoring legacy")
                    _restore_authenticated_legacy_request(cfg, proposal)
                    val = legacy_validation
                    finalized = finalized_proposal(proposal)
                    manifest_fingerprint = (validated_attempt(
                        finalized, val, status="would_queue").feasibility_manifest_fingerprint
                        if finalized is not None else _feasibility_manifest_fingerprint(val))
                    feasible_points = _feasible_point_count(val, proposal.n_points or 1)
                    log_event(log_path, "exception", {
                        "action": "scan_design_authority_late_authentication_failed",
                        "severity": "high", "error": str(error), "iter": it,
                        "recovery": "authenticated_legacy_request",
                    })
            body = _scan_body(proposal, cfg.engine, cfg.base_seed + scan_counter,
                              sample=cfg.sample, background=cfg.sample_background_frozen)
            idempotency_key = f"{objective.name or 'campaign'}-{cfg.run_nonce}-{scan_counter}"
            log_event(log_path, "submission_intent", {
                "iter": it,
                "attempt": attempt,
                "idempotency_key": idempotency_key,
                "proposal_fingerprint": (proposal.meta or {}).get("proposal_fingerprint"),
                "scan_design_authority_decision_ref": (proposal.meta or {}).get(
                    "scan_design_authority_decision_ref"),
                "scan_design_authority_decision_fingerprint": (proposal.meta or {}).get(
                    "scan_design_authority_decision_fingerprint"),
                "scan_design_authority_selected_role": (proposal.meta or {}).get(
                    "scan_design_authority_selected_role"),
            })
            try:
                submit = client.submit_scan(
                    body,
                    idempotency_key=idempotency_key)
            except TaviError as error:
                code = _tavi_error_code(error)
                if error.status == 400 and code == "all_points_infeasible":
                    log_event(log_path, "exception", {
                        "action": "geometry_unreachable",
                        "error": str(error),
                        "hkl": [proposal.h, proposal.k, proposal.l],
                        "iter": it,
                    })
                    observations.append(
                        _declined_observation(proposal, "geometry_unreachable"))
                    declined_verdict = "all_points_infeasible"
                    break
                if error.status == 400 and code == "infeasible_points":
                    # Partial infeasibility that validation did not catch or could not localize.
                    # The q is reachable — only this exact grid is not — so the consequence is scoped
                    # to the request. Loud, because reaching here means the validation-time trim
                    # above did not fire and the manifest contract deserves a look.
                    _LOG.warning(
                        "instrument refused a partially-infeasible grid at submit; "
                        "validation should have trimmed it")
                    log_event(log_path, "exception", {
                        "action": "infeasible_points_at_submit",
                        "severity": "high",
                        "error": str(error),
                        "hkl": [proposal.h, proposal.k, proposal.l],
                        "e_lo": float(proposal.e_lo), "e_hi": float(proposal.e_hi),
                        "e_step": float(proposal.e_step),
                        "n_points": int(proposal.n_points or 0),
                        "note": ("validation reported the grid submittable; the trim at validation "
                                 "time did not fire, so only this request is suppressed"),
                        "iter": it,
                    })
                    observations.append(
                        _declined_observation(proposal, "infeasible_points_at_submit"))
                    declined_verdict = "infeasible_points_at_submit"
                    break
                raise
            scan_counter += 1
            job_id = submit.get("job_id")
            _m["job_id"] = job_id
        eta = (submit.get("eta") or {}).get("estimated_seconds")
        _charge_budget(budget_state, proposal, eta, n_points=feasible_points)
        # The run event doubles as the durable SPEND LEDGER for crash resume: kind/ncount/n_points/
        # hkl + the charged cost (= n_points×ncount) let a restart recover budget + scan_counter.
        submission_payload = {
            "job_id": job_id, "state": submit.get("state"), "eta": submit.get("eta"),
            "attempt": attempt, "acquisition_id": proposal.acquisition_id,
            "kind": proposal.kind, "ncount": int(proposal.ncount),
            "submitted_ncount": int(proposal.ncount),
            "requested_points": int(proposal.n_points or 0), "n_points": feasible_points,
            "hkl": [proposal.h, proposal.k, proposal.l],
            "acquisition_target_energy": float(proposal.e_pred),
            # The searched energy window. For an auxiliary detection probe a trustworthy clean null over
            # this window is negative evidence the tracker consumes; persisted here so it survives a
            # crash-resume finalize rather than being re-derived by hkl-nearness.
            "search_energy_lo": float(proposal.e_lo),
            "search_energy_hi": float(proposal.e_hi),
            "search_energy_step": float(proposal.e_step),
            "driver_inserted": bool((proposal.meta or {}).get("driver_inserted", False)),
            "certification_eligible": bool((proposal.meta or {}).get(
                "certification_eligible", True)),
            "insertion_purpose": (proposal.meta or {}).get("insertion_purpose"),
            "count_diagnostics": dict((proposal.meta or {}).get("count_diagnostics") or {}),
            "acquisition_action_role": (proposal.meta or {}).get("acquisition_action_role"),
            "target_branch_role": (proposal.meta or {}).get("target_branch_role"),
            "rival_branch_role": (proposal.meta or {}).get("rival_branch_role"),
            "evidence_sink": (proposal.meta or {}).get("evidence_sink") or CANONICAL_MASTER,
            "authoritative_evidence_revision": (proposal.meta or {}).get(
                "authoritative_evidence_revision"),
            "proposal_fingerprint": (proposal.meta or {}).get("proposal_fingerprint"),
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
            # The other two lifecycle identities, so a submitted scan joins back to the scientific
            # question it answers and the geometry its request was accepted under.
            "scientific_action_fingerprint": (proposal.meta or {}).get(
                "scientific_action_fingerprint"),
            "feasibility_manifest_fingerprint": manifest_fingerprint,
            "cost": float(feasible_points) * float(proposal.ncount), "iter": it,
        }
        log_event(log_path, "run", submission_payload)
        log_event(log_path, "physical_submission", submission_payload)

        with activity(log_path, "scan", q=[proposal.h, proposal.k, proposal.l],
                      job_id=job_id, iteration=it):
            with timed(log_path, "wait_scan", iteration=it, job_id=job_id) as _m:
                snap = client.wait_scan(job_id, timeout=cfg.wait_timeout)
                _m["state"] = snap.get("state")
        if snap.get("state") != "done":
            log_event(log_path, "exception", {"action": "scan_not_done", "job_id": job_id,
                                              "state": snap.get("state"), "error": snap.get("error"),
                                              "iter": it})
            observations.append(_declined_observation(proposal, str(snap.get("state"))))
            # A job that never reached ``done`` says nothing about the science: it classes as
            # transient (the fail-open default) and suppresses nothing.
            declined_verdict = f"scan_not_done:{snap.get('state')}"
            break

        rows, data = _finalize_completed_submission(
            cfg=cfg, objective=objective, proposal=proposal,
            submission_payload=submission_payload, budget_state=budget_state,
            observations=observations, consumed=consumed, scan_kind=scan_kind,
            it=it, attempt=attempt)

        target_delivered = _pooled_target_delivered(
            observations, proposal, getattr(objective, "target_branch", "acoustic"))
        clean_null = _auxiliary_clean_null(proposal, rows)
        if not _rows_missed(rows) or target_delivered or clean_null:
            if clean_null and _rows_missed(rows):
                log_event(log_path, "decide", {
                    "event": "clean_null_suppressed", "action": "missed_window_suppressed",
                    "reason": "auxiliary identity probe returned a trustworthy clean null; the rival "
                              "was not detected in the searched window — not a missed window",
                    "hkl": [proposal.h, proposal.k, proposal.l], "iter": it})
            elif target_delivered and _rows_missed(rows):
                log_event(log_path, "decide", {
                    "event": "missed_window_suppressed", "action": "missed_window_suppressed",
                    "reason": "pooled master delivered the selected target mode",
                    "hkl": [proposal.h, proposal.k, proposal.l], "iter": it})
            break
        # Missed window: LOOK at the counts to re-center + widen, up to the attempt cap.
        with timed(log_path, "missed_window", iteration=it):
            result = data.get("result") or {}
            pooled_inputs = _pooled_recenter_inputs(
                cfg.workspace, (proposal.h, proposal.k, proposal.l))
            recenter_counts, recenter_energy = (pooled_inputs if pooled_inputs is not None else
                                                (result.get("counts") or [],
                                                 result.get("scan_values_1") or []))
            act = exc.handle_missed_window(
                recenter_counts, recenter_energy,
                e_lo=proposal.e_lo, e_hi=proposal.e_hi, attempt=attempt,
                resolution_fwhm=(proposal.fwhm_dE or None))
        log_event(log_path, "exception", {**act.to_dict(), "iter": it})
        # Both `recenter` and `recenter_refused_elastic` mutate the window (act.params carry e_lo/e_hi)
        # and re-enter the loop; the elastic refusal widens about the OLD center rather than chasing
        # ΔE ≈ 0. Any other action (escalate) ends the retry loop.
        if act.action not in ("recenter", "recenter_refused_elastic"):
            break
        previous_request_fingerprint = (proposal.meta or {}).get("proposal_fingerprint")
        _invalidate_realized_comparison_for_recovery(proposal)
        proposal.e_lo = act.params["e_lo"]
        proposal.e_hi = act.params["e_hi"]
        try:
            _finalize_proposal(proposal, objective, cfg, budget_state, it=it)
        except InstrumentPointLimitExceeded as error:
            proposal.n_points, _sanitized_step = resolve_point_count(
                proposal.e_lo, proposal.e_hi, proposal.e_step,
                adapter=TAVI_GRID_ADAPTER)
            _stamp_proposal_provenance(proposal)
            _record_scan_design_realization(
                cfg, proposal, adjustment="missed_window_recovery_point_limit_refusal",
                previous_request_fingerprint=previous_request_fingerprint,
                it=it, attempt=attempt + 1)
            log_event(log_path, "exception", {
                "action": "instrument_point_limit_exceeded", "phase": "missed_window_recovery",
                "error": str(error), "hkl": [proposal.h, proposal.k, proposal.l], "iter": it})
            observations.append(
                _declined_observation(proposal, "instrument_point_limit_exceeded"))
            declined_verdict = "instrument_point_limit_exceeded"
            break
        _record_scan_design_realization(
            cfg,
            proposal,
            adjustment=(
                "missed_window_recenter"
                if act.action == "recenter"
                else "missed_window_elastic_refusal_widening"
            ),
            previous_request_fingerprint=previous_request_fingerprint,
            it=it,
            attempt=attempt + 1,
        )
        attempt += 1

    if not eig_event_emitted:
        _log_eig_decision(cfg, proposal, it=it)
    return rows, scan_counter, declined_verdict


def _certification_revision(point: dict) -> str | None:
    revision = point.get("master_revision")
    return str(revision) if revision not in (None, "") else None


def _certification_decision_signature(decision) -> tuple:
    """Stable scientific state used only to prevent zero-progress certification loops."""
    return tuple((tuple(point.get("hkl") or ()), _certification_revision(point),
                  point.get("calibrated_halfwidth"), point.get("assessment_state"),
                  bool(point.get("converged", False)))
                 for point in decision.per_q or ())


def _certification_acceptance_path(workspace: Path) -> Path:
    return workspace / "report" / "certification" / "accepted.json"


def _certification_acceptance_contract(cfg: CampaignConfig, tier: str) -> str:
    policy_path = cfg.workspace / "card.policy.toml"
    policy_sha = (hashlib.sha256(policy_path.read_bytes()).hexdigest()
                  if policy_path.is_file() else None)
    state_path = cfg.workspace / "card.state.json"
    state = (json.loads(state_path.read_text(encoding="utf-8"))
             if state_path.is_file() else {})
    calibration = getattr(cfg.calibration, "raw", None) or {
        "inflation": getattr(cfg.calibration, "inflation", None),
        "coverage68": getattr(cfg.calibration, "coverage68", None),
        "center_pull_mean": getattr(cfg.calibration, "center_pull_mean", None),
        "bucket_by": getattr(cfg.calibration, "bucket_by", None),
        "buckets": getattr(cfg.calibration, "buckets", None),
        "truth_source": getattr(cfg.calibration, "truth_source", None),
    }
    payload = {
        "tier": tier, "policy_sha256": policy_sha,
        "calibration": calibration, "floor_meV": cfg.floor,
        # These are global scientific identities. Running dispersion/branch beliefs are deliberately
        # excluded: unchanged q certificates survive evidence arriving at another q, while a changed
        # calibration, resolution, hypothesis, or campaign target invalidates them fail-closed.
        "state_identity": {
            "resolution_versions": state.get("resolution_versions"),
            "energy_zero_series": state.get("energy_zero_series"),
            "hypothesis_snapshot": state.get("hypothesis_snapshot"),
            "campaign_target": state.get("campaign_target"),
        },
    }
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, default=str,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _certification_point_contexts(cfg: CampaignConfig) -> dict[tuple[float, float, float], str]:
    """Fingerprint resolved per-q inputs that may change without changing pooled membership."""
    results_path = cfg.workspace / "report" / "results.csv"
    if not results_path.is_file():
        return {}
    fields = (
        "zero", "zero_source", "gauss_fwhm", "lorentz_fwhm",
        "resolution_source", "resolution_version", "prior_omega", "prior_sigma",
        "branch_id", "target_branch", "target_label", "spectrum_ref", "content_hash",
        "master_revision", "n_members", "members",
    )
    contexts = {}
    for row in read_results_csv(cfg.workspace):
        hkl = _row_hkl(row)
        if not row.get("pooled") or hkl is None:
            continue
        payload = {field: row.get(field) for field in fields}
        contexts[tuple(float(value) for value in hkl)] = hashlib.sha256(json.dumps(
            payload, sort_keys=True, default=str,
            separators=(",", ":")).encode("utf-8")).hexdigest()
    return contexts


def _load_certification_acceptance(cfg: CampaignConfig) -> dict:
    path = _certification_acceptance_path(cfg.workspace)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        log_event(cfg.log_path, "exception", {
            "action": "certification_acceptance_read_failed", "path": str(path),
            "error": str(error)})
        return {}
    if payload.get("schema") != "isar.certification.accepted/1":
        log_event(cfg.log_path, "exception", {
            "action": "certification_acceptance_invalid", "path": str(path),
            "schema": payload.get("schema")})
        return {}
    return {str(item.get("objective_index")): item
            for item in payload.get("points") or ()
            if item.get("objective_index") is not None}


def _pending_certification_indices(cfg: CampaignConfig, decision, tier: str) -> list[int]:
    """Return objective indices whose current master revision lacks a passing certificate."""
    accepted = _load_certification_acceptance(cfg)
    contract = _certification_acceptance_contract(cfg, tier)
    contexts = _certification_point_contexts(cfg)
    pending = []
    for index, point in enumerate(decision.per_q or ()):
        revision = _certification_revision(point)
        prior = accepted.get(str(index))
        hkl = tuple(float(value) for value in point.get("hkl") or ())
        if (revision is None or prior is None or prior.get("tier") != tier
                or prior.get("contract_fingerprint") != contract
                or prior.get("point_context_fingerprint") != contexts.get(hkl)
                or prior.get("master_revision") != revision
                or list(prior.get("hkl") or ()) != list(point.get("hkl") or ())):
            pending.append(index)
    return pending


def _record_certification_acceptance(cfg: CampaignConfig, decision, tier: str) -> None:
    """Atomically retain only currently passing, revision-matched certification points."""
    points = []
    contract = _certification_acceptance_contract(cfg, tier)
    contexts = _certification_point_contexts(cfg)
    for index, point in enumerate(decision.per_q or ()):
        revision = _certification_revision(point)
        if not point.get("converged", False) or revision is None:
            continue
        points.append({
            "objective_index": index, "hkl": list(point.get("hkl") or ()),
            "master_revision": revision, "tier": tier,
            "contract_fingerprint": contract,
            "point_context_fingerprint": contexts.get(
                tuple(float(value) for value in point.get("hkl") or ())),
            "calibrated_halfwidth": point.get("calibrated_halfwidth"),
            "tolerance": point.get("tolerance"),
        })
    path = _certification_acceptance_path(cfg.workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema": "isar.certification.accepted/1", "points": points}
    temporary = path.with_suffix(".json.tmp")
    try:
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        log_event(cfg.log_path, "exception", {
            "action": "certification_acceptance_write_failed", "path": str(path),
            "error": str(error)})
        raise


def _certification_request(cfg: CampaignConfig, objective: ObjectiveSpec,
                           decision, tier: str, *, activity_id: str,
                           requested_indices: list[int] | None = None) -> Path:
    """Persist the drive-owned request; the session runner resolves the scientific manifest."""
    M = 200
    bayes_opts = {"n_live": 250, "dlogz": 0.5, "max_iter": 30_000}
    for key, env_name, cast in (
            ("n_live", "ISAR_BAYES_NLIVE", int),
            ("dlogz", "ISAR_BAYES_DLOGZ", float),
            ("max_iter", "ISAR_BAYES_MAXITER", int)):
        raw_value = os.environ.get(env_name)
        if raw_value not in (None, ""):
            try:
                bayes_opts[key] = cast(raw_value)
            except (TypeError, ValueError):
                _LOG.warning("ignoring invalid %s=%r while freezing certification settings",
                             env_name, raw_value)
    boot_adaptive = tier in {"full", "bayes"}
    all_decision_points = list(decision.per_q or ())
    if len(all_decision_points) != len(objective.path):
        raise ValueError(
            "final decision does not contain exactly one state for each objective-path point")
    indices = (list(range(len(objective.path))) if requested_indices is None
               else list(requested_indices))
    if not indices or len(set(indices)) != len(indices) or any(
            index < 0 or index >= len(objective.path) for index in indices):
        raise ValueError("certification request needs unique in-range objective indices")
    decision_points = [all_decision_points[index] for index in indices]
    objective_points = [objective.path[index] for index in indices]
    requested_points: list[dict[str, Any]] = []
    for canonical_index, objective_hkl, point in zip(indices, objective_points, decision_points):
        raw_hkl = point.get("hkl") if isinstance(point, dict) else None
        if not isinstance(raw_hkl, (list, tuple)) or len(raw_hkl) != 3:
            raise ValueError(f"invalid q in final decision at path index {canonical_index}")
        hkl = [float(value) for value in raw_hkl]
        if any(abs(left - float(right)) > 1e-8
               for left, right in zip(hkl, objective_hkl)):
            raise ValueError(
                f"final decision q {hkl} does not match objective path index "
                f"{canonical_index}: {list(objective_hkl)}")
        # Carry the stopping decision's coverage verdict. Without it certification cannot tell a q
        # that is unresolvable by construction (an inferred reduced-q origin, which has no pooled
        # master and never will) from one whose master is genuinely missing, and aborts the whole
        # run on the former. Additive to the request contract; the driver still ships a decision
        # summary rather than the full per-q record.
        requested_points.append({
            "hkl": hkl, "objective_index": canonical_index,
            "precision_coverage_eligible": bool(point.get("precision_coverage_eligible", True)),
            "structural_endpoint_status": point.get("structural_endpoint_status"),
            "is_reduced_q_origin": bool(point.get("is_reduced_q_origin", False)),
            "reason": point.get("reason"),
        })
    payload = {
        "schema": "isar.certification.request/1",
        "tier": tier,
        "objective_path": [list(hkl) for hkl in objective_points],
        "per_q": requested_points,
        "fit_options": {"M": M, "boot_adaptive": boot_adaptive,
                        "bayes_opts": bayes_opts},
        "calibration": {"inflation": cfg.calibration.inflation, "floor_meV": cfg.floor},
        "seeds": {
            "scan_seed_base": 6000,
            "scan_rule": "6000 + scan_number",
            "master_rule": "6000 + persisted master scan_number",
            "error_attachment_default": 12345,
            "bayes_seed_offset": 1_000_003,
        },
        "runtime": {"activity_id": activity_id, "log_path": str(cfg.log_path),
                    "total": len(decision_points)},
    }
    fingerprint_body = dict(payload)
    fingerprint_body.pop("runtime", None)
    canonical = json.dumps(
        fingerprint_body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    fingerprint = hashlib.sha256(canonical).hexdigest()
    payload["fingerprint"] = fingerprint
    request = cfg.workspace / "report" / "certification" / "requests" / f"{fingerprint}.json"
    if request.exists():
        existing = json.loads(request.read_text(encoding="utf-8"))
        existing_contract = dict(existing)
        existing_contract.pop("runtime", None)
        new_contract = dict(payload)
        new_contract.pop("runtime", None)
        if existing_contract != new_contract:
            raise ValueError(f"certification request collision at {request}")
        if existing == payload:
            return request
    request.parent.mkdir(parents=True, exist_ok=True)
    temporary = request.with_suffix(".json.tmp")
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, request)
    return request


def _verify_certification_publication(request_path: Path, workspace: Path) -> Path:
    """Fail closed unless the runner published this exact complete canonical generation."""
    request = json.loads(request_path.read_text(encoding="utf-8"))
    request_fingerprint = request.get("fingerprint")
    current_path = workspace / "report" / "certification" / "current.json"
    current = json.loads(current_path.read_text(encoding="utf-8"))
    revision = str(current.get("revision") or "")
    manifest_path = workspace / "report" / "certification" / revision / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fingerprint = manifest.get("fingerprint")
    if manifest.get("request_fingerprint") != request_fingerprint:
        raise RuntimeError("final certification publication does not match this drive request")
    expected_units = [str(unit["unit_key"]) for unit in manifest.get("worklist") or ()]
    complete_path = manifest_path.parent / "complete.json"
    complete = json.loads(complete_path.read_text(encoding="utf-8"))
    if (complete.get("schema") != "isar.certification.complete/2"
            or complete.get("fingerprint") != fingerprint
            or complete.get("unit_keys") != expected_units):
        raise RuntimeError("final certification has no complete canonical checkpoint generation")
    if (current.get("schema") != "isar.certification.current/1"
            or current.get("fingerprint") != fingerprint
            or current.get("revision") != manifest_path.parent.name):
        raise RuntimeError("final certification publication marker does not select this revision")
    complete_hashes = complete.get("published_hashes") or {}
    current_hashes = current.get("published_hashes") or {}
    paths = {
        "session.json": workspace / "session.json",
        "card.state.json": workspace / "card.state.json",
        "results.csv": workspace / "report" / "results.csv",
        "card.policy.toml": workspace / "card.policy.toml",
    }
    if not complete_hashes or current_hashes != complete_hashes:
        raise RuntimeError("final certification publication hashes are absent or inconsistent")
    for name, path in paths.items():
        if (not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != complete_hashes.get(name)):
            raise RuntimeError(f"final certification canonical artifact mismatch: {name}")
    return manifest_path


def _certify(*, cfg: CampaignConfig, objective: ObjectiveSpec, decision,
             observations: list[dict], scan_kind: dict) -> tuple[list[dict], bool]:
    """The end-of-campaign certification pass (the "one bootstrap to certify" decision).

    When the loop stopped ``met``, run ONE forced full (bootstrap+profile) ``isar run`` so every
    reported ω₀ interval is bootstrap-backed, then rebuild ``observations`` from the certified
    ``results.csv``. A certification hiccup logs ``certify_failed`` and reports
    ``ran_certify=False``; the caller converts the provisional ``met`` state to ``incomplete`` so a
    failed final analysis can never masquerade as a certified campaign. Returns
    ``(observations, ran_certify)`` — the unchanged physical-fit provenance plus whether the master
    generation was published."""
    log_path, workspace, data_dir = cfg.log_path, cfg.workspace, cfg.data_dir
    ran_certify = decision is not None and decision.state == "met"
    if not ran_certify:
        return observations, False
    full_tier = _full_tier(cfg)
    log_event(log_path, "decide", {"event": "certify_start", "action": "certify_start", "tier": full_tier})
    # escalate_fit PROMOTE (spec §5.1): the end-of-campaign certify pass forces every q to the full
    # (bootstrap+profile) tier — or `bayes` when the campaign opted in, see _full_tier. Campaign-wide,
    # so no single hkl/attempt — reason names the pass.
    log_event(log_path, "escalate_fit",
              {"phase": "promote", "iter": None, "hkl": None, "from_tier": "fast",
               "to_tier": full_tier, "reason": "certify_pass"})
    try:
        master_only = bool(getattr(cfg.runner, "supports_master_certification", True))
        requested_indices = (_pending_certification_indices(cfg, decision, full_tier)
                             if master_only else list(range(len(decision.per_q or ()))))
        total_points = len(requested_indices)
        if not requested_indices:
            log_event(log_path, "decide", {
                "event": "certify_reused", "action": "certify_reused", "tier": full_tier,
                "reused_points": len(decision.per_q or ())})
            return observations, True
        with activity(log_path, "final_certification", fit_tier=full_tier,
                      total=total_points, progress_unit="points") as activity_id:
            request_path = (_certification_request(
                cfg, objective, decision, full_tier, activity_id=activity_id,
                requested_indices=requested_indices)
                if master_only else None)
            log_activity_progress(log_path, activity_id, completed=0, total=total_points,
                                  progress_unit="points")
            with timed(log_path, "certify", iteration=None):
                run_kwargs = {"errors_tier": full_tier,
                              "force": not master_only, "render": False}
                parameters = inspect.signature(cfg.runner.run).parameters
                if (request_path is not None and ("certification_request" in parameters
                        or any(p.kind == inspect.Parameter.VAR_KEYWORD
                               for p in parameters.values()))):
                    run_kwargs["certification_request"] = request_path
                with analysis_operation(
                        log_path, (f"certification:{request_path.stem}"
                                   if request_path is not None else "certification:callable-seam"),
                        "final_certification", limit_seconds=objective.analysis_limit_seconds):
                    run_kwargs.update(_analysis_runner_kwargs(cfg, objective))
                    run_result = cfg.runner.run(workspace, [data_dir], **run_kwargs)
                    _require_complete_analysis(run_result, objective)
            if request_path is not None:
                _verify_certification_publication(request_path, workspace)
            log_activity_progress(log_path, activity_id, completed=total_points,
                                  total=total_points, progress_unit="points")
        # Physical rows are deliberately unchanged by master-only certification. Steering
        # observations therefore remain the same provenance ledger; final stop evaluation reads the
        # newly published pooled masters directly from results.csv.
        # Embed the certification subprocess timings (iter=None — the certify pass is reported
        # separately, never averaged into per-iteration stats).
        timings = _read_run_timings(workspace)
        analyze_payload = {"job_id": None, "errors_tier": full_tier, "iter": None,
                           "n_rows": len(observations),
                           "dispositions": [o.get("disposition") or o.get("verdict")
                                            for o in observations]}
        if timings is not None:
            analyze_payload["timings"] = timings
        log_event(log_path, "analyze", analyze_payload)
        log_event(log_path, "decide", {"event": "certify_done", "action": "certify_done"})
        return observations, True
    except AnalysisLimitExceeded:
        log_event(log_path, "exception", {
            "action": "certify_checkpointed", "reason": "analysis_limit"})
        raise
    except Exception as e:   # noqa: BLE001 — record the boundary failure; caller reports incomplete
        log_event(log_path, "exception", {"action": "certify_failed", "error": str(e)})
        return observations, False


def _render_campaign_report(cfg: CampaignConfig) -> None:
    """End-of-campaign report render (Stage 3 report deferral).

    Every ``isar run`` in the loop passes ``render=False`` (report layer / matplotlib import is the
    dominant per-invocation cost and nothing mid-campaign consumes report.md/.html/.json/figures), so
    the report is stale on disk. Render it ONCE here from persisted state via ``isar report`` (the same
    re-render path a human would use), in a subprocess (matplotlib stays out of the driver process, and
    the analysis boundary is preserved). Uses the runner's own subprocess mechanism directly rather than
    the runner seam — this is an ``isar report``, not an ``isar run``.

    Best-effort: skipped when no analysis ran (no ``results.csv``); any failure logs an ``exception``
    event and never fails a completed campaign. The ``report_render`` timing span is always recorded."""
    if not (cfg.workspace / "report" / "results.csv").exists():
        return   # no analysis ran (no scans) → nothing to render
    cmd = [sys.executable, "-m", "isar.cli.main", "report", "--workspace", str(cfg.workspace)]
    try:
        with timed(cfg.log_path, "report_render", iteration=None):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"isar report exited {proc.returncode}: {(proc.stderr or '').strip()}")
    except Exception as e:   # noqa: BLE001 — a render hiccup never fails an otherwise-complete campaign
        log_event(cfg.log_path, "exception", {"action": "report_render_failed", "error": str(e)})


def _render_campaign_diagnostic(cfg: CampaignConfig) -> None:
    """End-of-campaign CAMPAIGN_DIAGNOSTIC.md render (Part 2).

    The architect's post-mortem — assembled deterministically from the file contract (see
    ``isar.report.campaign_diagnostic``). Rendered in a subprocess (matplotlib / report layer stays
    out of the driver process, analysis boundary preserved), right after ``_render_campaign_report``
    and BEFORE the terminal ``stop`` event so ``stop`` remains the last logged decision.

    Best-effort: skipped when no analysis ran (no ``results.csv``); any failure logs an ``exception``
    event and never fails a completed campaign. The ``diagnostic_render`` timing span is recorded."""
    if not (cfg.workspace / "report" / "results.csv").exists():
        return   # no analysis ran (no scans) → nothing to diagnose
    cmd = [sys.executable, "-m", "isar.cli.main", "diagnose", "--workspace", str(cfg.workspace)]
    try:
        with timed(cfg.log_path, "diagnostic_render", iteration=None):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(f"isar diagnose exited {proc.returncode}: {(proc.stderr or '').strip()}")
    except Exception as e:   # noqa: BLE001 — a render hiccup never fails an otherwise-complete campaign
        log_event(cfg.log_path, "exception", {"action": "diagnostic_render_failed", "error": str(e)})


def _operator_stop_requested(cfg: CampaignConfig, it: int) -> bool:
    """True when the operator has requested a cooperative stop for THIS campaign's control token.

    The GUI launches ``isar drive`` as a subprocess and requests a stop by writing
    ``<workspace>/report/control/stop-<control_token>.json``. A hard kill during ``wait_scan`` would
    strand the in-flight scan (its ``run`` event is the spend ledger, so resume would re-propose and
    double-spend); the cooperative token lets the loop finish the current iteration and exit at the
    next boundary instead.

    Only THIS token's file is consulted, so a previous campaign's stale stop file is inert
    automatically. Existence is the whole protocol — the file's JSON body is provenance, not a command,
    so it is never read here and never modified/deleted (nothing is ever deleted). A stat error is
    logged (never swallowed) and treated as NOT a stop: a corrupt/racing request must not be silently
    honoured — the GUI retries by rewriting."""
    token = cfg.control_token
    if not token:
        return False
    marker = cfg.workspace / "report" / "control" / f"stop-{token}.json"
    try:
        return marker.exists()
    except OSError as e:
        log_event(cfg.log_path, "exception",
                  {"action": "operator_stop_check_failed", "token": token, "iter": it, "error": str(e)})
        return False


def _terminalize_certification_regression(decision: StopDecision) -> StopDecision:
    """Turn a certified ``continue`` into an honest, resumable terminal outcome.

    ``continue`` is an in-loop instruction, not one of the campaign's terminal outcomes.  A full
    certification pass can legitimately widen an interval that passed the provisional tier; when
    that happens at the end of an invocation the persisted result must say both that the objective
    remains incomplete and that another acquisition run is required.
    """
    if decision.state != "continue":
        return decision

    failed = [row for row in decision.per_q if not row.get("converged", False)]
    labels = []
    for row in failed:
        hkl = row.get("hkl")
        halfwidth = row.get("calibrated_halfwidth")
        tolerance = row.get("tolerance")
        if isinstance(hkl, (list, tuple)) and len(hkl) == 3:
            q_text = "(" + ", ".join(f"{float(value):g}" for value in hkl) + ")"
        else:
            q_text = str(hkl or "unknown q")
        if halfwidth is not None and tolerance is not None:
            labels.append(f"{q_text}: ±{float(halfwidth):.4g} > ±{float(tolerance):.4g} meV")
        else:
            labels.append(q_text)
    detail = "; ".join(labels) if labels else "one or more requested q points"
    return StopDecision(
        "incomplete",
        ["final certification left provisional success outside the objective; "
         f"resume the campaign to collect more data ({detail})"],
        decision.per_q,
    )


def run_campaign(objective: ObjectiveSpec, config: dict | None = None) -> dict[str, Any]:
    """Run an adaptive measurement campaign to satisfy ``objective`` (LLM_HARNESS_DESIGN §8).

    Returns a campaign summary dict (built by :func:`_campaign_summary`). Every ``config`` key, its
    type, its default, and where it is used are resolved and documented by :func:`_campaign_config` /
    :class:`CampaignConfig` — see there for the full key reference."""
    cfg = _campaign_config(objective, config)
    # Freeze the declared background before anything is proposed or spent: one expansion per
    # campaign, stamped unchanged on every scan (see _freeze_sample_background). A bad spec or a
    # resume under drifted background truth aborts the campaign here, having submitted nothing.
    background_frozen, background_meta = _freeze_sample_background(cfg)
    if background_frozen is not None:
        cfg = replace(cfg, sample_background_frozen=background_frozen)

    # Crash-safe resume: recover the spend ledger + folded rows + observations from the durable files
    # (session_log + results.csv). A cold workspace yields the fresh-start values.
    resumed = _resume_state(cfg.workspace, cfg.log_path, cfg.calibration)
    close_interrupted_activities(cfg.log_path)
    close_interrupted_analysis(cfg.log_path)
    budget = dict(objective.budget or {})
    budget_state = {"kind": budget.get("kind"), "amount": budget.get("amount"),
                    "neutrons": resumed["neutrons"], "seconds": resumed["seconds"], "spent": 0.0}
    budget_state["spent"] = (budget_state["neutrons"] if budget_state["kind"] == "neutrons" else
                             budget_state["seconds"] if budget_state["kind"] == "counting_seconds"
                             else 0.0)

    observations: list[dict] = resumed["observations"]
    consumed: set = resumed["consumed"]
    scan_kind: dict = resumed["scan_kind"]   # (scan, detector) -> (kind, ncount), to rebuild obs after certify
    scan_counter = resumed["scan_counter"]
    proposal_counter = resumed.get("proposal_counter", 0)
    # Derived from the durable log, never a second store: a resume that could not rebuild this would
    # re-propose an action the instrument has already refused on every remaining iteration.
    action_suppressions, unreachable_hkl, request_suppressions = suppressed_actions(cfg.log_path)
    limit_envelope = {
        "discovered": dict(cfg.instrument_limits or {}),
        "effective": _runtime_limits(cfg, objective),
    }

    start_payload = {"event": "campaign_start", "action": "campaign_start",
                     "objective": objective.to_dict(),
                     "calibration": {"inflation": cfg.calibration.inflation,
                                     "floor_meV": cfg.floor},
                     "instrument_limits": limit_envelope,
                     "resumed": {"scan_counter": scan_counter,
                                 "proposal_counter": proposal_counter,
                                 "n_observations": len(observations),
                                 "spent": budget_state["spent"],
                                 "analysis_spent_seconds": analysis_clock_state(
                                     cfg.log_path)["spent_seconds"]}}
    if cfg.source is not None:
        start_payload["source"] = cfg.source           # provenance fingerprint (goal/spec/resume + sha256)
    log_event(cfg.log_path, "decide", start_payload)
    if background_frozen is not None:
        # The durable record of the campaign's background truth — also what a later resume
        # re-expands against to detect drift.
        log_event(cfg.log_path, "decide",
                  {"event": "sample_background_frozen", "action": "sample_background_frozen",
                   "spec": cfg.sample_background, "frozen": background_frozen, **background_meta})

    decision = None
    operator_stop = False        # set when a cooperative operator-stop file is found at an iteration top
    op_stop_iter: int | None = None
    pending_recovery_reason: str | None = None
    certified_terminal = False
    promotion_terminal_blocked = False
    last_regression_basis: tuple | None = None
    prev_action_fingerprint: str | None = None    # last selected scientific action, for change-triggered persistence
    terminal_inventory_proof: dict | None = None  # versioned inventory proof stamped on an inventory terminal
    try:
        pending_recovery_reason = _recover_unmatched_submissions(
            cfg=cfg, objective=objective,
            unmatched=list(resumed.get("unmatched_submissions") or []),
            budget_state=budget_state, observations=observations,
            consumed=consumed, scan_kind=scan_kind)
        # Instrumentation only (no logic change): each stage runs inside a ``timed`` span that appends a
        # "timing" event (resume-inert — _resume_state reads only "run" events), and every in-loop
        # log_event carries the iteration index ``it``. The outer ``timed(... "iteration")`` covers the
        # whole body including all break paths (a break exits the span normally, ok=True).
        # Budget-bounded, with NO iteration cap: the campaign runs until it completes, is stopped,
        # or its declared budget (counting time, or neutrons when that is the declared kind) trips.
        # An iteration ceiling is not a scientific limit — it silently truncated campaigns that were
        # still making progress and still had beam time — so ``it`` is a counter for the log and the
        # records only. A budget of ``None`` is unlimited by operator choice, and the loop honours it.
        for it in itertools.count():
            if pending_recovery_reason is not None:
                break
            with timed(cfg.log_path, "iteration", iteration=it):
                # Cooperative operator stop: honoured at the iteration boundary so the previous
                # iteration's scan is already persisted/analysed (no stranded in-flight spend). Log ONE
                # acknowledgement and break; the terminal state becomes operator_stopped (part B).
                if _operator_stop_requested(cfg, it):
                    log_event(cfg.log_path, "decide",
                              {"event": "operator_stop", "action": "operator_stop",
                               "iter": it, "token": cfg.control_token})
                    operator_stop = True
                    op_stop_iter = it
                    break
                with timed(cfg.log_path, "stop_eval", iteration=it):
                    master_rows, grid = _grid_state(objective, observations, cfg.workspace, cfg.calibration)
                    if _record_eligible_crossings(grid, observations, cfg.log_path):
                        master_rows, grid = _grid_state(
                            objective, observations, cfg.workspace, cfg.calibration)
                    decision = evaluate_stop(objective, grid, budget_state, calibration=cfg.calibration, floor=cfg.floor)
                if decision.state == "met":
                    unresolved_promotions = _unresolved_current_promotions(cfg.workspace)
                    if unresolved_promotions:
                        promotion_ids = [str(record.get("promotion_id"))
                                         for record in unresolved_promotions]
                        decision = StopDecision(
                            "incomplete",
                            ["terminal decision refused: unresolved current master promotion(s): "
                             + ", ".join(promotion_ids)], decision.per_q)
                        log_event(cfg.log_path, "recovery_pending", {
                            "action": "master_promotion_pending", "promotion_ids": promotion_ids,
                            "reason": ("terminal stopping and certification require resolved "
                                       "promotions"), "iter": it})
                        promotion_terminal_blocked = True
                        break
                    certification_basis = _certification_decision_signature(decision)
                    if (last_regression_basis is not None
                            and certification_basis == last_regression_basis):
                        decision = StopDecision(
                            "incomplete",
                            ["certification remained outside the objective without a changed "
                             "scientific state; automatic continuation stopped to avoid a retry loop"],
                            decision.per_q)
                        break
                    analysis_spent = analysis_clock_state(cfg.log_path)["spent_seconds"]
                    if (objective.analysis_limit_seconds is not None
                            and analysis_spent >= float(objective.analysis_limit_seconds)):
                        decision = StopDecision(
                            "incomplete",
                            [f"analysis limit exhausted before final certification ({analysis_spent:.1f}/"
                             f"{float(objective.analysis_limit_seconds):.1f}s)"], decision.per_q)
                        break
                    try:
                        observations, ran_certify = _certify(
                            cfg=cfg, objective=objective, decision=decision,
                            observations=observations, scan_kind=scan_kind)
                    except AnalysisLimitExceeded as error:
                        decision = StopDecision("incomplete", [str(error)], decision.per_q)
                        break
                    if not ran_certify:
                        # The only way here from a ``met`` decision is the catch-all fault in
                        # ``_certify`` (an ``AnalysisLimitExceeded`` checkpoint is handled on its own
                        # branch above, and the reused-early-exit returns True), so this is a
                        # controller fault, not a scientific outcome.  Name it as such — checkpoints
                        # are retained and the campaign resumes after repair — so a software bug is
                        # never reported as a data-limited ``incomplete``.
                        decision = StopDecision(
                            "incomplete",
                            ["controller_fault: final certification did not complete; checkpoints "
                             "retained and resume is supported after repair"],
                            decision.per_q, terminal_cause=TERMINAL_CONTROLLER_FAULT)
                        break
                    master_rows, grid = _grid_state(
                        objective, observations, cfg.workspace, cfg.calibration)
                    decision = evaluate_stop(
                        objective, grid, budget_state,
                        calibration=cfg.calibration, floor=cfg.floor)
                    _record_certification_acceptance(cfg, decision, _full_tier(cfg))
                    if decision.state == "met":
                        certified_terminal = True
                        break
                    log_event(cfg.log_path, "exception", {
                        "action": "certify_regression", "state": decision.state,
                        "automatic_resume": True, "iter": it,
                        "per_q": [row for row in decision.per_q
                                  if not row.get("converged", False)],
                        "note": "full certification left provisional success outside the objective"})
                    last_regression_basis = certification_basis
                    # Certification does not consume an acquisition iteration.  With a physical and
                    # analysis budget still available, fall through and propose the corrective scan.
                    if _operator_stop_requested(cfg, it):
                        log_event(cfg.log_path, "decide", {
                            "event": "operator_stop", "action": "operator_stop",
                            "iter": it, "token": cfg.control_token,
                            "boundary": "post_certification"})
                        operator_stop = True
                        op_stop_iter = it
                        break
                elif decision.state != "continue":
                    break
                analysis_spent = analysis_clock_state(cfg.log_path)["spent_seconds"]
                if (objective.analysis_limit_seconds is not None
                        and analysis_spent >= float(objective.analysis_limit_seconds)):
                    decision = StopDecision(
                        "incomplete",
                        [f"analysis limit exhausted ({analysis_spent:.1f}/"
                         f"{float(objective.analysis_limit_seconds):.1f}s)"],
                        decision.per_q)
                    break

                # Plan, select, and — at most once per iteration — provision the predictive basis
                # the selected request will be scored against, then replan against it. The promotion
                # is deliberately inside the planning step rather than a sweep before it: only the
                # source master of the request the controller actually chose is worth a charged
                # full-tier reanalysis (R1-C).
                predictive_promotion_spent = False
                promotion_changed_stop = False
                for _plan_attempt in range(2):
                    with timed(cfg.log_path, "model_fit", iteration=it):
                        model = acq_model.fit(model_points(grid))
                    # Classification still consumes durable branch state. Expected-count acquisition is
                    # supplied exclusively by each master's frozen predictive artifact; the driver never
                    # reconstructs an analysis model from branch summaries.
                    with timed(cfg.log_path, "classification_provider", iteration=it):
                        durable_state = read_card_state(cfg.workspace)
                        campaign_slice, campaign_slice_error = resolve_campaign_slice(
                            durable_state, objective, sample=cfg.sample,
                            binning_policy_fingerprint=_temperature_binning_fingerprint(cfg.workspace))
                        classify, rival_band = _make_classification_providers(
                            durable_state, objective, cfg.resolution_cb, sample=cfg.sample,
                            temperature_bin=(campaign_slice.temperature_bin
                                             if campaign_slice is not None else None))
                    action_inventory = None
                    plan = None
                    proposal = None
                    selection_terminal: StopDecision | None = None
                    with timed(cfg.log_path, "propose", iteration=it):
                        try:
                            analysis_remaining = (float("inf") if objective.analysis_limit_seconds is None
                                                  else max(float(objective.analysis_limit_seconds) -
                                                           float(analysis_clock_state(
                                                               cfg.log_path)["spent_seconds"]), 0.0))
                            scoring_allowance = min(60.0, 0.05 * analysis_remaining)
                            with analysis_operation(
                                    cfg.log_path, f"score:{cfg.run_nonce}:{proposal_counter}",
                                    "predictive_scoring",
                                    limit_seconds=objective.analysis_limit_seconds, iteration=it):
                                plan = plan_actions(
                                    objective, model, observations, cfg.resolution_cb, budget_state,
                                    master_rows=master_rows, fisher=None,
                                    classify=classify, rival_band=rival_band,
                                    runtime_limits=_runtime_limits(cfg, objective),
                                    track_anchor_index=((durable_state.get("branch_tracks") or {}).get(
                                        "acquisition_seed_index",
                                        (durable_state.get("branch_tracks") or {}).get("anchor_index"))),
                                    track_zone_period=_workspace_zone_period(cfg.workspace),
                                    branch_track=(durable_state.get("branch_tracks") or {}),
                                    scoring_allowance_seconds=scoring_allowance,
                                    suppressed=action_suppressions,
                                    unreachable_hkl=unreachable_hkl,
                                    campaign_slice=campaign_slice,
                                    campaign_slice_error=campaign_slice_error)
                                action_inventory = plan.inventory
                            # Selection + finalization are one bounded step: an unaffordable / instrument-
                            # refused first action is charged and the next eligible candidate tried, rather
                            # than terminating the campaign while a cheaper action is still affordable (O29).
                            proposal, selection_terminal = _next_finalized_proposal(
                                plan, objective, cfg, grid, budget_state, request_suppressions, it=it)
                        except AnalysisLimitExceeded as error:
                            decision = StopDecision("incomplete", [str(error)], decision.per_q)
                            proposal = None
                    if predictive_promotion_spent or proposal is None:
                        break
                    promotion = _predictive_promotion_target(proposal, grid, cfg.log_path)
                    if promotion is None:
                        break
                    promotion_item, promotion_revision = promotion
                    outcome, promotion_error = _promote_master_predictive_basis(
                        cfg, objective, promotion_item, promotion_revision, it=it)
                    predictive_promotion_spent = True
                    if outcome == "analysis_limit":
                        decision = StopDecision(
                            "incomplete", [str(promotion_error)], decision.per_q)
                        proposal = None
                        break
                    if outcome != "completed":
                        # The failure is already logged as an escalate_fit outcome. Keep the request
                        # that was selected against the basis that exists rather than stalling: a
                        # measurement still advances the campaign, and the revision is not retried.
                        break
                    master_rows, grid = _grid_state(
                        objective, observations, cfg.workspace, cfg.calibration)
                    promoted_decision = evaluate_stop(
                        objective, grid, budget_state,
                        calibration=cfg.calibration, floor=cfg.floor)
                    if promoted_decision.state != "continue":
                        # The promoted basis answered the stopping question. Hand the iteration back
                        # to the top so the terminal/certification path decides what happens next —
                        # replanning here would reach the no-proposal path instead and report a
                        # blocked frontier for a campaign that is actually finished.
                        decision = promoted_decision
                        proposal = None
                        promotion_changed_stop = True
                        break
                if promotion_changed_stop:
                    continue
                if selection_terminal is not None:
                    # No remaining candidate could be finalized/afforded: a budget (incomplete) or
                    # instrument (infeasible) terminal. The proposal-less path below leaves an already
                    # non-continue decision untouched and still stamps the inventory proof.
                    decision = selection_terminal
                    proposal = None
                if action_inventory is not None:
                    inventory_ref, selected_fingerprint = _persist_action_inventory(
                        cfg, action_inventory, it=it, proposal=proposal,
                        prev_fingerprint=prev_action_fingerprint, force_full=(proposal is None))
                    if proposal is not None:
                        prev_action_fingerprint = selected_fingerprint
                    elif action_inventory.terminal_cause(GENERATOR_REGISTRY) is not None:
                        terminal_inventory_proof = {
                            "inventory_version": action_inventory.inventory_version,
                            "generator_registry_version": GENERATOR_REGISTRY_VERSION,
                            "inventory_ref": inventory_ref}
                if proposal is None:
                    if decision is None or decision.state == "continue":
                        decision = evaluate_stop(
                            objective, grid, budget_state,
                            calibration=cfg.calibration, floor=cfg.floor)
                    decision = _blocked_frontier_decision(
                        objective, grid, decision, action_inventory)
                    decision = _exhausted_action_space_decision(decision, action_inventory)
                    break
                # A refused request must not cost the whole iteration while the inventory still holds
                # other eligible actions (PR2.3). Bounded, so a pathological inventory cannot spin;
                # ``stop_campaign`` carries the terminal decisions out, since a ``break`` here would
                # only leave this retry loop.
                stop_campaign = False
                for _action_attempt in range(MAX_ACTION_ATTEMPTS_PER_ITERATION):
                    proposal.acquisition_id = (proposal.acquisition_id
                                               or f"{cfg.run_nonce}:{proposal_counter:08d}")
                    proposal_counter += 1
                    log_event(cfg.log_path, "propose", {**proposal.to_dict(), "iter": it})
                    if (proposal.meta or {}).get("purpose") == "differentiate":
                        transition = (proposal.meta or {}).get("evidence_transition") or {}
                        log_event(cfg.log_path, "differentiation_probe", {
                            "hkl": [proposal.h, proposal.k, proposal.l],
                            "kind": proposal.kind, "reason": proposal.reason,
                            "acquisition_id": proposal.acquisition_id, "iter": it,
                            **transition,
                        })

                    b_act = exc.handle_budget(budget_state)
                    if b_act is not None:
                        log_event(cfg.log_path, "exception", {**b_act.to_dict(), "iter": it})
                        if b_act.action == "exhausted":
                            stop_campaign = True
                            break

                    try:
                        rows, scan_counter, declined_verdict = _execute_proposal(
                            cfg=cfg, objective=objective, proposal=proposal, it=it,
                            scan_counter=scan_counter, budget_state=budget_state,
                            observations=observations, consumed=consumed, scan_kind=scan_kind)
                    except AnalysisLimitExceeded as error:
                        decision = StopDecision(
                            "incomplete", [str(error)],
                            evaluate_stop(objective, grid, budget_state,
                                          calibration=cfg.calibration, floor=cfg.floor).per_q)
                        stop_campaign = True
                        break
                    except TaviError as error:
                        # A communication failure after ``physical_submission`` is not a failed submit:
                        # the job may still be running or its data may already be retrievable.  Preserve
                        # the durable unmatched submission and use the normal Resume recovery contract.
                        recovery_state = _resume_state(
                            cfg.workspace, cfg.log_path, cfg.calibration)
                        unmatched = [item for item in recovery_state.get("unmatched_submissions", [])
                                     if (item.get("payload") or {}).get("acquisition_id")
                                     == proposal.acquisition_id]
                        if unmatched:
                            payload = unmatched[-1].get("payload") or {}
                            pending_recovery_reason = (
                                f"job {payload.get('job_id')} recovery is pending after instrument "
                                f"communication failed ({error})")
                            log_event(cfg.log_path, "recovery_pending", {
                                "job_id": payload.get("job_id"), "state": "unknown",
                                "decline": False, "reason": pending_recovery_reason,
                                "acquisition_id": proposal.acquisition_id, "iter": it})
                            stop_campaign = True
                            break
                        log_event(cfg.log_path, "exception", {
                            "action": "instrument_submission_failed", "error": str(error),
                            "status": error.status, "iter": it})
                        decision = StopDecision(
                            "incomplete", [f"instrument submission failed: {error}"],
                            evaluate_stop(objective, grid, budget_state,
                                          calibration=cfg.calibration, floor=cfg.floor).per_q)
                        stop_campaign = True
                        break
                    except (BudgetInsufficientForMinimumBatch,
                            InstrumentLimitBelowMinimumBatch,
                            InstrumentPointLimitExceeded) as error:
                        log_event(cfg.log_path, "exception", {
                            "action": (
                                "instrument_point_limit_exceeded"
                                if isinstance(error, InstrumentPointLimitExceeded)
                                else "instrument_limit_below_minimum_batch"
                                if isinstance(error, InstrumentLimitBelowMinimumBatch)
                                else "budget_insufficient_for_minimum_batch"),
                            "error": str(error), "iter": it})
                        decision = StopDecision(
                            ("infeasible" if isinstance(
                                error, (InstrumentLimitBelowMinimumBatch,
                                        InstrumentPointLimitExceeded))
                             else "incomplete"), [str(error)],
                            evaluate_stop(objective, grid, budget_state,
                                          calibration=cfg.calibration, floor=cfg.floor).per_q)
                        stop_campaign = True
                        break

                    if declined_verdict is None:
                        break
                    # Charge the refusal at its scope (populating suppressions / by_request and logging
                    # the durable event). A transient refusal (scope "none") suppresses nothing and does
                    # not advance to another action, matching the original break.
                    _record_action_rejection(
                        cfg, None, proposal, declined_verdict, action_suppressions,
                        unreachable_hkl, request_suppressions, it=it)
                    if classify_rejection(declined_verdict).scope == "none":
                        break
                    # Pull + finalize the next candidate through the same bounded helper the initial
                    # selection uses, so a budget/instrument refusal here is classified into a terminal
                    # instead of escaping as an exception (GAP 1), and an already-refused request is not
                    # re-emitted (GAP 2).
                    proposal, fallback_terminal = _next_finalized_proposal(
                        plan, objective, cfg, grid, budget_state, request_suppressions, it=it)
                    if fallback_terminal is not None:
                        decision = fallback_terminal
                        stop_campaign = True
                        break
                    if proposal is None:
                        break
                if stop_campaign:
                    break

                # ---- exception branch on the final verdict (flagged / held) ----------------------------
                with timed(cfg.log_path, "verdict_exceptions", iteration=it):
                    for r in rows:
                        disp = r.get("disposition")
                        status = r.get("status")
                        if status in exc.HELD_STATUSES:
                            log_event(cfg.log_path, "exception", {**exc.handle_held(status).to_dict(), "iter": it})
                        elif disp not in (None, "resolved") and disp not in exc.MISSED_VERDICTS:
                            log_event(cfg.log_path, "exception",
                                      {**exc.handle_flagged(disp, verdict=r.get("verdict")).to_dict(), "iter": it})

                with timed(cfg.log_path, "model_update", iteration=it):
                    master_rows, grid = _grid_state(objective, observations, cfg.workspace, cfg.calibration)
                    grid_snapshot = evaluate_stop(
                        objective, grid, budget_state, calibration=cfg.calibration, floor=cfg.floor)
                log_event(cfg.log_path, "decide", {"event": "model_update", "action": "model_update",
                                                   "n_observations": len(observations),
                                                   "converged": sum(1 for g in grid if g.converged),
                                                   "n_grid": len(grid), "per_q": grid_snapshot.per_q,
                                                   "iter": it})
                for point in grid:
                    if point.n_production == point.max_production_batches:
                        log_event(cfg.log_path, "strategy_ceiling", {
                            "hkl": list(point.hkl), "n_production": point.n_production,
                            "maximum_production_batches_per_q": point.max_production_batches,
                            "action": "switch_to_differentiation_probe", "iter": it,
                        })
                    if point.clamped_productions == 2 and point.exposure_saturated:
                        log_event(cfg.log_path, "exposure_saturation", {
                            "hkl": list(point.hkl),
                            "reason": "requested dwell repeatedly clamped to the same instrument limit",
                            "clamped_productions": point.clamped_productions, "iter": it,
                        })

        if pending_recovery_reason is not None:
            master_rows, grid = _grid_state(objective, observations, cfg.workspace, cfg.calibration)
            snapshot = evaluate_stop(
                objective, grid, budget_state, calibration=cfg.calibration, floor=cfg.floor)
            stop_override = {
                "state": "pending_recovery",
                "reasons": [pending_recovery_reason, "press Resume to retry recovery"],
                "per_q": list(snapshot.per_q),
                "grid_state": snapshot.to_dict(),
            }
            summary = _campaign_summary(
                objective, grid, snapshot, budget_state, cfg.workspace,
                cfg.log_path, len(observations), stop_override=stop_override,
                instrument_limits=limit_envelope)
            stop_payload = {"state": "pending_recovery", "reasons": stop_override["reasons"],
                            "grid_state": stop_override["grid_state"]}
        elif operator_stop:
            # Operator-stop terminal state (part B): an honest snapshot WITHOUT certification — a stop
            # request must never trigger the end-of-campaign certify bootstrap. Recompute the final grid
            # + evaluate_stop for a truthful picture, but record it under stop.grid_state; the terminal
            # state itself is operator_stopped. The decision-None path (stop found before any iteration
            # completed) is safe here: the snapshot is computed fresh from the grid, never from
            # ``decision``, so no AttributeError.
            master_rows, grid = _grid_state(objective, observations, cfg.workspace, cfg.calibration)
            snapshot = evaluate_stop(objective, grid, budget_state, calibration=cfg.calibration, floor=cfg.floor)
            stop_override = {
                "state": "operator_stopped",
                "reasons": [f"operator stop request honored at iteration {op_stop_iter}",
                            f"token {cfg.control_token}"],
                "per_q": list(snapshot.per_q),
                "grid_state": snapshot.to_dict(),
            }
            summary = _campaign_summary(objective, grid, snapshot, budget_state, cfg.workspace,
                                        cfg.log_path, len(observations), stop_override=stop_override,
                                        instrument_limits=limit_envelope)
            stop_payload = {"state": "operator_stopped", "reasons": stop_override["reasons"],
                            "grid_state": stop_override["grid_state"]}
        else:
            # Certification: the loop converged on fast HESSE intervals; run ONE full bootstrap+profile
            # pass (forced) so every reported ω₀ interval is bootstrap-backed (the "one bootstrap to
            # certify" decision). Bounded — once, at the end, only when the objective is met. Then
            # re-evaluate honestly: if a q's bootstrap-certified interval crosses tolerance the summary
            # reflects that, not the optimistic fast-tier stop (the triage tool reports the certified truth).
            unresolved_promotions = ([] if promotion_terminal_blocked else
                                     _unresolved_current_promotions(cfg.workspace))
            promotion_blocked = promotion_terminal_blocked or bool(unresolved_promotions)
            if unresolved_promotions:
                promotion_ids = [str(record.get("promotion_id"))
                                 for record in unresolved_promotions]
                prior_rows = [] if decision is None else decision.per_q
                decision = StopDecision(
                    "incomplete",
                    ["terminal decision refused: unresolved current master promotion(s): "
                     + ", ".join(promotion_ids)], prior_rows)
                log_event(cfg.log_path, "recovery_pending", {
                    "action": "master_promotion_pending", "promotion_ids": promotion_ids,
                    "reason": "terminal stopping and certification require resolved promotions"})
            analysis_spent = analysis_clock_state(cfg.log_path)["spent_seconds"]
            if (not certified_terminal and decision is not None and decision.state == "met"
                    and objective.analysis_limit_seconds is not None
                    and analysis_spent >= float(objective.analysis_limit_seconds)):
                decision = StopDecision(
                    "incomplete",
                    [f"analysis limit exhausted before final certification ({analysis_spent:.1f}/"
                     f"{float(objective.analysis_limit_seconds):.1f}s)"], decision.per_q)
            if promotion_blocked:
                ran_certify = False
            elif certified_terminal:
                ran_certify = True
            else:
                try:
                    observations, ran_certify = _certify(
                        cfg=cfg, objective=objective, decision=decision, observations=observations,
                        scan_kind=scan_kind)
                except AnalysisLimitExceeded as error:
                    ran_certify = False
                    decision = StopDecision("incomplete", [str(error)], decision.per_q)

            if decision is not None and decision.state == "met" and not ran_certify:
                # A certify fault reached here from a ``met`` decision is a controller fault (the
                # analysis-limit checkpoint and promotion-blocked paths above set their own reasons),
                # so classify it rather than let a software bug read as a data-limited ``incomplete``.
                decision = StopDecision(
                    "incomplete",
                    ["controller_fault: final certification did not complete; checkpoints "
                     "retained and resume is supported after repair"],
                    decision.per_q, terminal_cause=TERMINAL_CONTROLLER_FAULT)

            # Recompute the stop decision on the FINAL grid unconditionally: the in-loop decision is from
            # the top of the last iteration, so a campaign that breaks on budget or completion
            # would otherwise report a stale pre-scan verdict in the summary.
            master_rows, grid = _grid_state(objective, observations, cfg.workspace, cfg.calibration)
            final_decision = evaluate_stop(
                objective, grid, budget_state, calibration=cfg.calibration, floor=cfg.floor)
            # A prospective neutron-budget refusal occurs before spend reaches the nominal amount; a
            # plain final budget evaluation therefore still says "continue". Preserve that terminal
            # decision while retaining ordinary recomputation for every measured-scan exit path.
            if not (decision is not None and decision.state in {"incomplete", "infeasible"}):
                decision = final_decision
            if ran_certify:
                _record_certification_acceptance(cfg, decision, _full_tier(cfg))
            if ran_certify and decision.state != "met":
                decision = _terminalize_certification_regression(decision)
                log_event(cfg.log_path, "exception",
                          {"action": "certify_regression", "state": decision.state,
                           "reasons": decision.reasons,
                           "per_q": [row for row in decision.per_q
                                     if not row.get("converged", False)],
                           "note": "full certification left provisional success outside the objective"})

            summary = _campaign_summary(objective, grid, decision, budget_state, cfg.workspace,
                                        cfg.log_path, len(observations),
                                        instrument_limits=limit_envelope,
                                        inventory_proof=terminal_inventory_proof)
            stop_payload = {"state": decision.state, "reasons": decision.reasons}
            if decision.terminal_cause is not None:
                stop_payload["terminal_cause"] = decision.terminal_cause
            if decision.terminal_cause == TERMINAL_CONTROLLER_FAULT:
                # A controller fault is an unattended-safe terminal: checkpoints are retained and the
                # campaign resumes after repair rather than crashing overnight.
                stop_payload["resume_supported"] = True
            if terminal_inventory_proof is not None:
                stop_payload.update(terminal_inventory_proof)

        (cfg.workspace / "report").mkdir(parents=True, exist_ok=True)
        (cfg.workspace / "report" / "campaign_summary.json").write_text(
            json.dumps(summary, indent=2), encoding="utf-8")
        # Report deferral (Stage 3): the loop ran every `isar run` with --no-render, so refresh the
        # human report once now from persisted state (incl. any certify pass). Best-effort — reads only
        # persisted files, which are final here, so it precedes the terminal `stop` event (keeping
        # `stop` the last logged decision).
        _render_campaign_report(cfg)
        _render_campaign_diagnostic(cfg)
        log_event(cfg.log_path, "stop", stop_payload)
    finally:
        cfg.runner.close()
    return summary


def _charge_budget(budget_state: dict, proposal, eta, *, n_points: int | None = None) -> None:
    points = int(proposal.n_points or 1) if n_points is None else max(int(n_points), 0)
    budget_state["neutrons"] += float(proposal.ncount) * float(points)
    if eta is not None:
        budget_state["seconds"] += float(eta)
    budget_state["spent"] = (budget_state["neutrons"] if budget_state["kind"] == "neutrons" else
                             budget_state["seconds"] if budget_state["kind"] == "counting_seconds"
                             else 0.0)


def _reconcile_budget_points(budget_state: dict, ncount: int, planned: int, executed: int,
                             *, eta: float | None = None) -> tuple[float, float]:
    """Replace planned point/time cost with executed cost; return durable ledger deltas."""
    delta = float(ncount) * float(int(executed) - int(planned))
    budget_state["neutrons"] = max(0.0, float(budget_state["neutrons"]) + delta)
    seconds_delta = 0.0
    if eta is not None and int(planned) > 0:
        seconds_delta = float(eta) * (float(executed) / float(planned) - 1.0)
        budget_state["seconds"] = max(0.0, float(budget_state["seconds"]) + seconds_delta)
    budget_state["spent"] = (budget_state["neutrons"] if budget_state["kind"] == "neutrons" else
                             budget_state["seconds"] if budget_state["kind"] == "counting_seconds"
                             else 0.0)
    return delta, seconds_delta


def _campaign_summary(objective, grid, decision, budget_state, workspace, log_path, n_obs,
                      *, stop_override: dict | None = None,
                      instrument_limits: dict | None = None,
                      inventory_proof: dict | None = None) -> dict:
    def _omega0(g):
        return (g.master or {}).get("omega0")

    def _hw(g):
        return (g.master or {}).get("halfwidth")

    dispersion = []
    for g in grid:
        if _omega0(g) is None:
            continue
        m = g.master or {}
        n_members = m.get("n_members")
        members = m.get("members")
        # Chain-of-custody guard: a pooled dispersion point reported without its member provenance
        # breaks the evidence chain. Skip only when the results.csv predates the members column
        # (members_field_present is False) — the transition-tolerance case.
        if (n_members is not None and m.get("members_field_present")
                and (not members or len(members) != n_members)):
            raise RuntimeError(
                f"dispersion point {list(g.hkl)}: pooled value reported without member provenance "
                f"(evidence chain broken)")
        dispersion.append({"hkl": list(g.hkl), "q": g.q, "omega0": _omega0(g),
                           "calibrated_halfwidth": _hw(g), "n_members": n_members,
                           "members": members, "converged": g.converged})
    try:
        card = read_card_state(workspace)
        prior_points = card.get("dispersion_prior") or []
        mismatch = dict(card.get("hypothesis_mismatch") or {})
        branch_beliefs = list(card.get("branch_belief") or [])
    except Exception as error:   # noqa: BLE001
        log_event(log_path, "exception", {"event": "summary_state_read_failed",
                                           "error": str(error)})
        prior_points = []
        mismatch = {}
        branch_beliefs = []
    analysis = analysis_clock_state(log_path)
    analysis["limit_seconds"] = objective.analysis_limit_seconds
    stop = dict(stop_override) if stop_override is not None else decision.to_dict()
    if inventory_proof is not None:
        # The session log stays canonical, but the summary is what a reader opens first, and a
        # terminal_cause with no path to the inventory that produced it is unverifiable from here.
        stop["action_inventory"] = dict(inventory_proof)
    return {
        "objective": objective.to_dict(),
        # ``stop_override`` carries the operator_stopped terminal state (+ grid_state snapshot); the
        # default is the honest StopDecision. Additive — the dict shape is otherwise unchanged.
        "stop": stop,
        "grid": [{"hkl": list(g.hkl), "q": g.q, "converged": g.converged,
                  "assessment_state": g.assessment_state,
                  "calibrated_halfwidth": g.cert_halfwidth, "omega0": _omega0(g),
                  "master_halfwidth": (g.master or {}).get("halfwidth"),
                  "master_revision": g.master_revision,
                  "n_members": (g.master or {}).get("n_members"),
                  "members": (g.master or {}).get("members"),
                  "n_production": g.n_production, "diagnostics": g.diagnostics,
                  # Without these the summary shows a point as "inferred_unresolved" but cannot say
                  # why it was excluded from coverage, which is exactly the question a reader asks.
                  "precision_coverage_eligible": g.precision_coverage_eligible,
                  "structural_endpoint_status": g.structural_endpoint_status,
                  "is_reduced_q_origin": g.is_reduced_q_origin,
                  "track_state": g.track_state,
                  "linewidth_state": g.linewidth_state,
                  **local_exhaustion_status(g)} for g in grid],
        "dispersion": dispersion,
        "dispersion_prior_points": len(prior_points),
        "branch_beliefs": branch_beliefs,
        "hypothesis_mismatch": mismatch,
        "budget": dict(budget_state),
        "analysis": analysis,
        "instrument_limits": dict(instrument_limits or {}),
        "n_observations": n_obs,
        "session_log": str(log_path),
        "session_log_relative": ("report/session_log.jsonl"
                                 if Path(log_path) == Path(workspace) / "report" / "session_log.jsonl"
                                 else None),
    }
