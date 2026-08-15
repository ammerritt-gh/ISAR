"""Readiness gating + per-question run accounting (PR 4.2b C5 / B4): a dependency-open question
never touches a generator; a ready question runs exactly its declared generators, in order, with
per-generator fault isolation; an empty declared set is ``no_generators``, never a fault.
"""
from __future__ import annotations

import pytest

from isar.schema.question_policy import resolve_policy
from isar.scene.agenda import derive_agenda
from isar.scene.model import fingerprint
from isar.scene.questions import PredicateDeclaration, QuestionAssessment, QuestionSpec, QuestionSubject

import isar.drive.steering.readiness as readiness_module
from isar.drive.steering.generators import DiscoveryContext, GeneratorOutcome
from isar.drive.steering.readiness import (
    DEPENDENCY_BLOCKED,
    NO_GENERATORS,
    RAN,
    READY,
    QuestionRunRecord,
    classify_readiness,
    run_ready_questions,
)

P1 = "a" * 64


# ----------------------------------------------------------------------------
# Builders
# ----------------------------------------------------------------------------
def make_spec(family, claim_kind, subject, params, *, priority=0, deps=(), generator_ids=()):
    return QuestionSpec(
        family=family, claim_kind=claim_kind, subject=subject,
        predicate=PredicateDeclaration(kind=claim_kind, version=1, parameters=params),
        priority=priority, dependencies=deps, generator_ids=generator_ids)


def open_assessment(question_id, **kw):
    return QuestionAssessment(question_id=question_id, status="open", **kw)


def answered_assessment(question_id, answer=(("complete", True),)):
    return QuestionAssessment(question_id=question_id, status="answered", authority="exact",
                              answer=answer)


def agenda_of(specs, assessments):
    return {"specs": [s.to_dict() for s in specs],
            "assessments": [a.to_dict() for a in assessments]}


def ctx_for(agenda_payload, scene=None, step_payload=None):
    scene = scene or {}
    return DiscoveryContext(scene_payload=scene, agenda_payload=agenda_payload,
                            step_payload=step_payload or {}, basis_agenda_digest="d" * 64,
                            scene_digest=fingerprint(scene))


def outcome_of(intents_and_associations=(), clean_zero=None, inapplicable=None,
              exhaustion_clean=False):
    if clean_zero is not None:
        return GeneratorOutcome.clean_zero(clean_zero, exhaustion_clean=exhaustion_clean)
    if inapplicable is not None:
        return GeneratorOutcome.inapplicable(inapplicable)
    return GeneratorOutcome.proposed(intents_and_associations)


# ----------------------------------------------------------------------------
# classify_readiness
# ----------------------------------------------------------------------------
def test_classify_readiness_partitions_open_questions():
    a = make_spec("DiscoverRegion", "coverage_complete", QuestionSubject(kind="region"), {})
    b = make_spec("ConfirmPresence", "structural_presence", QuestionSubject(kind="phenomenon"),
                 {}, deps=(a.question_id,))
    c = make_spec("VerifyCompleteness", "completeness_relative", QuestionSubject(kind="campaign"),
                 {}, deps=(a.question_id, b.question_id))
    d = make_spec("TestModelMismatch", "mismatch_assessed", QuestionSubject(kind="model"), {})
    agenda = agenda_of([a, b, c, d], [
        open_assessment(a.question_id),                     # no deps -> ready
        open_assessment(b.question_id),                     # dep A is open -> blocked
        open_assessment(c.question_id),                     # dep B is open -> blocked
        answered_assessment(d.question_id)])                # closed -> out of scope entirely
    readiness = classify_readiness(agenda)
    assert readiness == {a.question_id: READY, b.question_id: DEPENDENCY_BLOCKED,
                         c.question_id: DEPENDENCY_BLOCKED}
    assert d.question_id not in readiness


