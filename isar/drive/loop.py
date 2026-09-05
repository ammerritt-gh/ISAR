"""``run_campaign`` — the adaptive-loop orchestrator (LLM_HARNESS_DESIGN §8, the worked MVP loop).

Wires the three parts into the §8 loop:

    objective spec → (σ-calibration is milestone 0, loaded here) → adaptive loop {
        acquisition proposes a scan → POST /validate → POST /scan (isolated, seeded) → wait →
        save scan JSON → `isar run` (subprocess) → read results.csv / card.state.json →
        fold into the ω(q) model → stopping check + exception branches
    } → stop (met / incomplete / infeasible) → campaign summary

The loop is **decision-only**: numbers come only from ISAR's ``results.csv`` (read through the file
contract), never from a fit imported here. Every decision is written to the session log
(:mod:`isar.drive.session_log`).

There is exactly ONE place a Q, an energy window or an exposure is chosen:
:func:`isar.drive.acquisition.eig_planner.plan_next`, called at the single seam in
:func:`run_campaign`. No survey, no frontier or bridge rule, no same-q ceiling, no opening book, no
scan-window comparison and no missed-window recentre-and-widen — a wide window happens only because
a belief is wide, and a scan that finds nothing is ordinary evidence the planner prices as a clean
null. ``met`` is the planner's prize (every prize point's target energy known to the requested bar);
:func:`~isar.drive.supervisor.stopping.evaluate_stop` decides only ``infeasible`` / ``incomplete`` /
``continue``.

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
from typing import Any, Callable

import json
import hashlib
import itertools
import logging
import math
import os
import statistics
import subprocess
import sys
import time
import uuid

from isar.drive.acquisition.acquire import (
    DEFAULT_FWHM_MEV,
    BudgetInsufficientForMinimumBatch,
    InstrumentLimitBelowMinimumBatch,
    InstrumentPointLimitExceeded,
    ScanProposal,
    _resolve_fwhm,
    hkl_key,
)
from isar.drive.acquisition.classify_eig import belief_predict
from isar.drive.acquisition.eig_planner import (
    KB_MEV_PER_K,
    Candidate,
    IntensityScale,
    ExecutedScan,
    PlannerInput,
    default_intensity_scale,
    plan_next,
)
from isar.drive.acquisition.scan_grid import (
    MAX_POINTS,
    resolve_uniform_grid,
    snap_to_lattice,
)
from isar.drive.acquisition.grid_adapters import TAVI_GRID_ADAPTER
from isar.drive.acquisition.inventory import (
    TERMINAL_CONTROLLER_FAULT, CampaignSliceKey, MeasurementIntent, RejectionClass, canonical_json)
from isar.drive.acquisition.calibration import Calibration, load_calibration
from isar.drive.instrument.tavi_client import TaviClient, TaviError
from isar.drive.instrument.request_realization import (
    DEFAULT_MAX_POINTS,
    _emitted_point_count,
    _executed_point_count,
    _feasibility_manifest_fingerprint,
    _feasible_point_count,
    _is_feasibility_shaped_refusal,
    _preflight_instrument_limits,
    _scan_body,
    _target_supporting_feasible_points,
    _validate_body,
    finalized_proposal,
    plan_feasibility_response,
    validated_attempt,
    validation_event_fields,
)
from isar.drive.isar_io.readers import (read_card_state, read_master_rows, read_results_csv,
                                        read_run_timings)
from isar.drive.isar_io.runner import SubprocessRunner, as_runner, ingest_new, refresh_analysis
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
    match_grid_index,
    summarize_grid,
    systematic_floor,
)
from isar.schema.card import Card
from isar.schema.hypotheses import MAX_NCOUNT_PER_SCAN, preset
from isar.schema.measurement import (CAMPAIGN_PROVENANCE_FIELDS, TOPOLOGY_ROLES,
                                     campaign_provenance)
from isar.schema.steering import CANONICAL_MASTER, realized_outcome_for

DEFAULT_WAIT_TIMEOUT = 600.0
# A runaway backstop, NOT a scientific limit. Campaign length is bounded by the declared budget, and
# an operator-chosen iteration cap silently truncated campaigns that were still making progress
# (ruling: the --max-iters removal). But a budget of ``None`` is "unlimited", and an instrument
# whose every job comes back failed produces a rejection class that suppresses nothing, so the
# identical action is re-selected and re-submitted forever with nothing spent. This ceiling is set far
# above any real campaign (a real one converges in tens of iterations) so it can only ever be reached
# by a controller that is not making progress; hitting it is classified as a controller fault, which
# is unattended-safe — checkpoints are retained and the campaign resumes after repair.
_ABSOLUTE_ITERATION_CEILING = 10_000
_DEFAULT_CALIBRATION = (Path(__file__).resolve().parents[2] / "calibration"
                        / "phonon_dft_deterministic.json")
_LOG = logging.getLogger(__name__)


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


def _analysis_allowance_seconds(cfg, objective) -> float | None:
    """The campaign's remaining analysis time, or ``None`` when it declares no analysis limit.

    The clock is the loop's; :func:`~isar.drive.isar_io.runner.refresh_analysis` only forwards the
    number. ``None`` propagates as "send neither allowance nor ``finish_initial_wave``" — an
    unlimited campaign imposes no deadline keywords on the runner seam."""
    limit = objective.analysis_limit_seconds
    if limit is None:
        return None
    spent = float(analysis_clock_state(cfg.log_path)["spent_seconds"])
    return max(float(limit) - spent, 0.0)


def _snap_to_lattice(proposal, log_path, it: int, *, event_sink=log_event) -> bool:
    """Put ``proposal``'s energy window on the shared lattice BEFORE anything derives from it.

    A repeat scan is only worth its neutrons if the reducer can add them to the master it is topping
    up, and :func:`isar.reduce.collate._merge_group` can only do that for members whose executed
    energies coincide (see :func:`~isar.drive.acquisition.scan_grid.snap_to_lattice` for the measured
    failure).  Nothing upstream guarantees that: two designs for the same point routinely differ by
    hundredths of a meV, and the campaign then tops up a pool that never grows.

    The rule is the same one the feasibility trim already lives by -- the EMITTED window is the truth
    downstream, not the designed one -- so the snap happens here, at the single normalization seam
    every proposal passes immediately before validation and submission, and therefore covers every
    repeat rather than only the planner's.  Returns ``True`` when the window moved.
    """
    e_lo, e_hi, e_step = snap_to_lattice(proposal.e_lo, proposal.e_hi, proposal.e_step)
    if (e_lo, e_hi, e_step) == (float(proposal.e_lo), float(proposal.e_hi), float(proposal.e_step)):
        return False
    if event_sink is not None:
        # Routine bookkeeping, not a problem: nearly every planner proposal is snapped, and the
        # feed showed each one as "Problem: …" (219 of 583 exception lines at one q, 2026-09-05).
        # ``severity: low`` keeps the record for forensics and renders it as a plain note.
        event_sink(log_path, "exception", {
            "action": "proposal_snapped_to_lattice", "severity": "low",
            "hkl": [proposal.h, proposal.k, proposal.l],
            "old_window": [float(proposal.e_lo), float(proposal.e_hi), float(proposal.e_step)],
            "new_window": [e_lo, e_hi, e_step],
            "iter": it,
        })
    proposal.e_lo, proposal.e_hi, proposal.e_step = e_lo, e_hi, e_step
    return True


class ProposalOutsideEnergyDomain(ValueError):
    """The emitted grid cannot be kept inside the derived energy domain with three points left."""


def _clip_to_energy_domain(proposal, objective, cfg, it: int, *, event_sink=log_event) -> bool:
    """Keep the EMITTED grid inside the domain the design was scored in.

    The lattice snap moves bounds outward and the point guard coarsens the step, and neither knew
    the domain: a window designed at the optic floor came out one step below it, and one designed
    up to the instrument ceiling came out one step above (PR #37 review, second round). The domain
    is the planner's own (:func:`_planner_energy_domain`); bounds are moved INWARD along the
    lattice, dropping whole points, so the surviving grid is still the shared lattice. Returns
    ``True`` when the window moved; raises :class:`ProposalOutsideEnergyDomain` when fewer than
    three points survive, which the ranking loop treats like any other unrealizable design.
    """
    try:
        path = tuple(tuple(float(c) for c in point) for point in objective.path)
        lo_d, hi_d = _planner_energy_domain(objective, path, cfg.resolution_cb)
    except (PlannerRefusal, AttributeError, TypeError, ValueError) as error:
        _LOG.warning("energy domain unavailable at finalize (%s); window left as emitted", error)
        return False
    step = float(proposal.e_step)
    lo, hi = float(proposal.e_lo), float(proposal.e_hi)
    if step <= 0.0 or not all(math.isfinite(v) for v in (lo, hi, step)):
        return False
    new_lo = lo if lo >= lo_d - 1e-9 else lo + math.ceil((lo_d - lo) / step - 1e-9) * step
    new_hi = hi if hi <= hi_d + 1e-9 else hi - math.ceil((hi - hi_d) / step - 1e-9) * step
    new_lo, new_hi = round(new_lo, 6), round(new_hi, 6)
    if (new_lo, new_hi) == (lo, hi):
        return False
    n_points = int(math.floor((new_hi - new_lo) / step + 1e-9)) + 1 if new_hi > new_lo else 0
    if n_points < 3:
        raise ProposalOutsideEnergyDomain(
            f"proposal_outside_energy_domain: window [{lo:g}, {hi:g}] meV keeps fewer than three "
            f"points inside the domain [{lo_d:g}, {hi_d:g}]")
    if event_sink is not None:
        event_sink(cfg.log_path, "exception", {
            "action": "proposal_clipped_to_energy_domain",
            "hkl": [proposal.h, proposal.k, proposal.l],
            "old_window": [lo, hi, step], "new_window": [new_lo, new_hi, step],
            "energy_domain": [float(lo_d), float(hi_d)], "iter": it})
    proposal.e_lo, proposal.e_hi, proposal.n_points = new_lo, new_hi, n_points
    return True


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
        proposal.e_lo, proposal.e_hi, proposal.e_step, max_points=max_points)
    if grid.n_points > int(max_points):
        raise InstrumentPointLimitExceeded(
            "instrument_point_limit_exceeded: coarsening resolves to "
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


def _finalize_proposal(proposal, objective, cfg, budget_state: dict, *, it: int,
                       event_sink=log_event) -> None:
    """Normalize one proposal immediately before validation/submission.

    Two ceilings are authoritative and nothing else is: the frozen steering point ceiling and the
    live instrument neutrons-per-point limit. There is NO exposure floor — ``quicklook_ncount`` /
    ``production_batch_ncount`` were the quicklook/production ladder's minimum batch, and the ladder
    is retired: the planner chooses the exposure, its ladder runs from half a quicklook upward, and
    raising its choice to a policy floor would be legislated placement. Affordability is judged
    against the proposal's OWN request once the true emitted point count is known; hour budgets
    remain scan-boundary-only because ISAR has no invented ncount-to-seconds conversion.
    """
    policy = dict(getattr(objective, "steering", None) or {})
    _snap_to_lattice(proposal, cfg.log_path, it, event_sink=event_sink)
    limits = _runtime_limits(cfg, objective)
    cap = int(limits["max_points"])
    source = f"policy+{limits['sources'].get('max_points', 'default')}"
    _guard_point_count(
        proposal, cap, source, cfg.log_path, it, event_sink=event_sink)
    # The energy domain is the TARGETED planner's ("_planner_energy_domain" reads the target
    # branch and search band). A discovery step submits through a carrier objective whose
    # target fields are inert defaults, and its signed window was realized under its own
    # declared domain — clipping it here would hand the carrier's defaults scan authority.
    if not (getattr(objective, "meta", None) or {}).get("discover_and_map"):
        _clip_to_energy_domain(proposal, objective, cfg, it, event_sink=event_sink)
    n_points = int(proposal.n_points or 0)
    limits = _runtime_limits(cfg, objective, n_points)
    instrument_cap = int(limits["max_neutrons_per_point"])
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
        _stamp_proposal_provenance(proposal)
        return
    # The design is unaffordable as designed, or it is not: the planner ranked cheaper designs too,
    # and the loop tries the next one rather than silently buying a different measurement.
    remaining = max(float(budget["amount"]) - float(budget.get("spent", 0.0)), 0.0)
    cost = float(proposal.ncount) * float(max(n_points, 1))
    if cost > remaining:
        raise BudgetInsufficientForMinimumBatch(
            f"budget_insufficient_for_minimum_batch: the design costs {cost:g} neutrons "
            f"({n_points} points x {int(proposal.ncount)}), have {remaining:g}")
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
    """Whether ``row``'s ω₀ interval may SEAT convergence (ruling S3).

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
        "sample": row.get("sample"), "T": row.get("T"),
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
    # A master whose tracked target selection has been CLEARED (``_clear_tracked_selection`` nulls
    # ``omega0`` for an ambiguous or missing target) owns no target: its fit-local model interval
    # still describes the fit's primary mode, but nothing says that mode is the target. Reading it
    # anyway restored a certified precision the tracker had just withdrawn, and the planner then
    # bound that width to the nearest candidate and could claim the prize on it (PR #37 review,
    # second round, P1). The row stays a measurement (diagnostic half-width below); it cannot stand
    # as the target's delivered number until a selection exists.
    has_selection = row.get("omega0") is not None
    model_lo, model_med, model_hi = (row.get("model_interval_lo"),
                                         row.get("model_interval_med"),
                                         row.get("model_interval_hi"))
    model_robust = bool(deliverable and has_selection and row.get("model_robust") is True
                        and model_lo is not None and model_hi is not None)
    # The model-averaged interval is fit-local: it describes the fit's PRIMARY mode, and the
    # tracker may have selected another mode as the target (h = 3.0 on the reference path: the
    # selected acoustic at 5.99 meV, the model interval at 7.95-8.00 around the optic). An interval
    # that does not describe the row's ``omega0`` cannot certify it, and reading it anyway
    # certified the target on the rival's precision (PR #37 review, P1). The selected mode then
    # certifies on its OWN interval chain -- bootstrap, profile, trusted HESSE -- which
    # ``_select_mode`` copied onto the row from the candidate table.
    selected_omega0 = row.get("omega0")
    try:
        selected_omega0 = float(selected_omega0)
    except (TypeError, ValueError):
        selected_omega0 = math.nan
    # Only a row whose model IS settled (the session's ``model_robust``) may fall back to the
    # selected mode's own chain: a non-robust model has no settled answer for any mode to certify,
    # and that rule stands (test_a_non_model_robust_certification_cannot_produce_met).
    other_mode = bool(model_robust and math.isfinite(selected_omega0) and model_med is not None
                      and abs(float(model_med) - selected_omega0) > _PRIMARY_MODE_TOLERANCE_MEV)
    if other_mode:
        model_robust = False
    hw = None
    if model_robust:
        try:
            hw = calibration.calibrated_halfwidth(model_lo, model_hi, bucket=calibration_arg)
        except Exception:  # noqa: BLE001 — an invalid robust interval cannot certify
            model_robust = False
    diagnostic_hw, diagnostic_method = _diagnostic_halfwidth(row, calibration, calibration_arg)
    if model_robust:
        diagnostic_hw, diagnostic_method = hw, "model_averaged_predictive"
    own_certifiable = False
    if other_mode and deliverable and not model_robust and math.isfinite(selected_omega0):
        own_lo, own_hi, own_ok = _pick_interval(row)
        if _is_certifiable(row, own_ok):
            try:
                hw = calibration.calibrated_halfwidth(own_lo, own_hi, bucket=calibration_arg)
                own_certifiable = hw is not None and math.isfinite(float(hw))
            except Exception:  # noqa: BLE001 — an invalid interval cannot certify
                hw = None
    certifiable = model_robust or own_certifiable
    disp = row.get("disposition")
    return {
        "hkl": _row_hkl(row),
        "omega0": ((model_med if model_robust and model_med is not None else row.get("omega0"))
                   if deliverable else None),
        "halfwidth": hw if certifiable else None,
        "reliable": certifiable, "certifiable": certifiable,
        "model_robust": model_robust, "model_entropy": row.get("model_entropy"),
        "model_interval": ([model_lo, model_med, model_hi] if model_robust else None),
        # ``usable`` is what model_points and the signal-seen check read, so a withheld delivery has
        # to clear it too or the ω(q) model would still be fitted through the auxiliary energy.
        "usable": (deliverable and disp in _USABLE and row.get("omega0") is not None),
        "target_delivery_eligible": deliverable,
        "disposition": disp, "verdict": row.get("verdict"), "n_members": row.get("n_members"),
        "effective_exposure": effective_exposure, "exposure_source": exposure_source,
        "exposure_bucket": bucket,
        "diagnostic_halfwidth": diagnostic_hw, "interval_method": diagnostic_method,
        "calibration_version": _calibration_version(calibration),
        "branch_id": row.get("branch_id"), "branch_posterior": row.get("branch_posterior"),
        "assignment_entropy": row.get("assignment_entropy"),
        "target_branch": row.get("target_branch"),
        "candidate_id": row.get("candidate_id"), "candidate_state": row.get("candidate_state"),
        "branch_candidates": row.get("branch_candidates"),
        # The delivered fit's linear amplitudes, in the exposure basis ``effective_exposure`` names.
        # This is the intensity map the EIG planner prices designs against (_planner_scale).
        "amplitudes": row.get("amplitudes"),
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
        # The condition this master belongs to: the planner's world is one (sample, T) slice, and a
        # reused workspace may hold the same Q under another (PR #37 review, ``_slice_rows``).
        "sample": row.get("sample"), "T": row.get("T"),
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


def _read_master_rows(workspace, calibration: Calibration) -> list[dict]:
    """The seam's master-row reader bound to this module's row interpretation.

    File mechanics live in :func:`isar.drive.isar_io.readers.read_master_rows`; ``_row_hkl`` and
    ``_master_obs_from_row`` are the loop's science and stay here."""
    return read_master_rows(workspace, calibration, row_hkl=_row_hkl,
                            master_obs_from_row=_master_obs_from_row)


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


def _unfolded_master(rows, hkl) -> str | None:
    """Ledger id of the usable pooled master at ``hkl`` that is not yet a ``fold`` fit, else ``None``.

    Only a ``fold`` master (full tier: bootstrap samples pooled across the surviving structures and
    found unimodal) may enter the running dispersion prior — ``session_prior.add_point`` with
    ``require_fold``, Hard Rule 5. A fast-tier fit has no bootstrap samples, so its modality is
    ``unpooled`` whether or not its HESSE interval is trusted: it is not a fork, but nothing has
    examined whether it would be one, which is why the gate is right to hold it out and why this
    predicate does not stop at "trusted".
    """
    for r in rows:
        if not _hkl_close(_row_hkl(r), hkl):
            continue
        if not r.get("pooled"):
            continue
        if r.get("disposition") not in _USABLE or r.get("omega0") is None:
            continue
        # Only ``unpooled`` — never pooled across structures, the fast tier's signature — can
        # become a fold by refitting. A ``fork`` is already the full tier's answer (two answers),
        # and re-fitting it would only be asked again on every iteration.
        if r.get("modality") != "unpooled":
            return None
        return r.get("scan_id")
    return None


def _pending_anchor_escalations(workspace, plan_decision, *, sample=None, temperature_bin=None,
                                t_round=None, attempted=frozenset()) -> list[str]:
    """Master ids of prize points the planner is finished with (``within_bar``) whose fit is not yet
    a ``fold`` master, and so cannot enter the running dispersion prior as a belief anchor.

    The session's own identity tracker is happy to call a point ``confirmed`` off a fast HESSE fit
    and move on; the evidence pipeline is right to then refuse that same fit as an anchor. Left
    alone the two never reconcile: nothing else asks for the full-tier fit that would let a point
    the campaign is done navigating past also become durable evidence. Measured live at the zone
    boundary of the acoustic reference program (HANDOFF-eig.md §7.29/§7.30, 2026-09-03): every point
    from u=0.55 to u=0.95 reached ``track_state=confirmed`` at target_probability up to 0.9998 on a
    fast fit and never entered the dispersion prior, so the belief at the path's end had no anchor
    closer than half a zone away and could not separate two peaks 2 meV apart at any exposure. A
    first version escalated only UNtrusted HESSE fits (``_q_needs_full_errors``'s test) and the chain
    broke again at the first trusted one: trusted or not, a fast fit is never a fold.
    """
    hkls = [tuple(float(c) for c in point.hkl) for point in plan_decision.per_point
            if not point.exempt and point.within_bar]
    if not hkls:
        return []
    try:
        rows = read_results_csv(workspace)
    except FileNotFoundError:
        return []
    rows = _slice_rows(rows, sample, temperature_bin, t_round)
    ids = []
    for hkl in hkls:
        scan_id = _unfolded_master(rows, hkl)
        # A master escalated once at THIS data revision and still not a fold is not asked again:
        # the refit had its say, and re-buying the same analysis every iteration is the churn the
        # idempotency claim forbids. New members change the revision and reopen it — keying on the
        # id alone barred a q for the rest of the run after better data arrived (PR #37 review,
        # second round, P1).
        if scan_id and _anchor_escalation_key(rows, scan_id) not in attempted:
            ids.append(scan_id)
    return ids


def _anchor_escalation_key(rows, scan_id: str) -> str:
    """``<master id>@<master revision>``: one attempt per master AND data revision."""
    revision = next((r.get("master_revision") for r in rows
                     if r.get("scan_id") == scan_id and r.get("master_revision")), None)
    return f"{scan_id}@{revision}"


def _attempted_anchor_escalations(log_path) -> set[str]:
    """The escalation attempts already made, rebuilt from the durable log so a resumed run and an
    uninterrupted one hold the same eligibility (the in-memory set alone forgot them on restart)."""
    keys: set[str] = set()
    for record in read_log(log_path):
        if record.get("kind") != "escalate_fit":
            continue
        payload = record.get("payload") or {}
        if payload.get("phase") == "anchor":
            keys.update(str(k) for k in (payload.get("master_keys") or ()))
    return keys


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
        # The action identity a resumed scan must not lose. Without the sink a recovered auxiliary
        # probe becomes canonical by default and pools into the certified master; the scientific and
        # feasibility fingerprints are its audit identity. All three are already persisted on the
        # physical_submission event, so recovery only has to read them back.
        "evidence_sink": payload.get("evidence_sink") or CANONICAL_MASTER,
        "scientific_action_fingerprint": payload.get("scientific_action_fingerprint"),
        "feasibility_manifest_fingerprint": payload.get("feasibility_manifest_fingerprint"),
    }
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
    # The scan ran and analysis produced nothing at all (a fit crash, an analysis failure).
    "analysis_delivered_no_row": (RejectionClass.STRUCTURAL, "request"),
    # The scan ran, was analysed, and held no peak: a clean null. The absence is real evidence and
    # the planner prices it (``PlannerInput.nulls``), but re-buying the IDENTICAL window at the
    # identical exposure can only reproduce it — and the planner, whose beliefs the null barely
    # moves, will otherwise re-rank that same design first every iteration for the rest of the
    # campaign. Charged to the request only, so a wider window, a longer count or another q at the
    # same point all stay open; that is what "decide again" means here.
    "clean_null_delivered_no_peak": (RejectionClass.STRUCTURAL, "request"),
}


