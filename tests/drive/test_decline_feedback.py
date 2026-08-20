"""Durable-blocker production emission — the decline → re-plan cycle (SCENE-4.3 C7, D5).

The one lane in this build that turns an instrument refusal into a CLOSED question. A GEOMETRY
decline says the machine literally cannot perform the measurement, which is a durable fact about
the instrument and this evidence basis; the cycle that follows is:

    decline → :class:`QuestionBlockerEvidence` for every question the declined action served
            → the declined ``scientific_action_fingerprint`` recorded
            → re-plan against the SAME pinned scene, both carried in
            → the planner re-marks matching evaluations REJECTED/STRUCTURAL in the RUN RECORD
            → producer rule 6 (rejected-only, all-STRUCTURAL, matching evidence) emits the proof
            → ``apply_exhaustion`` closes the question ``unresolvable``

Everything below pins one link of that chain against the real planner, the real producer and the
real agenda store; only the wire and the scene-build subprocess are stood in for, exactly as in
``test_program_runner.py`` (whose builders these reuse).

The two SUPPRESSION CHANNELS are deliberately distinct and both stay visible in the decision basis:
C6's ``retired_physical_actions`` narrows the SELECTION input only and leaves the evaluations
eligible (a broken detector is not a statement about the physics), while C7's declined fingerprints
are a durable STRUCTURAL rejection that the exhaustion producer is entitled to close a question on.
Transport declines never enter this lane — ``tests/drive/test_broken_scan_lane.py`` owns that arm.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import isar.drive.program_runner as runner_mod
from isar.drive.acquisition.inventory import ActionEligibility, RejectionClass
from isar.drive.instrument.tavi_client import TaviError
from isar.drive.program_runner import (
    GEOMETRY_BLOCKER_NAME, MAX_DECLINE_REPLANS_PER_DELIVERY, VERDICT_IN_PROGRESS,
    VERDICT_MACHINE_FAILURE, run_program)
from isar.scene.agenda import AGENDA_STORE
from isar.schema.campaign_program import CampaignProgram

from drive.test_program_runner import (
    COLD_START_SCENE, DecliningTavi, FakeBuild, MockTavi, config_for, decision_lines,
    discover_step, log_payloads, workspace)


# ---------------------------------------------------------------------------- builders
class DeclineFirstTavi(MockTavi):
    """Geometry refuses the first ``n`` requests and queues everything after.

    The sibling / staleness cases need a decline that does not repeat forever: the point of both is
    what the step does NEXT, which a permanently-refusing instrument never lets it reach.
    """

    def __init__(self, n: int = 1):
        super().__init__()
        self.remaining = n

    def validate(self, body):
        if self.remaining > 0:
            self.remaining -= 1
            return {"would_queue": False, "blockers": ["q_unreachable_geometry"],
                    "requested_points": 10, "feasible_points": 0}
        return super().validate(body)


class SubmitRefusingTavi(MockTavi):
    """Validation queues the request; the SUBMIT refuses it with one structured TAVI code.

    The two submit-time refusals sit either side of the geometry line and cannot be reached through
    validation: ``all_points_infeasible`` is scope ``geometry`` (durable), ``infeasible_points`` is
    scope ``request`` (a bad grid at a reachable q, never durable).
    """

    def __init__(self, code: str):
        super().__init__()
        self.code = code

    def submit_scan(self, body, idempotency_key=None):
        raise TaviError(400, f"instrument refused: {self.code}",
                        json.dumps({"error": {"code": self.code}}))


def agenda_payload(root: Path) -> dict:
    _pointer, payload = AGENDA_STORE.read_current(root / "report")
    return payload


def assessment_for(payload: dict, question_id: str) -> dict:
    return next(a for a in payload["assessments"] if a["question_id"] == question_id)


def inventory_rows(root: Path, decision: dict) -> dict[str, dict]:
    blob = json.loads((root / decision["inventory_ref"]["path"]).read_text(encoding="utf-8"))
    return {row["scientific_action_fingerprint"]: row for row in blob["evaluations"]}


def emissions(root: Path) -> list[dict]:
    return log_payloads(root, "durable_blocker_emitted")


def two_eligible_actions(monkeypatch, coords):
    """Two eligible survivors on the survey question, so "the step planned a DIFFERENT action" is
    observable at all. Same synthetic generator ``tests/drive/test_planner.py`` already uses."""
    from drive.test_planner import _two_action_generator
    import isar.drive.steering.readiness as readiness_module
    from isar.drive.steering.readiness import GENERATOR_FUNCTIONS

    patched = dict(GENERATOR_FUNCTIONS)
    patched["opening_book_survey"] = _two_action_generator(coords)
    monkeypatch.setattr(readiness_module, "GENERATOR_FUNCTIONS", patched)


# ---------------------------------------------------------------------------- the full chain
def test_a_geometry_decline_blocks_its_question_and_asks_the_operator(
        tmp_path, monkeypatch):
    """The whole C7 chain on one decline, link by link.

    KNOWN DEVIATION from the plan's acceptance wording: the resulting ``PlanResult.outcome`` here is
    ``no_eligible_action``, not ``no_action_with_honest_exhaustion``. That distinction is a property
    of the WORLD, not of this lane — the honest-exhaustion outcome requires EVERY question closed,
    and the cold-start world's two anchored ConfirmPresence questions stay open with no action of
    their own (their generator proposes nothing), which is the state ``no_eligible_action`` exists
    to name. What C7 owes is asserted directly instead: the declined action is NEVER selected again.
    """
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=DecliningTavi(), max_deliveries=5))

    # --- the decline was classed as geometry and produced exactly one re-plan ------------------
    assert outcome.result.declines == ("geometry_unreachable",)
    assert outcome.result.decline_replans == 1
    assert outcome.result.deliveries == 0            # a decline is not a delivery

    # --- evidence was emitted for the question the declined action served ----------------------
    emitted = emissions(root)
    assert len(emitted) == 1
    evidence = emitted[0]["evidence"]
    assert len(evidence) == 1
    assert evidence[0]["blocker_name"] == GEOMETRY_BLOCKER_NAME
    assert evidence[0]["supporting_generator_ids"] == ["opening_book_survey"]
    blocked_question = evidence[0]["question_id"]

    # --- the decision sequence: selected once, then never again -------------------------------
    decisions = decision_lines(root)
    assert [line["outcome"] for line in decisions] == ["selected", "no_eligible_action"]
    declined_fingerprint = decisions[0]["selection"]["fingerprint"]
    replan = decisions[1]
    assert replan["decision_basis"]["declined_scientific_actions"] == [declined_fingerprint]
    assert replan["selection"] is None
    # The evidence names the SAME derivation the re-plan ran: deterministic, so it authenticates.
    assert evidence[0]["basis_agenda_digest"] == replan["decision_basis"]["basis_digest"]
    assert evidence[0]["scene_digest"] == replan["decision_basis"]["scene_digest"]

    # --- the re-marked evaluation, in the set the PRODUCER classifies (== the persisted one) ---
    row = inventory_rows(root, replan)[declined_fingerprint]
    assert row["eligibility"] == ActionEligibility.REJECTED.value
    assert row["rejection_class"] == RejectionClass.STRUCTURAL.value
    assert row["rejection_reason"] == "instrument_declined_as_infeasible"
    # ...and it is in no selection or rank table anywhere after the decline.
    assert declined_fingerprint not in {
        entry["scientific_action_fingerprint"]
        for line in decisions[1:] if line["selection"]
        for entry in line["selection"]["rank_table"]}

    # --- the evidence attached, and the question is BLOCKED — deliberately not CLOSED ---------
    # It used to close ``unresolvable`` here. That was the arity hole (ruling H21(g)):
    # ``opening_book_survey`` offers ONE point inside an uncovered gap, so the instrument refusing
    # that point proves nothing about the gap, let alone about the region — a point a few
    # hundredths of an rlu away would have surveyed it just as well. The producer now requires the
    # generator to declare its offers EXHAUSTIVE before one refusal may close a question, and the
    # survey generator cannot, so the region stays on the books.
    payload = agenda_payload(root)
    blocked = assessment_for(payload, blocked_question)
    assert blocked["status"] == "open"
    assert [b["name"] for b in blocked["blockers"]] == [GEOMETRY_BLOCKER_NAME]
    notes = dict(blocked["notes"])
    assert "evidence_attached" in notes
    assert "unresolvable_closure" not in notes
    assert "blocker_evidence_rejected" not in notes, \
        "the evidence the driver stamped did not authenticate against the re-plan's derivation"
    assert payload["exhaustion_evidence"] == [], \
        "a sampled action space must not produce an exhaustion proof"

    # ...and the operator is still ASKED about it. Safety must not cost the operator surface: the
    # prompt now rides the BLOCK rather than the closure (owner ruling 2026-08-18), so a campaign
    # that hits a wall still puts the question to a human instead of only listing it as blocking.
    requests = [r for r in outcome.result.consultation_requests
                if r["kind"] == "unresolvable_closure"]
    assert [r["question_id"] for r in requests] == [blocked_question]


# ---------------------------------------------------------------------------- sibling question
def test_a_sibling_eligible_action_is_selected_while_the_declined_one_stays_excluded(
        tmp_path, monkeypatch):
    """Rule 2 still outranks rule 6: while an ELIGIBLE survivor serves the question, no proof is
    claimed for it — the step simply plans the other action and keeps measuring."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
    two_eligible_actions(monkeypatch, (2.0, 2.9))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=DeclineFirstTavi(1), max_deliveries=1))

    assert outcome.result.decline_replans == 1
    assert outcome.result.deliveries == 1, "the step never got past the declined action"

    selected = [line for line in decision_lines(root) if line["outcome"] == "selected"]
    assert len(selected) >= 2
    declined_fingerprint = selected[0]["selection"]["fingerprint"]
    assert selected[1]["selection"]["fingerprint"] != declined_fingerprint

    replan = selected[1]
    assert declined_fingerprint not in {entry["scientific_action_fingerprint"]
                                        for entry in replan["selection"]["rank_table"]}
    assert inventory_rows(root, replan)[declined_fingerprint]["eligibility"] == \
        ActionEligibility.REJECTED.value
    # The question kept an eligible action, so nothing was closed on the strength of the decline.
    blocked_question = emissions(root)[0]["evidence"][0]["question_id"]
    assert assessment_for(agenda_payload(root), blocked_question)["status"] != "unresolvable"


