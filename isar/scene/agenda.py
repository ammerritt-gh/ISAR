"""Agenda derivation, persistence, and the completion verdict (SCENE-4 PR 4.1).

``derive_agenda`` is PURE: identical (scene revision payload, frozen /2 step payload,
catalogue classes, exhaustion evidence) derive byte-identical agendas with identical digests,
regardless of any historical parent — it never reads ``current.json``, and there is no
metadata short-circuit (the store's byte comparison reports ``unchanged``). Lineage and time
live outside the blob, exactly as the scene store keeps them: the pointer carries
``parent``/``written_at``, and the typed transition records (opened / answered / reopened /
dissolved — relations BETWEEN two agendas, which a pure snapshot cannot contain) are appended
to ``transitions.jsonl`` by :func:`build_and_store_agenda` at flip time.

**Openers are declared, deterministic, and authority-aware.** Predetermined ConfirmPresence
questions open per (catalogue class, declared zone-centre anchor in region) — fold geometry
from the declared ``zone_period`` (H13), never derived from data, never a Bragg-reflection
assumption. Emergent questions open per frozen tile when the UPPER posterior bound of
structural presence reaches the frozen ``emergent_open_min_mass`` — omitted mass may contain
the phenomenon, so suppression needs the upper bound below threshold, not the lower.

**Unresolvable closure is evidence-gated.** A question closes ``unresolvable`` only when a
typed DURABLE blocker is recorded AND an authenticated :class:`QuestionExhaustion` proof is
fresh against this very derivation: its ``basis_agenda_digest`` must equal the same-pass
exhaustion-free derivation digest (recorded as ``basis_digest`` on the payload), its
``scene_digest`` the input scene, and its registry fingerprint the derivation manifest's. A
stale proof is rejected with the failing comparand recorded; exhaustion without a durable
blocker leaves the question open with a machine-readable ``generator_defect`` note. All of
4.1's production paths pass no evidence — nothing can close unresolvable until 4.2's producer
exists.

**The verdict carries no beam directive** (four-reasons law): ``complete`` / ``in_progress`` /
``incomplete_over_budget`` only. Completion requires every spec closed AND VerifyCompleteness
answering YES; a missing required VerifyCompleteness question is a derivation defect, never
implicit completion; unresolvable closures never block completion and surface as
``consultation_request`` records (descriptions only — the channel itself is 4.3).
"""
from __future__ import annotations

import json
import math
import os
from collections import Counter
from dataclasses import dataclass

from .model import canonical_bytes, digest_bytes, fingerprint
from .predicates import EVALUATORS, cluster_authority, evaluate_predicate, mass_interval
from .questions import (
    Blocker, PredicateDeclaration, QuestionAssessment, QuestionSpec, QuestionSubject,
    scene_partitions, snap_tile, validate_dependency_dag)
from .store import RevisionStore, StoreResult, UNSET

AGENDA_SCHEMA = "isar.agenda-revision/1"
AGENDA_STORE = RevisionStore("agenda", "isar.agenda-pointer/1", "agenda")
TRANSITIONS_NAME = "transitions.jsonl"

#: Bumping either re-derives every agenda (new digests) without minting new question IDs.
OPENER_VERSION = 1
MATCHER_VERSION = 1

#: Declared generator references (4.2 binds them to the real registry; here they are strings).
GENERATOR_REFS = {
    "DiscoverRegion": ("opening_book_survey",),
    "ConfirmPresence": ("presence_probe",),
    "ClassifyIdentity": ("identity_discriminator",),
    "ConnectTrack": ("track_bridge_scan",),
    "EstimateParameter": ("parameter_refine",),
    "TestModelMismatch": ("mismatch_probe",),
    "VerifyCompleteness": (),                    # closes on accounting, proposes nothing
}


