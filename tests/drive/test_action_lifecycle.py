"""The three-stage measurement-action lifecycle on the live path (PR2.2).

One action carries three identities, each stable across the stage it names:

* ``MeasurementIntent.scientific_action_fingerprint`` — *what uncertainty are we resolving?*
* ``FinalizedProposal.request_fingerprint`` — *what exact request did we ask the instrument to run?*
* ``ValidatedAttempt.feasibility_manifest_fingerprint`` — *under what geometry did TAVI judge it?*

Keeping them separate is what lets a failed request or a rejected geometry retry the same scientific
question with a different window, step, or q — and, conversely, what stops an unchanged scientific
question from looking new every time a clamp nudges the step. PR1 declared the three types; nothing
built them. These tests pin the wiring, and the invariant PR2.3 then depends on: re-finalizing a
request must move the request fingerprint and leave the scientific one alone.
"""
from __future__ import annotations

import json

import pytest

from isar.drive import loop as loop_mod
from isar.drive.acquisition import acquire as acq
from isar.drive.acquisition import model as acq_model
from isar.drive.acquisition.inventory import FinalizedProposal, ValidatedAttempt
from isar.drive.objective import ObjectiveSpec


def _obj():
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.5,
                                 budget={"kind": "neutrons", "amount": 1e12})


# -- stage 1 → 2: the proposal knows the intent it realizes ----------------------------------------

def test_the_proposal_carries_the_intent_it_was_built_from():
    """Without the intent object on the proposal the loop can only see a fingerprint *string*, and
    cannot construct the finalized stage against the scientific question it belongs to."""
    obj, model = _obj(), acq_model.fit([])
    proposal, inventory = acq.propose_action(obj, model, [], {"fwhm_dE": 0.8}, {})
    assert proposal is not None
    assert proposal.intent is not None
    selected = inventory.eligible_actions()[0].intent
    assert proposal.intent.scientific_action_fingerprint == \
        selected.scientific_action_fingerprint
    # The in-memory intent must not leak into the JSON event/file contract.
    assert "intent" not in proposal.to_dict()


# -- stage 2: the finalized request ----------------------------------------------------------------

def _finalized(tmp_path, proposal, objective, *, it=0):
    cfg = loop_mod._campaign_config(
        objective, {"workspace": tmp_path, "client": object(),
                    "run_isar": lambda *a, **k: None, "worker": False})
    loop_mod._finalize_proposal(proposal, objective, cfg, {}, it=it)
    return loop_mod.finalized_proposal(proposal)


def test_finalization_produces_a_request_matching_the_emitted_scan(tmp_path):
    obj, model = _obj(), acq_model.fit([])
    proposal, _inv = acq.propose_action(obj, model, [], {"fwhm_dE": 0.8}, {})
    finalized = _finalized(tmp_path, proposal, obj)

    assert isinstance(finalized, FinalizedProposal)
    assert (finalized.e_lo, finalized.e_hi, finalized.e_step) == \
        (proposal.e_lo, proposal.e_hi, proposal.e_step)
    assert finalized.n_points == proposal.n_points and finalized.ncount == proposal.ncount
    # The request fingerprint is the provenance hash already stamped over the *finalized* window and
    # count — not a second, competing hash of the same thing.
    assert finalized.request_fingerprint == proposal.meta["proposal_fingerprint"]
    assert finalized.intent.scientific_action_fingerprint == \
        proposal.meta["scientific_action_fingerprint"]


def test_refinalization_moves_the_request_fingerprint_and_not_the_scientific_one(tmp_path):
    """The load-bearing separation. A step shrink for target support, a point-count clamp, or a
    re-center is a different *request* answering the same *question*. If these shared one identity,
    anti-repeat suppression would either forget the question on every clamp or refuse to retry it with
    a better window."""
    obj, model = _obj(), acq_model.fit([])
    proposal, _inv = acq.propose_action(obj, model, [], {"fwhm_dE": 0.8}, {})
    first = _finalized(tmp_path, proposal, obj)

    proposal.e_step = float(proposal.e_step) / 2.0     # exactly what the target-support retry does
    second = _finalized(tmp_path, proposal, obj)

    assert second.request_fingerprint != first.request_fingerprint
    assert second.intent.scientific_action_fingerprint == \
        first.intent.scientific_action_fingerprint


