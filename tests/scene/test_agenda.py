"""Agenda derivation, closure, snapshot/history, and the verdict (SCENE-4 PR 4.1, slice 5).

The exit criteria this file carries: three closure paths; stable identity with moving
assessments; reopening recorded outside the digest; replay-identical content-addressed
derivation regardless of historical parent; stale exhaustion proofs rejected on their named
comparand; verdict law (no beam directive, VerifyCompleteness must answer YES, missing
required question = derivation defect, complete beats budget, unresolvable closures never
block completion, the ruling-1 in_progress consequence for phenomenon-bearing campaigns).
"""
import dataclasses
import json

import pytest

from isar.scene.agenda import (
    AGENDA_STORE, AgendaVerdict, apply_exhaustion, build_and_store_agenda, derive_agenda,
    evaluate_agenda, read_transitions)
from isar.scene.questions import Blocker, QuestionAssessment, QuestionExhaustion
from isar.schema.question_policy import resolve_policy

from .test_predicates import P1, alt, cand, cluster, fp, obs, payload



def step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0), spacing=0.5,
         domain=(0.0, 10.0)):
    return {"schema": "isar.campaign-step.discover_and_map/2",
            "region": {"kind": "path", "q_start": list(q_start), "q_end": list(q_end),
                       "maximum_initial_spacing_rlu": spacing},
            "energy": {"intrinsic_omega_domain_meV": list(domain)},
            "catalogue_profile": "isar.catalogue.inelastic-default/1",
            "deliverables": [{"kind": "phenomenon_map"}],
            "include_unknown": True,
            "resolved_policy": resolve_policy()}


def confirmed_scene(posterior=0.95, omega=5.0):
    """One trusted arena at the declared ZC 2.0 with a confidently present mode."""
    return payload(
        observations=[obs("e:1:d", [cand("A", omegas=(omega,)), cand("B")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], posterior),
                                      alt([("e:1:d", "B")], 1.0 - posterior)])],
        sample_knowledge={"zone_period": 1.0, "dispersion_slope_max": 10.0})


def derive(scene, step_payload=None, **kw):
    return derive_agenda(scene, step_payload or step(), step_id="discover-1",
                         program_fingerprint="prog-1", **kw)


def by_family(revision, family):
    return [s for s in revision.payload["specs"] if s["family"] == family]


def assessment_of(revision, question_id):
    return next(a for a in revision.payload["assessments"]
                if a["question_id"] == question_id)


# ----------------------------------------------------------------------------
# Derivation shape
# ----------------------------------------------------------------------------
def test_the_agenda_opens_declared_anchor_questions_and_chains_on_confirmation():
    revision = derive(confirmed_scene())
    p = revision.payload
    families = {s["family"] for s in p["specs"]}
    assert {"DiscoverRegion", "ConfirmPresence", "ClassifyIdentity", "EstimateParameter",
            "VerifyCompleteness"} <= families
    # No unknown-model competitor in this scene: mismatch surveillance is INERT, not an
    # eternally open question (PR #31 finding C).
    assert "TestModelMismatch" not in families
    assert any(n == "mismatch_surveillance_inert"
               for n, _ in map(tuple, p["derivation_notes"]))
    # Anchored presence at the declared folds 2.0 and 3.0 (zone_period 1.0 in [2, 3]) —
    # ONE class-agnostic question per anchor (finding A), never one per catalogue class.
    anchored = [s for s in by_family(revision, "ConfirmPresence")
                if s["subject"]["anchor"] is not None]
    assert sorted(s["subject"]["anchor"] for s in anchored) == [2.0, 3.0]
    assert all(s["subject"]["catalogue_class"] is None for s in anchored)
    # The confirmed anchor chains: the omega0 estimate (open by ruling 1) and the ZC endpoint
    # question — which here honestly answers INCOMPATIBLE: the arena sits AT the fold and the
    # mode is at 5 meV, so under any declared slope the branch does not reach zero.
    estimates = by_family(revision, "EstimateParameter")
    assert {s["claim_kind"] for s in estimates} == {"parameter_within_tolerance",
                                                    "zc_linear_compatibility"}
    for spec in estimates:
        a = assessment_of(revision, spec["question_id"])
        if spec["claim_kind"] == "parameter_within_tolerance":
            assert a["status"] == "open"
        else:
            assert dict(a["answer"]) == {"zc_compatibility": "incompatible"}