def classify_rejection(verdict: str) -> ActionRejection:
    """Class one refusal verdict into its durable consequence. See :class:`ActionRejection`."""
    rejection_class, scope = _REJECTION_RULES.get(
        str(verdict), (RejectionClass.TRANSIENT, "none"))
    return ActionRejection(verdict=str(verdict), rejection_class=rejection_class, scope=scope)


#: Two windows differing by less than a micro-eV are the same physical measurement — the wire
#: renders ``scan_command1`` at six significant digits, and no instrument resolves 1e-3 meV. The
#: planner's designs drift by far less than that as a belief settles, so an un-rounded key would let
#: a numerically-fresh copy of an already-refused design through on every iteration.
_DESIGN_KEY_DECIMALS = 3


def design_key(hkl, e_lo, e_hi, e_step, ncount) -> tuple:
    """The exact design a refusal or a non-result is charged to (addendum G.3).

    ``ncount = 0`` is the WINDOW key: the same geometry at any exposure. Validation judges whether
    the instrument can reach a grid, and the dose plays no part in that answer, so a geometry-shaped
    refusal condemns the window itself. Charging only the exact design let the planner — which now
    ranks every window against every exposure rung — re-offer one refused window once per rung, and
    a campaign spent 174 proposals discovering the same refusal.
    """
    return (tuple(round(float(value), 6) for value in hkl),
            round(float(e_lo), _DESIGN_KEY_DECIMALS), round(float(e_hi), _DESIGN_KEY_DECIMALS),
            round(float(e_step), _DESIGN_KEY_DECIMALS), int(ncount))


#: Refusals that judge the GEOMETRY of a request. The instrument answered about the window, not the
#: dose, so re-asking at another exposure buys nothing but another refusal. A budget refusal is the
#: opposite — it is entirely about the dose — and is deliberately absent.
_GEOMETRY_SHAPED_VERDICTS = frozenset({
    "validation_rejected", "insufficient_target_support", "infeasible_points_at_submit",
    "geometry_unreachable", "all_points_infeasible", "instrument_point_limit_exceeded"})


def suppressed_actions(log_path) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict],
                                          set[tuple]]:
    """Rebuild the durable suppression sets from the session log.

    Suppression is derived state, never a second durable store: a resume that could not rebuild it
    would re-propose something the instrument has already refused, every iteration, for the rest of
    the campaign. Returns ``(by_scientific_action, by_hkl, by_request)``.

    ``by_request`` is keyed by ``request_fingerprint`` and holds requests that were refused *as that
    exact request* (``validation_rejected`` / ``insufficient_target_support``). It deliberately does
    NOT feed ``by_action``: a different window at the same q answers the same scientific question and
    must stay open, so only the identical request is blocked from re-emitting. ``by_design`` is the
    same idea at the design the planner chose (addendum G.3). Transient entries are recorded for
    forensics and narrow nothing -- they carry no ``design_keys``, so a resume cannot revive them.
    """
    by_action: dict[str, dict] = {}
    by_hkl: dict[str, dict] = {}
    by_request: dict[str, dict] = {}
    by_design: set[tuple] = set()
    for event in read_log(log_path):
        if event.get("kind") != "action_suppressed":
            continue
        payload = event.get("payload") or {}
        scope = payload.get("scope")
        entry = {
            "class": str(payload.get("rejection_class") or RejectionClass.STRUCTURAL.value),
            "reason": str(payload.get("reason") or "previously refused"),
        }
        for entry_key in (payload.get("design_keys") or []):
            hkl, e_lo, e_hi, e_step, ncount = entry_key
            by_design.add(design_key(hkl, e_lo, e_hi, e_step, ncount))
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
    return by_action, by_hkl, by_request, by_design


# How many actions one iteration may try before giving the loop back its turn. A refused request
# should not cost the whole iteration, but an inventory that refuses everything must not spin either.
MAX_ACTION_ATTEMPTS_PER_ITERATION = 3


