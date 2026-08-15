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

from isar.schema.generators import FAMILY_GENERATORS

from .model import canonical_bytes, digest_bytes, fingerprint
from .predicates import (
    EVALUATORS, cluster_authority, combine_existential, evaluate_predicate,
    identity_hypothesis_classes, mass_interval)
from .questions import (
    Blocker, PredicateDeclaration, QuestionAssessment, QuestionSpec, QuestionSubject,
    match_subject, scene_partitions, snap_tile, validate_dependency_dag)
from .store import RevisionStore, StoreResult, UNSET

AGENDA_SCHEMA = "isar.agenda-revision/1"
AGENDA_STORE = RevisionStore("agenda", "isar.agenda-pointer/1", "agenda")
TRANSITIONS_NAME = "transitions.jsonl"

#: Bumping either re-derives every agenda (new digests) without minting new question IDs.
OPENER_VERSION = 4
MATCHER_VERSION = 1

#: Declared generator references. The vocabulary itself lives in
#: :mod:`isar.schema.generators` (PR 4.2b B1) — agenda derivation and the drive controller
#: need the exact same words and neither may import the other, so the declaration sits below
#: both. Re-exported under the historical name; the values are identical to the ones this
#: module declared through C1 (digest-neutral relocation — GENERATOR_REFS is serialized per
#: spec, not manifest-hashed, so equal values mean equal bytes).
GENERATOR_REFS = FAMILY_GENERATORS


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
    """The path region as a scalar interval along its one varying reciprocal axis.

    A path varying in MORE than one component is REFUSED loudly (PR #31 finding G): the
    scene's footprints carry only a single-axis ``axis_coordinate``, so a diagonal path would
    match no footprint and its DiscoverRegion question could never complete — a campaign
    silently impossible to finish. The general path-projection representation is 4.2+ work,
    named here rather than faked.
    """
    region = step_payload["region"]
    start, end = region["q_start"], region["q_end"]
    deltas = [abs(b - a) for a, b in zip(start, end)]
    axis = deltas.index(max(deltas))
    off_axis = [component for i, component in enumerate(deltas)
                if i != axis and component > 1e-9]
    if off_axis:
        raise ValueError(
            f"discovery path {tuple(start)} -> {tuple(end)} varies in more than one "
            "reciprocal component; the question layer's scalar path projection cannot "
            "represent it (general path projection is a SCENE-4.2+ representation) — "
            "refusing rather than scalarizing to the dominant axis")
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


def _weakest_lane(lanes) -> str:
    for lane in ("unresolved", "bounded", "exact"):
        if lane in lanes:
            return lane
    return "exact"


def _assert_unique(specs) -> None:
    """Refuse duplicate question IDs — two specs claiming one semantic question would make
    dependency, evaluation, and replay semantics depend on dict-overwrite order (PR #31
    review, finding B). Checked before DAG validation, before evaluation, and again at
    payload assembly."""
    seen: dict[str, str] = {}
    for spec in specs:
        label = f"{spec.family}/{spec.claim_kind}"
        if spec.question_id in seen:
            raise ValueError(
                f"duplicate question_id {spec.question_id} ({seen[spec.question_id]} vs "
                f"{label}) — two specs claim one semantic question")
        seen[spec.question_id] = label