def test_without_declared_zone_geometry_predetermined_openers_are_inert_not_guessed():
    scene = confirmed_scene()
    scene["sample_knowledge"] = {}
    revision = derive(scene)
    assert "predetermined_openers_inert" in [
        n for n, _ in map(tuple, revision.payload["derivation_notes"])]
    anchored = [s for s in by_family(revision, "ConfirmPresence")
                if s["subject"]["anchor"] is not None]
    assert anchored == []


def test_an_emergent_question_opens_on_the_upper_bound_not_the_retained_mass():
    """An unresolved cluster has upper bound 1.0: omitted mass may contain the phenomenon,
    so the question OPENS even at low retained mass — suppression needs the upper bound low."""
    scene = confirmed_scene()
    scene["sample_knowledge"] = {}                # no anchors: only emergent openers possible
    scene["clusters"][0]["alternatives"][0]["posterior"] = 0.1   # low retained presence
    scene["clusters"][0]["alternatives"][1]["posterior"] = 0.9
    scene["clusters"][0]["truncated"] = True
    scene["clusters"][0]["truncation_unbounded"] = True
    revision = derive(scene)
    emergent = [s for s in by_family(revision, "ConfirmPresence")
                if dict(s["policy_provenance"]).get("opened_by") == "emergent"]
    assert len(emergent) == 1
    assert dict(emergent[0]["policy_provenance"])["opener_authority"] == "unresolved"
    # The subject's ω band is the FROZEN ω tile the mode snapped to, never the whole domain.
    assert emergent[0]["subject"]["omega_band_meV"] == [5.0, 6.0]


def test_two_modes_in_one_q_tile_are_two_emergent_questions():
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(2.2, 8.4)), cand("B")])],
        footprints=[fp("e:1:d", 2.6)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.95),
                                      alt([("e:1:d", "B")], 0.05)])])
    revision = derive(scene)
    emergent = [s for s in by_family(revision, "ConfirmPresence")
                if dict(s["policy_provenance"]).get("opened_by") == "emergent"]
    assert sorted(s["subject"]["omega_band_meV"] for s in emergent) == [[2.0, 3.0],
                                                                       [8.0, 9.0]]


def test_the_omega_tile_quantum_is_identity_bearing():
    """Changing the frozen quantum re-derives different emergent subjects and IDs."""
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(2.2,)), cand("B")])],
        footprints=[fp("e:1:d", 2.6)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.95),
                                      alt([("e:1:d", "B")], 0.05)])])
    coarse = step()
    coarse["resolved_policy"] = json.loads(json.dumps(coarse["resolved_policy"]))
    coarse["resolved_policy"]["thresholds"]["emergent_omega_tile_meV"] = 2.0

    def emergent_ids(revision):
        return {s["question_id"]: s["subject"]["omega_band_meV"]
                for s in by_family(revision, "ConfirmPresence")
                if dict(s["policy_provenance"]).get("opened_by") == "emergent"}

    fine = emergent_ids(derive(scene))
    wide = emergent_ids(derive(scene, coarse))
    assert list(fine.values()) == [[2.0, 3.0]] and list(wide.values()) == [[2.0, 4.0]]
    assert set(fine) != set(wide)


def test_joint_cluster_evidence_opens_what_no_single_cluster_could():
    """Two independent clusters at 0.4 upper each: jointly 0.64 ≥ 0.5 opens the tile's
    question; per-cluster thresholding would have suppressed exactly this case."""
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.2,)), cand("B")]),
                      obs("e:2:d", [cand("A", omegas=(5.4,)), cand("B")])],
        footprints=[fp("e:1:d", 2.55), fp("e:2:d", 2.6)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.4),
                                      alt([("e:1:d", "B")], 0.6)], cluster_id="c0"),
                  cluster(["e:2:d"], [alt([("e:2:d", "A")], 0.4),
                                      alt([("e:2:d", "B")], 0.6)], cluster_id="c1")])
    revision = derive(scene)
    emergent = [s for s in by_family(revision, "ConfirmPresence")
                if dict(s["policy_provenance"]).get("opened_by") == "emergent"]
    assert len(emergent) == 1
    assert emergent[0]["subject"]["region_rlu"] == [2.5, 3.0]
    assert emergent[0]["subject"]["omega_band_meV"] == [5.0, 6.0]


