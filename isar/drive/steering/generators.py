"""``DiscoveryContext`` and the seven discovery generator bindings (PR 4.2b C4 / B3).

Each generator is a pure function ``(context, spec_dict) -> GeneratorOutcome`` for ONE ready
question spec of its family (readiness — dependency-open gating — is C5's job; a generator here
is only ever called on a spec its caller has already decided is ready). It either proposes one
or more ``(MeasurementIntent, QuestionActionAssociation)`` pairs, or returns a typed zero-action
outcome: a clean zero (the closure the generator's own predicate serves is already satisfied —
see the matrix in ``docs/design/scene.md`` §5, landing at C9) or an INAPPLICABLE (a durable or
model-authority reason the generator cannot even attempt this question). A generator that hits a
genuine defect raises — fault isolation across generators is the caller's frame (C5/C7), not
this module's.

**hkl reconstruction is shared and pure** (:func:`_hkl_at`): the step's declared ``q_start`` /
``q_end`` name the varying axis (mirroring ``isar.scene.agenda._region_axis_interval`` exactly —
the agenda already refused any path varying in more than one component, so re-deriving the axis
here can never disagree with it), and ``hkl(t)`` is ``q_start`` with that one component replaced
by ``t``. Every generator that names a scalar coordinate goes through this one function.

**Vocabulary is uniform across all seven** (the matrix's shared rules, fixed PR #33 A-P0-1):
``window_family = "discovery"``; ``action_class = DISCOVERY_ACTION_CLASS`` — ONE physical
action class for every discovery generator, never the generator id. A generator id in
``action_class`` was a PHYSICAL-IDENTITY bug, not a labeling choice: two generators proposing
the identical physical scan (same q, same ω window, same partition) minted two different
scientific fingerprints and could never dedup onto one action — which generator ASKED is
provenance, and it still rides on every :class:`~isar.schema.generators.QuestionActionAssociation`
and on ``ActionEvaluation.generator``; it must never fork what counts as "the same measurement".
``evidence_sink = CANONICAL_MASTER`` (nothing else is a discovery generator's business to
propose); ``evidence_basis_fingerprint`` = the scene digest carried on :class:`DiscoveryContext`;
``campaign_slice_fingerprint`` = the subject's partition token (``None`` only when the subject
is itself program-scoped — unreachable for the shipped seven, since every generator that could
receive a program-scoped subject either refuses it (cold-start INAPPLICABLE) or is structurally
unreachable while one is open) — so two subjects with the IDENTICAL (q, ω) in two DIFFERENT
partitions never dedup onto one action. ``hypotheses_fingerprint`` stays ``None`` (4.2b is
offline; the 4.3 driver stamps hypotheses). ``window_center_meV`` is set only where the matrix
names an explicit target mode (``identity_discriminator``, ``parameter_refine``,
``mismatch_probe``) — the matrix is silent on it for the other four, so this module leaves it
unset there rather than inventing realization metadata the matrix never asked for.

Several helpers re-derive small walks from ``isar.scene.predicates`` rather than importing its
private (underscore) functions — the same idiom ``opening_book.py`` already uses, so the two can
be pinned equal by test without this module reaching into another module's implementation
details. The public predicate primitives (``cluster_authority``, ``combine_universal``,
``mass_interval``, ``identity_hypothesis_classes``, ``tile_contains``, ``match_subject``) are
imported directly — there is exactly one implementation of those.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Mapping

from isar.schema.action_identity import MeasurementIntent
from isar.schema.generators import FAMILY_GENERATORS, QuestionActionAssociation
from isar.schema.steering import CANONICAL_MASTER
from isar.scene.predicates import (
    cluster_authority, combine_universal, identity_hypothesis_classes, mass_interval,
    tile_contains)
from isar.scene.questions import QuestionSubject, match_subject

from isar.drive.acquisition.opening_book import propose_survey

__all__ = ["DiscoveryContext", "GeneratorOutcome", "GENERATOR_FUNCTIONS", "dependency_depth",
          "DISCOVERY_ACTION_CLASS"]

#: The ONE physical action class every discovery generator proposes under (PR #33 A-P0-1).
#: "Which generator asked" is provenance (``ActionEvaluation.generator`` and every
#: ``QuestionActionAssociation``), never physical identity — a generator id here would fork one
#: physical scan into as many scientific actions as generators that happened to want it.
DISCOVERY_ACTION_CLASS = "discovery_scan"


# ----------------------------------------------------------------------------
# Context and outcome
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class DiscoveryContext:
    """Everything the seven generators need, and nothing else (kept minimal per the plan).

    ``agenda_payload`` is the derived pass-1 (exhaustion-free) agenda — specs and assessments as
    persisted dicts, the same shape ``derive_agenda`` returns from ``.payload``.
    ``basis_agenda_digest`` is the pass-1 basis digest (the same comparand
    ``QuestionExhaustion``/``QuestionActionAssociation`` bind against — the digest exists before
    generators run, unlike the final agenda digest). ``scene_digest`` is the fingerprint of
    ``scene_payload`` — computed once by the caller (``fingerprint(scene_payload)``) and carried
    here rather than recomputed per generator call.
    """

    scene_payload: Mapping
    agenda_payload: Mapping
    step_payload: Mapping
    basis_agenda_digest: str
    scene_digest: str
    # SCENE-4 PR 4.3 (D4): the cold-start declaration as
    # ``(partition_id, lattice_epoch, program_step)``, or
    # None off the cold-start path. Carried only so the opening book can seed its frame
    # attribution for a partition no observation has placed yet; nothing here derives from it.
    declared_partition: tuple | None = None
    # The card's declared hypothesis set, fingerprinted (review finding 2026-08-18). It reaches the
    # INTENT here rather than being stamped on the executable request later: the fingerprint is an
    # input to ``scientific_fingerprint``, so introducing it after selection left the inventory,
    # the scientific identity, the physical retry key, the decision basis and the scheduler all
    # describing an action WITHOUT the physics that would interpret its measurement — and a card
    # edit could leave a persisted decision byte-identical while changing what the scan means.
    # Read once per step by the caller, so it cannot move mid-step.
    hypotheses_fingerprint: str | None = None


@dataclass(frozen=True)
class GeneratorOutcome:
    """Exactly one of: a non-empty proposal list, a clean zero, or an INAPPLICABLE — never a
    silent empty success (the matrix treats an unexplained zero as a generator defect for every
    row except the three explicitly documented "none" cases, which this module encodes as
    always-non-empty generators instead of a fourth outcome shape).

    Every INAPPLICABLE reason in the matrix is model/program-authority and explicitly NOT
    exhaustion-clean — enforced structurally rather than left to each call site to get right.
    """

    proposals: tuple = ()
    clean_zero_reason: str | None = None
    inapplicable_reason: str | None = None
    exhaustion_clean: bool = False
    #: Whether ``proposals`` SPANS the question's admissible action space, or samples it (review
    #: finding 2026-08-18). The exact counterpart of ``exhaustion_clean`` on the other branch: that
    #: field lets a generator say "my ZERO means the space is genuinely empty" rather than "I did
    #: not find anything", and this one lets it say "if you refuse everything I offered, there is
    #: nothing else that would answer this question". Without it, "every offered action was
    #: refused" collapsed to "the one heuristic pick was refused" for every single-proposal
    #: generator, and one unreachable point closed a whole region as unanswerable.
    proposals_exhaustive: bool = False

    def __post_init__(self) -> None:
        set_flags = (bool(self.proposals), self.clean_zero_reason is not None,
                    self.inapplicable_reason is not None)
        if sum(set_flags) != 1:
            raise ValueError(
                "GeneratorOutcome must carry exactly one of proposals / clean_zero_reason / "
                f"inapplicable_reason, got proposals={len(self.proposals)}, "
                f"clean_zero_reason={self.clean_zero_reason!r}, "
                f"inapplicable_reason={self.inapplicable_reason!r}")
        if self.inapplicable_reason is not None and self.exhaustion_clean:
            raise ValueError(
                "an INAPPLICABLE outcome is never exhaustion-clean in this matrix (every "
                "declared reason is durable-program or model-authority) — a caller asking for "
                "one is a construction bug, not a real generator state")

    @classmethod
    def proposed(cls, pairs, *, proposals_exhaustive: bool) -> "GeneratorOutcome":
        """``proposals_exhaustive`` is REQUIRED, exactly as ``exhaustion_clean`` is on
        :meth:`clean_zero`: a generator that cannot say whether its offers span the question's
        admissible actions must say so explicitly, never by omission. ``False`` is the fail-safe
        answer — it leaves the question OPEN and blocked instead of closing it unresolvable."""
        return cls(proposals=tuple(pairs), proposals_exhaustive=bool(proposals_exhaustive))

    @classmethod
    def clean_zero(cls, reason: str, *, exhaustion_clean: bool) -> "GeneratorOutcome":
        return cls(clean_zero_reason=reason, exhaustion_clean=exhaustion_clean)

    @classmethod
    def inapplicable(cls, reason: str) -> "GeneratorOutcome":
        return cls(inapplicable_reason=reason)


# ----------------------------------------------------------------------------
# Shared construction rules
# ----------------------------------------------------------------------------
def _hkl_at(step_payload: Mapping, t: float) -> tuple[float, float, float]:
    """The scalar-to-3-vector inverse map: ``q_start`` with the varying axis set to ``t``.

    Mirrors ``isar.scene.agenda._region_axis_interval``'s axis selection exactly (the STEP is
    authoritative, never re-derived from scene data — one-scan scenes have no direction of their
    own) so a reconstructed footprint's ``axis_coordinate`` always matches the axis the agenda
    itself would compute.
    """
    region = step_payload["region"]
    start, end = region["q_start"], region["q_end"]
    deltas = [abs(b - a) for a, b in zip(start, end)]
    axis = deltas.index(max(deltas))
    hkl = list(start)
    hkl[axis] = t
    return tuple(hkl)


def _domain(step_payload: Mapping) -> tuple[float, float]:
    lo, hi = step_payload["energy"]["intrinsic_omega_domain_meV"]
    return float(lo), float(hi)


def _clip_omega(step_payload: Mapping, band: tuple[float, float] | None) -> tuple[float, float]:
    """``band`` clipped to the step's frozen domain; ``None`` means "the full domain"."""
    domain_lo, domain_hi = _domain(step_payload)
    if band is None:
        return domain_lo, domain_hi
    lo, hi = float(band[0]), float(band[1])
    return max(lo, domain_lo), min(hi, domain_hi)


