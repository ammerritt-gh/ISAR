"""DiscoverRegion's deterministic proposal generator, sparse-coverage regime (SCENE-4 PR 4.2a A5).

``propose_survey`` is a PURE function of ``(scene_payload, step_payload)`` — no internal memory,
no clock, no random draw. Call it again after k proposals have been measured (their footprints now
in ``scene_payload``) and it proposes exactly the remainder; it never needs to be told what it
proposed last time.

**Three proposal kinds, each bound to the closure predicate it serves** (mirrors
``isar.scene.predicates`` — never imports its private helpers, only re-derives the same walk
shape so the two can be pinned equal by test):

- ``missing_q_node`` — serves ``DiscoverRegion``'s gap check (``_coverage_complete``'s
  ``widest = max(edge deltas)`` over ``[region_lo, *sorted(surveyed axis_coordinate), region_hi]``).
  Every gap wider than ``maximum_initial_spacing_rlu`` gets ``ceil(gap/spacing) - 1`` evenly spaced
  interior nodes — the minimal count that leaves every sub-gap ``<= spacing``.
- ``missing_omega_band`` — serves ``DiscoverRegion``'s ω leg (the SAME cursor walk over sorted
  bands from ``domain_lo``): one proposal per surveyed node whose bands leave a gap, naming that
  node's first uncovered ``(lo, hi)`` interval.
- ``underexposed_cell`` — serves ``VerifyCompleteness``'s exposure floor
  (``_completeness_relative``'s ``effective_exposure < min_effective_exposure``): one proposal per
  surveyed node whose exposure is below the frozen floor (or missing), naming the deficit. A
  low-exposure node is never reported as a q/ω gap — ``DiscoverRegion`` and ``VerifyCompleteness``
  are different closures and this book never conflates them. The two ARE independent, though: a
  node with both an ω gap and low exposure gets both proposals in the same round, rather than
  making the campaign spend an extra round to discover the second problem.

A scene answering both closures yields zero proposals.

**Grid**: anchored to the step's own ``q_start``/``q_end`` endpoints, never a global zero-anchored
rounding grid — a region that does not align to any external grid still gets exactly the right
interior nodes, because they are placed as fractions of ITS OWN gaps.

**Ordering never changes the proposal set** (asserted by test), only the sequence: it is computed
from the required set above, then sorted by the rank tuple

    (-anchor_score, axis_coordinate, KIND_RANK[kind], stable_tiebreak)

where ``anchor_score`` is 0 unless ``resolved_policy.survey_order == "placement_informed"`` (the
default) AND the scene payload carries a declared ``sample_knowledge.zone_period`` — mirroring
``isar.scene.agenda._zc_anchors``, since a step payload alone never declares zone geometry today.
When both hold, ``anchor_score = best_zone_centre_order_weight / (1 + distance to nearest
zone-centre anchor in the region)``, where ``best_zone_centre_order_weight`` is the highest
``order_weight`` among the catalogue profile's ``anchor_kind="zone_centre"`` classes
(:func:`isar.analyze.catalogue.placement_rules`) — the only placement-rule input this generic,
class-agnostic book can honestly use, since a bare coverage/exposure gap carries no catalogue
class of its own. Under ``survey_order="uniform"`` every score is 0 and ordering falls straight
through to geometric position, so the SET is identical by construction while the ORDER is not.
``stable_tiebreak`` is ``(kind, round(axis_coordinate, 9), round(extra sort key, 9))`` — deterministic
regardless of input iteration order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from isar.analyze.catalogue import placement_rules

KIND_RANK = {"missing_q_node": 0, "missing_omega_band": 1, "underexposed_cell": 2}


@dataclass(frozen=True, slots=True)
class SurveyProposal:
    """One opening-book proposal. ``detail`` carries the kind-specific payload:

    - ``missing_q_node``: ``{}`` — the coordinate alone is the proposal, a new node to measure.
    - ``missing_omega_band``: ``{"omega_lo": float, "omega_hi": float}`` — the first uncovered
      interval at this already-surveyed node.
    - ``underexposed_cell``: ``{"deficit": float}`` — how far under the floor this node's
      ``effective_exposure`` sits (``min_effective_exposure`` when exposure is entirely absent).
    """

    kind: str
    axis_coordinate: float
    detail: Mapping[str, Any]


def _region_axis_interval(step_payload: Mapping) -> tuple[float, float]:
    """The path region as a scalar interval on its one varying axis (mirrors
    ``isar.scene.agenda._region_axis_interval`` — diagonal paths are refused upstream of the
    book, so this only needs to find the dominant axis, never handle more than one)."""
    region = step_payload["region"]
    start, end = region["q_start"], region["q_end"]
    deltas = [abs(b - a) for a, b in zip(start, end)]
    axis = deltas.index(max(deltas))
    lo, hi = start[axis], end[axis]
    return (lo, hi) if lo <= hi else (hi, lo)


def _gap_nodes(gap_lo: float, gap_hi: float, spacing: float) -> list[float]:
    """Interior nodes closing one gap: ``ceil(gap/spacing) - 1`` of them, evenly spaced —
    the minimal count leaving every resulting sub-gap ``<= spacing``.

    Node coordinates land exactly on the even grid (``gap_lo + i * gap/(n+1)``), including the
    boundary case ``gap == k * spacing`` — the predicate's gap check compares raw floats with
    no tolerance, so a caller assembling an exact-divisor region should expect the ordinary
    float noise of any such comparison, the same as it would surveying that grid by hand.

    ``n`` is computed with NO epsilon: this is only ever called on a gap the caller already
    detected as ``> spacing`` (also with no tolerance), so ``ceil(gap/spacing) - 1`` is always
    ``>= 1`` here — a detected gap must never produce zero nodes, or the coverage guarantee this
    proposal exists to satisfy is silently broken for gaps a hair over the threshold."""
    gap = gap_hi - gap_lo
    n = math.ceil(gap / spacing) - 1
    if n <= 0:
        return []
    step = gap / (n + 1)
    return [round(gap_lo + i * step, 9) for i in range(1, n + 1)]


def _first_omega_gap(bands, lo: float, hi: float) -> tuple[float, float] | None:
    """First uncovered ``(lo, hi)`` sub-interval of the domain, by the SAME cursor walk as
    ``isar.scene.predicates._coverage_complete`` — re-derived rather than imported (that
    function is private and returns a bool, not the interval this proposal needs to name)."""
    cursor = lo
    for b_lo, b_hi in sorted((b["lo"], b["hi"]) for b in bands or ()):
        if b_lo > cursor:
            return (cursor, b_lo)
        cursor = max(cursor, b_hi)
        if cursor >= hi:
            return None
    return (cursor, hi) if cursor < hi else None


def _zc_anchors(scene_payload: Mapping, region: tuple[float, float]) -> tuple[float, ...]:
    """Declared zone-centre coordinates inside the region, mirroring
    ``isar.scene.agenda._zc_anchors``: multiples of a declared ``sample_knowledge.zone_period``
    anchored at q=0. Undeclared period -> no anchors, ordering falls through to geometric.

    Filtering is exact (ordering-only input, so no epsilon should ever include or exclude a
    boundary anchor by float noise): widen the candidate range by one multiple on each side,
    then keep exactly the multiples landing in ``[region[0], region[1]]``."""
    period = (scene_payload.get("sample_knowledge") or {}).get("zone_period")
    if not period:
        return ()
    first = math.floor(region[0] / period) - 1
    last = math.ceil(region[1] / period) + 1
    return tuple(round(n * period, 9) for n in range(first, last + 1)
                 if region[0] <= n * period <= region[1])


def _best_zone_centre_weight(profile: str) -> float:
    rules = placement_rules(profile)
    weights = [r.rule.order_weight for r in rules.values() if r.rule.anchor_kind == "zone_centre"]
    return max(weights) if weights else 0.0


def propose_survey(scene_payload: Mapping,
                    step_payload: Mapping) -> tuple[SurveyProposal, ...]:
    """The ordered opening-book proposal set for the current scene against a frozen step.

    Pure: identical inputs give byte-identical output, regardless of the scene's internal
    footprint/observation iteration order.
    """
    region = _region_axis_interval(step_payload)
    spacing = float(step_payload["region"]["maximum_initial_spacing_rlu"])
    domain_lo, domain_hi = (float(x) for x in step_payload["energy"]["intrinsic_omega_domain_meV"])
    policy = step_payload["resolved_policy"]
    exposure_floor = float(policy["thresholds"]["min_effective_exposure"])
    survey_order = policy["survey_order"]

    footprints = [fp for fp in (scene_payload.get("footprints") or ())
                  if fp.get("axis_coordinate") is not None]
    coords = sorted(fp["axis_coordinate"] for fp in footprints)

    proposals: list[SurveyProposal] = []

    edges = [region[0], *coords, region[1]]
    for a, b in zip(edges, edges[1:]):
        if b - a > spacing:
            for node in _gap_nodes(a, b, spacing):
                proposals.append(SurveyProposal("missing_q_node", node, {}))

    for fp in footprints:
        gap = _first_omega_gap(fp.get("bands"), domain_lo, domain_hi)
        if gap is not None:
            proposals.append(SurveyProposal(
                "missing_omega_band", fp["axis_coordinate"],
                {"omega_lo": gap[0], "omega_hi": gap[1]}))
        # Independent closures (DiscoverRegion's ω leg vs. VerifyCompleteness's exposure
        # floor): a node can owe both a band gap AND a deficit in the same round, and
        # under-reporting either costs the campaign an extra survey round to discover it.
        exposure = fp.get("effective_exposure")
        if exposure is None or exposure < exposure_floor:
            deficit = exposure_floor if exposure is None else exposure_floor - exposure
            proposals.append(SurveyProposal(
                "underexposed_cell", fp["axis_coordinate"], {"deficit": round(deficit, 9)}))

    anchors = ()
    zc_weight = 0.0
    if survey_order == "placement_informed":
        anchors = _zc_anchors(scene_payload, region)
        if anchors:
            zc_weight = _best_zone_centre_weight(step_payload["catalogue_profile"])

    def anchor_score(coord: float) -> float:
        if not anchors or zc_weight == 0.0:
            return 0.0
        distance = min(abs(coord - a) for a in anchors)
        return zc_weight / (1.0 + distance)

    def rank(p: SurveyProposal):
        extra = p.detail.get("omega_lo", p.detail.get("deficit", 0.0))
        tiebreak = (p.kind, round(p.axis_coordinate, 9), round(float(extra), 9))
        return (-anchor_score(p.axis_coordinate), p.axis_coordinate, KIND_RANK[p.kind], tiebreak)

    return tuple(sorted(proposals, key=rank))