def _record_action_rejection(cfg, plan, proposal, verdict: str,
                             action_suppressions: dict, unreachable_hkl: dict,
                             request_suppressions: dict, *, it: int,
                             suppressed_designs: set | None = None):
    """Record one refusal at its correct scope and return the next action to try, if any.

    Always logs, because a refusal is evidence about the action space even when it suppresses
    nothing. Only a durable scope narrows the space *and* justifies moving on to another action this
    iteration:

    * ``geometry`` charges the **hkl** (``unreachable_hkl``);
    * ``action`` charges the **scientific action fingerprint** (``action_suppressions``);
    * ``request`` charges only the **request fingerprint** (``request_suppressions``) and the exact
      design (``suppressed_designs``) so neither is re-emitted, while the scientific action stays
      untouched and the planner is free to answer it with a different window or exposure;
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
    # The exact designs this refusal or non-result condemns (addendum G.3): the request as it
    # stands, AND the design the planner actually chose, which an in-flight feasibility trim will
    # have moved. Charging only the trimmed one leaves the planner's own design unsuppressed, and it
    # is re-ranked first, re-trimmed and re-refused on every remaining iteration. Recorded on the
    # event so a resume rebuilds the set from the durable log rather than a second store.
    # A TRANSIENT refusal -- the beam went down, the job was cancelled -- says nothing about the
    # design and must narrow nothing, in memory or on resume. Only a durable scope condemns it.
    keys = set()
    if rejection.scope != "none":
        keys = {design_key(hkl, proposal.e_lo, proposal.e_hi, proposal.e_step, proposal.ncount)}
        designed = (proposal.meta or {}).get("design_key")
        if designed:
            keys.add(design_key(*designed))
        if str(verdict) in _GEOMETRY_SHAPED_VERDICTS:
            keys.update(key[:-1] + (0,) for key in set(keys))
        if suppressed_designs is not None:
            suppressed_designs.update(keys)
    log_event(cfg.log_path, "action_suppressed", {
        "design_keys": [[list(key[0]), key[1], key[2], key[3], key[4]]
                        for key in sorted(keys)],
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
              "selector": getattr(objective, "target_selector", "tracked_branch"),
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


def _validated_campaign_provenance(payload: dict) -> dict:
    """The campaign-provenance block, refused at submission when the role is not in vocabulary or
    declares the canonical path without naming which path."""
    block = campaign_provenance(payload)
    role = block.get("topology_role")
    if role is not None and role not in TOPOLOGY_ROLES:
        raise ValueError(
            f"topology_role {role!r} is not in {TOPOLOGY_ROLES}; refusing to submit the request "
            "rather than poison the pre-dispatch stub at delivery")
    if role == "canonical_path" and block.get("path_fingerprint") is None:
        raise ValueError(
            "topology_role 'canonical_path' carries no path_fingerprint; refusing to submit the "
            "request rather than poison the pre-dispatch stub at delivery")
    return block


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
        # Authoritative mode only (``None`` in off mode). The realized_selected_* keys above
        # carry whichever request was actually executed, which on an authority grant is the
        # target_core request rather than the legacy one
        # ``ScanDesignRealizedComparison.selected_request_fingerprint`` returns. Recording the
        # deciding artifact and the executed identity under their own keys is what tells an auditor
        # which of the two answers this record means, instead of leaving the record and the
        # comparison beside it silently disagreeing.
        "scan_design_authority_decision_ref": payload.get("scan_design_authority_decision_ref"),
        "scan_design_authority_decision_fingerprint": payload.get(
            "scan_design_authority_decision_fingerprint"),
        "scan_design_authority_selected_request_fingerprint": payload.get(
            "scan_design_authority_selected_request_fingerprint"),
        "scan_design_authority_selected_grid_fingerprint": payload.get(
            "scan_design_authority_selected_grid_fingerprint"),
        "scan_design_eig_applicability": payload.get("scan_design_eig_applicability"),
        "scan_design_eig_invalidation_reason": payload.get(
            "scan_design_eig_invalidation_reason"),
        "scan_design_executed_grid_status": payload.get("scan_design_executed_grid_status"),
        "scan_design_executed_grid_reason": payload.get("scan_design_executed_grid_reason"),
        # The one channel by which analysis learns this scan was not ordinary evidence. Written here,
        # before the raw scan is consumed, because reduction decides whether to pool it and the session
        # decides how the tracker may read it — both from this record, neither from the driver.
        "evidence_sink": payload.get("evidence_sink") or CANONICAL_MASTER,
        # The campaign-provenance block (SCENE-4 PR 4.3, D2): carried verbatim off the submitted
        # request so the delivery pipeline stamps it onto the measurement-arena record without ever
        # re-deriving campaign identity. A request that declares none leaves all four None.
        # Validated HERE, at submission, where refusing costs one request — a malformed role
        # surviving to the pre-dispatch stub would abort the whole delivery batch and destroy the
        # attempt-proof record that exists to survive failure.
        **_validated_campaign_provenance(payload),
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


def _row_provenance(row: dict, proposal, card_state: dict, log_path=None, *, it=None) -> dict:
    """The submission a newly published physical row belongs to, as observation kwargs.

    A row stamped with the current proposal's acquisition id (or none at all — a workspace before
    acquisition provenance existed) takes the proposal's kind, exposure and roles. A row from an
    earlier acquisition — one whose own analysis failed and which a later pass caught up — takes
    them from the ``acquisition_provenance`` record the loop persisted at its submission
    (``_persist_acquisition_provenance``), so exposure, Q and fingerprint stay its own. ``current``
    says which case applied (the scan cost is charged once, to the current acquisition's rows).
    """
    meta = dict(getattr(proposal, "meta", None) or {})
    row_acq = row.get("acquisition_id")
    if row_acq in (None, "") or str(row_acq) == str(proposal.acquisition_id):
        return {
            "current": True, "kind": proposal.kind, "ncount": int(proposal.ncount),
            "acquisition_id": proposal.acquisition_id,
            "driver_inserted": bool(meta.get("driver_inserted", False)),
            "certification_eligible": bool(meta.get("certification_eligible", True)),
            "count_diagnostics": meta.get("count_diagnostics"),
            "acquisition_action_role": meta.get("acquisition_action_role"),
            "target_branch_role": meta.get("target_branch_role"),
            "rival_branch_role": meta.get("rival_branch_role"),
            "authoritative_evidence_revision": meta.get("authoritative_evidence_revision"),
            "proposal_fingerprint": meta.get("proposal_fingerprint"),
            "evidence_sink": meta.get("evidence_sink") or CANONICAL_MASTER,
        }
    for item in reversed((card_state or {}).get("acquisition_provenance") or []):
        if str(item.get("acquisition_id")) != str(row_acq):
            continue
        return {
            "current": False,
            "kind": str(item.get("scan_kind") or proposal.kind),
            "ncount": int(item.get("submitted_ncount") or proposal.ncount),
            "acquisition_id": str(row_acq),
            "driver_inserted": False, "certification_eligible": True, "count_diagnostics": None,
            "acquisition_action_role": item.get("acquisition_action_role"),
            "target_branch_role": item.get("target_branch_role"),
            "rival_branch_role": item.get("rival_branch_role"),
            "authoritative_evidence_revision": item.get("authoritative_evidence_revision"),
            "proposal_fingerprint": item.get("proposal_fingerprint"),
            "evidence_sink": item.get("evidence_sink") or CANONICAL_MASTER,
        }
    # Provenance exists for the id but no record matches: keep the row's OWN id (the planner's
    # scan index then finds its physical_submission) and say so, rather than re-badge it.
    if log_path is not None:
        log_event(log_path, "exception", {
            "action": "row_acquisition_unmatched", "iter": it, "scan": row.get("scan"),
            "acquisition_id": str(row_acq),
            "reason": "no persisted acquisition provenance for this id; exposure taken from the "
                      "current proposal"})
    return {
        "current": False, "kind": proposal.kind, "ncount": int(proposal.ncount),
        "acquisition_id": str(row_acq),
        "driver_inserted": False, "certification_eligible": True, "count_diagnostics": None,
        "acquisition_action_role": None, "target_branch_role": None, "rival_branch_role": None,
        "authoritative_evidence_revision": None, "proposal_fingerprint": None,
        "evidence_sink": CANONICAL_MASTER,
    }


def _consecutive_alerts(log_path, alert: str) -> int:
    """How many ``campaign_alert`` events of this ``alert`` have been raised since the last
    successful analysis (an ``analyze`` event) — the count the operator, and any connector, needs
    to tell one transient collision from a broken worker. Counts the trailing run only."""
    count = 0
    for record in reversed(read_log(log_path)):
        kind = record.get("kind")
        payload = record.get("payload") or {}
        # Only a SUCCESSFUL analysis resets the run: the iteration still emits ``analyze`` after a
        # caught failure (with ``status: failed``), and counting that as success reported every
        # failure of a dead worker as the first (PR #37 review, second round).
        if kind == "analyze" and payload.get("status") != "failed":
            break
        if kind == "campaign_alert" and payload.get("alert") == alert:
            count += 1
    return count


def _raise_campaign_alert(log_path, alert: str, error: BaseException, *, it, job_id=None,
                          hkl=None, **detail) -> dict:
    """Yell for help without stopping: log one ``campaign_alert`` event (owner ruling 2026-09-05,
    policy A). The campaign continues; the alert carries what failed, the traceback, and how many
    have piled up in a row, so a banner or a connector can say "one glitch" or "every scan since
    18:09". Returns the written record."""
    import traceback
    consecutive = _consecutive_alerts(log_path, alert) + 1
    payload = {
        "alert": alert, "policy": "continue", "consecutive": consecutive, "iter": it,
        "job_id": job_id, "hkl": hkl,
        "error": f"{type(error).__name__}: {error}"[:2000],
        "traceback": "".join(traceback.format_exception(type(error), error,
                                                        error.__traceback__))[-4000:],
        **detail,
    }
    _LOG.error("campaign alert %s (%d in a row) at iteration %s: %s", alert, consecutive, it,
               payload["error"])
    return log_event(log_path, "campaign_alert", payload)


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

    # The scan is shown the moment it lands; its fit is drawn over it when the fit completes
    # (owner, 2026-09-03). Parse it into the ledger NOW, before the fit activity is announced, so
    # a viewer never reads "fitting at q" of data it cannot see. Best effort: the fit that follows
    # ingests the same file itself, so a failure here costs only the early sighting.
    with timed(cfg.log_path, "isar_ingest", iteration=it):
        try:
            ingest_new(cfg.runner, cfg.workspace, [scan_path])
        except Exception as error:  # noqa: BLE001 — never let the sighting block the fit
            log_event(cfg.log_path, "exception", {
                "action": "early_ingest_failed", "iter": it, "job_id": job_id,
                "hkl": [proposal.h, proposal.k, proposal.l], "error": str(error)})
    log_event(cfg.log_path, "scan_landed", {
        "iter": it, "job_id": job_id, "q": [proposal.h, proposal.k, proposal.l],
        "n_points": int(executed_points), "ncount": int(proposal.ncount),
        "e_lo": float(proposal.e_lo), "e_hi": float(proposal.e_hi)})

    scan_tier, tier = _errors_tiers_for(proposal, cfg.workspace, cfg)
    promoted = proposal.kind in PRODUCTION_KINDS and tier in ("full", "bayes")
    if promoted:
        log_event(cfg.log_path, "escalate_fit", {
            "phase": "promote", "iter": it, "attempt": attempt,
            "hkl": [proposal.h, proposal.k, proposal.l], "from_tier": "fast",
            "to_tier": tier, "reason": "untrusted_hesse_no_reliable_interval"})
    stage = "full_fit" if tier in ("full", "bayes") else "fast_fit"
    analysis_failed = False
    try:
        with activity(cfg.log_path, stage, q=[proposal.h, proposal.k, proposal.l],
                      job_id=job_id, fit_tier=tier, iteration=it):
            with timed(cfg.log_path, "isar_run", iteration=it, errors_tier=tier, attempt=attempt):
                with analysis_operation(
                        cfg.log_path, f"reduce:{proposal.acquisition_id}:{tier}:{attempt}",
                        "pooled_reduction_and_fit",
                        limit_seconds=objective.analysis_limit_seconds,
                        allow_overrun_start=True, iteration=it,
                        q=[proposal.h, proposal.k, proposal.l]):
                    allowance = _analysis_allowance_seconds(cfg, objective)
                    refresh_analysis(
                        cfg.runner, cfg.workspace, [cfg.data_dir], errors_tier=tier,
                        scan_errors_tier=scan_tier, master_errors_tier=tier, render=False,
                        analysis_allowance_seconds=allowance,
                        finish_initial_wave=(True if allowance is not None else None),
                        enforce_completion=True,
                        analysis_limit_seconds=objective.analysis_limit_seconds)
    except AnalysisLimitExceeded:
        raise   # the campaign's own analysis ceiling is a stop condition, not a failure
    except Exception as error:  # noqa: BLE001 — policy A (owner ruling 2026-09-05): never stop, shout
        # The campaign does not stop on an analysis failure. The scan is on disk and in the ledger
        # (early ingest above), and ``refresh_analysis`` re-runs over every record next iteration,
        # so the un-analysed scan is picked up then; this iteration folds no new rows and the
        # planner runs one scan stale, which is harmless once. What must not happen is silence: the
        # failure is raised as a ``campaign_alert`` with its consecutive count, the channel every
        # yell-for-help connector subscribes to. Measured 2026-09-05: a Windows file-lock race on
        # results.csv (a reader held the file during the atomic swap) killed the worker and, with no
        # handler here, the campaign — 174 iterations in, with beam time left.
        _raise_campaign_alert(cfg.log_path, "analysis_failed", error, it=it, job_id=job_id,
                              hkl=[proposal.h, proposal.k, proposal.l], tier=tier)
        analysis_failed = True
    tracks_after = _read_branch_tracks(cfg.workspace)
    if tracks_after and tracks_after.get("revision") != tracks_before.get("revision"):
        log_event(cfg.log_path, "branch_track_update", {
            "iter": it, "revision": tracks_after.get("revision"),
            "reduced_q_origin_index": tracks_after.get("reduced_q_origin_index")})
        for reassignment in tracks_after.get("reassignments") or []:
            log_event(cfg.log_path, "branch_reassignment", {
                "iter": it, "revision": tracks_after.get("revision"), **dict(reassignment)})

    with timed(cfg.log_path, "fold_rows", iteration=it) as metrics:
        rows = _new_rows(cfg.workspace, consumed)
        scan_cost = float(executed_points) * float(proposal.ncount)
        # Since the campaign continues past a failed analysis (policy A), a later successful pass
        # can publish rows from EARLIER acquisitions alongside this one. Each row is joined to its
        # own submission through the acquisition provenance the session stamped on it; stamping
        # every row with the current proposal gave a recovered scan the newest scan's exposure,
        # Q and fingerprint, and the planner then saw 160M at one q and nothing at another
        # (PR #37 review, second round, P1).
        card_state = read_card_state(cfg.workspace)
        charged = False
        for row in rows:
            prov = _row_provenance(row, proposal, card_state, cfg.log_path, it=it)
            cost = scan_cost if (prov["current"] and not charged) else 0.0
            charged = charged or prov["current"]
            observations.append(_observation_from_row(
                row, prov["kind"], prov["ncount"], cfg.calibration,
                proposal_cost=cost,
                driver_inserted=prov["driver_inserted"],
                certification_eligible=prov["certification_eligible"],
                acquisition_id=prov["acquisition_id"],
                count_diagnostics=prov["count_diagnostics"],
                acquisition_action_role=prov["acquisition_action_role"],
                target_branch_role=prov["target_branch_role"],
                rival_branch_role=prov["rival_branch_role"],
                authoritative_evidence_revision=prov["authoritative_evidence_revision"],
                proposal_fingerprint=prov["proposal_fingerprint"],
                evidence_sink=prov["evidence_sink"]))
            scan_kind[(row.get("scan"), row.get("detector"))] = (
                prov["kind"], prov["ncount"], prov["acquisition_id"])
        metrics["n_rows"] = len(rows)

    timings = read_run_timings(cfg.workspace)
    analyze_payload = {"job_id": job_id, "errors_tier": tier,
                       "scan_errors_tier": scan_tier, "master_errors_tier": tier, "iter": it,
                       "status": "failed" if analysis_failed else "ok",
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
    """Run one proposed scan to a folded result.

    Owns the inner validate → submit → wait → fetch/save → (lazy init) → ``isar run`` → fold → analyze
    step and the instrument's own feasibility trim / target-support retries. There is no
    missed-window recentre-and-widen: a scan that finds nothing is ordinary evidence (a clean null),
    the planner sees the beliefs unchanged and the point still unmeasured, and it decides again.
    Mutates ``observations`` (appends folded or declined rows), ``consumed`` (via :func:`_new_rows`),
    ``scan_kind`` and ``budget_state`` in place; returns ``(rows, scan_counter, declined_verdict)``
    — the folded rows (empty on a decline), the advanced scan counter, and the decline verdict
    (``None`` on success) that :func:`classify_rejection` turns into a durable consequence."""
    client, runner = cfg.client, cfg.runner
    log_path, workspace, data_dir = cfg.log_path, cfg.workspace, cfg.data_dir
    attempt = 0
    rows: list[dict] = []
    feasibility_trimmed = False
    # The request as it stood before a feasibility trim. A decline after the trim charges the
    # *trimmed* fingerprint, so without this the untrimmed original stays unsuppressed and the
    # selector re-emits it next iteration, repeating trim → decline for the rest of the campaign.
    pre_trim_request_fingerprint: str | None = None
    declined_verdict: str | None = None
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
        log_event(log_path, "validate", {**validation_event_fields(
                                              val, feasible_points=feasible_points,
                                              target_support=target_support),
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
                # No declared target inside the window: longest-run is the honest choice.
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
            decision = plan_feasibility_response(
                val, grid_energies, target_energy=target_energy, acceptance=acceptance,
                already_trimmed=feasibility_trimmed)
            run, run_decision = decision.run, decision.reason
            run_length = 0 if run is None else run[1] - run[0] + 1
            if decision.reason == "largest_feasible_run_below_minimum":
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
                    **decision.event_fields,
                    "iter": it})
                _mark_request_superseded(proposal, pre_trim_request_fingerprint)
                observations.append(_declined_observation(proposal, "validation_rejected"))
                declined_verdict = "validation_rejected"
                break
            if decision.reason == "target_excluded":
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
                    **decision.event_fields,
                    "iter": it})
                _mark_request_superseded(proposal, pre_trim_request_fingerprint)
                observations.append(_declined_observation(proposal, "validation_rejected"))
                declined_verdict = "validation_rejected"
                break
            if decision.reason == "feasibility_trim_did_not_converge":
                # One trim per proposal. A second partial verdict on an already-trimmed window means
                # the instrument's answer is not converging, so refuse *this request* and let the
                # ladder try a different window rather than looping on the same geometry.
                log_event(log_path, "exception", {
                    "action": "feasibility_trim_did_not_converge",
                    "hkl": [proposal.h, proposal.k, proposal.l],
                    "feasible_points": feasible_points,
                    "requested_points": requested_points,
                    **decision.event_fields,
                    "iter": it})
                _mark_request_superseded(proposal, pre_trim_request_fingerprint)
                observations.append(_declined_observation(proposal, "validation_rejected"))
                declined_verdict = "validation_rejected"
                break
            if decision.kind == "trim":
                old_e_lo, old_e_hi = float(proposal.e_lo), float(proposal.e_hi)
                e_step = float(proposal.e_step)
                previous_request_fingerprint = (proposal.meta or {}).get(
                    "proposal_fingerprint")
                pre_trim_request_fingerprint = previous_request_fingerprint
                proposal.e_lo = float(decision.new_e_lo)
                proposal.e_hi = float(decision.new_e_hi)
                # ``_finalize_proposal`` re-derives ``n_points`` from the mutated window through
                # ``_guard_point_count``, so the trimmed grid is re-costed and re-fingerprinted the
                # same way the step-shrink path is.
                _finalize_proposal(proposal, objective, cfg, budget_state, it=it)
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
                    **decision.event_fields,
                    "iter": it, "attempt": attempt})
                continue
            # No localization: fall through. The zero-feasible branch and the submit-time
            # ``infeasible_points`` fallback below both remain in force.
            _LOG.warning(
                "validation reported %d of %d points feasible with no positional manifest; "
                "submitting unchanged", feasible_points, requested_points)
        if (min_support > 0 and target_support is not None and target_support < min_support
                and val.get("would_queue", False)):
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

        with timed(log_path, "submit", iteration=it, attempt=attempt) as _m:
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
            # The campaign-provenance block (SCENE-4.3 D2/C1), carried off the proposal the
            # program-step runner built. Emitted only for a request that actually declares one, so
            # every targeted submission payload — and therefore every `run` event a resume replays
            # — stays byte-identical to what it was; `campaign_provenance` reads an absent field as
            # None either way, which is the manual/legacy lane.
            **{name: (proposal.meta or {})[name] for name in CAMPAIGN_PROVENANCE_FIELDS
               if (proposal.meta or {}).get(name) is not None},
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

        rows, _data = _finalize_completed_submission(
            cfg=cfg, objective=objective, proposal=proposal,
            submission_payload=submission_payload, budget_state=budget_state,
            observations=observations, consumed=consumed, scan_kind=scan_kind,
            it=it, attempt=attempt)
        # A scan whose window held nothing is a clean null, not a failure to recover from: it is
        # ordinary evidence of absence, the planner sees it as such, and the next decision is the
        # planner's. There is no recentre-and-widen retry.
        break

    return rows, scan_counter, declined_verdict