def test_classify_readiness_treats_a_missing_dependency_assessment_as_blocking():
    """A dependency this agenda never assessed (absent from ``assessments``) blocks exactly like
    an open one -- no proof is possible over an unknown state."""
    a = make_spec("DiscoverRegion", "coverage_complete", QuestionSubject(kind="region"), {})
    b = make_spec("ConfirmPresence", "structural_presence", QuestionSubject(kind="phenomenon"),
                 {}, deps=(a.question_id,))
    agenda = agenda_of([b], [open_assessment(b.question_id)])   # a's spec/assessment absent
    assert classify_readiness(agenda) == {b.question_id: DEPENDENCY_BLOCKED}


def test_classify_readiness_over_a_real_derivation():
    """The same partition, driven through the real agenda derivation (mirrors the C4 convention):
    an anchored ConfirmPresence depends on its partition's DiscoverRegion; both stay open on an
    unsurveyed region, so the presence question is blocked on the still-open coverage question."""
    scene = {"observations": [{"scan_ref": "e:1:d", "candidates": [], "count_trust": "trusted",
                              "sample": "s1", "lattice_epoch": 1, "conditions": [],
                              "best_label": None}],
            "clusters": [], "non_scoring": [],
            "sample_knowledge": {"zone_period": 1.0, "lattice_epoch": 1},
            "condition_t_round": None,
            "footprints": [{"scan_ref": "e:1:d", "axis_coordinate": 2.5, "partition_id": P1,
                            "effective_exposure": 5.0, "bands": []}]}
    step_payload = {"schema": "isar.campaign-step.discover_and_map/2",
                    "region": {"kind": "path", "q_start": [2.0, 0.0, 0.0],
                              "q_end": [3.0, 0.0, 0.0], "maximum_initial_spacing_rlu": 0.5},
                    "energy": {"intrinsic_omega_domain_meV": [0.0, 10.0]},
                    "catalogue_profile": "isar.catalogue.inelastic-default/1",
                    "deliverables": [{"kind": "phenomenon_map"}], "include_unknown": True,
                    "resolved_policy": resolve_policy()}
    revision = derive_agenda(scene, step_payload, step_id="discover-1",
                             program_fingerprint="prog-1")
    payload = revision.payload
    discover = next(s for s in payload["specs"] if s["family"] == "DiscoverRegion")
    presence = next(s for s in payload["specs"] if s["family"] == "ConfirmPresence"
                   and s["subject"]["anchor"] is not None)
    assessments = {a["question_id"]: a for a in payload["assessments"]}
    assert assessments[discover["question_id"]]["status"] == "open"
    assert assessments[presence["question_id"]]["status"] == "open"
    assert discover["question_id"] in presence["dependencies"]

    readiness = classify_readiness(payload)
    assert readiness[discover["question_id"]] == READY
    assert readiness[presence["question_id"]] == DEPENDENCY_BLOCKED


# ----------------------------------------------------------------------------
# run_ready_questions
# ----------------------------------------------------------------------------
def test_dependency_blocked_question_never_touches_a_generator():
    a = make_spec("DiscoverRegion", "coverage_complete", QuestionSubject(kind="region"), {},
                 generator_ids=("gen_a",))
    b = make_spec("ConfirmPresence", "structural_presence", QuestionSubject(kind="phenomenon"),
                 {}, deps=(a.question_id,), generator_ids=("gen_b",))
    agenda = agenda_of([a, b], [open_assessment(a.question_id), open_assessment(b.question_id)])
    readiness = {a.question_id: READY, b.question_id: DEPENDENCY_BLOCKED}

    def sentinel(_ctx, _spec):
        raise AssertionError("a dependency_blocked question's generator must never run")

    fake_table = {"gen_a": lambda ctx, spec: outcome_of(clean_zero="done", exhaustion_clean=True),
                 "gen_b": sentinel}
    result = _patch_and_run(fake_table, ctx_for(agenda), readiness)

    by_id = {r.question_id: r for r in result.records}
    assert by_id[a.question_id].disposition == RAN
    assert by_id[b.question_id].disposition == DEPENDENCY_BLOCKED
    assert by_id[b.question_id].generator_outcomes == ()
    assert result.evaluations == ()