def test_presence_is_class_agnostic_and_identity_comes_from_classification():
    """The two-class-one-mode regression (finding A): one generic mode used to answer every
    class-specific presence question YES at once. Now there is exactly ONE presence question
    per anchor, and the class is the classification's ANSWER."""
    scene = confirmed_scene()
    scene["observations"][0]["candidates"][0]["identity"] = "acoustic"
    revision = derive(scene)
    at_anchor = [s for s in by_family(revision, "ConfirmPresence")
                 if s["subject"]["anchor"] == 2.0]
    assert len(at_anchor) == 1
    classify = by_family(revision, "ClassifyIdentity")
    assert len(classify) == 1
    a = assessment_of(revision, classify[0]["question_id"])
    assert a["status"] == "answered" and dict(a["answer"]) == {"identity": "acoustic"}
    ids = [s["question_id"] for s in revision.payload["specs"]]
    assert len(ids) == len(set(ids))


def test_connect_track_chains_from_classified_answers():
    """Two anchors classified as the same class open ONE track whose class came from the
    scene's answers, with the classify questions as its dependencies."""
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,), identity="acoustic"),
                                    cand("B")]),
                      obs("e:2:d", [cand("A", omegas=(5.5,), identity="acoustic"),
                                    cand("B")])],
        footprints=[fp("e:1:d", 2.0), fp("e:2:d", 3.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.95),
                                      alt([("e:1:d", "B")], 0.05)], cluster_id="c0"),
                  cluster(["e:2:d"], [alt([("e:2:d", "A")], 0.95),
                                      alt([("e:2:d", "B")], 0.05)], cluster_id="c1")],
        sample_knowledge={"zone_period": 1.0, "dispersion_slope_max": 10.0})
    revision = derive(scene)
    tracks = by_family(revision, "ConnectTrack")
    assert len(tracks) == 1
    assert tracks[0]["subject"]["catalogue_class"] == "acoustic"
    classify_ids = {s["question_id"] for s in by_family(revision, "ClassifyIdentity")}
    assert set(tracks[0]["dependencies"]) <= classify_ids


def test_mismatch_opens_only_where_the_scene_can_answer_it():
    without = derive(confirmed_scene())
    assert by_family(without, "TestModelMismatch") == []

    with_unknown = confirmed_scene()
    with_unknown["observations"][0]["candidates"].append(
        cand("U", omegas=(5.1,), identity="unknown"))
    with_unknown["clusters"][0]["alternatives"] = [
        alt([("e:1:d", "A")], 0.9), alt([("e:1:d", "U")], 0.05),
        alt([("e:1:d", "B")], 0.05)]
    revision = derive(with_unknown)
    assert len(by_family(revision, "TestModelMismatch")) == 1

    opted_out = step()
    opted_out["include_unknown"] = False
    assert by_family(derive(with_unknown, opted_out), "TestModelMismatch") == []


def test_duplicate_question_ids_refuse_derivation_artifacts():
    from isar.scene.agenda import _assert_unique
    from isar.scene.questions import PredicateDeclaration, QuestionSpec, QuestionSubject
    spec = QuestionSpec(family="ConfirmPresence", claim_kind="structural_presence",
                        subject=QuestionSubject(kind="phenomenon", partition_id=P1),
                        predicate=PredicateDeclaration(kind="structural_presence", version=1,
                                                       parameters={"min_mass": 0.9}))
    with pytest.raises(ValueError, match="duplicate question_id"):
        _assert_unique([spec, spec])


def test_two_partitions_fan_out_and_never_pool():
    p2 = "b" * 64
    scene = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,)), cand("B")]),
                      obs("e:2:d", [cand("A", omegas=(5.0,)), cand("B")], sample="s2")],
        footprints=[fp("e:1:d", 2.0), fp("e:2:d", 2.0, partition=p2)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.95),
                                      alt([("e:1:d", "B")], 0.05)], cluster_id="c0"),
                  cluster(["e:2:d"], [alt([("e:2:d", "A")], 0.95),
                                      alt([("e:2:d", "B")], 0.05)], cluster_id="c1")],
        sample_knowledge={"zone_period": 1.0})
    revision = derive(scene)
    partitions = {s["subject"]["partition_id"] for s in revision.payload["specs"]}
    assert partitions == {P1, p2}
    verify = by_family(revision, "VerifyCompleteness")
    assert len(verify) == 2                       # one completeness question per partition