def _emergent_tiles(clusters, coord_of, candidates_of, *, spacing: float,
                    omega_quantum: float, domain, covered_q, threshold: float) -> dict:
    """``(q tile, ω tile) -> (combined interval, weakest lane)`` for tiles whose JOINT
    cross-cluster upper bound reaches the opener threshold.

    Both quanta are frozen program policy (the identity contract): a mode's evidence lands in
    the ω tile its |ω| snaps to, so two modes at 2 and 8 meV in one q tile are two questions,
    and changing ``emergent_omega_tile_meV`` re-derives different subjects. Evidence for one
    tile COMBINES across independent clusters before thresholding (two 0.4 uppers jointly
    open a 0.64 existential proposition) — per-cluster thresholding would suppress exactly
    the joint case. An unresolved cluster contributes the vacuous ``(0, 1)`` on the tiles its
    retained alternatives place modes in — fail-open where there is a concrete ω hypothesis;
    the everywhere-else omitted mass is DiscoverRegion's coverage question, not an emergent
    presence question.
    """
    evidence: dict[tuple, list] = {}
    for cluster in clusters:
        lane, log_q = cluster_authority(cluster)
        masses: dict[tuple, float] = {}
        for alternative in cluster.get("alternatives") or ():
            posterior = alternative.get("posterior") or 0.0
            tiles_hit = set()
            for scan_ref, label in alternative.get("assignment") or ():
                coord = coord_of.get(scan_ref)
                if coord is None:
                    continue
                q_tile = snap_tile(coord, spacing)
                if q_tile in covered_q:
                    continue
                row = candidates_of.get(scan_ref, {}).get(label)
                if row is None:
                    continue
                omegas = row.get("omegas") or []
                for component in row.get("components") or ():
                    if component.get("kind") != "mode":
                        continue
                    slot = component.get("slot")
                    omega = (omegas[slot]
                             if isinstance(slot, int) and 0 <= slot < len(omegas) else None)
                    if omega is None or not domain[0] <= abs(omega) <= domain[1]:
                        continue
                    tiles_hit.add((q_tile, snap_tile(abs(omega), omega_quantum)))
            for tile in tiles_hit:
                masses[tile] = masses.get(tile, 0.0) + posterior
        for tile, mass in masses.items():
            interval = (0.0, 1.0) if lane == "unresolved" else mass_interval(mass, log_q)
            evidence.setdefault(tile, []).append((interval, lane))
    out = {}
    for tile, contributions in evidence.items():
        combined = combine_existential([interval for interval, _ in contributions])
        if combined[1] >= threshold:
            out[tile] = (combined, _weakest_lane([lane for _, lane in contributions]))
    return out


def _spec(family, kind, subject, params, *, priority, deps=(), provenance=()):
    return QuestionSpec(
        family=family, claim_kind=kind, subject=subject,
        predicate=PredicateDeclaration(kind=kind, version=1, parameters=params),
        priority=priority, dependencies=deps, generator_ids=GENERATOR_REFS[family],
        policy_provenance=provenance)


