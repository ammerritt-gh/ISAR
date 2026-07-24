"""Rejection classing and try-the-next-action (PR2.3).

Before this, one rejected proposal cost the whole iteration: the loop appended a declined observation
and broke out, so a q the instrument cannot reach silently burned an iteration each time it was
re-selected, and an inventory could report ``action_inventory_exhausted`` without ever recording
*why* each action had stopped being usable.

The lifecycle identities PR2.2 established are what make this safe, and the scope rule below is the
whole point of having three of them:

* an unreachable **q** (``geometry_unreachable`` / ``all_points_infeasible``) suppresses the **hkl**.
  Not the scientific action fingerprint: unreachability is a property of the geometry, not of the
  question asked there, and the scientific fingerprint carries the evidence basis — so keying it
  there would make the suppression evaporate the instant any evidence advanced, and the controller
  would re-propose the same unreachable q for the rest of the campaign. That is not hypothetical:
  the campaign test caught exactly this, validating an unreachable q twice;
* a rejected **request** (``validation_rejected`` / ``insufficient_target_support``) suppresses only
  the **request**, because the widen / re-center / step-shrink ladder answering the same scientific
  question with a different window is a legitimate and already-working recovery;
* a **transient** failure (instrument communication, queue) suppresses nothing, ever.

Getting that wrong in the safe-looking direction — suppressing the scientific action on a rejected
request — would silently disable the missed-window recovery ladder.
"""
from __future__ import annotations

import types

import pytest

from isar.drive import loop as loop_mod
from isar.drive.acquisition import acquire as acq
from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition.inventory import (
    GENERATOR_REGISTRY,
    ActionEligibility,
    GeneratorSpec,
    LEGACY_SELECTOR_GENERATOR,
    RejectionClass,
)
from isar.drive.objective import ObjectiveSpec


def _register(monkeypatch, generators: dict):
    """Register a synthetic generator set, registry and dispatch kept in step (see PR2.1's tests)."""
    monkeypatch.setattr(acq, "GENERATOR_REGISTRY",
                        tuple(GeneratorSpec(name, version=1) for name in generators))
    monkeypatch.setattr(acq, "ACTION_GENERATORS", dict(generators))
    monkeypatch.setattr(acq, "ACTION_EVALUATORS",
                        {name: acq._legacy_selector_evaluate for name in generators})


def _obj():
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.5,
                                 budget={"kind": "neutrons", "amount": 1e12})


def _selected_fingerprint(objective, model):
    _inventory, candidates = acq.enumerate_actions(
        objective, model, [], resolution_info={"fwhm_dE": 0.8})
    return candidates[0].evaluation.intent.scientific_action_fingerprint


# -- suppression is visible in the inventory, not hidden in loop state ------------------------------

def test_a_suppressed_action_is_rejected_in_the_inventory_with_its_class():
    """Suppression has to live in the inventory. Held privately in the loop, the terminal proof would
    claim an empty action space while the reason the space emptied was invisible to it."""
    obj, model = _obj(), acq_model.fit([])
    fingerprint = _selected_fingerprint(obj, model)

    inventory, candidates = acq.enumerate_actions(
        obj, model, [], resolution_info={"fwhm_dE": 0.8},
        suppressed={fingerprint: {"class": "structural", "reason": "geometry_unreachable at (2.1,0,0)"}})

    assert candidates == []
    assert len(inventory.evaluations) == 1
    evaluation = inventory.evaluations[0]
    assert evaluation.eligibility is ActionEligibility.REJECTED
    assert evaluation.rejection_class is RejectionClass.STRUCTURAL
    assert "geometry_unreachable" in evaluation.rejection_reason
    # A suppressed action still *counts* as enumerated: the accounting must stay consistent, or the
    # exhaustion proof would be structurally invalid rather than merely empty.
    assert inventory.reports[0].generated == 1
    assert inventory.structural_problems() == []
    assert inventory.is_complete is True
    assert inventory.has_eligible_action is False
    assert inventory.terminal_cause() == "action_inventory_exhausted"


def test_suppressing_a_different_action_leaves_this_one_eligible():
    obj, model = _obj(), acq_model.fit([])
    inventory, candidates = acq.enumerate_actions(
        obj, model, [], resolution_info={"fwhm_dE": 0.8},
        suppressed={"some-other-question": {"class": "structural", "reason": "elsewhere"}})
    assert len(candidates) == 1
    assert inventory.has_eligible_action is True