# ---------------------------------------------------------------------------- the request lane
def test_a_bad_grid_at_a_reachable_q_never_mints_a_durable_blocker(tmp_path, monkeypatch):
    """``infeasible_points_at_submit`` is scope ``request``, not ``geometry``.

    The instrument refused THIS GRID; the q is reachable and a differently-gridded window at the
    same q is still submittable. Reading the word "infeasible" in the verdict as geometry — which a
    substring match over the TAVI blocker markers did — would close a question on a refusal the
    instrument never made. Nothing durable may come of it: no evidence, no declined fingerprint,
    no evaluation re-marked STRUCTURAL, and the question stays open.
    """
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=SubmitRefusingTavi("infeasible_points"),
                                     max_deliveries=5))

    assert outcome.result.declines == ("infeasible_points_at_submit",)
    assert outcome.verdict == VERDICT_IN_PROGRESS
    assert outcome.result.reason == "request_declined: infeasible_points_at_submit"
    # Nothing durable: no re-plan, no evidence, no declined fingerprint anywhere in the basis.
    assert outcome.result.decline_replans == 0
    assert emissions(root) == []
    decisions = decision_lines(root)
    assert [line["outcome"] for line in decisions] == ["selected"]
    assert "declined_scientific_actions" not in decisions[0]["decision_basis"]
    # ...and no question was closed on the strength of it.
    payload = agenda_payload(root)
    assert payload["exhaustion_evidence"] == []
    assert all(a["status"] != "unresolvable" for a in payload["assessments"])
    assert all(not a["blockers"] for a in payload["assessments"])