@dataclass(frozen=True, slots=True)
class AgendaRevision:
    """Canonical bytes + digest, the same content-addressed shape as ``SceneRevision``."""

    canonical: bytes
    digest: str

    @classmethod
    def of(cls, payload: dict) -> "AgendaRevision":
        blob = canonical_bytes(payload)
        return cls(canonical=blob, digest=digest_bytes(blob))

    @property
    def payload(self) -> dict:
        return json.loads(self.canonical)


# ----------------------------------------------------------------------------
# Derivation
# ----------------------------------------------------------------------------
def _region_axis_interval(step_payload: dict) -> tuple[float, float]:
    """The path region as a scalar interval along its dominant reciprocal axis."""
    region = step_payload["region"]
    start, end = region["q_start"], region["q_end"]
    deltas = [abs(b - a) for a, b in zip(start, end)]
    axis = deltas.index(max(deltas))
    lo, hi = start[axis], end[axis]
    return (lo, hi) if lo <= hi else (hi, lo)


def _zc_anchors(scene_payload: dict, region: tuple[float, float]) -> tuple[float, ...]:
    """Declared zone-centre coordinates inside the region: multiples of the declared
    ``zone_period`` anchored at q = 0 (the O44 rule). Fold geometry, NOT the reflection list —
    FCC (3,0,0) is a fold with an extinct reflection and still gets its anchor. Undeclared
    period ⇒ no predetermined anchors; the capability is inert, never guessed."""
    period = (scene_payload.get("sample_knowledge") or {}).get("zone_period")
    if not period:
        return ()
    first = math.ceil(region[0] / period - 1e-9)
    last = math.floor(region[1] / period + 1e-9)
    return tuple(round(n * period, 9) for n in range(first, last + 1))


def _presence_upper_bound(cluster: dict, scan_ref: str, candidates_of, band) -> float:
    """Upper posterior bound that ``scan_ref`` carries a placeable in-band mode — the opener's
    fail-open side: an unresolved cluster has upper 1.0 and OPENS the question."""
    lane, log_q = cluster_authority(cluster)
    if lane == "unresolved":
        return 1.0
    mass = 0.0
    for alternative in cluster.get("alternatives") or ():
        for ref, label in alternative.get("assignment") or ():
            if ref != scan_ref:
                continue
            row = candidates_of.get(ref, {}).get(label)
            if row is None:
                continue
            omegas = row.get("omegas") or []
            for component in row.get("components") or ():
                if component.get("kind") != "mode":
                    continue
                slot = component.get("slot")
                omega = omegas[slot] if isinstance(slot, int) and 0 <= slot < len(omegas) \
                    else None
                if omega is not None and (band is None or band[0] <= abs(omega) <= band[1]):
                    mass += alternative.get("posterior") or 0.0
                    break
            else:
                continue
            break
    return mass_interval(mass, log_q)[1]


def _spec(family, kind, subject, params, *, priority, deps=(), provenance=()):
    return QuestionSpec(
        family=family, claim_kind=kind, subject=subject,
        predicate=PredicateDeclaration(kind=kind, version=1, parameters=params),
        priority=priority, dependencies=deps, generator_ids=GENERATOR_REFS[family],
        policy_provenance=provenance)


