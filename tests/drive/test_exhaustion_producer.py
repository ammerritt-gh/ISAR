"""The exhaustion producer (PR 4.2b C6 / B5): one test per matrix row, an integration closure
through the real ``isar.scene.agenda.apply_exhaustion`` (durable blocker + clean accounting
closes one question while a sibling with an eligible action stays open), stale-comparand
rejection by name through the real path, the generator_defect note through the real store path,
and ``QuestionBlockerEvidence``'s durable-only refusal.
"""
from __future__ import annotations

import pytest

from isar.drive.acquisition.inventory import ActionEligibility, ActionEvaluation, RejectionClass
from isar.schema.action_identity import MeasurementIntent
from isar.schema.generators import DURABLE_BLOCKER_NAMES, QuestionActionAssociation, QuestionBlockerEvidence
from isar.schema.question_policy import resolve_policy
from isar.scene.agenda import AGENDA_STORE, apply_exhaustion, build_and_store_agenda, derive_agenda
from isar.scene.questions import (
    BLOCKER_CLASS_BY_NAME, BLOCKER_DURABLE, Blocker, PredicateDeclaration, QuestionAssessment,
    QuestionSpec, QuestionSubject)

import isar.drive.steering.exhaustion as exh
from isar.drive.steering.generators import DISCOVERY_ACTION_CLASS, DiscoveryContext
from isar.drive.steering.readiness import (
    DEPENDENCY_BLOCKED, NO_GENERATORS, RAN, GeneratorRunOutcome, QuestionRunRecord, RunResult)
from isar.drive.steering.exhaustion import (
    BLOCKED_WITHOUT_EVIDENCE, BUDGET_ONLY, CONTROLLER_FAULT, DEFERRED, NO_GENERATORS_OUTCOME,
    NOT_EXHAUSTED, PROOF, RETRYABLE, produce_exhaustion)

REGISTRY_FP = "isar.generator-registry/1:test"
BASIS = "b" * 64
SCENE = "s" * 64


# ----------------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------------
def ctx(basis=BASIS, scene=SCENE):
    return DiscoveryContext(scene_payload={}, agenda_payload={}, step_payload={},
                            basis_agenda_digest=basis, scene_digest=scene)


def record(question_id, generator_ids, disposition, outcomes=(), family="ConfirmPresence"):
    return QuestionRunRecord(question_id, family, tuple(generator_ids), disposition,
                             tuple(outcomes))


def association(question_id, generator, priority=1, basis=BASIS):
    return QuestionActionAssociation(basis_agenda_digest=basis, question_id=question_id,
                                     generator=generator, family_priority=priority,
                                     dependency_depth=0, reason="test")


def evaluation(question_id, generator, *, eligibility=ActionEligibility.ELIGIBLE,
              rejection_class=None, hkl=(2.0, 0.0, 0.0)):
    # PR #33 A-P0-1: real discovery intents share ONE physical action_class; "which generator"
    # rides on the association, not on the intent. Two evaluations built with the same hkl now
    # genuinely collide in dedup, matching what test_an_evaluation_deduped_onto_an_eligible_
    # survivor_still_counts_not_exhausted's docstring already claimed.
    intent = MeasurementIntent(action_class=DISCOVERY_ACTION_CLASS, hkl=hkl, energy_lo=1.0,
                               energy_hi=8.0, window_family="discovery")
    return ActionEvaluation(intent=intent, generator=generator, eligibility=eligibility,
                            rejection_class=rejection_class,
                            associations=(association(question_id, generator),))


def evidence(question_id, blocker_name="instrument_geometry_infeasible", supporting=("presence_probe",),
            basis=BASIS, scene=SCENE, registry=REGISTRY_FP):
    return QuestionBlockerEvidence(
        basis_agenda_digest=basis, question_id=question_id, scene_digest=scene,
        generator_registry_fingerprint=registry, blocker_name=blocker_name,
        supporting_generator_ids=supporting)


def run(records, evaluations=(), *, evidence_list=(), registry=REGISTRY_FP, context=None):
    return produce_exhaustion(context or ctx(), RunResult(records=tuple(records),
                                                          evaluations=tuple(evaluations)),
                              generator_registry_fingerprint=registry,
                              blocker_evidence=tuple(evidence_list))