def _slice_fingerprint(partition_id: str | None) -> str | None:
    """The subject's partition token, prefixed for legibility (``isar.partition/<pid>``) so a
    reader can tell it apart from a scene/agenda digest at a glance — the partition_id itself is
    already a stable token, this only labels it. ``None`` only for a program-scoped subject,
    which no shipped generator actually reaches (see the module docstring)."""
    return None if partition_id is None else f"isar.partition/{partition_id}"


def _make_intent(context: DiscoveryContext, hkl, energy_lo, energy_hi, *, partition_id: str | None,
                 window_center_meV: float | None = None) -> MeasurementIntent:
    return MeasurementIntent(
        action_class=DISCOVERY_ACTION_CLASS, hkl=hkl, energy_lo=energy_lo, energy_hi=energy_hi,
        window_family="discovery", evidence_basis_fingerprint=context.scene_digest,
        hypotheses_fingerprint=context.hypotheses_fingerprint,
        campaign_slice_fingerprint=_slice_fingerprint(partition_id),
        evidence_sink=CANONICAL_MASTER, window_center_meV=window_center_meV)


def dependency_depth(spec_dict: Mapping, agenda_payload: Mapping) -> int:
    """The longest dependency chain ending at this spec, within the agenda payload's specs.

    A pure function over declared ``dependencies`` — not the topological-scheduling depth among
    READY questions (a C7 scheduler concern); a dependency naming a question absent from this
    agenda payload counts as depth 0 (the DAG only reasons about specs actually present).
    """
    specs_by_id = {s["question_id"]: s for s in agenda_payload.get("specs") or ()}
    memo: dict[str, int] = {}

    def depth(question_id: str) -> int:
        if question_id in memo:
            return memo[question_id]
        spec = specs_by_id.get(question_id)
        deps = spec["dependencies"] if spec else ()
        memo[question_id] = 0  # break cycles defensively; the agenda already validates a DAG
        result = 0 if not deps else 1 + max(depth(d) for d in deps)
        memo[question_id] = result
        return result

    return depth(spec_dict["question_id"])


def _make_association(context: DiscoveryContext, spec_dict: Mapping, generator_id: str, *,
                      reason: str, placement_rank: int = 0,
                      canonical: bool = True) -> QuestionActionAssociation:
    """``placement_rank`` is this generator's own preference order over the proposals it emitted
    for THIS question — 0 first. Generators that emit one proposal per question leave it at 0.

    ``canonical`` (SCENE-5 5.2, D4) defaults True — every generator's proposals are wholly
    canonical unless it explicitly widens its admissible set beyond its own heuristic pick
    (currently only ``track_bridge_scan``'s between-node proposals)."""
    return QuestionActionAssociation(
        basis_agenda_digest=context.basis_agenda_digest, question_id=spec_dict["question_id"],
        generator=generator_id, family_priority=int(spec_dict["priority"]),
        dependency_depth=dependency_depth(spec_dict, context.agenda_payload), reason=reason,
        placement_rank=int(placement_rank), canonical=bool(canonical))


