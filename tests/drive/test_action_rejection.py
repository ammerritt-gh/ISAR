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
    NARROW_RIVAL_PROBE_GENERATOR,
    RejectionClass,
)
from isar.drive.objective import ObjectiveSpec


def _obj():
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.5,
                                 budget={"kind": "neutrons", "amount": 1e12})


# -- suppression is visible in the inventory, not hidden in loop state ------------------------------

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

# -- an empty action space is a terminal, on every objective ---------------------------------------

# -- a policy-forbidden exhaustion names its own escape (O31) --------------------------------------

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

    by_action, by_hkl, by_request, by_design = loop_mod.suppressed_actions(log_path)
    assert by_design == set()          # none of these events named a design
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
        h=2.5, k=0.0, l=0.0, e_lo=-1.0, e_hi=5.0, e_step=0.2, ncount=5_000_000,
        acquisition_id="acq-1",
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