def one(result):
    assert len(result.outcomes) == 1
    return result.outcomes[0]


# ----------------------------------------------------------------------------
# Matrix rows
# ----------------------------------------------------------------------------
def test_an_eligible_surviving_evaluation_is_not_exhausted():
    r = record("q1", ("presence_probe",), RAN,
              [GeneratorRunOutcome("presence_probe", "proposals", generated=1)])
    outcome = one(run([r], [evaluation("q1", "presence_probe")]))
    assert outcome.outcome == NOT_EXHAUSTED and outcome.proof is None


def test_an_evaluation_deduped_onto_an_eligible_survivor_still_counts_not_exhausted():
    """Two generators mint the SAME scientific action; dedup unions both associations onto the
    survivor, so q2's association (folded onto q1's survivor) still marks q2 not-exhausted."""
    e1 = evaluation("q1", "opening_book_survey", hkl=(2.0, 0.0, 0.0))
    e2 = evaluation("q2", "exposure_fill", hkl=(2.0, 0.0, 0.0))
    r1 = record("q1", ("opening_book_survey",), RAN,
               [GeneratorRunOutcome("opening_book_survey", "proposals", generated=1)])
    r2 = record("q2", ("exposure_fill",), RAN,
               [GeneratorRunOutcome("exposure_fill", "proposals", generated=1)])
    result = run([r1, r2], [e1, e2])
    by_id = {o.question_id: o for o in result.outcomes}
    assert by_id["q1"].outcome == NOT_EXHAUSTED
    assert by_id["q2"].outcome == NOT_EXHAUSTED


def test_a_failed_required_generator_is_a_controller_fault_no_proof_ever():
    r = record("q1", ("presence_probe",), RAN,
              [GeneratorRunOutcome("presence_probe", "failed", error_code="RuntimeError: boom")])
    outcome = one(run([r]))
    assert outcome.outcome == CONTROLLER_FAULT and outcome.proof is None


def test_a_missing_required_generator_outcome_is_also_a_controller_fault():
    r = record("q1", ("presence_probe",), RAN, [])   # no outcome recorded for the required id
    outcome = one(run([r]))
    assert outcome.outcome == CONTROLLER_FAULT


def test_controller_fault_takes_priority_over_an_eligible_sibling_generator():
    r = record("q1", ("presence_probe", "identity_discriminator"), RAN,
              [GeneratorRunOutcome("presence_probe", "failed", error_code="boom"),
               GeneratorRunOutcome("identity_discriminator", "proposals", generated=1)])
    outcome = one(run([r], [evaluation("q1", "identity_discriminator")]))
    assert outcome.outcome == CONTROLLER_FAULT


def test_dependency_blocked_disposition_is_deferred():
    r = record("q1", ("presence_probe",), DEPENDENCY_BLOCKED)
    outcome = one(run([r]))
    assert outcome.outcome == DEFERRED and outcome.proof is None


def test_transient_rejection_only_is_retryable():
    r = record("q1", ("presence_probe",), RAN,
              [GeneratorRunOutcome("presence_probe", "proposals", generated=1)])
    rejected = evaluation("q1", "presence_probe", eligibility=ActionEligibility.REJECTED,
                          rejection_class=RejectionClass.TRANSIENT)
    outcome = one(run([r], [rejected]))
    assert outcome.outcome == RETRYABLE and outcome.proof is None


def test_budget_rejection_only_is_budget_only():
    r = record("q1", ("presence_probe",), RAN,
              [GeneratorRunOutcome("presence_probe", "proposals", generated=1)])
    rejected = evaluation("q1", "presence_probe", eligibility=ActionEligibility.REJECTED,
                          rejection_class=RejectionClass.BUDGET)
    outcome = one(run([r], [rejected]))
    assert outcome.outcome == BUDGET_ONLY and outcome.proof is None