def test_a_transient_suppression_is_refused_outright():
    """A network blip must never become a scientific conclusion. The type system will not stop a
    caller passing one in, so enumeration refuses it rather than quietly making it durable."""
    obj, model = _obj(), acq_model.fit([])
    fingerprint = _selected_fingerprint(obj, model)
    with pytest.raises(ValueError, match="transient"):
        acq.enumerate_actions(obj, model, [], resolution_info={"fwhm_dE": 0.8},
                              suppressed={fingerprint: {"class": "transient", "reason": "timeout"}})


# -- classing a rejection ---------------------------------------------------------------------------

@pytest.mark.parametrize("verdict,expected_class,expected_scope", [
    ("geometry_unreachable", RejectionClass.STRUCTURAL, "geometry"),
    ("all_points_infeasible", RejectionClass.STRUCTURAL, "geometry"),
    ("validation_rejected", RejectionClass.STRUCTURAL, "request"),
    ("insufficient_target_support", RejectionClass.STRUCTURAL, "request"),
    ("instrument_submission_failed", RejectionClass.TRANSIENT, "none"),
])
def test_each_rejection_verdict_gets_its_durable_scope(verdict, expected_class, expected_scope):
    rejection = loop_mod.classify_rejection(verdict)
    assert rejection.rejection_class is expected_class
    assert rejection.scope == expected_scope


def test_an_unknown_verdict_is_transient_and_therefore_never_suppressing():
    """Fail open on classification: an unrecognised verdict must not become a permanent scientific
    suppression on the strength of a string nobody has mapped yet."""
    rejection = loop_mod.classify_rejection("something_new_from_tavi")
    assert rejection.rejection_class is RejectionClass.TRANSIENT
    assert rejection.scope == "none"


# -- try the next eligible action -------------------------------------------------------------------

def test_the_plan_hands_out_each_eligible_action_once_then_stops(monkeypatch):
    """The mechanism that stops a refused request costing the whole iteration. With only the legacy
    adapter registered the plan holds one action, so this is exercised against a two-generator
    registry — PR2.7 is what makes it fire on a real campaign."""
    def second(context):
        base = acq._legacy_selector_generate(context).actions
        if not base:
            return acq.GeneratorResult()
        original = base[0]
        other = next((point for point in context.grid
                      if tuple(point.hkl) != tuple(original.pick.hkl)), None)
        if other is None:
            return acq.GeneratorResult()
        intent = acq._intent_from_pick(context.objective, other, original.kind, original.score)
        return acq.GeneratorResult([acq.GeneratedAction(
            intent=intent, generator="second", pick=other, kind=original.kind,
            reason="second action", score=original.score)])

    _register(monkeypatch, {LEGACY_SELECTOR_GENERATOR: acq._legacy_selector_generate,
                            "second": second})
    plan = acq.plan_actions(_obj(), acq_model.fit([]), [], resolution_info={"fwhm_dE": 0.8},
                            budget_state={})

    first = plan.next_proposal()
    second_proposal = plan.next_proposal()
    assert first is not None and second_proposal is not None
    assert (first.h, first.k, first.l) != (second_proposal.h, second_proposal.k, second_proposal.l)
    # Each scientific action is offered once per iteration; the plan then reports the space spent
    # rather than looping over the same actions until the attempt cap runs out.
    assert plan.next_proposal() is None


def test_the_plan_never_re_offers_a_rejected_action_within_an_iteration():
    obj, model = _obj(), acq_model.fit([])
    plan = acq.plan_actions(obj, model, [], resolution_info={"fwhm_dE": 0.8}, budget_state={})
    assert plan.next_proposal() is not None
    assert plan.next_proposal() is None


# -- an empty action space is a terminal, on every objective ---------------------------------------

def test_an_empty_action_space_terminalizes_a_non_tracked_objective():
    """A latent bug that durable suppression made reachable. ``_blocked_frontier_decision`` speaks only
    for a tracked identity frontier; every other objective fell through it, and since the loop breaks
    unconditionally on a missing proposal the campaign reported ``continue`` as its final state — a
    non-terminal with no cause and no inventory proof."""
    from isar.drive.acquisition.inventory import (
        ActionEvaluation, ActionInventory, GeneratorReport, GeneratorStatus)
    from isar.drive.supervisor.stopping import StopDecision

    # Registry-complete: every registered generator reports, so the only thing under test is the
    # empty action space itself and not a missing-generator controller fault.
    empty = ActionInventory(
        evaluations=[],
        reports=[GeneratorReport(generator=spec.generator_id,
                                 status=GeneratorStatus.COMPLETED, generated=0,
                                 reason_code="no_selectable_action")
                 for spec in GENERATOR_REGISTRY])
    decision = loop_mod._exhausted_action_space_decision(
        StopDecision("continue", [], []), empty)

    assert decision.state == "incomplete"
    assert decision.terminal_cause == "action_inventory_exhausted"
    assert "action_inventory_exhausted" in decision.reasons[0]