def derive_agenda(scene_payload: dict, step_payload: dict, *, step_id: str,
                  program_fingerprint: str, catalogue_classes: tuple[str, ...] = (),
                  exhaustion_evidence: tuple = (),
                  generator_registry_fingerprint: str | None = None) -> AgendaRevision:
    """Derive the agenda for one frozen discover step against one scene revision. Pure.

    ``catalogue_classes`` is the class vocabulary of the step's frozen catalogue profile,
    resolved by the CALLER (4.2 binds the real catalogue; tests pass literals) — the agenda
    layer reads the profile by reference only. ``exhaustion_evidence`` are
    :class:`QuestionExhaustion` proofs; anything stale against this derivation is rejected,
    never applied.
    """
    policy = step_payload["resolved_policy"]
    thresholds = policy["thresholds"]
    priorities = policy["priorities"]
    region = _region_axis_interval(step_payload)
    spacing = float(step_payload["region"]["maximum_initial_spacing_rlu"])
    domain = tuple(step_payload["energy"]["intrinsic_omega_domain_meV"])
    scene_digest = fingerprint(scene_payload)
    frame = (("catalogue_profile", step_payload["catalogue_profile"]),
             ("program_region", f"{region[0]}..{region[1]}"),
             ("defaults_profile",
              f"{policy['profile']['id']}/{policy['profile']['version']}"))

    partitions = scene_partitions(scene_payload) or (None,)
    candidates_of = {obs["scan_ref"]: {c["label"]: c for c in obs.get("candidates") or ()}
                    for obs in scene_payload.get("observations") or ()}
    footprints = scene_payload.get("footprints") or ()
    clusters = scene_payload.get("clusters") or ()

    specs: list[QuestionSpec] = []
    notes: list[tuple] = []
    for pid in partitions:
        # A scene with no partitions yet is an unmeasured workspace: the program-scoped
        # questions exist from scan one (Topic 5 — the question layer is authoritative from
        # the first scan, there is no bootstrap phase).
        base = dict(partition_id=pid) if pid is not None else dict(scope_selector=())
        discover = _spec(
            "DiscoverRegion", "coverage_complete",
            QuestionSubject(kind="region", region_rlu=region, omega_band_meV=domain,
                            frame=frame, **base),
            {"maximum_initial_spacing_rlu": spacing, "omega_domain_meV": list(domain)},
            priority=priorities["DiscoverRegion"])
        specs.append(discover)

        anchors = _zc_anchors(scene_payload, region)
        if not anchors:
            notes.append(("predetermined_openers_inert",
                          "no zone_period declared; anchored ConfirmPresence openers cannot "
                          "be placed from declared knowledge"))
        for catalogue_class in sorted(catalogue_classes):
            for anchor in anchors:
                specs.append(_spec(
                    "ConfirmPresence", "structural_presence",
                    QuestionSubject(kind="phenomenon", region_rlu=snap_tile(anchor, spacing),
                                    omega_band_meV=domain, catalogue_class=catalogue_class,
                                    anchor=anchor, frame=frame, **base),
                    {"min_mass": thresholds["confirm_presence_min_mass"]},
                    priority=priorities["ConfirmPresence"],
                    deps=(discover.question_id,),
                    provenance=(("opened_by", "declared_zc_anchor"),)))

        # Emergent per-tile openers: upper bound decides (fail open), tiles snapped to the
        # frozen spacing grid, one question per tile however many scans land in it.
        if pid is not None:
            covered = {spec.subject.region_rlu for spec in specs
                       if spec.family == "ConfirmPresence"
                       and spec.subject.partition_id == pid}
            emergent: dict[tuple, tuple] = {}
            coord_of = {fp["scan_ref"]: fp["axis_coordinate"] for fp in footprints
                        if fp.get("partition_id") == pid
                        and fp.get("axis_coordinate") is not None}
            for cluster in clusters:
                for scan_ref in cluster.get("scan_refs") or ():
                    coord = coord_of.get(scan_ref)
                    if coord is None:
                        continue
                    tile = snap_tile(coord, spacing)
                    if tile in covered or tile in emergent:
                        continue
                    upper = _presence_upper_bound(cluster, scan_ref, candidates_of, domain)
                    if upper >= thresholds["emergent_open_min_mass"]:
                        lane, _ = cluster_authority(cluster)
                        emergent[tile] = (upper, lane)
            for tile in sorted(emergent):
                upper, lane = emergent[tile]
                specs.append(_spec(
                    "ConfirmPresence", "structural_presence",
                    QuestionSubject(kind="phenomenon", partition_id=pid, region_rlu=tile,
                                    omega_band_meV=domain, frame=frame),
                    {"min_mass": thresholds["confirm_presence_min_mass"]},
                    priority=priorities["ConfirmPresence"],
                    deps=(discover.question_id,),
                    provenance=(("opened_by", "emergent"),
                                ("opener_authority", lane),
                                ("opener_upper_bound", round(upper, 9)))))

        specs.append(_spec(
            "TestModelMismatch", "mismatch_assessed",
            QuestionSubject(kind="model", region_rlu=region, omega_band_meV=domain,
                            frame=frame, **base),
            {"min_mass": thresholds["mismatch_min_mass"]},
            priority=priorities["TestModelMismatch"], deps=(discover.question_id,)))

    # First evaluation pass: everything opened so far, in dependency order.
    validate_dependency_dag(specs)
    assessments = _evaluate_in_order(specs, scene_payload)

    # Chained openers per confirmed phenomenon (regime 1): ClassifyIdentity and
    # EstimateParameter auto-open, anchored subjects additionally ask the endpoint question,
    # and a class with two or more confirmed anchors asks for its connecting track.
    chained: list[QuestionSpec] = []
    confirmed_anchor_tiles: dict[tuple, list] = {}
    for spec in [s for s in specs if s.family == "ConfirmPresence"]:
        assessment = assessments.get(spec.question_id)
        if assessment is None or assessment.status != "answered" \
                or dict(assessment.answer).get("present") is not True:
            continue
        subject = spec.subject
        chained.append(_spec(
            "ClassifyIdentity", "identity_resolved", subject.__class__(
                kind="phenomenon", partition_id=subject.partition_id,
                region_rlu=subject.region_rlu, omega_band_meV=subject.omega_band_meV,
                frame=subject.frame),
            {"min_mass": thresholds["classify_identity_min_mass"]},
            priority=priorities["ClassifyIdentity"], deps=(spec.question_id,)))
        chained.append(_spec(
            "EstimateParameter", "parameter_within_tolerance", subject.__class__(
                kind="parameter", partition_id=subject.partition_id,
                region_rlu=subject.region_rlu, omega_band_meV=subject.omega_band_meV,
                parameter="omega0", frame=subject.frame),
            {"tolerance_meV": thresholds["parameter_default_tolerance_meV"],
             "statistical_floor_meV": thresholds["parameter_statistical_floor_meV"]},
            priority=priorities["EstimateParameter"], deps=(spec.question_id,)))
        if subject.anchor is not None:
            chained.append(_spec(
                "EstimateParameter", "zc_linear_compatibility", subject.__class__(
                    kind="parameter", partition_id=subject.partition_id,
                    region_rlu=subject.region_rlu, omega_band_meV=subject.omega_band_meV,
                    parameter="zc_endpoint_omega", anchor=subject.anchor,
                    frame=subject.frame),
                {"requested_zc_confidence": thresholds["requested_zc_confidence"]},
                priority=priorities["EstimateParameter"], deps=(spec.question_id,)))
            key = (subject.partition_id, subject.catalogue_class)
            confirmed_anchor_tiles.setdefault(key, []).append(
                (subject.region_rlu, spec.question_id))
    for (pid, catalogue_class), tiles in sorted(
            confirmed_anchor_tiles.items(), key=lambda kv: (str(kv[0][0]), str(kv[0][1]))):
        if catalogue_class is None or len(tiles) < 2:
            continue
        tiles.sort()
        chained.append(_spec(
            "ConnectTrack", "track_connected", QuestionSubject(
                kind="track", partition_id=pid, catalogue_class=catalogue_class,
                endpoint_a_rlu=tiles[0][0], endpoint_b_rlu=tiles[-1][0], frame=frame),
            {"min_mass": thresholds["connect_track_min_mass"]},
            priority=priorities["ConnectTrack"],
            deps=tuple(qid for _, qid in tiles)))
    if chained:
        specs.extend(chained)
        validate_dependency_dag(specs)
        assessments.update(_evaluate_in_order(chained, scene_payload,
                                              prior=dict(assessments)))

    # VerifyCompleteness last, depending on every other question in its partition.
    for pid in partitions:
        base = dict(partition_id=pid) if pid is not None else dict(scope_selector=())
        peers = tuple(sorted(
            s.question_id for s in specs
            if (s.subject.partition_id == pid if pid is not None
                else not s.subject.partition_id)))
        verify = _spec(
            "VerifyCompleteness", "completeness_relative",
            QuestionSubject(kind="campaign", region_rlu=region, omega_band_meV=domain,
                            frame=frame, **base),
            {"min_effective_exposure": thresholds["min_effective_exposure"]},
            priority=priorities["VerifyCompleteness"], deps=peers)
        specs.append(verify)
        assessments[verify.question_id] = evaluate_predicate(
            verify, scene_payload, prior_assessments=assessments)
    validate_dependency_dag(specs)

    manifest = {"opener_version": OPENER_VERSION, "matcher_version": MATCHER_VERSION,
                "evaluator_versions": {kind: version
                                       for kind, (version, _) in sorted(EVALUATORS.items())},
                "generator_registry_fingerprint": generator_registry_fingerprint,
                "catalogue_classes": sorted(catalogue_classes)}

    def payload_of(assessment_map, *, basis_digest=None, evidence=()):
        return {
            "schema": AGENDA_SCHEMA,
            "program_fingerprint": program_fingerprint,
            "step_id": step_id,
            "scene_digest": scene_digest,
            "resolved_policy_fingerprint": fingerprint(policy),
            "derivation_manifest": manifest,
            "derivation_notes": [list(n) for n in notes],
            "basis_digest": basis_digest,
            "exhaustion_evidence": [e.to_dict() for e in evidence],
            "specs": [s.to_dict() for s in sorted(specs, key=lambda s: s.question_id)],
            "assessments": [assessment_map[qid].to_dict()
                            for qid in sorted(assessment_map)],
        }

    # The assessment basis: this very derivation with no exhaustion applied. Its digest is the
    # freshness comparand a proof must name — computable in the same pass, self-consistent.
    basis_digest = AgendaRevision.of(payload_of(assessments)).digest
    assessments, applied = apply_exhaustion(
        assessments, exhaustion_evidence, basis_digest=basis_digest,
        scene_digest=scene_digest,
        generator_registry_fingerprint=generator_registry_fingerprint)
    return AgendaRevision.of(payload_of(assessments, basis_digest=basis_digest,
                                        evidence=applied))