def _assessment_of(agenda_payload: Mapping, question_id: str) -> dict | None:
    for assessment in agenda_payload.get("assessments") or ():
        if assessment["question_id"] == question_id:
            return assessment
    return None


def _tile_midpoint(region: tuple[float, float]) -> float:
    return (region[0] + region[1]) / 2.0


# ----------------------------------------------------------------------------
# Re-derived predicate-adjacent walks (re-derived, never importing predicates' private helpers —
# the opening_book.py idiom: pinned equal by test, not shared by reaching into another module).
# ----------------------------------------------------------------------------
def _in_band(omega, band) -> bool:
    if omega is None or not (isinstance(omega, (int, float)) and math.isfinite(omega)):
        return False
    if band is None:
        return True
    return band[0] <= abs(omega) <= band[1]


def _candidates_by_ref(scene_payload: Mapping) -> dict[str, dict[str, Mapping]]:
    out: dict[str, dict[str, Mapping]] = {}
    for obs in scene_payload.get("observations") or ():
        out[obs["scan_ref"]] = {c["label"]: c for c in obs.get("candidates") or ()}
    return out


def _mode_matches(alternative: Mapping, refs: set, candidates: Mapping, band) -> list[tuple]:
    """Every ``(row, slot, scan_ref, label)`` this alternative asserts with a placeable in-band
    mode — the shared per-alternative walk (mirrors ``isar.scene.predicates._matched_mode_omegas``
    / ``_alternative_matched_identities``'s inner loop)."""
    out = []
    for scan_ref, label in alternative.get("assignment") or ():
        if scan_ref not in refs:
            continue
        row = candidates.get(scan_ref, {}).get(label)
        if row is None:
            continue
        omegas = row.get("omegas") or []
        for component in row.get("components") or ():
            if component.get("kind") != "mode":
                continue
            slot = component["slot"]
            omega = omegas[slot] if 0 <= slot < len(omegas) else None
            if _in_band(omega, band):
                out.append((row, slot, scan_ref, label))
    return out


def _trusted_matched_interval(alternative: Mapping, refs: set, candidates: Mapping, band):
    """Mirrors ``isar.scene.predicates._trusted_matched_interval`` exactly (predicates.py:
    463-507): the ONE trusted, matched per-mode interval, or ``None`` when absent, ambiguous
    (more than one in-band match), or not writer-verified ``producer_sound``."""
    matches = _mode_matches(alternative, refs, candidates, band)
    if len(matches) != 1:
        return None
    row, slot, scan_ref, label = matches[0]
    mode_intervals = row.get("mode_intervals") or []
    interval = mode_intervals[slot] if 0 <= slot < len(mode_intervals) else None
    if interval is None:
        return None
    authority = interval.get("authority") or {}
    if authority.get("authority_basis") != "producer_sound":
        return None
    return {"low": float(interval["low"]), "high": float(interval["high"]),
            "scan_ref": scan_ref, "label": label, "slot": slot}


def _acceptance_window(interval: Mapping, tolerance: float):
    """Mirrors ``isar.scene.predicates._acceptance_window`` exactly."""
    if interval["high"] - interval["low"] > 2.0 * tolerance:
        return None
    return (interval["high"] - tolerance, interval["low"] + tolerance)


# ----------------------------------------------------------------------------
# 1. opening_book_survey (DiscoverRegion / coverage_complete)
# ----------------------------------------------------------------------------
def opening_book_survey(context: DiscoveryContext, spec_dict: Mapping) -> GeneratorOutcome:
    subject = QuestionSubject.from_dict(spec_dict["subject"])
    if subject.partition_id is None:
        # Cold start (PR 4.2b C0 / owner ruling): the agenda deliberately derives a
        # program-scoped DiscoverRegion on an unmeasured workspace. The book front-door raises
        # on a program-scoped subject, so the wrapper checks BEFORE calling it — never a
        # controller fault, never a blind seed.
        return GeneratorOutcome.inapplicable("program_scoped_subject_unfanned")
    proposals = [p for p in propose_survey(context.scene_payload, context.step_payload, subject,
                                          context.declared_partition)
                if p.kind in ("missing_q_node", "missing_omega_band")]
    if not proposals:
        return GeneratorOutcome.clean_zero("coverage_complete", exhaustion_clean=True)
    pairs = []
    # ``propose_survey`` returns its proposals in PLACEMENT-INFORMED order (zone-centre anchoring,
    # then axis coordinate). That order is the physics this generator knows and the scheduler
    # cannot re-derive, so it travels as ``placement_rank`` rather than being dropped — see
    # QuestionActionAssociation for what dropping it cost.
    for rank, p in enumerate(proposals):
        if p.kind == "missing_q_node":
            energy_lo, energy_hi = _clip_omega(context.step_payload, None)
        else:
            energy_lo, energy_hi = _clip_omega(
                context.step_payload, (p.detail["omega_lo"], p.detail["omega_hi"]))
        hkl = _hkl_at(context.step_payload, p.axis_coordinate)
        intent = _make_intent(context, hkl, energy_lo, energy_hi,
                              partition_id=subject.partition_id)
        association = _make_association(context, spec_dict, "opening_book_survey",
                                        reason=f"opening_book:{p.kind}", placement_rank=rank)
        pairs.append((intent, association))
    # NOT exhaustive: the coverage question asks whether the REGION is surveyed at the declared
    # spacing, and each proposal is ONE point inside an uncovered gap. A different point in the
    # same gap fills it just as well, so the instrument refusing this one proves nothing about the
    # gap — let alone about the region. (The short-region arm is starker still: it seeds a single
    # deterministic node near the midpoint.)
    return GeneratorOutcome.proposed(pairs, proposals_exhaustive=False)