def test_an_already_terminal_decision_is_left_alone():
    from isar.drive.supervisor.stopping import StopDecision
    met = StopDecision("met", ["every q within tolerance"], [])
    assert loop_mod._exhausted_action_space_decision(met, None) is met


def test_an_eligible_inventory_without_a_proposal_is_a_controller_fault():
    """Never coerce a controller bug into an exhaustion proof — the PR1.7a rule, applied to the
    general path as well as the tracked frontier."""
    from isar.drive.acquisition.inventory import (
        ActionEvaluation, ActionInventory, GeneratorReport, GeneratorStatus, MeasurementIntent)
    from isar.drive.supervisor.stopping import StopDecision

    intent = MeasurementIntent(action_class="production", hkl=(2.1, 0.0, 0.0), energy_lo=-2.0,
                               energy_hi=7.0, window_family="target")
    eligible = ActionInventory(
        evaluations=[ActionEvaluation(intent=intent, generator=LEGACY_SELECTOR_GENERATOR,
                                      eligibility=ActionEligibility.ELIGIBLE)],
        reports=[GeneratorReport(generator=LEGACY_SELECTOR_GENERATOR,
                                 status=GeneratorStatus.COMPLETED, generated=1)]
        + [GeneratorReport(generator=spec.generator_id, status=GeneratorStatus.COMPLETED,
                           generated=0, reason_code="not_applicable_here")
           for spec in GENERATOR_REGISTRY if spec.generator_id != LEGACY_SELECTOR_GENERATOR])
    with pytest.raises(loop_mod.ControllerInvariantError):
        loop_mod._exhausted_action_space_decision(StopDecision("continue", [], []), eligible)


def test_a_missing_registered_generator_faults_through_the_loops_terminal_path():
    """``terminal_cause()`` grew a registry argument but no production caller passed it, so an
    inventory whose own accounting is clean while a registered generator never ran could still be
    stamped ``action_inventory_exhausted``. The generator's actions are simply unknown; exhaustion is
    not provable over it."""
    from isar.drive.acquisition.inventory import (ActionInventory, GeneratorReport,
                                                  GeneratorStatus, TERMINAL_CONTROLLER_FAULT)
    from isar.drive.supervisor.stopping import StopDecision

    assert len(GENERATOR_REGISTRY) > 1, "the test needs a registry it can leave a hole in"
    incomplete = ActionInventory(
        evaluations=[],
        reports=[GeneratorReport(generator=LEGACY_SELECTOR_GENERATOR,
                                 status=GeneratorStatus.COMPLETED, generated=0)])
    assert incomplete.is_complete, "its own accounting is clean — that is the trap"

    decision = loop_mod._exhausted_action_space_decision(
        StopDecision("continue", [], []), incomplete)
    assert decision.terminal_cause == TERMINAL_CONTROLLER_FAULT
    assert "controller_fault" in decision.reasons[0]


# -- suppressions survive a resume ------------------------------------------------------------------

def test_suppressions_are_rederived_from_the_session_log(tmp_path):
    """Suppression is derived state, never a separate durable store: a resume that could not rebuild
    it would re-propose something the instrument has already refused, every iteration, forever."""
    log_path = tmp_path / "session_log.jsonl"
    loop_mod.log_event(log_path, "action_suppressed", {
        "hkl": [3.0, 0.0, 0.0], "scope": "geometry",
        "rejection_class": "structural", "reason": "geometry_unreachable at [3.0, 0.0, 0.0]",
        "iter": 4})
    loop_mod.log_event(log_path, "action_suppressed", {
        "request_fingerprint": "req-1", "hkl": [2.5, 0.0, 0.0], "scope": "request",
        "rejection_class": "structural", "reason": "validation_rejected", "iter": 5})
    # A transient event is recorded for forensics but must never re-enter the suppression set.
    loop_mod.log_event(log_path, "action_suppressed", {
        "scientific_action_fingerprint": "fp-blip", "hkl": [2.6, 0.0, 0.0], "scope": "none",
        "rejection_class": "transient", "reason": "connection lost", "iter": 6})

    by_action, by_hkl, by_request = loop_mod.suppressed_actions(log_path)
    assert by_action == {}
    assert set(by_hkl) == {"3,0,0"}
    assert by_hkl["3,0,0"]["class"] == "structural"
    assert "geometry_unreachable" in by_hkl["3,0,0"]["reason"]
    # A request-scoped event now rebuilds the third suppression set, keyed by request_fingerprint, so
    # an unchanged request the instrument already refused is not re-emitted after a resume. It must NOT
    # touch by_action: the widen / re-center ladder answering the same question with a different window
    # keys off the scientific action fingerprint and has to stay open.
    assert set(by_request) == {"req-1"}
    assert by_request["req-1"]["class"] == "structural"
    assert "validation_rejected" in by_request["req-1"]["reason"]