def apply_exhaustion(assessments: dict, exhaustion_evidence, *, basis_digest: str,
                     scene_digest: str, generator_registry_fingerprint: str | None
                     ) -> tuple[dict, tuple]:
    """The pure closure transition: ``(assessments, applied proofs)`` after evidence.

    Public because it IS the contract 4.2's producer closes against: a question becomes
    ``unresolvable`` only when its open assessment carries a typed DURABLE blocker and the
    proof is fresh on every comparand. In 4.1 no evaluator emits a durable blocker yet, so no
    live derivation can close unresolvable — this function is exercised with synthetic
    assessments until the 4.2/4.3 producers exist.
    """
    out = dict(assessments)
    applied = []
    for proof in exhaustion_evidence:
        assessment = out.get(proof.question_id)
        if assessment is None or assessment.status != "open":
            continue
        stale = None
        if proof.basis_agenda_digest != basis_digest:
            stale = ("basis_agenda_digest", proof.basis_agenda_digest, basis_digest)
        elif proof.scene_digest != scene_digest:
            stale = ("scene_digest", proof.scene_digest, scene_digest)
        elif proof.generator_registry_fingerprint != generator_registry_fingerprint:
            stale = ("generator_registry_fingerprint", proof.generator_registry_fingerprint,
                     generator_registry_fingerprint)
        if stale is not None:
            out[proof.question_id] = QuestionAssessment.from_dict({
                **assessment.to_dict(),
                "notes": [*assessment.to_dict()["notes"],
                          ["exhaustion_proof_rejected_stale",
                           f"{stale[0]}: proof {stale[1]!r} != derivation {stale[2]!r}"]]})
            continue
        if not proof.exhausted():
            continue
        if not assessment.durable_blockers():
            # Exhaustion with no durable physical blocker is a generator defect, never an
            # unresolvable question (Topic 4; the controller-fault lane routes it in 4.3).
            out[proof.question_id] = QuestionAssessment.from_dict({
                **assessment.to_dict(),
                "notes": [*assessment.to_dict()["notes"],
                          ["generator_defect",
                           "generators exhausted without a durable blocker"]]})
            continue
        applied.append(proof)
        closed = assessment.to_dict()
        closed["status"] = "unresolvable"
        closed["notes"] = [*closed["notes"],
                           ["unresolvable_closure", "durable blocker + fresh exhaustion"]]
        out[proof.question_id] = QuestionAssessment.from_dict(closed)
    return out, tuple(applied)