def test_a_submit_time_geometry_refusal_takes_the_durable_lane(tmp_path, monkeypatch):
    """The sibling verdict, on the geometry side of the same line. ``all_points_infeasible`` is the
    submit-time form of ``geometry_unreachable`` — scope ``geometry`` — so it earns evidence and a
    re-plan exactly as the validation-time refusal does."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=SubmitRefusingTavi("all_points_infeasible"),
                                     max_deliveries=5))

    assert outcome.result.declines[0] == "all_points_infeasible"
    assert outcome.result.decline_replans >= 1
    emitted = emissions(root)
    assert emitted and emitted[0]["evidence"][0]["blocker_name"] == GEOMETRY_BLOCKER_NAME
    assert decision_lines(root)[1]["decision_basis"]["declined_scientific_actions"] == [
        decision_lines(root)[0]["selection"]["fingerprint"]]


# ---------------------------------------------------------------------------- the audit stream
def test_a_geometry_decline_is_recorded_in_the_action_rejection_stream(tmp_path, monkeypatch):
    """Audit parity with the transport arm: every refusal the step meets is in the session log's
    ``action_suppressed`` stream, so the refusal history a human (or a resume) reads is complete
    whichever lane the refusal took."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
    two_eligible_actions(monkeypatch, (2.0, 2.9))

    run_program(CampaignProgram(steps=(step,)), step.id,
                config_for(root, client=DeclineFirstTavi(1), max_deliveries=1))

    suppressed = log_payloads(root, "action_suppressed")
    assert [p["verdict"] for p in suppressed] == ["geometry_unreachable"]
    assert suppressed[0]["scope"] == "geometry"
    assert suppressed[0]["rejection_class"] == RejectionClass.STRUCTURAL.value