def _certification_revision(point: dict) -> str | None:
    revision = point.get("master_revision")
    return str(revision) if revision not in (None, "") else None


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
                with analysis_operation(
                        log_path, (f"certification:{request_path.stem}"
                                   if request_path is not None else "certification:callable-seam"),
                        "final_certification", limit_seconds=objective.analysis_limit_seconds):
                    allowance = _analysis_allowance_seconds(cfg, objective)
                    refresh_analysis(
                        cfg.runner, workspace, [data_dir], errors_tier=full_tier,
                        force=not master_only, render=False,
                        certification_request=request_path,
                        analysis_allowance_seconds=allowance,
                        finish_initial_wave=(False if allowance is not None else None),
                        enforce_completion=True,
                        analysis_limit_seconds=objective.analysis_limit_seconds)
            if request_path is not None:
                _verify_certification_publication(request_path, workspace)
            log_activity_progress(log_path, activity_id, completed=total_points,
                                  total=total_points, progress_unit="points")
        # Physical rows are deliberately unchanged by master-only certification. Steering
        # observations therefore remain the same provenance ledger; final stop evaluation reads the
        # newly published pooled masters directly from results.csv.
        # Embed the certification subprocess timings (iter=None — the certify pass is reported
        # separately, never averaged into per-iteration stats).
        timings = read_run_timings(workspace)
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


# --------------------------------------------------------------------------- the EIG planner seam

class PlannerRefusal(RuntimeError):
    """A declared input the EIG planner needs is missing, so the campaign cannot be planned.

    Raised rather than defaulted: guessing a zone period or an energy domain would silently place
    every scan of the campaign in the wrong coordinate.
    """


#: The card template ``isar init`` builds a TAVI workspace's ``card.policy.toml`` from. The drive's
#: targeted lane is TAVI-only (``_scan_body`` / ``_validate_body`` speak that wire), so this is the
#: declaration the workspace WILL carry, read one step early.
_TAVI_CARD_TEMPLATE = Path(__file__).resolve().parents[1] / "parsers" / "cards" / "tavi.policy.toml"


def _declared_card_policy(workspace) -> dict:
    """The scientist-owned card policy: the workspace's own card once it exists, else its template.

    ``isar init`` writes ``card.policy.toml`` only after the FIRST scan has landed, and the planner
    needs the zone period and the sample temperature before the first scan is chosen. Reading the
    template the workspace is about to be initialised from is the same declaration one step earlier,
    not a guessed default — a workspace whose card later declares different values uses those from
    then on, because this reads the card whenever it is there.
    """
    import tomllib
    path = Path(workspace) / "card.policy.toml"
    source = path if path.exists() else _TAVI_CARD_TEMPLATE
    try:
        return tomllib.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError) as error:
        _LOG.warning("could not read the card policy from %s: %s", source, error)
        return {}


def _declared_zone_period(workspace) -> float | None:
    """The reduced-zone fold period the branch beliefs' ``u`` is measured in."""
    declared = _workspace_zone_period(Path(workspace))
    if declared:
        return declared
    value = (_declared_card_policy(workspace).get("dispersion") or {}).get("zone_period")
    return float(value) if value is not None else None


#: An optic branch lives above the elastic line: the domain a targeted optic campaign plans in
#: starts this many elastic-resolution FWHMs above zero transfer (owner ruling 2026-09-05, three so
#: the line's wings never leak in). A class prior on "optic", like "gapless" is on "acoustic" — not a
#: placement rule: it says where the phenomenon can be, and the planner still chooses every window.
OPTIC_ELASTIC_EXCLUSION_FWHM = 3.0


def _planner_energy_domain(objective, path, resolution_cb) -> tuple[float, float]:
    """The energy domain the planner builds windows in and truncates beliefs against.

    Two honest sources and nothing phonon-specific (owner ruling 2026-09-05; the retired
    ``[-2, 12]`` / ``[-9, 9]`` per-branch ranges were the demo sample's numbers and made an
    acoustic branch above 9 meV or an optic branch above 12 meV unmeasurable):

    - above: the objective's ``energy_search_meV[1]``, which the compiler sets to the card's
      ``max_energy_transfer_meV`` — the instrument's kinematic ceiling;
    - below: zero for an acoustic target (the branch is gapless there), and
      :data:`OPTIC_ELASTIC_EXCLUSION_FWHM` elastic resolution widths for an optic target. The
      elastic FWHM is the resolution callback at zero transfer along the path (median); its
      advisory default stands in when the instrument cannot answer, as everywhere else.
    """
    lo, hi = (float(v) for v in objective.energy_search_meV)
    lo = max(lo, 0.0)
    if str(getattr(objective, "target_branch", "acoustic")) == "optic":
        widths = []
        for point in path:
            try:
                widths.append(float(_resolve_fwhm(resolution_cb, point[0], point[1], point[2], 0.0)))
            except Exception as error:                     # noqa: BLE001 - resolution is advisory
                _LOG.warning("could not resolve the elastic FWHM at %s (%s); using %s meV",
                             point, error, DEFAULT_FWHM_MEV)
                widths.append(float(DEFAULT_FWHM_MEV))
        elastic_fwhm = float(statistics.median(widths)) if widths else float(DEFAULT_FWHM_MEV)
        lo = max(lo, OPTIC_ELASTIC_EXCLUSION_FWHM * elastic_fwhm)
    if not lo < hi:
        raise PlannerRefusal(
            f"campaign refused: the energy domain [{lo:g}, {hi:g}] meV is empty — the card's "
            "max_energy_transfer_meV sits below the elastic exclusion")
    return lo, hi


def _reduced_u(hkl, period: float) -> float:
    """Reduced-zone coordinate — each component folded about its nearest zone centre, Euclidean.

    Identical to :func:`isar.session.branch_assignment.reduced_q`, which is what wrote the ``u`` of
    every anchor in ``card.state.json["branch_belief_models"]``. A belief prediction is only
    meaningful in the coordinate its anchors were stored in, so the two must agree exactly.
    """
    half = 0.5 * float(period)
    return math.sqrt(sum(((((float(c) + half) % float(period)) - half) ** 2) for c in hkl))


def _bose_plus_one(omega_meV: float, temperature_K: float) -> float:
    """``n(w,T)+1 = 1/(1-exp(-w/k_BT))`` — the factor :func:`isar.analyze.phonon.bose_occupation`
    applies, re-stated here because the drive may not import the analysis package."""
    if temperature_K is None or float(temperature_K) <= 0.0 or float(omega_meV) <= 0.0:
        return 1.0
    return 1.0 / -math.expm1(-float(omega_meV) / (KB_MEV_PER_K * float(temperature_K)))


def _prior_belief_row(hypothesis: Mapping, period: float) -> dict:
    """A prior-only belief row for a branch with no measured anchors yet (the cold start).

    ``_store_branch_beliefs`` writes nothing until the first scan lands, so without this the planner
    would see one branch and could never price "which of the two is this peak".
    """
    return {"branch_id": str(hypothesis.get("name")), "anchors": [], "n_anchors": 0,
            "period": float(period),
            "parameter_prior": {"zc_energy": dict(hypothesis.get("zc_energy") or {}),
                                "slope_zc": dict(hypothesis.get("slope_zc") or {}),
                                "confidence": float(hypothesis.get("confidence", 0.0) or 0.0)}}