# ----------------------------------------------------------------------------
# Replay + snapshot purity
# ----------------------------------------------------------------------------
def test_derivation_is_replay_identical_and_parent_independent(tmp_path):
    scene = confirmed_scene()
    first = build_and_store_agenda(tmp_path, scene, step(), step_id="discover-1",
                                   program_fingerprint="prog-1")
    assert first.unchanged is False
    again = build_and_store_agenda(tmp_path, scene, step(), step_id="discover-1",
                                   program_fingerprint="prog-1")
    assert again.unchanged is True and again.digest == first.digest
    # The pure derivation never saw the stored parent: same digest with a populated store.
    assert derive(scene).digest == first.digest
    # An unchanged rebuild appends no transition — nothing happened.
    assert len(read_transitions(tmp_path)) == 1   # the initial opened-set only


def test_the_digest_covers_no_parent_and_no_transitions(tmp_path):
    scene = confirmed_scene()
    build_and_store_agenda(tmp_path, scene, step(), step_id="discover-1",
                           program_fingerprint="prog-1")
    pointer, stored = AGENDA_STORE.read_current(tmp_path)
    assert {"parent", "written_at", "transitions"}.isdisjoint(stored)
    assert pointer["digest"] == derive(scene).digest


def test_reopening_is_a_transition_outside_the_digest(tmp_path):
    confident = confirmed_scene(posterior=0.95)
    build_and_store_agenda(tmp_path, confident, step(), step_id="discover-1",
                           program_fingerprint="prog-1")
    presence = [s for s in by_family(derive(confident), "ConfirmPresence")
                if s["subject"]["anchor"] == 2.0][0]
    assert assessment_of(derive(confident), presence["question_id"])["status"] == "answered"

    # Confidence lost to genuine ambiguity (an unresolved truncation), not a confident NO —
    # a decided reversal would be answered->answered, which is not a reopening.
    shaken = confirmed_scene(posterior=0.95)
    shaken["clusters"][0]["truncated"] = True
    shaken["clusters"][0]["truncation_unbounded"] = True
    build_and_store_agenda(tmp_path, shaken, step(), step_id="discover-1",
                           program_fingerprint="prog-1")
    revision = derive(shaken)
    assert assessment_of(revision, presence["question_id"])["status"] == "open"
    reopened = [t for record in read_transitions(tmp_path)
                for t in record["transitions"] if t["relation"] == "reopened"]
    assert [t["question_id"] for t in reopened] == [presence["question_id"]]


def test_a_program_revision_dissolves_subjects_as_transitions_never_silently(tmp_path):
    scene = confirmed_scene()
    build_and_store_agenda(tmp_path, scene, step(), step_id="discover-1",
                           program_fingerprint="prog-1")
    revised = step(q_start=(5.0, 0.0, 0.0), q_end=(6.0, 0.0, 0.0))
    build_and_store_agenda(tmp_path, scene, revised, step_id="discover-1",
                           program_fingerprint="prog-2")
    dissolved = [t for record in read_transitions(tmp_path)
                 for t in record["transitions"] if t["relation"] == "dissolved"]
    assert dissolved and all(t["equivalence"] == "unproven" for t in dissolved)


# ----------------------------------------------------------------------------
# Closure paths
# ----------------------------------------------------------------------------
def open_with_durable(question_id="q1"):
    return {question_id: QuestionAssessment(
        question_id=question_id, status="open",
        blockers=(Blocker("bose_dead", "T=4K, anti-Stokes dead"),))}


def proof(question_id="q1", basis="b" * 64, scene="s" * 64, registry="r1",
          status="exhausted"):
    return QuestionExhaustion(
        question_id=question_id, basis_agenda_digest=basis, scene_digest=scene,
        generator_registry_fingerprint=registry,
        required_generator_ids=("presence_probe",),
        generator_status={"presence_probe": status})