def test_ready_question_runs_exactly_its_declared_generators_in_order():
    spec = make_spec("EstimateParameter", "parameter_within_tolerance",
                     QuestionSubject(kind="parameter"), {}, generator_ids=("z_gen", "a_gen"))
    agenda = agenda_of([spec], [open_assessment(spec.question_id)])
    readiness = {spec.question_id: READY}
    calls = []

    def make_gen(name):
        def _gen(ctx, sp):
            calls.append(name)
            return outcome_of(inapplicable=f"{name}_reason")
        return _gen

    fake_table = {"z_gen": make_gen("z_gen"), "a_gen": make_gen("a_gen"), "unused": make_gen("x")}
    _patch_and_run(fake_table, ctx_for(agenda), readiness)
    # generator_ids is sorted at QuestionSpec construction ("a_gen", "z_gen"); the runner must
    # follow the SPEC's order, not dict/registration order.
    assert calls == ["a_gen", "z_gen"]


def test_synthetic_zero_generator_family_is_no_generators_and_nothing_runs():
    spec = make_spec("VerifyCompleteness", "completeness_relative", QuestionSubject(kind="campaign"),
                     {}, generator_ids=())
    agenda = agenda_of([spec], [open_assessment(spec.question_id)])
    readiness = {spec.question_id: READY}

    def sentinel(_ctx, _spec):
        raise AssertionError("a no_generators question must never call a generator")

    result = _patch_and_run({"whatever": sentinel}, ctx_for(agenda), readiness)
    record = result.records[0]
    assert record.disposition == NO_GENERATORS
    assert record.generator_outcomes == ()
    assert result.evaluations == ()


def test_a_raising_generator_is_isolated_from_its_sibling_and_from_other_questions():
    q1 = make_spec("EstimateParameter", "parameter_within_tolerance",
                   QuestionSubject(kind="parameter"), {}, generator_ids=("boom", "fine"))
    q2 = make_spec("TestModelMismatch", "mismatch_assessed", QuestionSubject(kind="model"), {},
                   generator_ids=("fine",))
    agenda = agenda_of([q1, q2], [open_assessment(q1.question_id), open_assessment(q2.question_id)])
    readiness = {q1.question_id: READY, q2.question_id: READY}

    def boom(ctx, spec):
        raise RuntimeError("kaboom")

    def fine(ctx, spec):
        return outcome_of(clean_zero="all_clear", exhaustion_clean=True)

    result = _patch_and_run({"boom": boom, "fine": fine}, ctx_for(agenda), readiness)
    by_id = {r.question_id: r for r in result.records}

    q1_outcomes = {o.generator: o for o in by_id[q1.question_id].generator_outcomes}
    assert q1_outcomes["boom"].kind == "failed"
    assert "RuntimeError: kaboom" in q1_outcomes["boom"].error_code
    assert q1_outcomes["fine"].kind == "clean_zero" and q1_outcomes["fine"].exhaustion_clean

    q2_outcomes = {o.generator: o for o in by_id[q2.question_id].generator_outcomes}
    assert q2_outcomes["fine"].kind == "clean_zero"
    assert by_id[q1.question_id].disposition == RAN
    assert by_id[q2.question_id].disposition == RAN


def test_proposals_flow_into_evaluations_ready_for_dedupe():
    from isar.schema.action_identity import MeasurementIntent
    from isar.schema.generators import QuestionActionAssociation

    spec = make_spec("ConfirmPresence", "structural_presence", QuestionSubject(kind="phenomenon"),
                     {}, generator_ids=("presence_probe",))
    agenda = agenda_of([spec], [open_assessment(spec.question_id)])
    readiness = {spec.question_id: READY}
    intent = MeasurementIntent(action_class="presence_probe", hkl=(2.0, 0.0, 0.0), energy_lo=1.0,
                               energy_hi=8.0, window_family="discovery")
    association = QuestionActionAssociation(
        basis_agenda_digest="d" * 64, question_id=spec.question_id, generator="presence_probe",
        family_priority=0, dependency_depth=0, reason="test")  # spec.priority defaults to 0

    def gen(ctx, sp):
        return outcome_of(((intent, association),))

    result = _patch_and_run({"presence_probe": gen}, ctx_for(agenda), readiness)
    assert len(result.evaluations) == 1
    evaluation = result.evaluations[0]
    assert evaluation.intent is intent
    assert evaluation.generator == "presence_probe"
    assert evaluation.eligibility.value == "eligible"
    assert evaluation.associations == (association,)