def test_an_unchanged_request_finalizes_to_the_same_fingerprint(tmp_path):
    obj, model = _obj(), acq_model.fit([])
    proposal, _inv = acq.propose_action(obj, model, [], {"fwhm_dE": 0.8}, {})
    first = _finalized(tmp_path, proposal, obj)
    second = _finalized(tmp_path, proposal, obj)
    assert second.request_fingerprint == first.request_fingerprint


# -- stage 3: the validated attempt ----------------------------------------------------------------

def _validation(mask=(True, True, True), *, would_queue=True, blockers=()):
    return {"would_queue": would_queue, "blockers": list(blockers),
            "planned_feasible_mask": list(mask), "requested_points": len(mask),
            "feasible_points": sum(1 for value in mask if value)}


def test_the_validated_attempt_fingerprints_the_geometry_it_was_judged_under(tmp_path):
    obj, model = _obj(), acq_model.fit([])
    proposal, _inv = acq.propose_action(obj, model, [], {"fwhm_dE": 0.8}, {})
    finalized = _finalized(tmp_path, proposal, obj)

    attempt = loop_mod.validated_attempt(finalized, _validation(), status="would_queue")
    assert isinstance(attempt, ValidatedAttempt)
    assert attempt.proposal is finalized
    assert attempt.feasible_points == 3 and attempt.status == "would_queue"
    assert attempt.feasibility_manifest_fingerprint


def test_a_different_feasibility_manifest_is_a_different_attempt(tmp_path):
    """The same scientific question and the same request can be feasible under one geometry and not
    another. Only this third fingerprint distinguishes them, so a geometry rejection must not be
    charged to the request — let alone to the scientific action."""
    obj, model = _obj(), acq_model.fit([])
    proposal, _inv = acq.propose_action(obj, model, [], {"fwhm_dE": 0.8}, {})
    finalized = _finalized(tmp_path, proposal, obj)

    reachable = loop_mod.validated_attempt(finalized, _validation(), status="would_queue")
    blocked = loop_mod.validated_attempt(
        finalized, _validation((True, False, False), would_queue=False, blockers=["a2_limit"]),
        status="validation_rejected")

    assert blocked.feasibility_manifest_fingerprint != reachable.feasibility_manifest_fingerprint
    # ...while the two stages above are untouched: same request, same question.
    assert blocked.proposal.request_fingerprint == reachable.proposal.request_fingerprint
    assert blocked.proposal.intent.scientific_action_fingerprint == \
        reachable.proposal.intent.scientific_action_fingerprint


def test_the_manifest_fingerprint_is_stable_for_an_identical_verdict(tmp_path):
    obj, model = _obj(), acq_model.fit([])
    proposal, _inv = acq.propose_action(obj, model, [], {"fwhm_dE": 0.8}, {})
    finalized = _finalized(tmp_path, proposal, obj)
    first = loop_mod.validated_attempt(finalized, _validation(), status="would_queue")
    second = loop_mod.validated_attempt(finalized, _validation(), status="would_queue")
    assert first.feasibility_manifest_fingerprint == second.feasibility_manifest_fingerprint


def test_the_manifest_hash_treats_positional_masks_as_positional():
    """A reordered feasible mask is a *different* geometry, not the same one rearranged. Sorting it
    would make two geometries collide, letting a suppression formed under one apply to the other —
    a false match, which is the dangerous direction. Blockers are an unordered reason set, so those
    do normalize."""
    manifest = loop_mod._feasibility_manifest_fingerprint
    assert manifest(_validation((True, False, True))) != manifest(_validation((True, True, False)))
    assert manifest(_validation(blockers=["a2_limit", "s2_limit"])) == \
        manifest(_validation(blockers=["s2_limit", "a2_limit"]))


def test_the_manifest_hash_refuses_an_unserializable_verdict():
    """Same discipline as the inventory artifacts (PR1.7e): a value that is not JSON must fail rather
    than be stringified into a stable-looking address that means nothing."""
    with pytest.raises(TypeError):
        loop_mod._feasibility_manifest_fingerprint({"would_queue": True, "infeasible": {object()}})