def _belief_sample(durable_state: dict, cfg) -> str:
    """Which sample's belief rows ARE this campaign's hypothesis map.

    The session layer stamps the sample name it parsed off the instrument payload (``"tavi"``); the
    driver submits under its own configured name (``cfg.sample``, ``"Al_phonon_DFT"`` by default).
    They are routinely different, and matching on the driver's name found nothing: every iteration
    of the first live run planned against a synthesised class prior while six confirmed anchors sat
    in ``card.state.json``, and asked for 100-point windows where a settled belief wants twelve.

    So the map is the workspace's own. One sample in the workspace IS the campaign's sample,
    whatever the driver calls it. Several, and the driver's name is what disambiguates them — and
    if it names none of them there is no map here, which is a refusal, not a licence to plan against
    a prior the workspace has already superseded.
    """
    samples = sorted({str(row["sample"])
                      for row in (durable_state.get("branch_belief_models") or [])
                      if isinstance(row, Mapping) and row.get("sample")})
    if not samples:
        return str(cfg.sample)
    if len(samples) == 1:
        if samples[0] != str(cfg.sample):
            _LOG.info("planning against the workspace's belief rows for sample %r "
                      "(the campaign submits as %r)", samples[0], cfg.sample)
        return samples[0]
    if str(cfg.sample) in samples:
        return str(cfg.sample)
    raise PlannerRefusal(
        f"campaign refused: the workspace holds belief rows for {', '.join(samples)} and none for "
        f"{cfg.sample!r}, so there is no hypothesis map for this campaign to plan against; planning "
        "on the class prior instead would ignore evidence the workspace already holds")


def _planner_beliefs(durable_state: dict, objective, cfg, sample: str, temperature_bin,
                     period: float) -> dict:
    """The hypothesis map: one belief row per modelled branch, measured slice first, prior otherwise.

    BOTH branches are always present whichever is the target — the planner's joint peak-to-branch
    matching is what lets a scan reject the rival, and it cannot run against a single branch.
    """
    slices = _belief_slices(durable_state, sample=sample)
    rows = {str(row["branch_id"]): dict(row)
            for row in (slices.get(temperature_bin) or []) if row.get("branch_id")}
    declared = {str(h.get("name")): h for h in (objective.hypotheses or [])
                if isinstance(h, Mapping) and h.get("name") not in (None, "null")}
    for name in sorted(set(declared) | {"acoustic", "optic"}):
        if name in rows:
            continue
        hypothesis = declared.get(name)
        if hypothesis is None:
            hypothesis = preset(name, objective.energy_search_meV).to_dict()
        rows[name] = _prior_belief_row(hypothesis, period)
    return rows


#: How far the row's ``omega0`` may sit from a fitted mode and still be that mode — a resolution
#: width; beyond it the certifying interval describes no candidate and binds to none.
_PRIMARY_MODE_TOLERANCE_MEV = 0.5


def _planner_candidates(master_rows: list[dict], path, energy_domain) -> dict[int, list]:
    """Grid index to the pooled master's fitted peaks, as planner candidates.

    The candidate's sigma is the master row's CERTIFYING half-width and nothing else: the
    model-averaged interval ``_master_obs_from_row`` publishes as ``halfwidth`` alongside
    ``certifiable``. That is the number the campaign will certify and report, and the prize is
    stated over the reported number.

    ``diagnostic_halfwidth`` is deliberately NOT accepted, though it is the same calibrated scale.
    A row that is not model-robust publishes it *instead of* a certifying interval — the fit has an
    answer but the model behind it is not settled — and reading it here let the prize be claimed on
    a point that certification then refused to certify. The two numbers are equal on a model-robust
    row and only differ where it matters, which is exactly why substituting one for the other is
    invisible until a campaign certifies something it never measured. Never the fit's raw
    ``omega0_err`` either: a fast-tier HESSE error of 0.0003 meV would declare a point settled to a
    precision the published interval (0.06 meV) does not carry.

    Everything else is "detected, precision not established" — sigma = half the positive energy
    domain: a non-model-robust row, a fork (the fit has two answers), a pool-inconsistent row (its
    members disagree), or no interval at all. The peak still enters as evidence for *assignment*,
    and the planner still values a scan there, which is what escalates the errors tier and buys the
    counts the point needs.

    The certifying half-width belongs to ONE candidate: the mode the interval describes. It is the
    model-averaged predictive of the fit's primary mode, whose location the row publishes as
    ``omega0`` (``model_interval_med``); the session's tracker may later select a secondary mode or
    clear the selection as ambiguous without that interval moving (``_clear_tracked_selection``
    leaves ``model_interval`` alone). Handing it to every fitted peak let a secondary peak carry the
    primary mode's certified precision into the prize (PR #37 review), so it goes to the candidate
    nearest ``omega0`` and to no other; the rest are "detected, precision not established".

    Deliberately NOT gated on the tracker's ``track_state``: the session assigns each peak on its
    own (``assign``) and reads a lone near-fold peak as ``ambiguous`` under the optic's broad class
    prior, where the planner's joint matching with window-absence evidence settles it. Making the
    session's verdict a gate re-created the stall at h = 2.05 (23 scans, nothing within the bar).
    """
    unknown_sigma = 0.5 * (float(energy_domain[1]) - max(float(energy_domain[0]), 0.0))
    out: dict[int, list] = {}
    for row in master_rows:
        hkl = row.get("hkl")
        index = None if hkl is None else match_grid_index(tuple(hkl), path)
        if index is None:
            continue
        try:
            halfwidth = float(row.get("halfwidth"))
        except (TypeError, ValueError):
            halfwidth = None
        trusted = (bool(row.get("certifiable")) and row.get("modality") != "fork"
                   and row.get("pool_consistent") is not False
                   and halfwidth is not None and math.isfinite(halfwidth) and halfwidth > 0.0)
        energies = []
        for candidate in (row.get("branch_candidates") or []):
            try:
                energy = float(candidate.get("energy_meV", candidate.get("omega0")))
            except (TypeError, ValueError):
                continue
            if math.isfinite(energy):
                energies.append((energy, candidate))
        primary = None
        try:
            omega0 = float(row.get("omega0"))
        except (TypeError, ValueError):
            omega0 = math.nan
        if trusted and energies and math.isfinite(omega0):
            nearest = min(range(len(energies)), key=lambda j: abs(energies[j][0] - omega0))
            if abs(energies[nearest][0] - omega0) <= _PRIMARY_MODE_TOLERANCE_MEV:
                primary = nearest
        elif trusted and len(energies) == 1:
            primary = 0                   # a lone mode is the one the interval describes
        candidates = []
        for j, (energy, candidate) in enumerate(energies):
            sigma = halfwidth if j == primary else unknown_sigma
            candidates.append(Candidate(energy, max(sigma, 1e-9),
                                        dict(candidate.get("probabilities") or {})))
        out[index] = candidates
    return out


def _planner_scans(log_path, observations: list[dict], path, branches) -> dict[int, list]:
    """Grid index to the scans already executed there: where each looked and for how long.

    What a scan contributes is its share of the exposure over an energy. Whether a branch was
    delivered is NOT recorded per scan, deliberately: ISAR pools every scan at a q into a master
    and fits the master, so the planner reads delivery off the point's pooled candidates. Asking
    one scan's own fit counted a single pooled observation once per scan that went into it.

    A count-untrusted arena is NOT evidence either way — a validation decline, a job that never
    reached ``done`` — and is skipped here (it carries no ``scan``). The window and exposure come
    from the durable ``physical_submission`` record, so a resume rebuilds the same set.
    """
    branches = tuple(str(b) for b in branches)
    windows: dict[str, dict] = {}
    for event in read_log(log_path):
        if event.get("kind") != "physical_submission":
            continue
        payload = event.get("payload") or {}
        if payload.get("acquisition_id") is not None:
            windows[str(payload["acquisition_id"])] = payload
    out: dict[int, list] = {}
    for observation in observations:
        if observation.get("kind") == "pooled_master_update" or observation.get("scan") is None:
            continue
        payload = windows.get(str(observation.get("acquisition_id")))
        if payload is None or payload.get("search_energy_lo") is None:
            continue
        index = match_grid_index(tuple(payload.get("hkl") or ()), path)
        if index is None:
            continue
        out.setdefault(index, []).append(ExecutedScan(
            float(payload["search_energy_lo"]), float(payload["search_energy_hi"]),
            int(payload.get("submitted_ncount") or payload.get("ncount") or 0)))
    # Every scan stands, repeats included: two identical scans are two independent observations and
    # the second really is more evidence than the first. The scenario-weight underflow that made the
    # planner divide by zero is guarded in the planner now, not by hiding measurements from it here.
    return out