# ----------------------------------------------------------------------------
# 2. exposure_fill (VerifyCompleteness / completeness_relative)
# ----------------------------------------------------------------------------
def exposure_fill(context: DiscoveryContext, spec_dict: Mapping) -> GeneratorOutcome:
    subject = QuestionSubject.from_dict(spec_dict["subject"])
    if subject.partition_id is None:
        return GeneratorOutcome.inapplicable("program_scoped_subject_unfanned")
    proposals = [p for p in propose_survey(context.scene_payload, context.step_payload, subject,
                                          context.declared_partition)
                if p.kind == "underexposed_cell"]
    if not proposals:
        return GeneratorOutcome.clean_zero("exposure_floor_met", exhaustion_clean=True)
    energy_lo, energy_hi = _clip_omega(context.step_payload, None)
    pairs = []
    for p in proposals:
        hkl = _hkl_at(context.step_payload, p.axis_coordinate)
        intent = _make_intent(context, hkl, energy_lo, energy_hi,
                              partition_id=subject.partition_id)
        association = _make_association(
            context, spec_dict, "exposure_fill",
            reason=f"underexposed_cell deficit={p.detail['deficit']!r}")
        pairs.append((intent, association))
    # Exhaustive: the question is whether THIS already-surveyed node meets the exposure floor, and
    # the only action that raises this node's exposure is re-measuring this node. There is no
    # alternative q to offer, so a refusal here really does exhaust the question.
    return GeneratorOutcome.proposed(pairs, proposals_exhaustive=True)


# ----------------------------------------------------------------------------
# 3. presence_probe (ConfirmPresence / structural_presence)
# ----------------------------------------------------------------------------
def presence_probe(context: DiscoveryContext, spec_dict: Mapping) -> GeneratorOutcome:
    """Open presence always admits (re)measurement — never a clean zero, never INAPPLICABLE; a
    generator that produced nothing here would be a defect, not a scientific outcome.

    SCENE-5 5.3 (Ruling A/B, ``design/scene.md`` H23): beside the canonical +E proposal, always
    emits one mirrored −E variant at the SAME node — window = the clipped +E window negated and
    swapped, ``canonical=False``. This is an ordinary on-path submission (Ruling B): no intent
    field, no provenance change, no role. The window sign alone distinguishes its
    ``scientific_action_fingerprint`` from the canonical row (``_identity_payload`` folds
    ``energy_lo``/``energy_hi`` into scientific identity). Emitted unconditionally — no
    temperature/Bose gate here; worth is priced later, at scoring, by the thermal_consistency
    corroboration term (Ruling A: the fitter's Bose tie already makes an untrusted mirror
    dominated by its +E twin under a trusted map, so gating in the producer would just duplicate
    what the doubt-priced MI term already decides)."""
    subject = QuestionSubject.from_dict(spec_dict["subject"])
    t = subject.anchor if subject.anchor is not None else _tile_midpoint(subject.region_rlu)
    energy_lo, energy_hi = _clip_omega(context.step_payload, subject.omega_band_meV)
    hkl = _hkl_at(context.step_payload, t)
    intent = _make_intent(context, hkl, energy_lo, energy_hi, partition_id=subject.partition_id)
    reason = ("reopen presence at declared anchor" if subject.anchor is not None
             else "reopen presence in emergent tile")
    association = _make_association(context, spec_dict, "presence_probe", reason=reason)
    mirror_intent = _make_intent(context, hkl, -energy_hi, -energy_lo,
                                 partition_id=subject.partition_id)
    mirror_association = _make_association(
        context, spec_dict, "presence_probe", reason=f"{reason} (−E mirror)",
        placement_rank=1, canonical=False)
    # One representative canonical action, not an enumeration of the admissible ones — so a
    # refusal of it does not prove the question unanswerable (review finding 2026-08-18); the
    # mirrored −E row is an additional admissible member, not an alternate representative.
    return GeneratorOutcome.proposed(
        ((intent, association), (mirror_intent, mirror_association)), proposals_exhaustive=False)


# ----------------------------------------------------------------------------
# 4. identity_discriminator (ClassifyIdentity / identity_resolved)
# ----------------------------------------------------------------------------
def _best_identity_witness(subject: QuestionSubject, scene_payload: Mapping):
    """The highest-posterior retained alternative asserting an EXPLICIT identity on a matched,
    in-band mode; ties break to the smallest ``(scan_ref, label, slot)``. ``None`` when no such
    witness exists (mirrors, but is not identical to, ``identity_hypothesis_classes`` — the
    caller checks that first and only calls this once it is known non-empty)."""
    match = match_subject(subject, scene_payload)
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), subject.omega_band_meV
    clusters = scene_payload.get("clusters") or ()
    best = None
    for index in match.cluster_indices:
        for alternative in clusters[index].get("alternatives") or ():
            posterior = alternative.get("posterior") or 0.0
            witnesses = []
            for scan_ref, label in alternative.get("assignment") or ():
                if scan_ref not in refs:
                    continue
                row = candidates.get(scan_ref, {}).get(label)
                if row is None or row.get("identity") is None:
                    continue
                omegas = row.get("omegas") or []
                for component in row.get("components") or ():
                    if component.get("kind") != "mode":
                        continue
                    slot = component["slot"]
                    omega = omegas[slot] if 0 <= slot < len(omegas) else None
                    if _in_band(omega, band):
                        witnesses.append((scan_ref, label, slot, float(omega)))
            if not witnesses:
                continue
            witnesses.sort()
            scan_ref, label, slot, omega = witnesses[0]
            candidate = (posterior, (scan_ref, label, slot), omega)
            if best is None or candidate[0] > best[0] or (
                    candidate[0] == best[0] and candidate[1] < best[1]):
                best = candidate
    if best is None:
        return None
    _, (scan_ref, label, slot), omega = best
    return {"scan_ref": scan_ref, "label": label, "slot": slot, "omega": omega}


def identity_discriminator(context: DiscoveryContext, spec_dict: Mapping) -> GeneratorOutcome:
    subject = QuestionSubject.from_dict(spec_dict["subject"])
    if not identity_hypothesis_classes(subject, context.scene_payload):
        return GeneratorOutcome.inapplicable("identity_hypotheses_unavailable")
    witness = _best_identity_witness(subject, context.scene_payload)
    if witness is None:
        # identity_hypothesis_classes and this walk share the same matched-cluster support and
        # the same "explicit, in-band, placeable" filter; a non-empty class set with no witness
        # here means the two have diverged -- a generator defect, not a scientific outcome.
        raise ValueError(
            f"identity_discriminator: identity_hypothesis_classes is non-empty for question "
            f"{spec_dict['question_id']!r} but no witness mode was found")
    t = _tile_midpoint(subject.region_rlu)
    energy_lo, energy_hi = _clip_omega(context.step_payload, subject.omega_band_meV)
    hkl = _hkl_at(context.step_payload, t)
    intent = _make_intent(context, hkl, energy_lo, energy_hi, partition_id=subject.partition_id,
                          window_center_meV=witness["omega"])
    association = _make_association(
        context, spec_dict, "identity_discriminator",
        reason=f"discriminate at {witness['scan_ref']}/{witness['label']}#{witness['slot']}")
    # One representative action, not an enumeration of the admissible ones — so a refusal of it
    # does not prove the question unanswerable (review finding 2026-08-18).
    return GeneratorOutcome.proposed(((intent, association),), proposals_exhaustive=False)