# -- request-scoped refusal: record the request, hand back the next action -------------------------

class _OneShotPlan:
    """Minimal plan stand-in: hands back a single fixed next action, counting how often it was asked."""

    def __init__(self, nxt):
        self._nxt = nxt
        self.calls = 0

    def next_proposal(self):
        self.calls += 1
        return self._nxt


def _refused_proposal():
    return types.SimpleNamespace(
        h=2.5, k=0.0, l=0.0, acquisition_id="acq-1",
        meta={"scientific_action_fingerprint": "sci-1", "proposal_fingerprint": "req-1"})


def test_a_request_scoped_rejection_records_by_request_and_offers_the_next_action(tmp_path):
    """GAP 2: a ``validation_rejected`` request must not cost the whole iteration. The refusal is
    charged to the *request* (so the exact request is not re-emitted), the scientific action is left
    untouched (its widen/recenter ladder stays open), and the plan's next eligible action is returned
    so the loop can attempt it immediately."""
    cfg = types.SimpleNamespace(log_path=tmp_path / "log.jsonl")
    nxt = object()
    plan = _OneShotPlan(nxt)
    action_suppressions: dict = {}
    unreachable_hkl: dict = {}
    by_request: dict = {}

    result = loop_mod._record_action_rejection(
        cfg, plan, _refused_proposal(), "validation_rejected",
        action_suppressions, unreachable_hkl, by_request, it=3)

    assert result is nxt
    assert plan.calls == 1
    assert set(by_request) == {"req-1"}
    assert by_request["req-1"]["class"] == "structural"
    # The scientific action fingerprint must stay live — request scope never narrows the action space.
    assert action_suppressions == {}
    assert unreachable_hkl == {}
    events = [e["payload"] for e in loop_mod.read_log(cfg.log_path)
              if e.get("kind") == "action_suppressed"]
    assert len(events) == 1
    assert events[0]["scope"] == "request"
    assert events[0]["request_fingerprint"] == "req-1"


def test_a_transient_rejection_records_nothing_durable_and_offers_no_next_action(tmp_path):
    """A transient refusal (network/queue) suppresses nothing and does not advance to another action:
    it must never become a durable scientific conclusion on the strength of one blip."""
    cfg = types.SimpleNamespace(log_path=tmp_path / "log.jsonl")
    plan = _OneShotPlan(object())
    action_suppressions: dict = {}
    unreachable_hkl: dict = {}
    by_request: dict = {}

    result = loop_mod._record_action_rejection(
        cfg, plan, _refused_proposal(), "instrument_submission_failed",
        action_suppressions, unreachable_hkl, by_request, it=3)

    assert result is None
    assert action_suppressions == {} and unreachable_hkl == {} and by_request == {}


def test_an_unreachable_q_stays_suppressed_when_the_evidence_basis_moves():
    """The regression the campaign test found. The scientific fingerprint includes the evidence
    basis, so a geometry refusal charged to it survives exactly until the next scan lands anywhere.
    Keyed by hkl, the q stays refused however much the evidence advances."""
    obj, model = _obj(), acq_model.fit([])
    unreachable = {acq.hkl_key((2.1, 0.0, 0.0)): {
        "class": "structural", "reason": "geometry_unreachable at [2.1, 0.0, 0.0]"}}

    for observations in ([], [{"hkl": (2.2, 0.0, 0.0), "kind": "quicklook",
                               "calibrated_halfwidth": 0.9, "resolved": True,
                               "disposition": "resolved", "omega0": 3.0}]):
        _inventory, candidates = acq.enumerate_actions(
            obj, model, observations, resolution_info={"fwhm_dE": 0.8},
            unreachable_hkl=unreachable)
        assert all(tuple(c.pick.hkl) != (2.1, 0.0, 0.0) for c in candidates)