def _ingest_counts(data_dir) -> dict[tuple, dict]:
    """``(scan_number, detector_id)`` -> the ``isar.unit_data/1`` record analysis ingested.

    The counts are the authoritative measurement (``DESIGN_GOALS``: fits are the human-readable
    layer, not the proof layer), and this is where they are durable. Found by scanning the directory
    rather than rebuilding the filename, because the name encoding belongs to
    :mod:`isar.session.unit_data` and the drive may not import the session package.
    """
    out: dict[tuple, dict] = {}
    ingest = Path(data_dir) / "ingest"
    if not ingest.is_dir():
        return out
    for path in sorted(ingest.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            _LOG.warning("could not read the ingested counts at %s: %s", path, error)
            continue
        if record.get("scan_number") is None:
            continue
        out[(str(record["scan_number"]), str(record.get("detector_id")))] = record
    return out


def _candidate_probabilities(candidate: Mapping, row: Mapping, branches,
                             index: int | None = None) -> dict[str, float]:
    """``P(branch | candidate)`` for one fitted mode, over the modelled branches.

    Only a POOLED master's candidates carry ``probabilities`` — the per-scan table
    (``evidence_update._mode_candidates``) has none, and the scan row's ``branch_id`` is the label
    the tracker gave THE SELECTED delivery, ``selected_candidate_index``. A label is a probability
    of one for that one mode: the tracker said this mode is that branch, so ``{branch: 1.0}`` with
    zero on the others is what it asserts. Every other mode on the row asserts nothing and returns
    an empty mapping — handing the row label to a two-mode fit's second lobe entered the optic's
    counts into the acoustic brightness (PR #37 review).
    """
    probabilities = {str(k): float(v)
                     for k, v in (candidate.get("probabilities") or {}).items()
                     if str(k) in branches}
    if probabilities:
        return {b: probabilities.get(b, 0.0) for b in branches}
    selected = row.get("selected_candidate_index")
    if selected is None and len(row.get("branch_candidates") or ()) == 1:
        selected = 0                      # a single fitted mode is the selection, named or not
    if index is not None and (selected is None or int(selected) != int(index)):
        return {}
    branch = str(row.get("branch_id") or "")
    if branch in branches:
        return {b: (1.0 if b == branch else 0.0) for b in branches}
    return {}


def _lobe_area(x, counts, background: float, centre: float, half_width: float, step: float,
               *, exclude=()) -> float | None:
    """Counts*meV above ``background`` within ``half_width`` of ``centre``, ignoring the points that
    belong to another lobe. ``None`` when this scan did not measure this lobe at all.

    ``None`` and ``0.0`` say different things and both are answers: ``None`` is "the window never
    looked here", which contributes nothing; ``0.0`` is "the window looked and there is nothing",
    which is a measurement and belongs in the median.
    """
    if not (x[0] - 1e-9 <= centre <= x[-1] + 1e-9):
        return None
    inside = [(value, count) for value, count in zip(x, counts)
              if abs(value - centre) <= half_width
              and not any(abs(value - other) <= half_width for other in exclude)]
    if not inside:
        return None
    return sum(max(count - background, 0.0) * step for _value, count in inside)


def _planner_scale(cfg, observations: list[dict], path, branches, temperature_K: float,
                   fwhm_meV) -> IntensityScale:
    """The intensity map, integrated from the counts each scan actually recorded.

    Read from the counts, not from the fits: the fitted amplitudes do not carry the convention this
    needs. Measured on the second live run, master (41,0,0) published ``phonon.A = 6.7e-5`` against
    an ``effective_exposure`` of 5e6 -- 1.3e-11 counts*meV per neutron -- while the scan that
    produced it holds an 18-count peak over a background of 6, about 8 counts*meV above background.
    The forward model was five orders of magnitude too dim, so no design could complete a point,
    every expected information was exactly zero, and the planner bought the cheapest probe forever.
    ``bg.BG`` swings 0.05-0.64 between scans of the same declared background, which is the same
    story. Counts are authoritative and fits are disposable, so the map comes from the counts.

    Per executed scan at a path point, from its ingested ``x``/``counts`` and its submitted
    ``ncount``:

    * ``background`` is the 20th percentile of the scan's counts -- the level away from the lobes;
    * a delivered candidate's **Stokes lobe area** is the counts above background within two
      resolution widths of its energy. When the window also holds the anti-Stokes lobe at ``-w`` its
      area joins in and the total divides by ``2n+1`` rather than ``n+1`` (``n`` the Bose occupation
      at that energy and the campaign temperature) -- the planner's ``peak_counts_meV`` is the
      **pre-Bose** area, and the forward model puts the occupation back;
    * the **elastic** area is measured the same way about zero, when zero is inside the window;
    * all three divide by ``ncount``, giving counts*meV (or counts per point) per ONE neutron.

    QC-rejected points are honoured, never deleted (hard rule 5): a point the reducer masked
    contributes to neither a lobe area nor the background percentile.

    TODO: this integrates the record's RAW abscissa, which is the energy transfer only while the
    card declares ``raw_abscissa_kind = "absolute"`` -- true of TAVI and of every instrument this
    lane drives today. A ``needs_zero`` instrument (hard rule 4) records an uncalibrated axis, and
    its counts must be shifted by the calibrated energy zero before any of these windows mean
    anything. Do not run this map on one until that shift is applied.

    The median over scans per branch, so one clipped window or one mislabelled mode cannot move the
    map. A branch nothing has measured keeps the planner's class default.

    # ponytail: an elastic line closer than two resolution widths to the phonon shares its counts
    # with it, and both areas are then over-read. Real below ~1 meV at this resolution; the median
    # over the path is what keeps it out of the map. Fix by fitting the two lobes jointly if the
    # low-q points ever need their own scale.
    """
    branches = tuple(str(b) for b in branches)
    records = _ingest_counts(cfg.data_dir)
    if not records:
        return default_intensity_scale(branches)
    submissions: dict[str, dict] = {}
    for event in read_log(cfg.log_path):
        if event.get("kind") != "physical_submission":
            continue
        payload = event.get("payload") or {}
        if payload.get("acquisition_id") is not None:
            submissions[str(payload["acquisition_id"])] = payload

    peaks: dict[str, list[float]] = {}
    elastic: list[float] = []
    background_levels: list[float] = []
    for observation in observations:
        if observation.get("kind") == "pooled_master_update" or observation.get("scan") is None:
            continue
        record = records.get((str(observation["scan"]), str(observation.get("detector"))))
        submission = submissions.get(str(observation.get("acquisition_id")))
        if record is None or submission is None:
            continue
        index = match_grid_index(tuple(submission.get("hkl") or ()), path)
        try:
            ncount = float(submission.get("submitted_ncount") or submission.get("ncount") or 0.0)
        except (TypeError, ValueError):
            continue
        if index is None or ncount <= 0.0:
            continue
        try:
            raw_x = [float(v) for v in (record.get("x") or ())]
            raw_counts = [float(v) for v in (record.get("counts") or ())]
        except (TypeError, ValueError):
            continue
        if len(raw_x) < 3 or len(raw_counts) != len(raw_x):
            continue
        step = abs(raw_x[1] - raw_x[0])
        if step <= 0.0:
            continue
        # QC masks, never deletes (hard rule 5): a rejected point is still in the record and must
        # weigh on nothing -- not a lobe area, not the background level.
        mask = record.get("mask")
        keep = ([bool(v) for v in mask] if isinstance(mask, list) and len(mask) == len(raw_x)
                else [True] * len(raw_x))
        x = [value for value, ok in zip(raw_x, keep) if ok]
        counts = [count for count, ok in zip(raw_counts, keep) if ok]
        if len(x) < 3:
            continue
        # Nearest-rank 20th percentile: the level the scan sits at away from its lobes.
        background = sorted(counts)[min(int(0.2 * len(counts)), len(counts) - 1)]
        background_levels.append(background / ncount)
        half_width = 2.0 * float(fwhm_meV[index])
        # The elastic line is only measured where nothing else is sitting on it. At the reduced-q
        # origin the acoustic Stokes/anti-Stokes pair straddles zero inside one resolution width, and
        # integrating that as "elastic" is what made the elastic term read ~30 counts*meV per 1e7 at
        # the near-fold points and ~0 everywhere else. Every delivered mode's own two lobes are
        # excluded; when nothing is left, this scan did not measure the elastic and says nothing.
        # ponytail: the converse contamination -- a real elastic line leaking into an unresolved
        # phonon's lobe -- is not corrected, because on this instrument there is no elastic line to
        # leak (scans at 1.8 and 2.7 meV read exactly zero counts at dE = 0). Split the two by a
        # joint fit if a sample with a bright elastic ever needs its own scale.
        occupied = [sign * float(candidate.get("energy_meV", candidate.get("omega0") or 0.0))
                    for candidate in (observation.get("branch_candidates") or ())
                    for sign in (1.0, -1.0)]
        area = _lobe_area(x, counts, background, 0.0, half_width, step, exclude=occupied)
        if area is not None:
            elastic.append(area / ncount)
        for j, candidate in enumerate(observation.get("branch_candidates") or ()):
            # The brightness goes to the mode's most probable branch, whole, rather than split
            # across branches in proportion. A lobe area is one physical quantity — those counts
            # came from one branch, whichever it was — and splitting it would hand the optic a
            # fraction of an acoustic peak and make the map describe a mode that does not exist.
            # The median over scans is what survives a wrong argmax; a smeared area would not.
            probabilities = _candidate_probabilities(candidate, observation, branches, j)
            branch = max(probabilities, key=probabilities.__getitem__) if probabilities else None
            try:
                energy = float(candidate.get("energy_meV", candidate.get("omega0")))
            except (TypeError, ValueError):
                continue
            if branch is None or probabilities[branch] <= 0.0:
                continue
            if not math.isfinite(energy) or energy <= 0.0:
                continue
            stokes = _lobe_area(x, counts, background, energy, half_width, step)
            if stokes is None or stokes <= 0.0:
                continue
            anti_stokes = _lobe_area(x, counts, background, -energy, half_width, step)
            occupation = _bose_plus_one(energy, temperature_K)                  # n + 1
            intrinsic = (stokes / occupation if anti_stokes is None
                         else (stokes + anti_stokes) / (2.0 * occupation - 1.0))   # 2n + 1
            if intrinsic > 0.0:
                peaks.setdefault(branch, []).append(intrinsic / ncount)

    default = default_intensity_scale(branches)

    def middle(values: list[float], fallback: float) -> float:
        return statistics.median(values) if values else float(fallback)

    # A background of zero is a real reading — at five million neutrons most of these windows hold
    # no background counts at all — but the forward model divides by the rate, so it needs the
    # smallest level the scan could have resolved rather than the zero it measured: half a count.
    background = middle(background_levels, default.background_per_point)
    if background <= 0.0:
        exposures = [float(s.get("submitted_ncount") or s.get("ncount") or 0.0)
                     for s in submissions.values()]
        exposures = [value for value in exposures if value > 0.0]
        background = (0.5 / statistics.median(exposures) if exposures
                      else default.background_per_point)
    return IntensityScale(
        peak_counts_meV={b: middle(peaks.get(b, []), default.peak_counts_meV[b])
                         for b in branches},
        elastic_counts_meV=middle(elastic, default.elastic_counts_meV),
        background_per_point=background)


def _planner_temperature(durable_state: dict, objective, cfg, sample: str,
                         temperature_bin) -> float:
    """The temperature the Bose factor runs at, from the same sample the hypothesis map came from.

    Ladder, re-walked every iteration so the first measured slice takes over the moment it lands:
    the resolved (complete) slice, else the newest slice of that sample at all, else the objective's
    declared temperature, else the card's ``[temperature_rule]`` setpoint. TAVI's ``GET /state``
    carries no sample temperature — its ``parameters`` block is all geometry and the simulator only
    reports ``temperature`` inside a completed scan — so the card gap-filler is the last declared
    source, and there is no bare constant below it.
    """
    if temperature_bin is not None:
        return float(temperature_bin)
    declared = sorted(_belief_slices(durable_state, sample=sample))
    if declared:
        return float(declared[-1])
    if getattr(objective, "target_temperature_K", None) is not None:
        return float(objective.target_temperature_K)
    setpoint = (_declared_card_policy(cfg.workspace).get("temperature_rule") or {}).get("setpoint")
    if setpoint is not None:
        _LOG.info("no measured belief slice yet; the EIG forward model uses the card's declared "
                  "sample setpoint %.4g K for the Bose factor", float(setpoint))
        return float(setpoint)
    raise PlannerRefusal(
        "campaign refused: no sample temperature is declared anywhere — no measured belief slice, "
        "no objective target_temperature_K, and no [temperature_rule] setpoint on the card — so the "
        "Bose factor in the forward model would be a guess")


def _declared_t_round(workspace) -> float | None:
    """``collation.t_round`` from the card policy — the temperature-bin granularity every session
    slice is keyed by (``round(T / t_round) * t_round``); ``None`` when the card declares none."""
    policy_path = Path(workspace) / "card.policy.toml"
    if not policy_path.exists():
        return None
    try:
        card = Card.load(policy_path, Path(workspace) / "card.state.json")
        value = (getattr(card.policy, "collation", None) or {}).get("t_round")
    except (OSError, ValueError, TypeError) as exc:
        _LOG.warning("could not read collation.t_round from %s: %s", policy_path, exc)
        return None
    return None if value is None else float(value)


def _slice_rows(rows: list[dict], sample: str | None, temperature_bin, t_round) -> list[dict]:
    """Only the rows measured under the campaign's own (sample, temperature bin).

    The beliefs are resolved to one slice every iteration; the candidates, banked exposure,
    intensity map and anchor escalations must read the same slice or a reused workspace holding
    the same Q at another temperature or sample plans and stops from the wrong master, and
    de-Bose-corrects old counts at the wrong temperature (PR #37 review). A row that carries no
    sample or T at all is kept — it predates the stamp and there is nothing to disagree with.

    The sample test applies only when the rows themselves carry more than one sample. Before the
    first belief row exists ``_belief_sample`` can only return the driver's configured name
    (``"Al_phonon_DFT"``), and the session stamps every row with the instrument's (``"tavi"``);
    filtering a one-sample workspace on that name dropped every master, so no anchor ever formed
    and the campaign sat at h = 2.05 for ever (live, 2026-09-03). One sample in the evidence IS the
    campaign's sample, whatever the driver calls it — the rule ``_belief_sample`` already applies.
    """
    present = {str(row.get("sample")) for row in rows if row.get("sample") is not None}
    if len(present) <= 1:
        sample = None
    def same_bin(value) -> bool:
        if temperature_bin is None or value is None:
            return True
        try:
            t = float(value)
        except (TypeError, ValueError):
            return True
        binned = round(t / float(t_round)) * float(t_round) if t_round else t
        return abs(binned - float(temperature_bin)) <= 1e-6

    def same_sample(value) -> bool:
        return sample is None or value is None or str(value) == str(sample)

    return [row for row in rows if same_sample(row.get("sample")) and same_bin(row.get("T"))]


def _abstained_points(master_rows: list[dict], path) -> dict[int, bool]:
    """Grid indices whose pooled master abstained as ``underpowered``.

    The fitter's own word for "I could not see it at this exposure". The planner must not read
    that as a null -- it is the reason a 20M first look at h = 2.30 (10 counts at the peak, the
    intensity map exactly right) pushed the acoustic's mass out of its own window and bought an
    8 meV scan to find it again (eig-2-test, 2026-09-03). The counts still bank; the silence is not
    evidence.
    """
    out: dict[int, bool] = {}
    for row in master_rows:
        hkl = row.get("hkl")
        index = None if hkl is None else match_grid_index(tuple(hkl), path)
        if index is None:
            continue
        if row.get("disposition") == "abstain" and row.get("verdict") == "underpowered":
            out[index] = True
    return out


def _build_planner_input(cfg, objective, master_rows: list[dict], observations: list[dict],
                         durable_state: dict):
    """Assemble the planner's world: the hypothesis map, the intensity map, and what is measured.

    Returns ``(input, campaign_slice)``. The sample and the slice are resolved HERE, every
    iteration, off the belief rows themselves — the campaign's own name for the sample is only a
    disambiguator (see :func:`_belief_sample`), and the slice a campaign acts on changes the moment
    its first measured belief lands.
    """
    sample = _belief_sample(durable_state, cfg)
    campaign_slice, campaign_slice_error = resolve_campaign_slice(
        durable_state, objective, sample=sample,
        binning_policy_fingerprint=_temperature_binning_fingerprint(cfg.workspace))
    if campaign_slice_error is not None:
        raise PlannerRefusal(campaign_slice_error)
    temperature_bin = None if campaign_slice is None else campaign_slice.temperature_bin
    period = _declared_zone_period(cfg.workspace)
    if not period:
        raise PlannerRefusal(
            "campaign refused: the card declares no dispersion.zone_period, so a path coordinate "
            "cannot be folded into the reduced zone the branch beliefs are stored in — declare "
            "[dispersion] zone_period in card.policy.toml rather than let the planner assume one")
    if objective.energy_search_meV is None:
        raise PlannerRefusal(
            "campaign refused: the objective declares no energy domain, so there is no energy "
            "domain to place a window in")
    path = tuple(tuple(float(c) for c in point) for point in objective.path)
    domain = _planner_energy_domain(objective, path, cfg.resolution_cb)
    u = tuple(_reduced_u(point, period) for point in path)
    target_branch = str(getattr(objective, "target_branch", "acoustic"))
    targets = ("acoustic", "optic") if target_branch == "both" else (target_branch,)
    beliefs = _planner_beliefs(durable_state, objective, cfg, sample, temperature_bin, period)
    temperature = _planner_temperature(durable_state, objective, cfg, sample, temperature_bin)
    fwhm = []
    for point, reduced in zip(path, u):
        try:
            mean, _sigma = belief_predict(dict(beliefs[targets[0]]), float(reduced))
            fwhm.append(_resolve_fwhm(cfg.resolution_cb, point[0], point[1], point[2], mean))
        except Exception as error:                    # noqa: BLE001 - resolution is advisory
            _LOG.warning("could not resolve the energy FWHM at %s (%s); using %s meV",
                         point, error, DEFAULT_FWHM_MEV)
            fwhm.append(DEFAULT_FWHM_MEV)
    t_round = _declared_t_round(cfg.workspace)
    master_rows = _slice_rows(master_rows, sample, temperature_bin, t_round)
    observations = _slice_rows(observations, sample, temperature_bin, t_round)
    return PlannerInput(
        path=path, u=u, targets=targets, beliefs=beliefs,
        measured=_planner_candidates(master_rows, path, domain),
        scans=_planner_scans(cfg.log_path, observations, path, tuple(sorted(beliefs))),
        energy_domain_meV=(float(domain[0]), float(domain[1])),
        fwhm_meV=tuple(float(value) for value in fwhm),
        temperature_K=temperature,
        scale=_planner_scale(cfg, observations, path, tuple(sorted(beliefs)),
                             temperature, fwhm),
        tolerance_meV=float(objective.tolerance_meV),
        base_ncount=_production_ncount(objective),
        max_points=int(_runtime_limits(cfg, objective)["max_points"]),
        abstained=_abstained_points(master_rows, path)), campaign_slice


def _production_ncount(objective) -> int:
    """The exposure the planner's ladder is built on: the objective's declared production count.

    The ladder ran from half a QUICKLOOK upward (5e6 neutrons per point on TAVI), and with pooling
    making a short look nearly as good as a long one the planner took the bottom rung almost every
    time: 2-4 counts per point per scan at the optic peak, 46 single-scan nulls at one q, and a
    master that needed eight scans to show a peak at all (owner, 2026-09-05: "not enough intensity
    to do a real experiment"). The base is the operator's number — ``steering.production_batch_ncount``
    in the compiled objective — so the operator declares what one real scan counts to; the planner
    still chooses the rung. Not a floor on the planner's choice (that ruling stands, see
    :func:`_finalize_proposal`): the whole design space scales.
    """
    policy = dict(getattr(objective, "steering", None) or {})
    try:
        value = int(policy.get("production_batch_ncount") or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else PRODUCTION_NCOUNT


def _certified_prize(*, cfg, objective, grid, budget_state: dict, observations: list[dict],
                     scan_kind: dict, it: int, previous_scale: dict | None = None):
    """Certify the prize points, then ask the planner again.

    ``(observations, met, terminal, logged_scale)``.

    The planner's first stop is claimed on whatever intervals the masters currently publish, and on
    the fast tier those are HESSE-backed and narrower than the bootstrap intervals the campaign will
    actually report — the first live run stopped ``met`` while every published interval sat outside
    the bar. So certification is part of asking the question, not a report filed after it: the
    existing ``_certify`` escalates each pending point to the full tier and republishes its interval,
    the map is rebuilt from those masters, and the prize is asked again.

    ``met`` only when the second ask also stops. Otherwise the caller continues the loop, and the
    planner — now seeing the widened half-widths — buys counts where they are widest. ``_certify``
    is idempotent per master revision (``_pending_certification_indices``), so a point whose master
    has not moved since its certificate is not re-fitted, and the loop cannot churn on it.
    """
    per_q = evaluate_stop(objective, grid, budget_state,
                          calibration=cfg.calibration, floor=cfg.floor).per_q
    provisional = StopDecision("met", ["provisional prize; certifying before it is claimed"], per_q)
    try:
        observations, ran_certify = _certify(
            cfg=cfg, objective=objective, decision=provisional,
            observations=observations, scan_kind=scan_kind)
    except AnalysisLimitExceeded as error:
        return observations, False, StopDecision("incomplete", [str(error)], per_q), previous_scale
    if not ran_certify:
        # ``_certify`` returns False here only through its own catch-all fault (the analysis-limit
        # checkpoint raises), so this is a controller fault, not a data-limited outcome.
        return observations, False, StopDecision(
            "incomplete",
            ["controller_fault: certification of the prize points did not complete; checkpoints "
             "retained and resume is supported after repair"],
            per_q, terminal_cause=TERMINAL_CONTROLLER_FAULT), previous_scale

    master_rows, certified_grid = _grid_state(
        objective, observations, cfg.workspace, cfg.calibration)
    certified = evaluate_stop(objective, certified_grid, budget_state,
                              calibration=cfg.calibration, floor=cfg.floor)
    _record_certification_acceptance(cfg, certified, _full_tier(cfg))
    try:
        planner_input, _slice = _build_planner_input(
            cfg, objective, master_rows, observations, read_card_state(cfg.workspace))
        outcome = plan_next(planner_input)
    except PlannerRefusal as error:
        return (observations, False, StopDecision("incomplete", [str(error)], certified.per_q),
                previous_scale)
    previous_scale = _log_eig_plan(cfg, planner_input, outcome, 0.0, it=it,
                                   previous_scale=previous_scale)
    if outcome.stop and outcome.stop_reason != _PLANNER_GOAL_MET:
        return observations, False, _not_the_prize(objective, outcome, certified.per_q), previous_scale
    if outcome.stop:
        return observations, True, None, previous_scale
    outstanding = [point for point in outcome.per_point
                   if not point.exempt and not point.within_bar]
    log_event(cfg.log_path, "decide", {
        "event": "certified_prize_not_met", "action": "certified_prize_not_met", "iter": it,
        "outstanding": len(outstanding),
        "prize_points_total": sum(1 for point in outcome.per_point if not point.exempt),
        "tolerance_meV": float(objective.tolerance_meV),
        "hkl": [list(point.hkl) for point in outstanding],
        "halfwidth_meV": [point.halfwidth_meV for point in outstanding],
        "reason": ("the certified intervals do not carry the prize the fast-tier map claimed; "
                   "the campaign continues at the points that are still above the bar")})
    return observations, False, None, previous_scale


#: The planner's ONE stop that means the objective was answered. Every other stop it can return --
#: today ``no_admissible_window``, when the point limit drops every candidate at a deficit point --
#: says the planner ran out of moves, not that the map is at the requested precision.
_PLANNER_GOAL_MET = "goal_met"


def _not_the_prize(objective, outcome, per_q) -> StopDecision:
    """A planner stop that is not the prize is ``incomplete``, in the planner's own words."""
    outstanding = sum(1 for point in outcome.per_point
                      if not point.exempt and not point.within_bar)
    return StopDecision(
        "incomplete",
        [f"the planner stopped without the prize ({outcome.stop_reason}): {outstanding} point(s) "
         f"remain above \u00b1{float(objective.tolerance_meV):g} meV and no admissible design "
         "remains at them"], per_q)


def _design_dict(design) -> dict:
    """One ranked design as the event records it.

    ``neutrons`` is the dose the instrument is asked for (points x ncount) and ``price`` is what the
    planner ranked on — the dose plus the fixed cost of taking a scan at all. They are separate
    because they answer different questions: the operator reconciles ``neutrons`` against the beam
    budget, and only ``price`` explains why a cheap-looking design lost.
    """
    return {"grid_index": design.grid_index, "hkl": list(design.hkl),
            "e_lo": design.e_lo, "e_hi": design.e_hi, "e_step": design.e_step,
            "n_points": design.n_points, "ncount": design.ncount,
            "utility_nats": design.utility_nats,
            "neutrons": design.neutrons, "price": design.price,
            "score": design.score}


#: The intensity map is logged in the unit an operator can hold: counts (or counts*meV) per ten
#: million neutrons, the exposure a scan is actually bought in.
_SCALE_REFERENCE_NCOUNT = 1.0e7


def _scale_dict(scale) -> dict:
    """The intensity map as counts per :data:`_SCALE_REFERENCE_NCOUNT` neutrons."""
    return {"per_ncount": _SCALE_REFERENCE_NCOUNT,
            "peak_counts_meV": {str(b): round(float(v) * _SCALE_REFERENCE_NCOUNT, 4)
                                for b, v in scale.peak_counts_meV.items()},
            "elastic_counts_meV": round(float(scale.elastic_counts_meV)
                                        * _SCALE_REFERENCE_NCOUNT, 4),
            "background_per_point": round(float(scale.background_per_point)
                                          * _SCALE_REFERENCE_NCOUNT, 4)}


def _log_eig_plan(cfg, inp, plan_decision, seconds: float, *, it: int,
                  previous_scale: dict | None = None) -> dict:
    """The one operator-facing record of why this scan and not another. Returns the logged scale."""
    prize = [point for point in plan_decision.per_point if not point.exempt]
    scale = _scale_dict(inp.scale)
    log_event(cfg.log_path, "eig_plan", {
        "iter": it, "seconds": float(seconds),
        "tolerance_meV": float(inp.tolerance_meV),
        # The domain the windows were built in: instrument ceiling above, elastic exclusion (optic)
        # or zero (acoustic) below. Recorded so a reader can see no phonon-specific range acted.
        "energy_domain_meV": [float(v) for v in inp.energy_domain_meV],
        # The planner says so when no design is expected to complete a point and it is taking the
        # cheapest probe; an empty note is the ordinary case.
        "note": str(getattr(plan_decision, "note", "") or ""),
        "intensity_scale": scale,
        "intensity_scale_changed": previous_scale != scale,
        "stop": bool(plan_decision.stop), "stop_reason": plan_decision.stop_reason,
        "chosen": None if plan_decision.chosen is None else _design_dict(plan_decision.chosen),
        # The planner now ranks EVERY design; the event keeps the top ten, which is what an operator
        # reads. ``n_ranked`` says how many it chose from, so the ten never read as the whole field.
        "ranked": [_design_dict(design) for design in plan_decision.ranked[:10]],
        "n_ranked": len(plan_decision.ranked),
        "per_point": [{"u": point.u, "hkl": list(point.hkl), "exempt": point.exempt,
                       "halfwidth_meV": point.halfwidth_meV, "deficit_nats": point.deficit_nats,
                       "identified_mass": point.identified_mass,
                       "within_bar": point.within_bar, "n_candidates": point.n_candidates}
                      for point in plan_decision.per_point],
        "prize_points_within_bar": sum(1 for point in prize if point.within_bar),
        "prize_points_total": len(prize)})
    return scale


def _proposal_from_design(objective, inp, design, campaign_slice) -> ScanProposal:
    """One ranked design to the submittable request, with every identity the chain downstream reads."""
    target = inp.targets[0]
    rival = next((b for b in sorted(inp.beliefs) if b != target), None)
    mean, sigma = design.e_pred_meV.get(target, (0.0, 0.0))
    slice_fingerprint = None if campaign_slice is None else campaign_slice.fingerprint
    intent = MeasurementIntent(
        action_class="production", hkl=tuple(float(c) for c in design.hkl),
        energy_lo=float(design.e_lo), energy_hi=float(design.e_hi), window_family="eig",
        target_branch_role=target, rival_branch_role=rival, evidence_sink=CANONICAL_MASTER,
        hypotheses_fingerprint=objective.hypothesis_fingerprint,
        campaign_slice_fingerprint=slice_fingerprint)
    meta = {
        "scientific_action_fingerprint": intent.scientific_action_fingerprint,
        "action_class": "production",
        "evidence_sink": CANONICAL_MASTER,
        "acquisition_action_role": "production",
        "target_branch_role": target,
        "rival_branch_role": rival,
        "certification_eligible": True,
        "driver_inserted": False,
        "count_diagnostics": {},
        "hypotheses_fingerprint": objective.hypothesis_fingerprint,
        "campaign_slice_fingerprint": slice_fingerprint,
        "design_prediction_source": "eig_planner",
        "design_prediction_mean": float(mean),
        "design_prediction_sigma": float(sigma),
        "score": float(design.score),
        "purpose": "eig",
        "max_scan_points": int(inp.max_points),
        # The design as the planner chose it, before any instrument clamp or feasibility trim moves
        # the window. A refusal or a clean null is charged to THIS as well as to what was executed.
        "design_key": [list(design.hkl), float(design.e_lo), float(design.e_hi),
                       float(design.e_step), int(design.ncount)],
    }
    return ScanProposal(
        h=float(design.hkl[0]), k=float(design.hkl[1]), l=float(design.hkl[2]),
        e_lo=float(design.e_lo), e_hi=float(design.e_hi), e_step=float(design.e_step),
        ncount=int(design.ncount), kind="production",
        reason=f"eig {design.score:.3g} nats/neutron",
        e_pred=float(mean), fwhm_dE=float(inp.fwhm_meV[design.grid_index]),
        n_points=int(design.n_points), acquisition_id=uuid.uuid4().hex,
        meta=meta, intent=intent)


def _next_planner_proposal(designs: list, objective, cfg, inp, grid, budget_state: dict,
                           request_suppressions: dict, unreachable_hkl: dict,
                           suppressed_designs: set, campaign_slice, *, it: int):
    """The best remaining ranked design that finalizes and is not an already-refused request.

    ``designs`` is consumed in place, so a retry after a decline continues down the same ranking
    instead of re-offering the design the instrument just refused. Two durable suppressions are
    consulted, both rebuilt from the session log by :func:`suppressed_actions`: a q the instrument
    declared unreachable is never proposed again (geometry is a property of the q, not of the
    question asked there), and an exact request it already refused is never re-emitted. Returns
    ``(proposal, None)``, or ``(None, StopDecision)`` when nothing left is realizable.
    """
    instrument_error: Exception | None = None
    budget_error: Exception | None = None
    while designs:
        design = designs.pop(0)
        key = design_key(design.hkl, design.e_lo, design.e_hi, design.e_step, design.ncount)
        if (hkl_key(design.hkl) in unreachable_hkl or key in suppressed_designs
                or key[:-1] + (0,) in suppressed_designs):
            continue
        proposal = _proposal_from_design(objective, inp, design, campaign_slice)
        try:
            _finalize_proposal(proposal, objective, cfg, budget_state, it=it)
        except (InstrumentLimitBelowMinimumBatch, InstrumentPointLimitExceeded) as error:
            log_event(cfg.log_path, "exception", {
                "action": ("instrument_point_limit_exceeded"
                           if isinstance(error, InstrumentPointLimitExceeded)
                           else "instrument_limit_below_minimum_batch"),
                "error": str(error), "hkl": list(design.hkl), "iter": it})
            instrument_error = error
            continue
        except ProposalOutsideEnergyDomain as error:
            log_event(cfg.log_path, "exception", {
                "action": "proposal_outside_energy_domain", "severity": "low",
                "error": str(error), "hkl": list(design.hkl), "iter": it})
            instrument_error = error
            continue
        except BudgetInsufficientForMinimumBatch as error:
            log_event(cfg.log_path, "exception", {
                "action": "budget_insufficient_for_minimum_batch",
                "error": str(error), "hkl": list(design.hkl), "iter": it})
            budget_error = error
            continue
        finalized = finalized_proposal(proposal)
        if finalized is not None and finalized.request_fingerprint in request_suppressions:
            log_event(cfg.log_path, "decide", {
                "event": "request_suppressed_skip", "action": "request_suppressed_skip",
                "request_fingerprint": finalized.request_fingerprint,
                "hkl": list(design.hkl), "iter": it})
            continue
        return proposal, None
    per_q = evaluate_stop(objective, grid, budget_state,
                          calibration=cfg.calibration, floor=cfg.floor).per_q
    if instrument_error is not None:
        return None, StopDecision("infeasible", [str(instrument_error)], per_q)
    if budget_error is not None:
        return None, StopDecision("incomplete", [str(budget_error)], per_q)
    # Every ranked design is an unchanged request the instrument already refused, or sits at a q it
    # declared unreachable. Re-emitting one would repeat the refusal once per iteration forever, so
    # this is a resumable controller fault, not a data-limited outcome: checkpoints are kept and the
    # campaign resumes after repair.
    return None, StopDecision(
        "incomplete",
        ["controller_fault: no ranked EIG design is realizable — every remaining candidate is an "
         "unchanged request the instrument already refused, or sits at an unreachable q. "
         "Checkpoints retained and resume is supported after repair."],
        per_q, terminal_cause=TERMINAL_CONTROLLER_FAULT)


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
    # Master@revision keys already sent for a belief-anchor escalation; a refit that did not leave
    # a fold is logged once and never re-bought at that revision (see _pending_anchor_escalations).
    # Rebuilt from the log so a resume keeps the same eligibility as an uninterrupted run.
    escalated_masters: set[str] = _attempted_anchor_escalations(cfg.log_path)
    # Derived from the durable log, never a second store: a resume that could not rebuild this would
    # re-propose an action the instrument has already refused on every remaining iteration.
    (action_suppressions, unreachable_hkl, request_suppressions,
     suppressed_designs) = suppressed_actions(cfg.log_path)
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
    certified_terminal = False   # the prize was asked again on certified intervals and still stopped
    logged_scale: dict | None = None   # the intensity map the feed last announced
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
        # records only. A budget of ``None`` is unlimited by operator choice, and the loop honours it —
        # bounded only by :data:`_ABSOLUTE_ITERATION_CEILING`, the non-scientific runaway backstop.
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
                if it >= _ABSOLUTE_ITERATION_CEILING:
                    # Runaway backstop (see _ABSOLUTE_ITERATION_CEILING): the campaign is neither
                    # completing nor spending, so no budget can ever trip. Terminate with a named
                    # reason rather than spin.
                    reason = (
                        "controller_fault: the campaign reached the absolute iteration ceiling "
                        f"({_ABSOLUTE_ITERATION_CEILING}) without completing or exhausting its "
                        "budget — the loop is not making progress. Checkpoints retained and resume "
                        "is supported after repair.")
                    log_event(cfg.log_path, "exception", {
                        "event": "iteration_ceiling_reached",
                        "action": "iteration_ceiling_reached",
                        "ceiling": _ABSOLUTE_ITERATION_CEILING, "iter": it})
                    decision = StopDecision("incomplete", [reason], decision.per_q,
                                            terminal_cause=TERMINAL_CONTROLLER_FAULT)
                    break
                # ``met`` is the EIG planner's prize and is produced at the planning seam below,
                # never here: ``evaluate_stop`` decides only infeasible / incomplete / continue.
                if decision.state != "continue":
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

                # ONE seam: the EIG planner chooses the Q, the energy window and the exposure, and
                # nothing else may. Every legislated placement rule is gone (ruling 2026-09-02 §6) —
                # no survey, no frontier or bridge, no same-q ceiling, no opening book, no scan-window
                # comparison. A wide window happens only because a belief is wide.
                plan_decision = None
                planner_input = None
                campaign_slice = None
                with timed(cfg.log_path, "propose", iteration=it):
                    try:
                        # Planner time is analysis time, one budget (operating rule 2026-09-03).
                        with analysis_operation(
                                cfg.log_path, f"plan:{cfg.run_nonce}:{proposal_counter}",
                                "eig_planning",
                                limit_seconds=objective.analysis_limit_seconds, iteration=it):
                            planner_input, campaign_slice = _build_planner_input(
                                cfg, objective, master_rows, observations,
                                read_card_state(cfg.workspace))
                            started = time.perf_counter()
                            plan_decision = plan_next(planner_input)
                            planner_seconds = time.perf_counter() - started
                    except AnalysisLimitExceeded as error:
                        decision = StopDecision("incomplete", [str(error)], decision.per_q)
                    except PlannerRefusal as error:
                        log_event(cfg.log_path, "exception", {
                            "action": "eig_planner_refused", "severity": "high",
                            "error": str(error), "iter": it})
                        decision = StopDecision("incomplete", [str(error)], decision.per_q)
                if plan_decision is None:
                    break
                # A prize point the planner has stopped revisiting (``within_bar``) but whose fit is
                # still an untrusted HESSE estimate can never become a belief anchor — escalate it to
                # the full tier now, incrementally, rather than waiting for a whole-campaign certify
                # pass this campaign may never reach if a further point's own convergence depends on
                # the anchor this one would supply (found live at the zone boundary, HANDOFF-eig.md
                # §7.29/§7.30). Idempotent: once escalated, ``_pending_anchor_escalations`` no longer
                # names it, so this can never spin on the same point. Decided BEFORE the plan is
                # logged: a plan set aside for an escalation is never acted on, and logging it drew
                # phantom "planning a 58-point scan" lines in the feed (eig-2-test, 2026-09-03).
                pending_anchors = _pending_anchor_escalations(
                    cfg.workspace, plan_decision,
                    sample=(None if campaign_slice is None else campaign_slice.sample),
                    temperature_bin=(None if campaign_slice is None
                                     else campaign_slice.temperature_bin),
                    t_round=_declared_t_round(cfg.workspace), attempted=escalated_masters)
                if pending_anchors:
                    full_tier = _full_tier(cfg)
                    anchor_rows = read_results_csv(cfg.workspace)
                    anchor_keys = [_anchor_escalation_key(anchor_rows, sid) for sid in pending_anchors]
                    log_event(cfg.log_path, "escalate_fit", {
                        "phase": "anchor", "iter": it, "hkl": None, "from_tier": "fast",
                        "to_tier": full_tier, "reason": "belief_anchor",
                        "master_ids": pending_anchors, "master_keys": anchor_keys})
                    escalated_masters.update(anchor_keys)
                    try:
                        with timed(cfg.log_path, "anchor_escalation", iteration=it):
                            with analysis_operation(
                                    cfg.log_path, f"anchor:{cfg.run_nonce}:{proposal_counter}",
                                    "anchor_escalation", limit_seconds=objective.analysis_limit_seconds,
                                    iteration=it):
                                allowance = _analysis_allowance_seconds(cfg, objective)
                                # enforce_completion: a checkpointed worker (exit 75) returns
                                # completed=False, and treating that as "escalated" would re-plan
                                # on masters that never became folds (PR #37 review).
                                refresh_analysis(
                                    cfg.runner, cfg.workspace, [cfg.data_dir], errors_tier=full_tier,
                                    force_masters=pending_anchors, render=False,
                                    analysis_allowance_seconds=allowance,
                                    finish_initial_wave=(False if allowance is not None else None),
                                    enforce_completion=True,
                                    analysis_limit_seconds=objective.analysis_limit_seconds)
                    except AnalysisLimitExceeded as error:
                        decision = StopDecision("incomplete", [str(error)], decision.per_q)
                        break
                    except Exception as error:  # noqa: BLE001 — policy A: never stop, shout
                        # The same continue-and-alert rule as the ordinary fit stage: a transient
                        # analysis failure here (the results.csv swap collision included) must not
                        # end the campaign because it happened in a refit rather than a fit (PR #37
                        # review, second round, P1). The attempt is not counted, so the next
                        # iteration may ask again.
                        escalated_masters.difference_update(anchor_keys)
                        _raise_campaign_alert(cfg.log_path, "anchor_escalation_failed", error,
                                              it=it, master_ids=pending_anchors)
                        master_rows, grid = _grid_state(
                            objective, observations, cfg.workspace, cfg.calibration)
                        continue
                    # Deliberately WITHOUT ``attempted``: the question is whether the masters just
                    # refit are still unfolded, and excluding them would make it unanswerable.
                    # ``refresh_analysis`` returned with ``enforce_completion``, so results.csv holds
                    # the refit.
                    still = _pending_anchor_escalations(
                        cfg.workspace, plan_decision,
                        sample=(None if campaign_slice is None else campaign_slice.sample),
                        temperature_bin=(None if campaign_slice is None
                                         else campaign_slice.temperature_bin),
                        t_round=_declared_t_round(cfg.workspace))
                    unresolved = sorted(set(still) & set(pending_anchors))
                    if unresolved:
                        log_event(cfg.log_path, "exception", {
                            "action": "anchor_escalation_unresolved", "iter": it,
                            "master_ids": unresolved,
                            "reason": "the full-tier refit did not leave a fold master; not retried"})
                    master_rows, grid = _grid_state(
                        objective, observations, cfg.workspace, cfg.calibration)
                    continue
                # The plan is acted on from here (a stop or a scan), so this is where it is logged.
                logged_scale = _log_eig_plan(
                    cfg, planner_input, plan_decision, planner_seconds, it=it,
                    previous_scale=logged_scale)
                if plan_decision.stop and plan_decision.stop_reason != _PLANNER_GOAL_MET:
                    decision = _not_the_prize(objective, plan_decision, decision.per_q)
                    break
                if plan_decision.stop:
                    # The prize is claimed on the intervals the campaign PUBLISHES, so certification
                    # is part of asking, not a report afterwards: certify every prize point that
                    # needs it, rebuild the map from the certified masters, and ask again. Only a
                    # second stop is ``met``. A first stop the certified intervals do not support
                    # simply continues the loop — the planner sees the widened half-widths and buys
                    # counts where they are widest, which is the whole mechanism.
                    observations, met, terminal, logged_scale = _certified_prize(
                        cfg=cfg, objective=objective, grid=grid, budget_state=budget_state,
                        observations=observations, scan_kind=scan_kind, it=it,
                        previous_scale=logged_scale)
                    if terminal is not None:
                        decision = terminal
                        break
                    master_rows, grid = _grid_state(
                        objective, observations, cfg.workspace, cfg.calibration)
                    if met:
                        certified_terminal = True
                        decision = StopDecision(
                            "met",
                            ["hypothesis map at the requested precision: every prize point within "
                             f"±{float(objective.tolerance_meV):g} meV, on the certified intervals"],
                            evaluate_stop(objective, grid, budget_state,
                                          calibration=cfg.calibration, floor=cfg.floor).per_q)
                        break
                    continue
                designs = list(plan_decision.ranked)
                proposal, selection_terminal = _next_planner_proposal(
                    designs, objective, cfg, planner_input, grid, budget_state,
                    request_suppressions, unreachable_hkl, suppressed_designs, campaign_slice,
                    it=it)
                if proposal is None:
                    decision = selection_terminal
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

                    # Re-check the analysis clock here, not only before planning: planning itself is
                    # charged (predictive + shadow + realized scoring), and ``analysis_operation``
                    # only raises when a NEW operation starts over the limit, so an overrun inside a
                    # started operation reaches this point silently. Without this, a campaign 30 s
                    # under the limit at the pre-planning check can submit a full batch of scans and
                    # their full-tier fits past the declared analysis budget.
                    analysis_spent = analysis_clock_state(cfg.log_path)["spent_seconds"]
                    if (objective.analysis_limit_seconds is not None
                            and analysis_spent >= float(objective.analysis_limit_seconds)):
                        decision = StopDecision(
                            "incomplete",
                            [f"analysis limit exhausted ({analysis_spent:.1f}/"
                             f"{float(objective.analysis_limit_seconds):.1f}s) before measurement submission"],
                            evaluate_stop(objective, grid, budget_state,
                                          calibration=cfg.calibration, floor=cfg.floor).per_q)
                        stop_campaign = True
                        break

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
                        # A scan that delivered no peak — no row at all, or rows carrying only a
                        # missed verdict — is a non-result for THIS exact design. It is recorded as
                        # evidence either way; suppressing the design is what stops the planner
                        # being handed the identical window and exposure again next iteration.
                        if not any(row.get("disposition") in _USABLE for row in rows):
                            _record_action_rejection(
                                cfg, None, proposal,
                                ("analysis_delivered_no_row" if not rows
                                 else "clean_null_delivered_no_peak"),
                                action_suppressions, unreachable_hkl, request_suppressions, it=it,
                                suppressed_designs=suppressed_designs)
                        break
                    # Charge the refusal at its scope (populating suppressions / by_request and logging
                    # the durable event). A transient refusal (scope "none") suppresses nothing and does
                    # not advance to another action, matching the original break.
                    _record_action_rejection(
                        cfg, None, proposal, declined_verdict, action_suppressions,
                        unreachable_hkl, request_suppressions, it=it,
                        suppressed_designs=suppressed_designs)
                    if classify_rejection(declined_verdict).scope == "none":
                        break
                    # Continue down the SAME EIG ranking through the same helper the initial
                    # selection used, so a budget/instrument refusal here is classified into a
                    # terminal instead of escaping as an exception, and an already-refused request is
                    # not re-emitted.
                    proposal, fallback_terminal = _next_planner_proposal(
                        designs, objective, cfg, planner_input, grid, budget_state,
                        request_suppressions, unreachable_hkl, suppressed_designs, campaign_slice,
                        it=it)
                    if fallback_terminal is not None:
                        decision = fallback_terminal
                        stop_campaign = True
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
            analysis_spent = analysis_clock_state(cfg.log_path)["spent_seconds"]
            if (decision is not None and decision.state == "met"
                    and objective.analysis_limit_seconds is not None
                    and analysis_spent >= float(objective.analysis_limit_seconds)):
                decision = StopDecision(
                    "incomplete",
                    [f"analysis limit exhausted before final certification ({analysis_spent:.1f}/"
                     f"{float(objective.analysis_limit_seconds):.1f}s)"], decision.per_q)
            if certified_terminal:
                # The prize was claimed on certified intervals at the seam; ``_certify`` there
                # already published them and recorded their acceptance.
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
            # ``met`` is preserved too: it is the planner's prize, and ``evaluate_stop`` no longer
            # produces it, so recomputing over it would silently downgrade a finished campaign.
            if not (decision is not None
                    and decision.state in {"met", "incomplete", "infeasible"}):
                decision = final_decision
            elif decision is not None and decision.state == "met":
                decision = StopDecision("met", decision.reasons, final_decision.per_q)
            if ran_certify and not certified_terminal:
                _record_certification_acceptance(cfg, decision, _full_tier(cfg))

            summary = _campaign_summary(objective, grid, decision, budget_state, cfg.workspace,
                                        cfg.log_path, len(observations),
                                        instrument_limits=limit_envelope)
            stop_payload = {"state": decision.state, "reasons": decision.reasons}
            if decision.terminal_cause is not None:
                stop_payload["terminal_cause"] = decision.terminal_cause
            if decision.terminal_cause == TERMINAL_CONTROLLER_FAULT:
                # A controller fault is an unattended-safe terminal: checkpoints are retained and the
                # campaign resumes after repair rather than crashing overnight.
                stop_payload["resume_supported"] = True

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
                  "linewidth_state": g.linewidth_state} for g in grid],
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