# ----------------------------------------------------------------------------
# 5. track_bridge_scan (ConnectTrack / track_connected)
# ----------------------------------------------------------------------------
def _endpoint_refs(scene_payload: Mapping, partition_id, tile) -> set:
    return {fp["scan_ref"] for fp in scene_payload.get("footprints") or ()
            if fp.get("partition_id") == partition_id and fp.get("axis_coordinate") is not None
            and tile_contains(tile, fp["axis_coordinate"])}


def _bridge_node(scene_payload: Mapping, partition_id, tile_a, tile_b) -> float:
    """Midpoint of the widest axis gap between matched in-partition footprints strictly between
    the two endpoint tiles, ties to the smaller coordinate. Degenerates cleanly to the tiles'
    own midpoint when nothing lies between them (or the tiles are adjacent/overlapping)."""
    lo, hi = tile_a[1], tile_b[0]
    coords = sorted(
        fp["axis_coordinate"] for fp in scene_payload.get("footprints") or ()
        if fp.get("partition_id") == partition_id and fp.get("axis_coordinate") is not None
        and lo < fp["axis_coordinate"] < hi)
    edges = [lo, *coords, hi]
    gaps = list(zip(edges, edges[1:]))
    widest = max(gaps, key=lambda g: (g[1] - g[0], -g[0]))
    return (widest[0] + widest[1]) / 2.0


def _matched_footprint_columns(scene_payload: Mapping, partition_id, tile_a, tile_b) -> list:
    """Distinct ``axis_coordinate`` columns of in-partition footprints between and including the
    two endpoint tiles, sorted -- ``(coordinate, {scan_ref, ...})`` pairs. Duplicate coordinates
    collapse onto one column (this is what "zero-width cuts skipped" means: a cut only exists
    between two DISTINCT coordinates)."""
    lo, hi = min(tile_a[0], tile_b[0]), max(tile_a[1], tile_b[1])
    by_coord: dict[float, set] = {}
    for footprint in scene_payload.get("footprints") or ():
        if footprint.get("partition_id") != partition_id:
            continue
        coord = footprint.get("axis_coordinate")
        if coord is None or not (lo <= coord <= hi):
            continue
        by_coord.setdefault(coord, set()).add(footprint["scan_ref"])
    return sorted(by_coord.items())


def _joint_cluster_alternatives(scene_payload: Mapping, ends_a: set, ends_b: set) -> list:
    """Every alternative from a cluster whose ``scan_refs`` intersect BOTH endpoint scan_ref
    sets -- mirrors ``isar.scene.predicates._track_connected``'s own ``joint`` selection
    (predicates.py:417-420), re-derived rather than imported (this module's own idiom)."""
    out = []
    for cluster in scene_payload.get("clusters") or ():
        refs = set(cluster.get("scan_refs") or ())
        if refs & ends_a and refs & ends_b:
            out.extend(cluster.get("alternatives") or ())
    return out


def _crosses(alternative: Mapping, left: set, right: set) -> bool:
    """Whether ``alternative``'s link graph connects a component on the ``left`` side to one on
    the ``right`` side -- mirrors ``_track_connected``'s ``qualifies`` reachability walk
    (predicates.py:428-445) exactly, generalized from the two fixed endpoint-tile sets to an
    arbitrary left/right split of the matched footprint columns."""
    adjacency: dict[str, set] = {}
    for a, b in alternative.get("links") or ():
        adjacency.setdefault(a, set()).add(b)
        adjacency.setdefault(b, set()).add(a)

    def scan_of(component_id: str) -> str:
        return component_id.rsplit("#", 2)[0]

    starts = [c for c in adjacency if scan_of(c) in left]
    seen = set(starts)
    frontier = list(starts)
    while frontier:
        node = frontier.pop()
        if scan_of(node) in right:
            return True
        for neighbour in adjacency.get(node, ()):
            if neighbour not in seen:
                seen.add(neighbour)
                frontier.append(neighbour)
    return False


def _weakest_cut_node(scene_payload: Mapping, partition_id, tile_a, tile_b) -> float:
    """The midpoint of the MINIMUM-strength adjacent-pair cut among the matched footprint
    columns between and including the two endpoint tiles -- ties to the smaller coordinate.

    Cut strength is the summed retained posterior mass of every joint-cluster alternative whose
    link graph connects a component on the cut's ``<=``-side to one on its ``>=``-side; this is
    independent of any cluster's authority lane (exact/bounded/unresolved) -- the same raw
    posterior sum applies whether the assessment reached ``open`` via a mass straddle, a
    bound-vacuous note, or a blocker, because all three are evidence deficits a re-measure at the
    weakest link addresses alike (the plan's branch-table unification)."""
    columns = _matched_footprint_columns(scene_payload, partition_id, tile_a, tile_b)
    if len(columns) < 2:
        raise ValueError(
            "track_bridge_scan: weakest-cut construction needs at least two distinct matched "
            f"footprint coordinates between {tile_a} and {tile_b}, found {len(columns)} -- both "
            "endpoint tiles are measured for this branch to be reached, so this is a generator "
            "defect, not a scientific state")
    ends_a = {fp["scan_ref"] for fp in scene_payload.get("footprints") or ()
              if fp.get("partition_id") == partition_id and fp.get("axis_coordinate") is not None
              and tile_contains(tile_a, fp["axis_coordinate"])}
    ends_b = {fp["scan_ref"] for fp in scene_payload.get("footprints") or ()
              if fp.get("partition_id") == partition_id and fp.get("axis_coordinate") is not None
              and tile_contains(tile_b, fp["axis_coordinate"])}
    alternatives = _joint_cluster_alternatives(scene_payload, ends_a, ends_b)

    left: set = set()
    best: tuple[float, float] | None = None      # (strength, cut_point) -- lexicographic min
    for i in range(len(columns) - 1):
        coord_i, refs_i = columns[i]
        coord_next, _ = columns[i + 1]
        left |= refs_i
        right: set = set().union(*(refs for _, refs in columns[i + 1:]))
        strength = sum((alt.get("posterior") or 0.0) for alt in alternatives
                       if _crosses(alt, left, right))
        cut_point = (coord_i + coord_next) / 2.0
        candidate = (strength, cut_point)
        if best is None or candidate < best:
            best = candidate
    return best[1]