@pytest.mark.parametrize("rejection_class", [RejectionClass.STRUCTURAL, RejectionClass.SATURATION])
def test_structural_or_saturation_rejection_with_matching_evidence_proves_exhaustion(
        rejection_class):
    r = record("q1", ("presence_probe",), RAN,
              [GeneratorRunOutcome("presence_probe", "proposals", generated=1)])
    rejected = evaluation("q1", "presence_probe", eligibility=ActionEligibility.REJECTED,
                          rejection_class=rejection_class)
    outcome = one(run([r], [rejected], evidence_list=[evidence("q1")]))
    assert outcome.outcome == PROOF
    assert outcome.proof.question_id == "q1"
    assert outcome.proof.required_generator_ids == ("presence_probe",)
    assert outcome.proof.exhausted()


def test_structural_rejection_without_matching_evidence_is_blocked_without_evidence():
    r = record("q1", ("presence_probe",), RAN,
              [GeneratorRunOutcome("presence_probe", "proposals", generated=1)])
    rejected = evaluation("q1", "presence_probe", eligibility=ActionEligibility.REJECTED,
                          rejection_class=RejectionClass.STRUCTURAL)
    # No evidence at all.
    outcome = one(run([r], [rejected]))
    assert outcome.outcome == BLOCKED_WITHOUT_EVIDENCE and outcome.proof is None
    # Evidence exists but for a DIFFERENT question -- still unmatched.
    outcome2 = one(run([r], [rejected], evidence_list=[evidence("other_question")]))
    assert outcome2.outcome == BLOCKED_WITHOUT_EVIDENCE
    # Evidence for this question but stale scene_digest -- still unmatched.
    stale = evidence("q1", scene="0" * 64)
    outcome3 = one(run([r], [rejected], evidence_list=[stale]))
    assert outcome3.outcome == BLOCKED_WITHOUT_EVIDENCE
    # Evidence fresh but supports a DIFFERENT generator than the question requires.
    unsupported = evidence("q1", supporting=("mismatch_probe",))
    outcome4 = one(run([r], [rejected], evidence_list=[unsupported]))
    assert outcome4.outcome == BLOCKED_WITHOUT_EVIDENCE


def test_clean_zero_across_every_required_generator_proves_exhaustion():
    r = record("q1", ("opening_book_survey",), RAN,
              [GeneratorRunOutcome("opening_book_survey", "clean_zero",
                                  clean_zero_reason="coverage_complete", exhaustion_clean=True)])
    outcome = one(run([r]))
    assert outcome.outcome == PROOF
    assert dict(outcome.proof.generator_status) == {"opening_book_survey": "exhausted"}
    assert outcome.proof.exhausted()


def test_a_partial_clean_zero_across_two_required_generators_does_not_prove():
    r = record("q1", ("gen_a", "gen_b"), RAN,
              [GeneratorRunOutcome("gen_a", "clean_zero", clean_zero_reason="done",
                                  exhaustion_clean=True),
               GeneratorRunOutcome("gen_b", "clean_zero", clean_zero_reason="done",
                                  exhaustion_clean=False)])
    outcome = one(run([r]))
    assert outcome.outcome == NOT_EXHAUSTED and outcome.proof is None


def test_inapplicable_outcome_never_proves_by_construction():
    """GeneratorOutcome structurally refuses an INAPPLICABLE with exhaustion_clean=True, so the
    producer can never see one -- this pins the CONSEQUENCE of that guarantee, not the guarantee
    itself (that lives in tests/drive/test_discovery_generators.py)."""
    r = record("q1", ("identity_discriminator",), RAN,
              [GeneratorRunOutcome("identity_discriminator", "inapplicable",
                                  inapplicable_reason="identity_hypotheses_unavailable",
                                  exhaustion_clean=False)])
    outcome = one(run([r]))
    assert outcome.outcome == NOT_EXHAUSTED and outcome.proof is None


def test_no_generators_disposition_can_never_exhaust():
    r = record("q1", (), NO_GENERATORS)
    outcome = one(run([r]))
    assert outcome.outcome == NO_GENERATORS_OUTCOME and outcome.proof is None