def _evaluate_in_order(specs, scene_payload, prior=None) -> dict[str, QuestionAssessment]:
    """Dependency-respecting deterministic evaluation: ready specs run in (priority,
    question_id) order; a dependency outside this batch counts as satisfied for ORDERING
    (its openness still reaches evaluators through ``prior``)."""
    assessments: dict[str, QuestionAssessment] = {}
    prior = dict(prior or {})
    pending = {s.question_id: s for s in specs}
    while pending:
        ready = [s for s in pending.values()
                 if all(d in assessments or d in prior or d not in pending
                        for d in s.dependencies)]
        ready.sort(key=lambda s: (s.priority, s.question_id))
        if not ready:                            # cycle — validate_dependency_dag catches
            raise ValueError("dependency deadlock during agenda evaluation")
        for spec in ready:
            merged = {**prior, **assessments}
            assessments[spec.question_id] = evaluate_predicate(
                spec, scene_payload, prior_assessments=merged)
            del pending[spec.question_id]
    return assessments


# ----------------------------------------------------------------------------
# Persistence + transition history
# ----------------------------------------------------------------------------
def build_and_store_agenda(report_dir, scene_payload: dict, step_payload: dict, *,
                           step_id: str, program_fingerprint: str,
                           catalogue_classes: tuple[str, ...] = (),
                           exhaustion_evidence: tuple = (),
                           generator_registry_fingerprint: str | None = None,
                           expected_parent=UNSET, source: str = "driver") -> StoreResult:
    """Derive, persist, and record typed transitions against the previous agenda.

    The previous agenda is an input to the TRANSITION RECORDS only — never to the derivation,
    so the digest stays parent-independent. Transitions (opened / answered / reopened /
    closed_unresolvable / dissolved) are appended to ``transitions.jsonl`` beside the store's
    ``history.jsonl``, one line per flip; an ``unchanged`` write appends nothing, because
    nothing happened.
    """
    previous = AGENDA_STORE.read_current(report_dir)
    revision = derive_agenda(
        scene_payload, step_payload, step_id=step_id,
        program_fingerprint=program_fingerprint, catalogue_classes=catalogue_classes,
        exhaustion_evidence=exhaustion_evidence,
        generator_registry_fingerprint=generator_registry_fingerprint)
    result = AGENDA_STORE.write_revision(report_dir, revision,
                                         expected_parent=expected_parent, source=source)
    if result.unchanged:
        return result

    old = {} if previous is None else {
        a["question_id"]: a["status"] for a in previous[1].get("assessments") or ()}
    new = {a["question_id"]: a["status"] for a in revision.payload["assessments"]}
    transitions = []
    for qid in sorted(set(old) | set(new)):
        was, now = old.get(qid), new.get(qid)
        if was == now:
            continue
        if was is None:
            transitions.append({"question_id": qid, "relation": "opened", "to": now})
        elif now is None:
            # The subject no longer derives from the current program+scene (a lattice epoch
            # moved, the program revised, the tile stopped qualifying). No physical
            # equivalence with any new question is asserted without a declared transform.
            transitions.append({"question_id": qid, "relation": "dissolved", "from": was,
                                "equivalence": "unproven"})
        elif was == "answered" and now == "open":
            transitions.append({"question_id": qid, "relation": "reopened"})
        else:
            transitions.append({"question_id": qid, "relation": "status_changed",
                                "from": was, "to": now})
    if transitions:
        with open(AGENDA_STORE._root(report_dir) / TRANSITIONS_NAME, "a",
                  encoding="utf-8") as stream:
            record = {"digest": revision.digest, "parent": result.parent,
                      "transitions": transitions}
            stream.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    return result