def _between_node_widening(context: DiscoveryContext, subject: QuestionSubject, tile_a, tile_b
                           ) -> list[float]:
    """Every UNMEASURED node of the frozen q-tile grid strictly between ``tile_a`` and
    ``tile_b`` (SCENE-5 5.2, D4's admissible-set widening for the one under-expressive
    generator). The grid quantum is the step's declared ``maximum_initial_spacing_rlu`` — the
    SAME identity-bearing q constant ``isar.schema.question_policy``'s ``IDENTITY_DEFAULTS``
    docstring names, and the exact quantum ``isar.drive.acquisition.opening_book._gap_nodes``
    walks for ``DiscoverRegion``'s own gap-fill proposals — so a "between-node" here is a node
    the ordinary survey grid would already have proposed, not an arbitrary interpolation.
    Node placement mirrors ``_gap_nodes`` (re-derived, not imported: that helper is private):
    ``ceil(gap/spacing) - 1`` nodes, evenly spaced across the whole endpoint-to-endpoint gap —
    degenerate (empty) when the tiles are adjacent or the gap is no wider than one quantum.
    """
    ordered_a, ordered_b = (tile_a, tile_b) if tile_a[0] <= tile_b[0] else (tile_b, tile_a)
    spacing = float(context.step_payload["region"]["maximum_initial_spacing_rlu"])
    if spacing <= 0.0:
        return []
    lo, hi = ordered_a[1], ordered_b[0]
    gap = hi - lo
    count = math.ceil(gap / spacing) - 1
    if count <= 0:
        return []
    step = gap / (count + 1)
    candidates = (lo + i * step for i in range(1, count + 1))
    return [node for node in candidates
           if not _already_measured(context.scene_payload, subject.partition_id, node)]


def track_bridge_scan(context: DiscoveryContext, spec_dict: Mapping) -> GeneratorOutcome:
    """The complete branch table (PR #33 A-P0-6): ``endpoint_unmeasured`` and
    ``endpoints_in_disjoint_clusters`` name a node directly; every OTHER open state with
    endpoints measured and jointly clustered -- no reason key at all, a bound-vacuous note, or a
    durable blocker -- is a mass-straddle the WEAKEST-CUT rule addresses the same way (all three
    are evidence deficits a re-measure fixes, never a generator defect). Only two states raise:
    ``no_endpoint_tiles_declared`` (the agenda never mints a ConnectTrack spec without both
    endpoint tiles, so reaching it is a spec-shape defect) and any reason key this table does not
    recognize (malformed)."""
    subject = QuestionSubject.from_dict(spec_dict["subject"])
    assessment = _assessment_of(context.agenda_payload, spec_dict["question_id"]) or {}
    reason = dict(assessment.get("notes") or ()).get("reason")
    tile_a, tile_b = subject.endpoint_a_rlu, subject.endpoint_b_rlu
    if reason == "endpoint_unmeasured":
        unmeasured_a = not _endpoint_refs(context.scene_payload, subject.partition_id, tile_a)
        target_tile = tile_a if unmeasured_a else tile_b
        t = _tile_midpoint(target_tile)
        detail = f"unmeasured endpoint tile {target_tile}"
    elif reason == "endpoints_in_disjoint_clusters":
        t = _bridge_node(context.scene_payload, subject.partition_id, tile_a, tile_b)
        detail = f"bridge gap between disjoint clusters spanning {tile_a}..{tile_b}"
    elif reason is None:
        # Endpoints measured and jointly clustered (evaluator verdict "open" with no named
        # reason -- mass straddle, bound-vacuous, or blocker-carrying alike): a midpoint
        # coinciding with an existing matched coordinate can only arise from degenerate
        # duplicate coordinates, and needs no special case -- ω is already the full domain
        # below, the same as every other re-measure of an already-matched node.
        t = _weakest_cut_node(context.scene_payload, subject.partition_id, tile_a, tile_b)
        detail = f"weakest-cut re-measure between {tile_a}..{tile_b}"
    else:
        raise ValueError(
            f"track_bridge_scan: question {spec_dict['question_id']!r} is open for reason "
            f"{reason!r} -- not endpoint_unmeasured, endpoints_in_disjoint_clusters, or a "
            "no-reason mass-straddle open state. no_endpoint_tiles_declared is a spec-shape "
            "defect (the agenda never mints a ConnectTrack spec without both endpoint tiles); "
            "any other key is malformed")
    energy_lo, energy_hi = _clip_omega(context.step_payload, None)
    hkl = _hkl_at(context.step_payload, t)
    intent = _make_intent(context, hkl, energy_lo, energy_hi, partition_id=subject.partition_id)
    association = _make_association(context, spec_dict, "track_bridge_scan", reason=detail)
    pairs = [(intent, association)]
    # SCENE-5 5.2 (D4): the one generator whose canonical pick under-expresses its admissible
    # set. Every unmeasured between-node of the frozen q-tile grid joins the proposal list as
    # canonical=False -- scene-EIG may pick one, but the software-fault fallback (which ranks
    # canonical rows only) never will. Ranked AFTER the canonical row, in coordinate order.
    for rank, node in enumerate(_between_node_widening(context, subject, tile_a, tile_b), start=1):
        extra_hkl = _hkl_at(context.step_payload, node)
        extra_intent = _make_intent(context, extra_hkl, energy_lo, energy_hi,
                                    partition_id=subject.partition_id)
        extra_association = _make_association(
            context, spec_dict, "track_bridge_scan", reason=f"between-node widen at {node}",
            placement_rank=rank, canonical=False)
        pairs.append((extra_intent, extra_association))
    # One representative canonical action, not an enumeration of the admissible ones — so a
    # refusal of it does not prove the question unanswerable (review finding 2026-08-18); the
    # widened non-canonical rows above are additional admissible members, not alternate reps.
    return GeneratorOutcome.proposed(tuple(pairs), proposals_exhaustive=False)