def test_determinism_under_reversed_record_order():
    r1 = record("q_b", ("presence_probe",), RAN,
               [GeneratorRunOutcome("presence_probe", "clean_zero", clean_zero_reason="x",
                                   exhaustion_clean=True)])
    r2 = record("q_a", ("presence_probe",), RAN,
               [GeneratorRunOutcome("presence_probe", "clean_zero", clean_zero_reason="x",
                                   exhaustion_clean=True)])
    forward = run([r1, r2])
    reverse = run([r2, r1])
    assert [o.question_id for o in forward.outcomes] == ["q_a", "q_b"]
    assert [o.question_id for o in reverse.outcomes] == ["q_a", "q_b"]
    assert forward.outcomes == reverse.outcomes


# ----------------------------------------------------------------------------
# QuestionBlockerEvidence: durable-only, DTO round trip, cross-layer pinning
# ----------------------------------------------------------------------------
def test_question_blocker_evidence_refuses_a_transient_name():
    with pytest.raises(ValueError, match="durable"):
        evidence("q1", blocker_name="quarantined_scan")


def test_question_blocker_evidence_refuses_a_model_authority_name():
    with pytest.raises(ValueError, match="durable"):
        evidence("q1", blocker_name="identity_hypotheses_unavailable")


def test_question_blocker_evidence_refuses_an_unknown_name():
    with pytest.raises(ValueError, match="durable"):
        evidence("q1", blocker_name="not_a_real_blocker")


def test_question_blocker_evidence_round_trips_and_sorts_supporting_ids():
    original = QuestionBlockerEvidence(
        basis_agenda_digest=BASIS, question_id="q1", scene_digest=SCENE,
        generator_registry_fingerprint=REGISTRY_FP, blocker_name="instrument_geometry_infeasible",
        supporting_generator_ids=("z_gen", "a_gen"), reason_codes=("r1", "r2"), detail="d")
    assert original.supporting_generator_ids == ("a_gen", "z_gen")
    round_tripped = QuestionBlockerEvidence.from_dict(original.to_dict())
    assert round_tripped == original


def test_durable_blocker_names_matches_the_scene_side_vocabulary():
    """Pinned by test, never assumed (schema may not import scene, so the set is duplicated) --
    exactly the posture this PR already uses for scene_digest/program_fingerprint equivalence."""
    scene_durable = {name for name, cls in BLOCKER_CLASS_BY_NAME.items() if cls == BLOCKER_DURABLE}
    assert DURABLE_BLOCKER_NAMES == scene_durable


# ----------------------------------------------------------------------------
# Integration: real apply_exhaustion closes one question, leaves a sibling open
# ----------------------------------------------------------------------------
def _presence_spec(question_id_subject_kwargs):
    return QuestionSpec(family="ConfirmPresence", claim_kind="structural_presence",
                        subject=QuestionSubject(kind="phenomenon", **question_id_subject_kwargs),
                        predicate=PredicateDeclaration(kind="structural_presence", version=1,
                                                       parameters={"min_mass": 0.9}),
                        generator_ids=("presence_probe",))


def test_a_synthetic_durable_blocker_closes_through_real_apply_exhaustion_sibling_stays_open():
    spec_a = _presence_spec({"partition_id": "a" * 64, "anchor": 2.0})
    spec_b = _presence_spec({"partition_id": "a" * 64, "anchor": 3.0})
    context = ctx()

    closing_record = record(spec_a.question_id, ("presence_probe",), RAN,
                           [GeneratorRunOutcome("presence_probe", "clean_zero",
                                               clean_zero_reason="none_open_never",
                                               exhaustion_clean=True)])
    open_record = record(spec_b.question_id, ("presence_probe",), RAN,
                        [GeneratorRunOutcome("presence_probe", "proposals", generated=1)])
    open_eval = evaluation(spec_b.question_id, "presence_probe")

    result = produce_exhaustion(
        context, RunResult(records=(closing_record, open_record), evaluations=(open_eval,)),
        generator_registry_fingerprint=REGISTRY_FP)
    proofs = result.proofs
    assert len(proofs) == 1 and proofs[0].question_id == spec_a.question_id

    assessments = {
        spec_a.question_id: QuestionAssessment(
            question_id=spec_a.question_id, status="open",
            blockers=(Blocker("instrument_geometry_infeasible", "T=4K, anti-Stokes dead"),)),
        spec_b.question_id: QuestionAssessment(question_id=spec_b.question_id, status="open"),
    }
    closed, applied = apply_exhaustion(
        assessments, proofs, specs_by_id={spec_a.question_id: spec_a, spec_b.question_id: spec_b},
        basis_digest=context.basis_agenda_digest, scene_digest=context.scene_digest,
        generator_registry_fingerprint=REGISTRY_FP)

    assert len(applied) == 1
    assert closed[spec_a.question_id].status == "unresolvable"
    assert closed[spec_b.question_id].status == "open"