def derive_agenda(scene_payload: dict, step_payload: dict, *, step_id: str,
                  program_fingerprint: str, exhaustion_evidence: tuple = (),
                  generator_registry_fingerprint: str | None = None) -> AgendaRevision:
    """Derive the agenda for one frozen discover step against one scene revision. Pure.

    Presence is CLASS-AGNOSTIC (PR #31 finding A, owner's door 2): `structural_presence`
    deliberately ignores identity, so per-class copies at one anchor were one question
    wearing N labels — one generic mode answered every class YES simultaneously. One
    ConfirmPresence opens per anchor/tile; ClassifyIdentity chains per confirmed tile and its
    ANSWER carries the class; ConnectTrack chains per classify-answered class. The catalogue
    class vocabulary re-enters in 4.2, when the opening book gives classes genuinely distinct
    declared findability. ``exhaustion_evidence`` are :class:`QuestionExhaustion` proofs;
    anything stale against this derivation is rejected, never applied.
    """
    policy = step_payload["resolved_policy"]
    thresholds = policy["thresholds"]
    priorities = policy["priorities"]
    region = _region_axis_interval(step_payload)
    spacing = float(step_payload["region"]["maximum_initial_spacing_rlu"])
    domain = tuple(step_payload["energy"]["intrinsic_omega_domain_meV"])
    include_unknown = step_payload["include_unknown"] is True
    scene_digest = fingerprint(scene_payload)
    endpoints = "({})->({})".format(
        ",".join(f"{c:.9g}" for c in step_payload["region"]["q_start"]),
        ",".join(f"{c:.9g}" for c in step_payload["region"]["q_end"]))
    # The FULL endpoints join the frame (finding G): two parallel paths share a dominant-axis
    # interval but are different physics, so they must be different subjects.
    frame = (("catalogue_profile", step_payload["catalogue_profile"]),
             ("path_endpoints", endpoints),
             ("program_region", f"{region[0]}..{region[1]}"),
             ("defaults_profile",
              f"{policy['profile']['id']}/{policy['profile']['version']}"))

    partitions = scene_partitions(scene_payload) or (None,)
    candidates_of = {obs["scan_ref"]: {c["label"]: c for c in obs.get("candidates") or ()}
                    for obs in scene_payload.get("observations") or ()}
    footprints = scene_payload.get("footprints") or ()
    clusters = scene_payload.get("clusters") or ()

    # Declared zone geometry belongs to ONE lattice frame (finding H, mirroring the 4.0
    # epoch-scoped knowledge factors): anchors open only in partitions whose members'
    # lattice epoch matches the declared knowledge's — an incompatible or unverifiable frame
    # stays inert rather than inheriting current-frame folds.
    declared_anchors = _zc_anchors(scene_payload, region)
    declared_epoch = (scene_payload.get("sample_knowledge") or {}).get("lattice_epoch")
    partition_of_ref = {fp["scan_ref"]: fp.get("partition_id") for fp in footprints}
    epoch_of_pid: dict = {}
    for observation in scene_payload.get("observations") or ():
        pid_of = partition_of_ref.get(observation["scan_ref"])
        if pid_of is not None:
            epoch_of_pid.setdefault(pid_of, observation.get("lattice_epoch"))

    specs: list[QuestionSpec] = []
    notes: list[tuple] = []
    if not declared_anchors:
        notes.append(("predetermined_openers_inert",
                      "no zone_period declared; anchored ConfirmPresence openers cannot "
                      "be placed from declared knowledge"))
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

        frame_compatible = pid is None or epoch_of_pid.get(pid, "missing") == declared_epoch
        anchors = declared_anchors if frame_compatible else ()
        if declared_anchors and not frame_compatible:
            notes.append(("anchored_openers_inert",
                          f"partition {pid}: lattice epoch "
                          f"{epoch_of_pid.get(pid, 'unknown')!r} does not match the declared "
                          f"knowledge frame {declared_epoch!r}"))
        for anchor in anchors:
            specs.append(_spec(
                "ConfirmPresence", "structural_presence",
                QuestionSubject(kind="phenomenon", region_rlu=snap_tile(anchor, spacing),
                                omega_band_meV=domain, anchor=anchor, frame=frame, **base),
                {"min_mass": thresholds["confirm_presence_min_mass"]},
                priority=priorities["ConfirmPresence"],
                deps=(discover.question_id,),
                provenance=(("opened_by", "declared_zc_anchor"),)))

        if pid is not None:
            # Emergent openers per (q tile, ω tile), joint evidence across clusters.
            covered_q = {spec.subject.region_rlu for spec in specs
                         if spec.family == "ConfirmPresence"
                         and spec.subject.partition_id == pid}
            coord_of = {fp["scan_ref"]: fp["axis_coordinate"] for fp in footprints
                        if fp.get("partition_id") == pid
                        and fp.get("axis_coordinate") is not None}
            in_pid = [c for c in clusters
                      if any(r in coord_of for r in c.get("scan_refs") or ())]
            emergent = _emergent_tiles(
                in_pid, coord_of, candidates_of, spacing=spacing,
                omega_quantum=thresholds["emergent_omega_tile_meV"], domain=domain,
                covered_q=covered_q, threshold=thresholds["emergent_open_min_mass"])
            for q_tile, omega_tile in sorted(emergent):
                combined, lane = emergent[(q_tile, omega_tile)]
                specs.append(_spec(
                    "ConfirmPresence", "structural_presence",
                    QuestionSubject(kind="phenomenon", partition_id=pid, region_rlu=q_tile,
                                    omega_band_meV=omega_tile, frame=frame),
                    {"min_mass": thresholds["confirm_presence_min_mass"]},
                    priority=priorities["ConfirmPresence"],
                    deps=(discover.question_id,),
                    provenance=(("opened_by", "emergent"),
                                ("opener_authority", lane),
                                ("opener_interval",
                                 f"{combined[0]:.9g}..{combined[1]:.9g}"))))

            # TestModelMismatch is CONDITIONAL (PR #31 finding C): its evaluator can only
            # answer from an explicit unknown-model competitor, and its absence-blocker is
            # model-authority — non-durable, unclosable. Opening it unconditionally made
            # ordinary campaigns impossible to complete. It opens only where the scene can
            # actually represent the answer, gated by the frozen include_unknown.
            # Judged over every arena IN THE PARTITION, coordinate or not — an unknown
            # competitor on an unplaceable arena is still representable evidence.
            pid_refs = {fp["scan_ref"] for fp in footprints
                        if fp.get("partition_id") == pid}
            has_unknown = any(
                row.get("identity") == "unknown"
                for ref in pid_refs for row in candidates_of.get(ref, {}).values())
            if include_unknown and has_unknown:
                specs.append(_spec(
                    "TestModelMismatch", "mismatch_assessed",
                    QuestionSubject(kind="model", partition_id=pid, region_rlu=region,
                                    omega_band_meV=domain, frame=frame),
                    {"min_mass": thresholds["mismatch_min_mass"]},
                    priority=priorities["TestModelMismatch"],
                    deps=(discover.question_id,)))
            else:
                notes.append(("mismatch_surveillance_inert",
                              f"partition {pid}: "
                              + ("include_unknown is false"
                                 if not include_unknown
                                 else "no explicit unknown-model competitor in the scene")))
        else:
            notes.append(("mismatch_surveillance_inert",
                          "unmeasured workspace: no competitor evidence can exist yet"))

    # First evaluation pass: everything opened so far, in dependency order.
    _assert_unique(specs)
    validate_dependency_dag(specs)
    assessments = _evaluate_in_order(specs, scene_payload)

    # Chained openers per confirmed phenomenon (regime 1): ClassifyIdentity and
    # EstimateParameter auto-open; anchored subjects additionally ask the endpoint question.
    # One presence per tile ⇒ one child set per tile (finding B, structurally).
    chained: list[QuestionSpec] = []
    for spec in [s for s in specs if s.family == "ConfirmPresence"]:
        assessment = assessments.get(spec.question_id)
        if assessment is None or assessment.status != "answered" \
                or dict(assessment.answer).get("present") is not True:
            continue
        subject = spec.subject
        # Representability gate (owner ruling, SCENE-4.2a A6): the production catalogue
        # declares no identity-conditioned priors, so every candidate carries identity=None
        # and ClassifyIdentity would open and never close. Open it only where the scene can
        # actually answer — SAME scoping and SAME identity-set rule the evaluator itself uses
        # (isar.scene.predicates.identity_hypothesis_classes), so a tile whose OWN matched
        # rows carry no identity never opens even when a sibling tile in the same partition
        # does (a partition-wide check would pass here and then open forever). Otherwise the
        # phenomenon still maps as real-but-unclassified: no question opens, and a note
        # records why, naming the same blocker word the evaluator uses for the symmetric
        # in-question case.
        classify_subject = subject.__class__(
            kind="phenomenon", partition_id=subject.partition_id,
            region_rlu=subject.region_rlu, omega_band_meV=subject.omega_band_meV,
            frame=subject.frame)
        if identity_hypothesis_classes(classify_subject, scene_payload):
            chained.append(_spec(
                "ClassifyIdentity", "identity_resolved", classify_subject,
                {"min_mass": thresholds["classify_identity_min_mass"]},
                priority=priorities["ClassifyIdentity"], deps=(spec.question_id,)))
        else:
            examined_refs = match_subject(classify_subject, scene_payload).scan_refs
            examined_candidates = sum(len(candidates_of.get(ref, {}))
                                      for ref in examined_refs)
            notes.append(("classify_identity_inert",
                          f"partition {subject.partition_id} tile {subject.region_rlu} "
                          f"(confirmed by {spec.question_id}): identity_hypotheses_unavailable "
                          f"— examined {len(examined_refs)} scan ref(s) / "
                          f"{examined_candidates} candidate(s), none declares an explicit "
                          "identity"))
        chained.append(_spec(
            "EstimateParameter", "parameter_within_tolerance", subject.__class__(
                kind="parameter", partition_id=subject.partition_id,
                region_rlu=subject.region_rlu, omega_band_meV=subject.omega_band_meV,
                parameter="omega0", frame=subject.frame),
            {"tolerance_meV": thresholds["parameter_default_tolerance_meV"],
             "statistical_floor_meV": thresholds["parameter_statistical_floor_meV"],
             "parameter_precision_min_mass": thresholds["parameter_precision_min_mass"]},
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
    if chained:
        specs.extend(chained)
        _assert_unique(specs)
        validate_dependency_dag(specs)
        assessments.update(_evaluate_in_order(chained, scene_payload,
                                              prior=dict(assessments)))

    # Third pass: ConnectTrack per classify-ANSWERED class — the class comes from the
    # identity ANSWER, never from an opener label (finding A: "per class" stays honest
    # because the scene, not the vocabulary, said what is where).
    classified: dict[tuple, list] = {}
    for spec in [s for s in specs if s.family == "ClassifyIdentity"]:
        assessment = assessments.get(spec.question_id)
        if assessment is None or assessment.status != "answered":
            continue
        identity = dict(assessment.answer).get("identity")
        if identity and identity != "unknown":
            classified.setdefault((spec.subject.partition_id, identity), []).append(
                (spec.subject.region_rlu, spec.question_id))
    tracks: list[QuestionSpec] = []
    for (pid, identity), tiles in sorted(classified.items(),
                                         key=lambda kv: (str(kv[0][0]), kv[0][1])):
        if len(tiles) < 2:
            continue
        tiles.sort()
        tracks.append(_spec(
            "ConnectTrack", "track_connected", QuestionSubject(
                kind="track", partition_id=pid, catalogue_class=identity,
                endpoint_a_rlu=tiles[0][0], endpoint_b_rlu=tiles[-1][0], frame=frame),
            {"min_mass": thresholds["connect_track_min_mass"]},
            priority=priorities["ConnectTrack"],
            deps=tuple(qid for _, qid in tiles)))

    # Class-agnostic connectivity (P0-B, retriggered — owner ruling 2026-08-14, H19): the
    # track exists wherever ANY confirmed tile in the partition lacks an answered
    # classification, because an unclassified thing's connectivity to its classified
    # neighbours is exactly as open as to another unclassified one. It spans ALL confirmed
    # tiles (classified ones included) and is suppressed only in a FULLY-classified
    # partition, whose connectivity belongs to the per-class tracks above — never duplicated.
    # This supersedes H18(f)'s "no per-class track derived" trigger and resolves its named
    # residual (a derived per-class track no longer hides out-of-span unclassified tiles).
    # Seen consequence, recorded in H19: a fully-classified MULTI-class partition (e.g. two
    # tiles in two classes — no per-class track possible) carries no class-None track either;
    # cross-class connectivity is deferred physics, not an unclassified lane.
    # Reuses the same confirmed-presence collection the chaining pass above walks.
    confirmed: dict = {}
    for spec in [s for s in specs if s.family == "ConfirmPresence"]:
        assessment = assessments.get(spec.question_id)
        if assessment is None or assessment.status != "answered" \
                or dict(assessment.answer).get("present") is not True:
            continue
        confirmed.setdefault(spec.subject.partition_id, []).append(
            (spec.subject.region_rlu, spec.question_id))
    # A tile is classified iff a ClassifyIdentity question on the SAME (partition, tile)
    # answered with a real identity — the join key is (partition_id, region_rlu), which the
    # chaining pass copies verbatim from the presence subject onto the classify subject.
    classified_tiles = {(pid, region) for (pid, _identity), tiles in classified.items()
                        for (region, _qid) in tiles}
    for pid, tiles in sorted(confirmed.items(), key=lambda kv: str(kv[0])):
        if len(tiles) < 2 or all((pid, region) in classified_tiles
                                 for region, _qid in tiles):
            continue
        tiles.sort()
        tracks.append(_spec(
            "ConnectTrack", "track_connected", QuestionSubject(
                kind="track", partition_id=pid, catalogue_class=None,
                endpoint_a_rlu=tiles[0][0], endpoint_b_rlu=tiles[-1][0], frame=frame),
            {"min_mass": thresholds["connect_track_min_mass"]},
            priority=priorities["ConnectTrack"],
            deps=tuple(qid for _, qid in tiles),
            provenance=(("opened_by", "presence_without_classification"),)))
    if tracks:
        specs.extend(tracks)
        _assert_unique(specs)
        validate_dependency_dag(specs)
        assessments.update(_evaluate_in_order(tracks, scene_payload,
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
    _assert_unique(specs)
    validate_dependency_dag(specs)

    manifest = {"opener_version": OPENER_VERSION, "matcher_version": MATCHER_VERSION,
                "evaluator_versions": {kind: version
                                       for kind, (version, _) in sorted(EVALUATORS.items())},
                "generator_registry_fingerprint": generator_registry_fingerprint}

    def payload_of(assessment_map, *, basis_digest=None, evidence=()):
        return {
            "schema": AGENDA_SCHEMA,
            "program_fingerprint": program_fingerprint,
            "step_id": step_id,
            "scene_digest": scene_digest,
            # The COMPLETE frozen policy block, not only its fingerprint (finding I): a
            # stored agenda must explain who chose every effective value in its predicates'
            # parameters without rejoining the program document.
            "resolved_policy": json.loads(json.dumps(policy)),
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
        assessments, exhaustion_evidence, specs_by_id={s.question_id: s for s in specs},
        basis_digest=basis_digest, scene_digest=scene_digest,
        generator_registry_fingerprint=generator_registry_fingerprint)
    return AgendaRevision.of(payload_of(assessments, basis_digest=basis_digest,
                                        evidence=applied))


def apply_exhaustion(assessments: dict, exhaustion_evidence, *, specs_by_id: dict,
                     basis_digest: str, scene_digest: str,
                     generator_registry_fingerprint: str | None) -> tuple[dict, tuple]:
    """The pure closure transition: ``(assessments, applied proofs)`` after evidence.

    Public because it IS the contract 4.2's producer closes against: a question becomes
    ``unresolvable`` only when its open assessment carries a typed DURABLE blocker and the
    proof is fresh on every comparand — INCLUDING the question's own frozen generator set
    (PR #31 finding E: a proof may not choose its own requirements; its
    ``required_generator_ids`` must equal ``spec.generator_ids`` exactly, or exhausting one
    invented generator would close a question that requires several real ones). In 4.1 no
    evaluator emits a durable blocker yet, so no live derivation can close unresolvable —
    this function is exercised with synthetic assessments until the 4.2/4.3 producers exist.
    """
    out = dict(assessments)
    applied = []
    for proof in exhaustion_evidence:
        assessment = out.get(proof.question_id)
        if assessment is None or assessment.status != "open":
            continue
        spec = specs_by_id.get(proof.question_id)
        stale = None
        if proof.basis_agenda_digest != basis_digest:
            stale = ("basis_agenda_digest", proof.basis_agenda_digest, basis_digest)
        elif proof.scene_digest != scene_digest:
            stale = ("scene_digest", proof.scene_digest, scene_digest)
        elif proof.generator_registry_fingerprint != generator_registry_fingerprint:
            stale = ("generator_registry_fingerprint", proof.generator_registry_fingerprint,
                     generator_registry_fingerprint)
        elif spec is None:
            stale = ("question_id", proof.question_id, "no spec in this agenda")
        elif proof.required_generator_ids != spec.generator_ids:
            stale = ("required_generator_ids", proof.required_generator_ids,
                     spec.generator_ids)
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
        program_fingerprint=program_fingerprint,
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
    # Validate BEFORE reducing to dictionaries (PR #31 finding F): dict comprehensions
    # silently collapse duplicates, and a spec with no assessment would simply vanish from
    # completion accounting — a partial agenda must fail closed, never read complete.
    spec_rows = list(agenda_payload.get("specs") or ())
    assessment_rows = list(agenda_payload.get("assessments") or ())
    spec_ids = [s["question_id"] for s in spec_rows]
    assessment_ids = [a["question_id"] for a in assessment_rows]
    for label, ids in (("spec", spec_ids), ("assessment", assessment_ids)):
        duplicated = sorted(i for i, count in Counter(ids).items() if count > 1)
        if duplicated:
            raise ValueError(f"agenda payload carries duplicate {label} ids {duplicated} — "
                             "refusing to compute a verdict over an ambiguous agenda")
    orphans = sorted(set(assessment_ids) - set(spec_ids))
    if orphans:
        raise ValueError(f"agenda payload carries assessments for absent specs {orphans} — "
                         "refusing to compute a verdict over an inconsistent agenda")
    specs = {s["question_id"]: s for s in spec_rows}
    assessments = {a["question_id"]: a for a in assessment_rows}

    reasons: list[str] = []
    consultations: list[dict] = []
    unassessed = sorted(set(spec_ids) - set(assessment_ids))
    if unassessed:
        reasons.append("derivation_defect_unassessed_specs")
    open_ids = sorted(set(unassessed)
                      | {q for q, a in assessments.items() if a["status"] == "open"})
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
