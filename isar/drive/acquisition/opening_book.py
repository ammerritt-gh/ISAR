"""DiscoverRegion's deterministic proposal generator, sparse-coverage regime (SCENE-4 PR 4.2a A5,
PR 32 wave W5: scoped, clipped, seeded, unrounded).

``propose_survey`` is a PURE function of ``(scene_payload, step_payload, subject)`` — no internal
memory, no clock, no random draw. Call it again after k proposals have been measured (their
footprints now in ``scene_payload``) and it proposes exactly the remainder; it never needs to be
told what it proposed last time.

**Scoped to one subject (P0-A).** ``subject`` is the ``DiscoverRegion`` question's
``QuestionSubject`` (a plain dict is accepted too, via :meth:`QuestionSubject.from_dict`).
Footprint selection is ``isar.scene.predicates.match_subject(subject, scene_payload)`` —
partition-exact, region-bounded on ``axis_coordinate`` — never every coordinate-bearing footprint
in the workspace: a footprint in another partition, or outside this subject's region, must never
close or perturb this gap check. The gap interval is ``subject.region_rlu``, not a value
re-derived from the step's raw endpoints — the subject is authoritative for the question being
served. A PROGRAM-scoped subject (``partition_id=None``) is refused outright (PR 4.2b C0):
physical evidence can never match one, so it reaching the book means the agenda's fan-out
contract was bypassed upstream — the cold-start path handles that case at the generator
wrapper, never here.

**Three proposal kinds, each bound to the closure predicate it serves** (mirrors
``isar.scene.predicates`` — never imports its private helpers, only re-derives the same walk
shape so the two can be pinned equal by test):

- ``missing_q_node`` — serves ``DiscoverRegion``'s gap check (``_coverage_complete``'s
  ``widest = max(edge deltas)`` over ``[region_lo, *sorted(matched axis_coordinate), region_hi]``).
  Every gap wider than ``maximum_initial_spacing_rlu`` gets ``ceil(gap/spacing) - 1`` evenly spaced
  interior nodes — the minimal count that leaves every sub-gap ``<= spacing``. Zero matched
  footprints AND a region no wider than spacing is a generator dead end otherwise (P1-E): in
  that narrow case (the walk above legitimately proposes nothing), this proposes exactly one
  deterministic seed node instead — the nearest epoch-compatible declared in-region anchor to the
  region midpoint (ties to the smaller coordinate), else the plain midpoint — so
  ``no_surveyed_nodes_in_region`` always has somewhere to go.
- ``missing_omega_band`` — serves ``DiscoverRegion``'s ω leg (the SAME cursor walk over sorted
  bands from ``domain_lo``, each band first clipped to the domain and any wholly-external band
  dropped — P1-D): one proposal per matched node whose bands leave a gap, naming that node's
  first uncovered ``(lo, hi)`` interval, which is always inside the domain.
- ``underexposed_cell`` — serves ``VerifyCompleteness``'s exposure floor
  (``_completeness_relative``'s ``effective_exposure < min_effective_exposure``): one proposal per
  matched node whose exposure is below the frozen floor (or missing), naming the deficit. A
  low-exposure node is never reported as a q/ω gap — ``DiscoverRegion`` and ``VerifyCompleteness``
  are different closures and this book never conflates them. The two ARE independent, though: a
  node with both an ω gap and low exposure gets both proposals in the same round, rather than
  making the campaign spend an extra round to discover the second problem.

A scene answering both closures yields zero proposals.

**Grid**: anchored to the subject's own region endpoints, never a global zero-anchored rounding
grid — a region that does not align to any external grid still gets exactly the right interior
nodes, because they are placed as fractions of ITS OWN gaps, with no rounding applied to the
placed coordinate (O: ``SurveyProposal`` is persisted nowhere, so a rounded node coordinate is a
pure liability — it can leave a sub-gap infinitesimally over the strict spacing threshold).

**Ordering never changes the proposal set** (asserted by test), only the sequence: it is computed
from the required set above, then sorted by the rank tuple

    (-anchor_score, axis_coordinate, KIND_RANK[kind], stable_tiebreak)

where ``anchor_score`` is 0 unless ``resolved_policy.survey_order == "placement_informed"`` (the
default) AND the scene payload carries a declared ``sample_knowledge.zone_period`` AND the
declared knowledge's ``lattice_epoch`` is compatible with the subject's partition — mirroring
``isar.scene.agenda`` epoch-scoping exactly (agenda.py:259-265 + :286: epoch-of-partition is read
from each partition's first observation; a missing or mismatched epoch fails CLOSED to zero
anchor score, i.e. uniform geometric ordering, never an exception). When all hold,
``anchor_score = best_zone_centre_order_weight / (1 + distance to nearest zone-centre anchor in
the region)``, where ``best_zone_centre_order_weight`` is the highest ``order_weight`` among the
catalogue profile's ``anchor_kind="zone_centre"`` classes
(:func:`isar.schema.placement.zone_centre_order_weight` — the class-list-blind, raw-registry
accessor; ``isar.drive`` may not import ``isar.analyze`` at all, so the book reads placement data
through ``isar.schema.placement`` rather than the class-list-aware
``isar.analyze.catalogue.placement_rules`` wrapper) — the only placement-rule input this generic,
class-agnostic book can honestly use, since a bare coverage/exposure gap carries no catalogue
class of its own. Under ``survey_order="uniform"`` every score is 0 and ordering falls straight
through to geometric position, so the SET is identical by construction while the ORDER is not.
``stable_tiebreak`` is ``(kind, round(axis_coordinate, 9), round(extra sort key, 9))`` — the
tiebreak rounding is ordering-only (never applied to the emitted coordinate) and stays
deterministic regardless of input iteration order.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from isar.schema.placement import zone_centre_order_weight
from isar.scene.questions import QuestionSubject, step_observed
from isar.scene.predicates import match_subject

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
    return [gap_lo + i * step for i in range(1, n + 1)]


def _first_omega_gap(bands, lo: float, hi: float) -> tuple[float, float] | None:
    """First uncovered ``(lo, hi)`` sub-interval of the domain, by the SAME cursor walk as
    ``isar.scene.predicates._coverage_complete`` — re-derived rather than imported (that
    function is private and returns a bool, not the interval this proposal needs to name).

    Bands are clipped to ``[lo, hi]`` before the walk and wholly-external bands are skipped
    entirely (P1-D): a band outside the program's own energy domain must never open a gap, or
    name a gap endpoint, beyond that domain."""
    cursor = lo
    clipped = []
    for b_lo, b_hi in ((b["lo"], b["hi"]) for b in bands or ()):
        if b_hi <= lo or b_lo >= hi:
            continue
        clipped.append((max(b_lo, lo), min(b_hi, hi)))
    for b_lo, b_hi in sorted(clipped):
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
    return tuple(n * period for n in range(first, last + 1)
                 if region[0] <= n * period <= region[1])


def _frame_compatible(subject: QuestionSubject, scene_payload: Mapping,
                      declared_partition: tuple | None = None) -> bool:
    """Whether declared zone geometry is in the SAME lattice frame as the subject's partition
    (mirrors ``isar.scene.agenda._derive_impl``, agenda.py:259-265 + :286 exactly): an
    incompatible or unverifiable epoch must stay inert rather than inheriting a foreign frame's
    anchors, so a mismatch fails CLOSED to zero anchor score, not an exception.

    The subject is always partition-scoped here — ``propose_survey`` refused a program-scoped
    one at the front door — so a permissive ``partition_id is None`` arm would be dead code
    wearing a live look (H1: deleted, not kept).

    ``declared_partition`` (SCENE-4 PR 4.3, D4) is the cold-start declaration as
    ``(partition_id, lattice_epoch, program_step)``. It SEEDS the attribution map — it never
    weakens the comparison: an observation-less declared partition would otherwise read the
    "missing" sentinel here, score every proposal at zero anchor weight, and silently degrade the
    campaign's very first scan to the region midpoint. ``setdefault`` keeps observations
    authoritative the moment any exist, and the seed itself stops once the DECLARING STEP has
    produced a TRUSTED observation (C10, verifier A1) — the same retirement rule ``isar.scene.agenda`` applies to the
    fan-out, so the two never disagree about whether the declaration is still live."""
    declared_epoch = (scene_payload.get("sample_knowledge") or {}).get("lattice_epoch")
    partition_of_ref = {fp["scan_ref"]: fp.get("partition_id")
                        for fp in scene_payload.get("footprints") or ()}
    epoch_of_pid: dict = {}
    for observation in scene_payload.get("observations") or ():
        pid_of = partition_of_ref.get(observation["scan_ref"])
        if pid_of is not None:
            epoch_of_pid.setdefault(pid_of, observation.get("lattice_epoch"))
    if declared_partition is not None and not step_observed(scene_payload, declared_partition[2]):
        epoch_of_pid.setdefault(declared_partition[0], declared_partition[1])
    return epoch_of_pid.get(subject.partition_id, "missing") == declared_epoch


def propose_survey(scene_payload: Mapping, step_payload: Mapping,
                    subject: QuestionSubject | Mapping,
                    declared_partition: tuple | None = None) -> tuple[SurveyProposal, ...]:
    """The ordered opening-book proposal set for the current scene against a frozen step,
    scoped to ONE ``DiscoverRegion`` question subject (P0-A): footprint selection is
    ``match_subject(subject, scene_payload)`` — partition-exact, region-bounded — never every
    coordinate-bearing footprint in the workspace. ``subject`` is authoritative for the region
    this question is about; a plain dict (driver convenience) is accepted via
    :meth:`QuestionSubject.from_dict`.

    Pure: identical inputs give byte-identical output, regardless of the scene's internal
    footprint/observation iteration order.
    """
    if isinstance(subject, Mapping):
        subject = QuestionSubject.from_dict(subject)
    if subject.partition_id is None:
        # PR 4.2b C0 (owner ruling 2026-08-14): a program-scoped subject can NEVER match
        # physical evidence — match_subject returns empty by the fan-out contract
        # (isar.scene.questions: program-scoped questions are fanned out to per-partition
        # children before any generator runs). One reaching the book is an upstream contract
        # violation, not a lattice-agnostic survey: refuse loudly rather than seed blindly.
        raise ValueError(
            "propose_survey: subject is program-scoped (partition_id=None); the agenda's "
            "fan-out contract (isar.scene.questions.resolve_scope) hands generators "
            "per-partition subjects only, and match_subject returns no physical evidence for "
            "a program-scoped subject by design — a partition-less subject reaching the "
            "opening book is an upstream contract violation")
    region = subject.region_rlu
    spacing = float(step_payload["region"]["maximum_initial_spacing_rlu"])
    domain_lo, domain_hi = (float(x) for x in step_payload["energy"]["intrinsic_omega_domain_meV"])
    policy = step_payload["resolved_policy"]
    exposure_floor = float(policy["thresholds"]["min_effective_exposure"])
    survey_order = policy["survey_order"]

    match = match_subject(subject, scene_payload)
    footprints = [fp for fp in match.footprints if fp.get("axis_coordinate") is not None]
    coords = sorted(fp["axis_coordinate"] for fp in footprints)

    proposals: list[SurveyProposal] = []

    edges = [region[0], *coords, region[1]]
    for a, b in zip(edges, edges[1:]):
        if b - a > spacing:
            for node in _gap_nodes(a, b, spacing):
                proposals.append(SurveyProposal("missing_q_node", node, {}))

    if not coords and not proposals:
        # P1-E: zero matched footprints AND a region no wider than spacing (including equality)
        # is a generator dead end — the ordinary gap walk above legitimately produces nothing
        # (the one [region_lo, region_hi] "gap" is <= spacing), yet DiscoverRegion stays open
        # (no_surveyed_nodes_in_region) with no proposal to close it. Seed one deterministic
        # node: the nearest epoch-compatible declared in-region anchor to the region midpoint
        # (ties to the smaller coordinate), else the plain midpoint. A region wider than spacing
        # with zero footprints already gets a full multi-node gap fill from the walk above and
        # never reaches here.
        midpoint = (region[0] + region[1]) / 2.0
        anchors = (_zc_anchors(scene_payload, region)
                  if _frame_compatible(subject, scene_payload, declared_partition) else ())
        seed = min(anchors, key=lambda a: (abs(a - midpoint), a)) if anchors else midpoint
        return (SurveyProposal("missing_q_node", seed, {}),)

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
    if survey_order == "placement_informed" and _frame_compatible(subject, scene_payload,
                                                                  declared_partition):
        anchors = _zc_anchors(scene_payload, region)
        if anchors:
            zc_weight = zone_centre_order_weight(step_payload["catalogue_profile"])

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