@pytest.mark.parametrize("mutate, comparand", [
    (lambda p: p.__class__(**{**p.__dict__, "basis_agenda_digest": "0" * 64}),
     "basis_agenda_digest"),
    (lambda p: p.__class__(**{**p.__dict__, "scene_digest": "0" * 64}), "scene_digest"),
    (lambda p: p.__class__(**{**p.__dict__, "generator_registry_fingerprint": "other"}),
     "generator_registry_fingerprint"),
])
def test_each_stale_comparand_is_rejected_by_name_through_real_apply_exhaustion(mutate, comparand):
    spec_a = _presence_spec({"partition_id": "a" * 64, "anchor": 2.0})
    context = ctx()
    closing_record = record(spec_a.question_id, ("presence_probe",), RAN,
                           [GeneratorRunOutcome("presence_probe", "clean_zero",
                                               clean_zero_reason="x", exhaustion_clean=True)])
    proof = produce_exhaustion(
        context, RunResult(records=(closing_record,), evaluations=()),
        generator_registry_fingerprint=REGISTRY_FP).proofs[0]
    stale = mutate(proof)

    assessments = {spec_a.question_id: QuestionAssessment(
        question_id=spec_a.question_id, status="open",
        blockers=(Blocker("instrument_geometry_infeasible", ""),))}
    result, applied = apply_exhaustion(
        assessments, [stale], specs_by_id={spec_a.question_id: spec_a},
        basis_digest=context.basis_agenda_digest, scene_digest=context.scene_digest,
        generator_registry_fingerprint=REGISTRY_FP)
    assert applied == () and result[spec_a.question_id].status == "open"
    note = dict(result[spec_a.question_id].notes)["exhaustion_proof_rejected_stale"]
    assert comparand in note


def test_generator_ids_mismatch_is_rejected_by_name_through_real_apply_exhaustion():
    spec_a = _presence_spec({"partition_id": "a" * 64, "anchor": 2.0})
    context = ctx()
    # A proof built under a DIFFERENT required-generator record than the real spec declares.
    wrong_record = record(spec_a.question_id, ("not_the_specs_generator",), RAN,
                         [GeneratorRunOutcome("not_the_specs_generator", "clean_zero",
                                             clean_zero_reason="x", exhaustion_clean=True)])
    proof = produce_exhaustion(
        context, RunResult(records=(wrong_record,), evaluations=()),
        generator_registry_fingerprint=REGISTRY_FP).proofs[0]

    assessments = {spec_a.question_id: QuestionAssessment(
        question_id=spec_a.question_id, status="open",
        blockers=(Blocker("instrument_geometry_infeasible", ""),))}
    result, applied = apply_exhaustion(
        assessments, [proof], specs_by_id={spec_a.question_id: spec_a},
        basis_digest=context.basis_agenda_digest, scene_digest=context.scene_digest,
        generator_registry_fingerprint=REGISTRY_FP)
    assert applied == () and result[spec_a.question_id].status == "open"
    assert "required_generator_ids" in dict(result[spec_a.question_id].notes)[
        "exhaustion_proof_rejected_stale"]


def test_exhaustion_without_a_durable_blocker_leaves_the_generator_defect_note():
    spec_a = _presence_spec({"partition_id": "a" * 64, "anchor": 2.0})
    context = ctx()
    closing_record = record(spec_a.question_id, ("presence_probe",), RAN,
                           [GeneratorRunOutcome("presence_probe", "clean_zero",
                                               clean_zero_reason="x", exhaustion_clean=True)])
    proof = produce_exhaustion(
        context, RunResult(records=(closing_record,), evaluations=()),
        generator_registry_fingerprint=REGISTRY_FP).proofs[0]

    # No durable blocker on the assessment -- a real evaluator today never emits one either.
    assessments = {spec_a.question_id: QuestionAssessment(
        question_id=spec_a.question_id, status="open")}
    result, applied = apply_exhaustion(
        assessments, [proof], specs_by_id={spec_a.question_id: spec_a},
        basis_digest=context.basis_agenda_digest, scene_digest=context.scene_digest,
        generator_registry_fingerprint=REGISTRY_FP)
    assert applied == () and result[spec_a.question_id].status == "open"
    assert dict(result[spec_a.question_id].notes)["generator_defect"]