# ----------------------------------------------------------------------------
# Serialization
# ----------------------------------------------------------------------------
def test_run_records_serialize_deterministically():
    spec = make_spec("EstimateParameter", "parameter_within_tolerance",
                     QuestionSubject(kind="parameter"), {}, generator_ids=("gen_a",))
    agenda = agenda_of([spec], [open_assessment(spec.question_id)])
    readiness = {spec.question_id: READY}
    result1 = _patch_and_run(
        {"gen_a": lambda ctx, sp: outcome_of(inapplicable="no_witness")}, ctx_for(agenda),
        readiness)
    result2 = _patch_and_run(
        {"gen_a": lambda ctx, sp: outcome_of(inapplicable="no_witness")}, ctx_for(agenda),
        readiness)
    dict1 = [r.to_dict() for r in result1.records]
    dict2 = [r.to_dict() for r in result2.records]
    assert dict1 == dict2
    assert dict1 == [
        {"question_id": spec.question_id, "family": "EstimateParameter",
         "generator_ids": ["gen_a"], "disposition": "ran",
         "generator_outcomes": [
             {"generator": "gen_a", "kind": "inapplicable", "generated": 0,
              "clean_zero_reason": None, "exhaustion_clean": False,
              "inapplicable_reason": "no_witness", "error_code": None}]}]


def test_blocked_and_no_generator_records_round_trip_through_to_dict():
    a = make_spec("DiscoverRegion", "coverage_complete", QuestionSubject(kind="region"), {},
                 generator_ids=("gen_a",))
    b = make_spec("ConfirmPresence", "structural_presence", QuestionSubject(kind="phenomenon"),
                 {}, deps=(a.question_id,), generator_ids=())
    record_blocked = QuestionRunRecord(b.question_id, "ConfirmPresence", (), DEPENDENCY_BLOCKED)
    record_empty = QuestionRunRecord(a.question_id, "DiscoverRegion", (), NO_GENERATORS)
    assert record_blocked.to_dict()["disposition"] == "dependency_blocked"
    assert record_blocked.to_dict()["generator_outcomes"] == []
    assert record_empty.to_dict()["disposition"] == "no_generators"


# ----------------------------------------------------------------------------
# helper
# ----------------------------------------------------------------------------
def _patch_and_run(fake_table, context, readiness):
    orig = readiness_module.GENERATOR_FUNCTIONS
    readiness_module.GENERATOR_FUNCTIONS = fake_table
    try:
        return run_ready_questions(context, readiness)
    finally:
        readiness_module.GENERATOR_FUNCTIONS = orig

def test_runner_refuses_mixed_payloads_loudly():
    """Readiness and specs from different derivations are a caller fault: silently skipping
    would let an exhaustion proof claim a question nobody looked at, and running an unminted
    readiness state would attribute work nobody classified. Both refuse by name (C7 converts
    the refusal into its controller_fault outcome)."""
    spec = make_spec("DiscoverRegion", "coverage_complete", QuestionSubject(kind="region"), {},
                     generator_ids=("opening_book_survey",))
    agenda = agenda_of([spec], [open_assessment(spec.question_id)])
    context = ctx_for(agenda)
    readiness = classify_readiness(agenda)
    with pytest.raises(ValueError, match="no such spec|same derivation"):
        run_ready_questions(context, {**readiness, "q:not-in-agenda": READY})
    with pytest.raises(ValueError, match="unknown readiness state"):
        run_ready_questions(context, {**readiness, spec.question_id: "answered"})