def test_unresolvable_needs_a_durable_blocker_and_a_fresh_proof():
    closed, applied = apply_exhaustion(open_with_durable(), [proof()],
                                       basis_digest="b" * 64, scene_digest="s" * 64,
                                       generator_registry_fingerprint="r1")
    assert closed["q1"].status == "unresolvable" and len(applied) == 1

    # The same evidence against a transient or authority blocker cannot close.
    transient = {"q1": QuestionAssessment(question_id="q1", status="open",
                                          blockers=(Blocker("quarantined_scan"),))}
    still_open, applied = apply_exhaustion(transient, [proof()],
                                           basis_digest="b" * 64, scene_digest="s" * 64,
                                           generator_registry_fingerprint="r1")
    assert still_open["q1"].status == "open" and applied == ()
    assert dict(still_open["q1"].notes)["generator_defect"]


@pytest.mark.parametrize("stale_proof, comparand", [
    (proof(basis="0" * 64), "basis_agenda_digest"),
    (proof(scene="0" * 64), "scene_digest"),
    (proof(registry="r2"), "generator_registry_fingerprint"),
])
def test_a_stale_proof_is_rejected_naming_its_failing_comparand(stale_proof, comparand):
    result, applied = apply_exhaustion(open_with_durable(), [stale_proof],
                                       basis_digest="b" * 64, scene_digest="s" * 64,
                                       generator_registry_fingerprint="r1")
    assert result["q1"].status == "open" and applied == ()
    note = dict(result["q1"].notes)["exhaustion_proof_rejected_stale"]
    assert comparand in note


def test_incomplete_exhaustion_closes_nothing():
    result, applied = apply_exhaustion(open_with_durable(),
                                       [proof(status="still_proposing")],
                                       basis_digest="b" * 64, scene_digest="s" * 64,
                                       generator_registry_fingerprint="r1")
    assert result["q1"].status == "open" and applied == ()


def test_no_live_derivation_can_close_unresolvable_in_this_slice():
    """4.1's evaluators emit no durable blockers, and derive_agenda defaults to no evidence —
    the closure lane exists, wired shut until 4.2's producer."""
    revision = derive(confirmed_scene())
    assert all(a["status"] != "unresolvable" for a in revision.payload["assessments"])


def test_derive_agenda_rejects_a_stale_proof_end_to_end():
    scene = confirmed_scene()
    revision = derive(scene, exhaustion_evidence=(proof(basis="0" * 64),),
                      generator_registry_fingerprint="r1")
    # No assessment closed; the payload records no applied evidence.
    assert revision.payload["exhaustion_evidence"] == []


# ----------------------------------------------------------------------------
# Verdict law
# ----------------------------------------------------------------------------
def agenda_doc(assessments, specs):
    return {"specs": specs, "assessments": assessments}


def synthetic_agenda(*, verify_answer=True, extra_status="answered"):
    verify_spec = {"question_id": "v1", "family": "VerifyCompleteness"}
    other_spec = {"question_id": "q1", "family": "ConfirmPresence"}
    verify = {"question_id": "v1", "status": "answered",
              "answer": [["complete", verify_answer]], "notes": []}
    other = {"question_id": "q1", "status": extra_status,
             "answer": [["present", True]] if extra_status == "answered" else [],
             "notes": []}
    return agenda_doc([verify, other], [verify_spec, other_spec])


def test_the_verdict_has_no_beam_directive():
    fields = {f.name for f in dataclasses.fields(AgendaVerdict)}
    assert fields == {"state", "open_by_family", "closed_counts", "blocking", "reasons",
                      "consultation_requests"}
    assert not any("beam" in f or "idle" in f or "stop" in f for f in fields)


def test_completion_requires_verify_completeness_to_answer_yes():
    assert evaluate_agenda(synthetic_agenda(verify_answer=True)).state == "complete"
    verdict = evaluate_agenda(synthetic_agenda(verify_answer=False))
    assert verdict.state == "in_progress"


def test_a_missing_verify_completeness_question_is_a_derivation_defect():
    doc = synthetic_agenda()
    doc["specs"] = [s for s in doc["specs"] if s["family"] != "VerifyCompleteness"]
    doc["assessments"] = [a for a in doc["assessments"] if a["question_id"] != "v1"]
    verdict = evaluate_agenda(doc)
    assert verdict.state == "in_progress"
    assert "derivation_defect_missing_verify_completeness" in verdict.reasons