# ----------------------------------------------------------------------------
# 6. parameter_refine (EstimateParameter / both claim kinds)
# ----------------------------------------------------------------------------
def _case_i_target(subject: QuestionSubject, scene_payload: Mapping, tolerance: float,
                   requirement: str):
    """Mirrors ``_parameter_within_tolerance``'s trusted-interval / best-center walk
    (predicates.py:540-598) using only the public bound-arithmetic primitives, to find the
    contributing interval whose acceptance window contains the current adversarial-best
    center — key ``(window width, scan_ref, label, slot)``."""
    match = match_subject(subject, scene_payload)
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), subject.omega_band_meV
    clusters = scene_payload.get("clusters") or ()

    capable = []
    for index in match.cluster_indices:
        for alternative in clusters[index].get("alternatives") or ():
            interval = _trusted_matched_interval(alternative, refs, candidates, band)
            if interval is None:
                continue
            window = _acceptance_window(interval, tolerance)
            if window is not None:
                capable.append((window, interval))
    if not capable:
        return None

    def universal_mass(c: float) -> tuple[float, float]:
        def qualifies(alternative):
            interval = _trusted_matched_interval(alternative, refs, candidates, band)
            if interval is None:
                return False
            window = _acceptance_window(interval, tolerance)
            return window is not None and window[0] <= c <= window[1]
        intervals = []
        for index in match.cluster_indices:
            cl = clusters[index]
            lane, log_q = cluster_authority(cl)
            if requirement == "exact" and lane == "bounded":
                lane = "unresolved"
            if lane == "unresolved":
                intervals.append((0.0, 1.0))
                continue
            mass = sum((a.get("posterior") or 0.0) for a in cl.get("alternatives") or ()
                       if qualifies(a))
            intervals.append(mass_interval(mass, log_q))
        return combine_universal(intervals)

    centers = sorted({lo for (lo, hi), _ in capable} | {hi for (lo, hi), _ in capable})
    best_c, best_lo = None, None
    for c in centers:
        lo_bound, _ = universal_mass(c)
        if best_lo is None or lo_bound > best_lo:
            best_c, best_lo = c, lo_bound

    contenders = [((hi - lo), interval["scan_ref"], interval["label"], interval["slot"], interval)
                 for (lo, hi), interval in capable if lo <= best_c <= hi]
    if not contenders:
        return None
    contenders.sort(key=lambda t: t[:4])
    _, scan_ref, label, slot, interval = contenders[0]
    omega = float(candidates[scan_ref][label]["omegas"][slot])
    return {"case": "i", "scan_ref": scan_ref, "label": label, "slot": slot, "omega": omega}


def _case_ii_target(subject: QuestionSubject, scene_payload: Mapping):
    """No trust required: the highest-posterior retained alternative with an in-band matched
    mode, ties ``(scan_ref, label, slot)``."""
    match = match_subject(subject, scene_payload)
    candidates = _candidates_by_ref(scene_payload)
    refs, band = set(match.scan_refs), subject.omega_band_meV
    clusters = scene_payload.get("clusters") or ()
    best = None
    for index in match.cluster_indices:
        for alternative in clusters[index].get("alternatives") or ():
            posterior = alternative.get("posterior") or 0.0
            matches = _mode_matches(alternative, refs, candidates, band)
            if not matches:
                continue
            matches.sort(key=lambda m: (m[2], m[3], m[1]))   # scan_ref, label, slot
            row, slot, scan_ref, label = matches[0]
            omega = float((row.get("omegas") or [])[slot])
            candidate = (posterior, (scan_ref, label, slot), omega)
            if best is None or candidate[0] > best[0] or (
                    candidate[0] == best[0] and candidate[1] < best[1]):
                best = candidate
    if best is None:
        return None
    _, (scan_ref, label, slot), omega = best
    return {"case": "ii", "scan_ref": scan_ref, "label": label, "slot": slot, "omega": omega}


def _parameter_refine_tolerance(context: DiscoveryContext, spec_dict: Mapping,
                                subject: QuestionSubject, params: dict) -> GeneratorOutcome:
    assessment = _assessment_of(context.agenda_payload, spec_dict["question_id"]) or {}
    blockers = {b["name"] for b in assessment.get("blockers") or ()}
    notes = dict(assessment.get("notes") or ())
    # Case (i) is EXACTLY "trusted capable intervals exist but support < min_mass"; everything
    # else (no trusted interval anywhere, no_support_capable_interval, or a mixed-semantics
    # refusal) is case (ii) — read straight off the assessment the real evaluator already
    # computed for this question, rather than re-deciding the same three-way split a second time.
    is_case_i = ("within_candidate_uncertainty_unavailable" not in blockers
                and notes.get("reason") != "no_support_capable_interval"
                and "mixed_interval_semantics" not in notes)
    tolerance = float(params["tolerance_meV"])
    floor = float(params["statistical_floor_meV"])
    if tolerance < floor:
        # PR #33 A-P1-b: an ask tighter than the statistical floor can never be satisfied by any
        # measurement -- generating against it forever would drive an unclosable predicate.
        # Model/program-authority, like every other INAPPLICABLE in this matrix, so NOT
        # exhaustion-clean by construction (GeneratorOutcome.inapplicable's own invariant).
        return GeneratorOutcome.inapplicable("tolerance_below_representable_precision")
    requirement = str(spec_dict["predicate"]["authority_requirement"])

    target = _case_i_target(subject, context.scene_payload, tolerance, requirement) \
        if is_case_i else None
    if target is None:
        # ponytail: a case-(i) walk with no contender (a float-boundary edge the assessment's
        # coarser blocker/note check didn't anticipate) falls back to case (ii) rather than
        # failing the whole question; upgrade path is exact case detection if this ever fires.
        target = _case_ii_target(subject, context.scene_payload)
    if target is None:
        raise ValueError(
            f"parameter_refine: no in-band matched mode for OPEN question "
            f"{spec_dict['question_id']!r} — its ConfirmPresence dependency answered 'present' "
            "so at least one witness is expected; generator defect")

    footprint = next((fp for fp in context.scene_payload.get("footprints") or ()
                      if fp.get("scan_ref") == target["scan_ref"]), None)
    if footprint is None or footprint.get("axis_coordinate") is None:
        raise ValueError(
            f"parameter_refine: no footprint coordinate for scan_ref {target['scan_ref']!r}")
    t = footprint["axis_coordinate"]
    half_width = 2.0 * max(tolerance, floor)
    energy_lo, energy_hi = _clip_omega(
        context.step_payload, (target["omega"] - half_width, target["omega"] + half_width))
    hkl = _hkl_at(context.step_payload, t)
    intent = _make_intent(context, hkl, energy_lo, energy_hi, partition_id=subject.partition_id,
                          window_center_meV=target["omega"])
    association = _make_association(
        context, spec_dict, "parameter_refine",
        reason=f"case_{target['case']}:{target['scan_ref']}/{target['label']}#{target['slot']}")
    # One representative action, not an enumeration of the admissible ones — so a refusal of it
    # does not prove the question unanswerable (review finding 2026-08-18).
    return GeneratorOutcome.proposed(((intent, association),), proposals_exhaustive=False)


