"""TAVI request realization — the wire knowledge one campaign request needs.

Everything here is about the *instrument's* vocabulary: the limit documents ``GET /state`` and
``GET /schema`` publish, the ``POST /validate`` and ``POST /scan`` request bodies, and the frozen
feasibility manifest TAVI answers with. :mod:`isar.drive.loop` decides *what* to measure and *what
a refusal costs*; this module knows what those decisions look like on the wire, and
:mod:`isar.drive.instrument.tavi_client` keeps the HTTP mechanics.

The one decision helper, :func:`plan_feasibility_response`, reads a partial-feasibility verdict and
says whether the request should be submitted, trimmed to a reachable run, or declined. It decides
only; the loop still owns the mutation, the EIG refresh, the session log, and the retry ladder.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Sequence

import hashlib
import logging
import math

from isar.drive.acquisition.inventory import FinalizedProposal, ValidatedAttempt, canonical_json
from isar.drive.acquisition.scan_grid import resolve_point_count

_LOG = logging.getLogger(__name__)


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


def _emitted_point_count(e_lo: float, e_hi: float, e_step: float) -> int:
    """The TRUE (unclamped) number of points TAVI computes from a ``deltaE lo hi step`` command.

    :func:`~isar.drive.acquisition.acquire.points_in_window` clamps to the acquisition budget
    (``MAX_POINTS``); TAVI does not — it counts the command literally. A missed-window recenter widens
    ``e_lo``/``e_hi`` without recomputing the (tight) ``e_step``, so the emitted command can carry far
    more points than the clamped ledger shows (the 594-pt over-limit scan). This is what the guard
    checks, so a wide-window/tight-step proposal cannot slip past the ledger clamp to TAVI."""
    count, _step = resolve_point_count(e_lo, e_hi, e_step)
    return count


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


def validation_event_fields(validation: dict, *, feasible_points: int,
                            target_support: int | None) -> dict[str, Any]:
    """The instrument-derived half of a ``validate`` session-log entry, in wire order.

    Kept here so the loop can record what TAVI answered without naming TAVI's manifest fields. The
    caller appends its own request-identity keys (fingerprints, iteration) after these.
    """
    return {"would_queue": validation.get("would_queue"),
            "blockers": validation.get("blockers"), "eta": validation.get("eta"),
            "requested_points": validation.get("requested_points"),
            "feasible_points": feasible_points,
            "target_supporting_feasible_points": target_support,
            "partial": validation.get("partial"),
            "feasible_segments": validation.get("feasible_segments"),
            "infeasible": validation.get("infeasible")}


def requested_point_count(validation: dict):
    """The point count TAVI judged, falling back to the positional mask length.

    ``planned_feasible_mask`` is positional against the requested point list, so its length IS the
    requested count. Without the fallback an adapter that omits the echoed count hides a partially
    infeasible request from the callers that compare feasible against requested.
    """
    count = validation.get("requested_points") if isinstance(validation, dict) else None
    if count is None:
        mask = validation.get("planned_feasible_mask") if isinstance(validation, dict) else None
        if isinstance(mask, list) and mask:
            return len(mask)
    return count


@dataclass(frozen=True)
class FeasibilityDecision:
    """What a partially-feasible verdict means for this request — decided, not yet acted on.

    ``kind`` is ``"submit"`` (send the request unchanged), ``"trim"`` (narrow the window to ``run``
    before re-finalizing), or ``"decline"``. ``reason`` carries the existing decision vocabulary
    verbatim: the run-selection verdict for a trim or an unactionable manifest
    (``"contains_target"`` / ``"longest"`` / ``"no_localization"``), and the decline tag otherwise.
    ``event_fields`` is the manifest-derived tail of the caller's session-log payload, so the loop
    records what the instrument said without reading its manifest itself.
    """

    kind: str
    run: tuple[int, int] | None
    reason: str | None
    new_e_lo: float | None
    new_e_hi: float | None
    event_fields: Mapping[str, Any] = field(default_factory=dict)


def plan_feasibility_response(validation: dict, grid_energies: Sequence[float], *,
                              target_energy: float | None,
                              acceptance: tuple[float, float] | None,
                              already_trimmed: bool) -> FeasibilityDecision:
    """Decide how to answer a verdict that reports fewer feasible points than were requested.

    Branch order is the campaign's, in decreasing generality: "nothing here is a scan" (no run
    reaches :data:`FEASIBLE_TRIM_MIN_POINTS`) is a more basic fact than "nothing here is your peak"
    (no run overlaps the declared target), which in turn outranks "the instrument's answer is not
    converging" (a second partial verdict on an already-trimmed window). Only then is a trim
    proposed; a manifest that cannot be indexed against this grid falls through to an unchanged
    submit, where the submit-time ``infeasible_points`` fallback remains in force.
    """
    requested_points = len(grid_energies)
    manifest_fields = {"planned_feasible_mask": validation.get("planned_feasible_mask")}
    run, run_decision = _select_feasible_run(
        validation, grid_energies, target_energy=target_energy, acceptance=acceptance)
    run_length = 0 if run is None else run[1] - run[0] + 1
    all_runs = _feasible_runs(validation, requested_points) or []
    longest_run_length = max((end - start + 1 for start, end in all_runs), default=0)
    if (all_runs and longest_run_length < FEASIBLE_TRIM_MIN_POINTS) or (
            run is not None and run_length < FEASIBLE_TRIM_MIN_POINTS):
        return FeasibilityDecision(
            "decline", None, "largest_feasible_run_below_minimum", None, None,
            {"feasible_run_points": max(run_length, longest_run_length),
             "minimum_points": FEASIBLE_TRIM_MIN_POINTS, **manifest_fields})
    if run_decision == "target_excluded":
        return FeasibilityDecision("decline", None, "target_excluded", None, None, manifest_fields)
    if run is not None and already_trimmed:
        return FeasibilityDecision("decline", None, "feasibility_trim_did_not_converge",
                                   None, None, manifest_fields)
    if run is not None:
        return FeasibilityDecision("trim", run, run_decision, float(grid_energies[run[0]]),
                                   float(grid_energies[run[1]]), manifest_fields)
    return FeasibilityDecision("submit", None, run_decision, None, None, manifest_fields)