def read_transitions(report_dir) -> list[dict]:
    path = AGENDA_STORE._root(report_dir) / TRANSITIONS_NAME
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
            if line]


# ----------------------------------------------------------------------------
# Verdict
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class AgendaVerdict:
    """Completion accounting, and nothing else — no beam directive, no failure states.

    Under the four-reasons law a bad fit, an unknown phenomenon, or an unresolvable question
    can never appear here as a stop reason: user choice and machine failure are caller lanes
    (4.3), budget is the one input this evaluator prices, and everything else is
    ``in_progress``.
    """

    state: str                                   # "complete" | "in_progress" | "incomplete_over_budget"
    open_by_family: tuple                        # sorted (family, count) pairs
    closed_counts: tuple                         # sorted (status, count) pairs
    blocking: tuple[str, ...]                    # open question ids gating completion
    reasons: tuple[str, ...]
    consultation_requests: tuple[dict, ...]      # descriptions only; no prompt_ref until 4.3


def evaluate_agenda(agenda_payload: dict, *, budget_state: dict | None = None
                    ) -> AgendaVerdict:
    specs = {s["question_id"]: s for s in agenda_payload.get("specs") or ()}
    assessments = {a["question_id"]: a for a in agenda_payload.get("assessments") or ()}

    reasons: list[str] = []
    consultations: list[dict] = []
    open_ids = sorted(q for q, a in assessments.items() if a["status"] == "open")
    verify_specs = [s for s in specs.values() if s["family"] == "VerifyCompleteness"]
    if not verify_specs:
        # A derivation that opened no completeness question is defective; its absence must
        # never read as implicit completion.
        reasons.append("derivation_defect_missing_verify_completeness")
    verify_yes = bool(verify_specs) and all(
        assessments.get(s["question_id"], {}).get("status") == "answered"
        and dict(assessments[s["question_id"]].get("answer") or ()).get("complete") is True
        for s in verify_specs)

    for qid, assessment in sorted(assessments.items()):
        for name, detail in assessment.get("notes") or ():
            if name == "generator_defect":
                reasons.append(f"generator_defect:{qid}")
        if assessment["status"] == "unresolvable":
            consultations.append({
                "kind": "unresolvable_closure",
                "question_id": qid,
                "description": "closed unresolvable (durable blocker + exhaustion); "
                               "answering can reopen it by overruling the closure",
                "default": "accept_close"})

    open_families = Counter(specs[q]["family"] for q in open_ids if q in specs)
    closed = Counter(a["status"] for a in assessments.values() if a["status"] != "open")

    complete = not open_ids and verify_yes and not reasons
    if complete:
        state = "complete"                        # complete beats budget on the same delivery
    elif budget_state is not None and _over_budget(budget_state):
        state = "incomplete_over_budget"
        reasons.append("budget_exhausted")
    else:
        state = "in_progress"
    return AgendaVerdict(
        state=state,
        open_by_family=tuple(sorted(open_families.items())),
        closed_counts=tuple(sorted(closed.items())),
        blocking=tuple(open_ids),
        reasons=tuple(reasons),
        consultation_requests=tuple(consultations))


def _over_budget(budget_state: dict) -> bool:
    amount = budget_state.get("amount")
    spent = budget_state.get("spent")
    return (isinstance(amount, (int, float)) and isinstance(spent, (int, float))
            and spent >= amount)