def _confirmed_tiles(agenda_payload: Mapping, partition_id, exclude: tuple) -> list[tuple]:
    """Every OTHER answered-present ConfirmPresence tile in ``partition_id`` (excluding the
    subject's own tile — it is trivially "confirmed", not something to bridge toward)."""
    assessments = {a["question_id"]: a for a in agenda_payload.get("assessments") or ()}
    tiles = []
    for spec_dict in agenda_payload.get("specs") or ():
        if spec_dict["family"] != "ConfirmPresence":
            continue
        subj = spec_dict["subject"]
        if subj["partition_id"] != partition_id or subj["region_rlu"] is None:
            continue
        region = tuple(subj["region_rlu"])
        if region == tuple(exclude):
            continue
        assessment = assessments.get(spec_dict["question_id"])
        if assessment and assessment["status"] == "answered" \
                and dict(assessment.get("answer") or ()).get("present") is True:
            tiles.append(region)
    return tiles


def _already_measured(scene_payload: Mapping, partition_id, coord: float) -> bool:
    return any(fp.get("partition_id") == partition_id and fp.get("axis_coordinate") is not None
              and abs(fp["axis_coordinate"] - coord) < 1e-9
              for fp in scene_payload.get("footprints") or ())


def _parameter_refine_zc(context: DiscoveryContext, spec_dict: Mapping,
                         subject: QuestionSubject) -> GeneratorOutcome:
    """Bridges from the declared anchor toward the nearest OTHER confirmed tile in the same
    partition. Clean-zero (``zc_adjacent_coverage_complete``) when there is no other confirmed
    tile to bridge toward, or when the bridge node this rule would name is already measured — an
    interpretation of the matrix's one-line construction note, since the matrix does not spell
    out the exact adjacent-coverage test; flagged in the PR body as an assumption, not a ruling.
    """
    anchor = subject.anchor
    confirmed = _confirmed_tiles(context.agenda_payload, subject.partition_id, subject.region_rlu)
    if not confirmed:
        return GeneratorOutcome.clean_zero("zc_adjacent_coverage_complete", exhaustion_clean=True)
    nearest = min(confirmed, key=lambda tile: (abs(_tile_midpoint(tile) - anchor), tile))
    node = (anchor + _tile_midpoint(nearest)) / 2.0
    if _already_measured(context.scene_payload, subject.partition_id, node):
        return GeneratorOutcome.clean_zero("zc_adjacent_coverage_complete", exhaustion_clean=True)
    energy_lo, energy_hi = _clip_omega(context.step_payload, subject.omega_band_meV)
    hkl = _hkl_at(context.step_payload, node)
    intent = _make_intent(context, hkl, energy_lo, energy_hi, partition_id=subject.partition_id)
    association = _make_association(context, spec_dict, "parameter_refine",
                                    reason=f"zc bridge node toward confirmed tile {nearest}")
    # One representative action, not an enumeration of the admissible ones — so a refusal of it
    # does not prove the question unanswerable (review finding 2026-08-18).
    return GeneratorOutcome.proposed(((intent, association),), proposals_exhaustive=False)


def parameter_refine(context: DiscoveryContext, spec_dict: Mapping) -> GeneratorOutcome:
    subject = QuestionSubject.from_dict(spec_dict["subject"])
    claim_kind = spec_dict["claim_kind"]
    if claim_kind == "zc_linear_compatibility":
        return _parameter_refine_zc(context, spec_dict, subject)
    if claim_kind != "parameter_within_tolerance":
        raise ValueError(f"parameter_refine: unknown EstimateParameter claim kind {claim_kind!r}")
    params = dict(spec_dict["predicate"]["parameters"])
    return _parameter_refine_tolerance(context, spec_dict, subject, params)


# ----------------------------------------------------------------------------
# 7. mismatch_probe (TestModelMismatch / mismatch_assessed)
# ----------------------------------------------------------------------------
def mismatch_probe(context: DiscoveryContext, spec_dict: Mapping) -> GeneratorOutcome:
    subject = QuestionSubject.from_dict(spec_dict["subject"])
    match = match_subject(subject, context.scene_payload)
    candidates = _candidates_by_ref(context.scene_payload)
    refs = set(match.scan_refs)
    footprints_by_ref = {fp["scan_ref"]: fp for fp in context.scene_payload.get("footprints") or ()
                        if fp.get("scan_ref") in refs and fp.get("axis_coordinate") is not None}
    witnesses = sorted(
        (footprints_by_ref[ref]["axis_coordinate"], ref)
        for ref in refs
        if ref in footprints_by_ref
        and any(row.get("identity") == "unknown" for row in candidates.get(ref, {}).values()))
    if not witnesses:
        return GeneratorOutcome.inapplicable("no_explicit_mismatch_competitor")
    coord, scan_ref = witnesses[0]

    band = subject.omega_band_meV
    omega = None
    for row in candidates.get(scan_ref, {}).values():
        if row.get("identity") != "unknown":
            continue
        omegas = row.get("omegas") or []
        for component in row.get("components") or ():
            if component.get("kind") != "mode":
                continue
            slot = component["slot"]
            candidate_omega = omegas[slot] if 0 <= slot < len(omegas) else None
            if _in_band(candidate_omega, band):
                omega = float(candidate_omega)
                break
        if omega is not None:
            break

    energy_lo, energy_hi = _clip_omega(context.step_payload, band)
    hkl = _hkl_at(context.step_payload, coord)
    intent = _make_intent(context, hkl, energy_lo, energy_hi, partition_id=subject.partition_id,
                          window_center_meV=omega)
    association = _make_association(context, spec_dict, "mismatch_probe",
                                    reason=f"remeasure unknown-identity arena {scan_ref}")
    # One representative action, not an enumeration of the admissible ones — so a refusal of it
    # does not prove the question unanswerable (review finding 2026-08-18).
    return GeneratorOutcome.proposed(((intent, association),), proposals_exhaustive=False)


# ----------------------------------------------------------------------------
# Dispatch table (mirrors acquire.py:1712-1718's declared-set assertion)
# ----------------------------------------------------------------------------
GENERATOR_FUNCTIONS: dict[str, Callable[[DiscoveryContext, Mapping], GeneratorOutcome]] = {
    "opening_book_survey": opening_book_survey,
    "exposure_fill": exposure_fill,
    "presence_probe": presence_probe,
    "identity_discriminator": identity_discriminator,
    "track_bridge_scan": track_bridge_scan,
    "parameter_refine": parameter_refine,
    "mismatch_probe": mismatch_probe,
}

_declared = {generator_id for ids in FAMILY_GENERATORS.values() for generator_id in ids}
if set(GENERATOR_FUNCTIONS) != _declared:
    raise RuntimeError(
        "isar.drive.steering.generators: GENERATOR_FUNCTIONS does not match "
        f"FAMILY_GENERATORS's declared id set: declared={sorted(_declared)} "
        f"dispatch={sorted(GENERATOR_FUNCTIONS)}")
del _declared