# ----------------------------------------------------------------------------
# Through the real store path (build_and_store_agenda): the SAME generator_defect note, because
# no 4.1/4.2 evaluator emits a durable blocker yet (isar.scene.agenda's own docstring) -- a proof
# minted for a genuinely open, live-derived question is honestly rejected as a defect rather than
# closing anything, which is the CORRECT behaviour here, not a gap.
# ----------------------------------------------------------------------------
def _step(q_start=(2.0, 0.0, 0.0), q_end=(3.0, 0.0, 0.0), spacing=0.5, domain=(0.0, 10.0)):
    return {"schema": "isar.campaign-step.discover_and_map/2",
            "region": {"kind": "path", "q_start": list(q_start), "q_end": list(q_end),
                      "maximum_initial_spacing_rlu": spacing},
            "energy": {"intrinsic_omega_domain_meV": list(domain)},
            "catalogue_profile": "isar.catalogue.inelastic-default/1",
            "deliverables": [{"kind": "phenomenon_map"}], "include_unknown": True,
            "resolved_policy": resolve_policy()}


def test_a_live_derived_proof_passes_through_the_real_store_without_closing_anything(tmp_path):
    scene = {"observations": [], "footprints": [], "clusters": [], "non_scoring": [],
            "sample_knowledge": {}, "condition_t_round": None}
    step_payload = _step()
    # Same generator_registry_fingerprint on both calls: it enters the derivation manifest that
    # basis_digest is computed over, so a mismatch here would make this test's own two calls
    # stale against each other for a reason that has nothing to do with what it is pinning.
    revision = derive_agenda(scene, step_payload, step_id="discover-1",
                             program_fingerprint="prog-1",
                             generator_registry_fingerprint="reg-fp-1")
    discover = next(s for s in revision.payload["specs"] if s["family"] == "DiscoverRegion")
    assert discover["subject"]["partition_id"] is None      # unmeasured workspace, cold start

    context = DiscoveryContext(scene_payload=scene, agenda_payload=revision.payload,
                               step_payload=step_payload,
                               basis_agenda_digest=revision.payload["basis_digest"],
                               scene_digest=revision.payload["scene_digest"])
    closing_record = record(discover["question_id"], tuple(discover["generator_ids"]), RAN,
                           [GeneratorRunOutcome(g, "clean_zero", clean_zero_reason="x",
                                               exhaustion_clean=True)
                            for g in discover["generator_ids"]],
                           family="DiscoverRegion")
    proof = produce_exhaustion(
        context, RunResult(records=(closing_record,), evaluations=()),
        generator_registry_fingerprint="reg-fp-1").proofs[0]

    build_and_store_agenda(
        tmp_path, scene, step_payload, step_id="discover-1", program_fingerprint="prog-1",
        exhaustion_evidence=(proof,), generator_registry_fingerprint="reg-fp-1")
    _pointer, stored_payload = AGENDA_STORE.read_current(tmp_path)
    closed_assessment = next(a for a in stored_payload["assessments"]
                             if a["question_id"] == discover["question_id"])
    assert closed_assessment["status"] == "open"
    assert dict(closed_assessment["notes"]).get("generator_defect")

def test_blocker_evidence_refuses_empty_supporting_generator_ids():
    """Review C6-1: an empty generator id could intersect a malformed required set and back
    a proof nothing actually supports -- refused at construction like every other malformed
    field."""
    fields = evidence("q:any").to_dict()
    fields["supporting_generator_ids"] = ["opening_book_survey", "  "]
    with pytest.raises(ValueError, match="non-empty generator ids"):
        QuestionBlockerEvidence.from_dict(fields)