def test_unresolvable_closures_never_block_completion_and_raise_a_consultation():
    doc = synthetic_agenda(extra_status="unresolvable")
    verdict = evaluate_agenda(doc)
    assert verdict.state == "complete"
    assert verdict.consultation_requests[0]["kind"] == "unresolvable_closure"
    assert verdict.consultation_requests[0]["default"] == "accept_close"
    assert "prompt_ref" not in verdict.consultation_requests[0]


def test_complete_beats_budget_and_budget_exhaustion_is_honest():
    over = {"kind": "hours", "amount": 4.0, "spent": 5.0}
    assert evaluate_agenda(synthetic_agenda(), budget_state=over).state == "complete"
    verdict = evaluate_agenda(synthetic_agenda(extra_status="open"), budget_state=over)
    assert verdict.state == "incomplete_over_budget"
    assert "budget_exhausted" in verdict.reasons
    under = {"kind": "hours", "amount": 4.0, "spent": 1.0}
    assert evaluate_agenda(synthetic_agenda(extra_status="open"),
                           budget_state=under).state == "in_progress"


def test_a_confirmed_phenomenon_yields_in_progress_with_the_open_estimate_surfaced():
    """The stated ruling-1 consequence: EstimateParameter never closes in v1, so a
    phenomenon-bearing campaign cannot reach complete until the interval re-baseline."""
    revision = derive(confirmed_scene())
    verdict = evaluate_agenda(revision.payload)
    assert verdict.state == "in_progress"
    open_families = dict(verdict.open_by_family)
    assert open_families.get("EstimateParameter", 0) >= 1
    estimate_ids = {s["question_id"] for s in by_family(revision, "EstimateParameter")}
    assert estimate_ids & set(verdict.blocking)


def test_a_fully_surveyed_blank_campaign_reaches_complete():
    """The reachability property PR #31 centres on: with mismatch conditional and presence
    class-agnostic, a searched-and-empty campaign closes every question it opens (presence
    answers honest NOs, nothing chains, nothing is eternally open) and the verdict is
    COMPLETE — completion is reachable whenever the deliberate EstimateParameter openness is
    not in play."""
    def empty_arena(ref, coord, cid):
        return (obs(ref, [cand("A", omegas=(5.0,)), cand("B")]),
                fp(ref, coord),
                cluster([ref], [alt([(ref, "A")], 0.02), alt([(ref, "B")], 0.98)],
                        cluster_id=cid))
    rows = [empty_arena("e:1:d", 2.0, "c0"), empty_arena("e:2:d", 2.4, "c1"),
            empty_arena("e:3:d", 2.8, "c2"), empty_arena("e:4:d", 3.0, "c3")]
    scene = payload(observations=[r[0] for r in rows], footprints=[r[1] for r in rows],
                    clusters=[r[2] for r in rows],
                    sample_knowledge={"zone_period": 1.0})
    revision = derive(scene)
    statuses = {a["status"] for a in revision.payload["assessments"]}
    assert statuses == {"answered"}
    verdict = evaluate_agenda(revision.payload)
    assert verdict.state == "complete"
    assert verdict.blocking == ()


def test_footprint_quantified_nulls_feed_completeness_through_closed_questions():
    """A searched-and-empty scene closes its presence questions as honest NOs, which is
    exactly what lets VerifyCompleteness account them — nulls are evidence, not gaps."""
    empty = payload(
        observations=[obs("e:1:d", [cand("A", omegas=(5.0,)), cand("B")])],
        footprints=[fp("e:1:d", 2.0)],
        clusters=[cluster(["e:1:d"], [alt([("e:1:d", "A")], 0.02),
                                      alt([("e:1:d", "B")], 0.98)])],
        sample_knowledge={"zone_period": 1.0})
    revision = derive(empty)
    anchored = [s for s in by_family(revision, "ConfirmPresence")
                if s["subject"]["anchor"] == 2.0]
    a = assessment_of(revision, anchored[0]["question_id"])
    assert a["status"] == "answered" and dict(a["answer"]) == {"present": False}
    # No confirmation -> no chained estimate questions -> nothing the ruling keeps open
    # at this anchor; completeness accounting sees a closed NO, not a hole.
    assert not by_family(revision, "ClassifyIdentity")