# ---------------------------------------------------------------------------- per-revision state
def test_a_delivery_clears_the_decline_state_it_made_stale(tmp_path, monkeypatch):
    """Decline state is per SCENE REVISION, and a delivery moves the scene.

    Carrying it past the delivery could never change an outcome — the declined fingerprint hashes
    the scene digest so it stops matching, and the evidence's digest triple goes stale — but it
    made every later decision re-derive evidence that could only be rejected, and stamped the
    agenda with a ``blocker_evidence_rejected`` note that says nothing about the current revision.
    """
    from drive.test_broken_scan_lane import moving_scenes

    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, moving_scenes()))
    two_eligible_actions(monkeypatch, (2.0, 2.9))

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=DeclineFirstTavi(1), max_deliveries=2))

    assert outcome.result.decline_replans == 1 and outcome.result.deliveries == 2
    stale_scene = emissions(root)[0]["evidence"][0]["scene_digest"]
    blocked_question = emissions(root)[0]["evidence"][0]["question_id"]

    # The last decision planned under a moved scene and carried NO decline state into it.
    last = decision_lines(root)[-1]
    assert last["decision_basis"]["scene_digest"] != stale_scene
    assert "declined_scientific_actions" not in last["decision_basis"]
    # ...so the expired evidence never reached the agenda to be rejected there.
    assessment = assessment_for(agenda_payload(root), blocked_question)
    assert "blocker_evidence_rejected" not in dict(assessment["notes"])
    assert assessment["status"] != "unresolvable"


# ---------------------------------------------------------------------------- acquisition identity
def test_a_declined_proposal_and_its_successor_do_not_share_an_acquisition_id(
        tmp_path, monkeypatch):
    """A decline is not a delivery, so an id keyed on the delivery count repeated itself — and
    every consumer that joins evidence on ``acquisition_id`` would have read two physically
    distinct requests as one acquisition."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))
    two_eligible_actions(monkeypatch, (2.0, 2.9))

    run_program(CampaignProgram(steps=(step,)), step.id,
                config_for(root, client=DeclineFirstTavi(1), max_deliveries=2))

    ids = [p["acquisition_id"] for p in log_payloads(root, "propose")]
    assert len(ids) >= 3, "the fixture did not produce a decline plus two deliveries"
    assert len(set(ids)) == len(ids), f"acquisition ids collided: {ids}"


# ---------------------------------------------------------------------------- the cap
def test_declines_that_never_narrow_the_action_set_trip_the_cap_as_a_controller_fault(
        tmp_path, monkeypatch):
    """The cap is a DEFENSE, not the mechanism. Reaching it means the narrowing this lane rests on
    is not happening, so the fixture has to break the narrowing on purpose: it drops
    ``declined_fingerprints`` on the way into the planner, which is exactly the controller bug the
    backstop exists for. A step that hits it fails as a machine — one of the four reasons — rather
    than looping on one refused action for the rest of the beamtime."""
    root = workspace(tmp_path)
    step = discover_step()
    monkeypatch.setattr(runner_mod, "build_scene_revision", FakeBuild(root, [COLD_START_SCENE]))

    real_plan = runner_mod.plan_next_action

    def plan_without_narrowing(*args, **kwargs):
        kwargs.pop("declined_fingerprints", None)
        kwargs.pop("blocker_evidence", None)
        return real_plan(*args, **kwargs)

    monkeypatch.setattr(runner_mod, "plan_next_action", plan_without_narrowing)

    outcome = run_program(CampaignProgram(steps=(step,)), step.id,
                          config_for(root, client=DecliningTavi(), max_deliveries=50))

    assert outcome.verdict == VERDICT_MACHINE_FAILURE
    assert "decline_replan_cap" in outcome.result.reason
    assert str(MAX_DECLINE_REPLANS_PER_DELIVERY) in outcome.result.reason
    assert outcome.result.decline_replans == MAX_DECLINE_REPLANS_PER_DELIVERY


# ---------------------------------------------------------------------------- the deferred half
def test_program_boundary_stays_a_valid_blocker_with_no_producer_in_this_slice():
    """DEFERRED, recorded: the owner's two-member durable taxonomy keeps ``program_boundary`` as a
    valid name, but 4.3 ships NO producer for it — geometry declines are the only durable evidence
    this build emits. Pinned by grep over the production tree rather than by intention, so the day
    someone adds an emitter this test fails and the deferral is revisited deliberately.
    """
    from isar.schema.generators import DURABLE_BLOCKER_NAMES
    from isar.scene.questions import BLOCKER_CLASS_BY_NAME, BLOCKER_DURABLE

    assert DURABLE_BLOCKER_NAMES == {"instrument_geometry_infeasible", "program_boundary"}
    assert BLOCKER_CLASS_BY_NAME["program_boundary"] == BLOCKER_DURABLE

    root = Path(__file__).resolve().parents[2] / "isar"
    hits = sorted(path.relative_to(root).as_posix() for path in root.rglob("*.py")
                  if "program_boundary" in path.read_text(encoding="utf-8"))
    assert hits == ["scene/questions.py", "schema/generators.py"], \
        f"program_boundary gained a producer outside its vocabulary declaration: {hits}"
