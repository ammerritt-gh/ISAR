"""PR1.4 — the point-level ``stuck`` reader audit.

Two independent contracts land here:

* **1.4a — forensic status correctness.** ``stuck`` was serialized as a hard-coded ``False`` in two
  places (the ``decide`` event's per-q rollup and the campaign summary's ``grid_state``), so a
  saturated point looked un-stuck to every forensic reader — AP6's index 18 logged ``stuck=false``
  while ``GridQ.stuck`` was ``True``. Both surfaces now route through one shared
  ``local_exhaustion_status`` serializer, so the real per-q state is reported and the two surfaces
  cannot drift again.

* **1.4b — the action-suppression contract.** ``GridQ.stuck`` (== ``ambiguity_exhausted``) is a
  legacy per-q *reporting* aggregate, not a global eligibility predicate: the canonical action
  inventory and its terminal decision must never treat one saturated action class as if the whole q
  were dead. The contract test proves the terminalizer honours a still-eligible action alongside a
  saturated one. The characterization tests **pin the current v1 behaviour** of the monolithic tracked
  generator — including the known blanket ``if anchor.stuck: return None`` at
  ``_tracked_frontier_target`` that suppresses a *legal* broadening action — so PR2's generator split
  is measured against a locked baseline (see the plan: PR2 deletes the blanket ``.stuck`` reads and
  flips the stuck-anchor characterization to propose the broadening intent).
"""
from __future__ import annotations

from isar.drive.acquisition.acquire import _tracked_frontier_target
from isar.drive.acquisition.inventory import (
    ActionEligibility,
    ActionEvaluation,
    ActionInventory,
    GeneratorReport,
    GeneratorStatus,
    MeasurementIntent,
    RejectionClass,
)
from isar.drive.supervisor.stopping import GridQ, evaluate_stop, local_exhaustion_status
from isar.drive.objective import ObjectiveSpec


def _identity_saturated_updates() -> list[dict]:
    """Exposure doubled across three revisions while the identity metrics stalled → identity_saturated."""
    return [
        {"effective_exposure": 1.0e7, "target_probability": 0.7069, "identity_entropy": 0.5972},
        {"effective_exposure": 2.0e7, "target_probability": 0.7069, "identity_entropy": 0.5972},
        {"effective_exposure": 4.0e7, "target_probability": 0.7069, "identity_entropy": 0.5972},
    ]


def _saturated_point(index=0, hkl=(2.9, 0.0, 0.0), q=0.9) -> GridQ:
    g = GridQ(index=index, hkl=hkl, q=q, tolerance=0.5, max_production_batches=8)
    g.track_state = "ambiguous"
    g.local_identity_clear = False
    g.n_production = 2
    g.n_quicklook = 1
    g.target_probability = 0.7069
    g.identity_entropy = 0.5972
    g.identity_updates = _identity_saturated_updates()
    g.evidence_master = {"branch_candidates": [{"energy_meV": 5.98, "sigma_meV": 0.05}],
                         "candidate_state": "provisional", "diagnostic_halfwidth": 0.5}
    return g


# -- 1.4a: the shared serializer reports the real per-q state -------------------------------------

def test_local_exhaustion_status_reports_the_real_saturated_state():
    saturated = _saturated_point()
    assert saturated.stuck is True
    status = local_exhaustion_status(saturated)
    assert status["stuck"] is True
    assert status["ambiguity_exhausted"] is True
    # The reason names the identity-saturation mechanism, matching the honest terminal wording.
    assert status["stuck_reason"] == saturated.stuck_reason
    assert "identity evidence stopped responding to exposure" in status["stuck_reason"]


def test_local_exhaustion_status_is_clean_for_an_unsaturated_point():
    clean = GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5, n_quicklook=1)
    assert clean.stuck is False
    assert local_exhaustion_status(clean) == {
        "stuck": False, "stuck_reason": None, "ambiguity_exhausted": False}


def test_evaluate_stop_per_q_serializes_real_stuck_not_a_hardcoded_false():
    """Regression: the ``decide`` per-q rollup once hard-coded ``stuck=False`` for every point."""
    obj = ObjectiveSpec.along_h(h_start=2.9, h_end=2.9, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e12},
                                target_branch="acoustic", target_selector="tracked_branch")
    saturated = _saturated_point(hkl=(2.9, 0.0, 0.0), q=0.0)
    decision = evaluate_stop(obj, [saturated], {"kind": "neutrons", "amount": 1e12, "spent": 0.0})
    row = decision.per_q[0]
    assert row["stuck"] is True
    assert row["ambiguity_exhausted"] is True
    assert row["stuck_reason"] == saturated.stuck_reason


# -- 1.4b: the terminalizer never treats saturation as a global filter ----------------------------

def _intent(action_class, hkl=(2.9, 0.0, 0.0)) -> MeasurementIntent:
    return MeasurementIntent(action_class=action_class, hkl=hkl, energy_lo=-2.0, energy_hi=7.0,
                             window_family="two_branch")


def test_a_saturated_same_q_action_does_not_terminalise_when_a_rival_action_is_eligible():
    """The canonical contract PR2's generator split must preserve: one saturated action class does not
    make the q dead. A REJECTED(saturation) same-q identity action alongside an ELIGIBLE focused-rival
    action leaves the inventory non-terminal — ``stuck`` is never a global eligibility predicate."""
    same_q = ActionEvaluation(
        intent=_intent("same_q_identity"), generator="same_q_identity",
        eligibility=ActionEligibility.REJECTED, rejection_class=RejectionClass.SATURATION,
        rejection_reason="same_q_identity_saturated")
    rival = ActionEvaluation(
        intent=_intent("focused_rival"), generator="focused_rival",
        eligibility=ActionEligibility.ELIGIBLE)
    inventory = ActionInventory(
        evaluations=[same_q, rival],
        reports=[GeneratorReport(generator="same_q_identity", status=GeneratorStatus.COMPLETED,
                                 generated=1),
                 GeneratorReport(generator="focused_rival", status=GeneratorStatus.COMPLETED,
                                 generated=1)])
    assert inventory.has_eligible_action is True
    assert [e.intent.action_class for e in inventory.eligible_actions()] == ["focused_rival"]
    assert inventory.terminal_cause() is None


# -- 1.4b: v1 baseline of the monolithic tracked generator (PR2 flips the stuck-anchor case) -------

def _classify(_g):
    return 1.0


def test_a_stuck_anchor_still_offers_the_legal_broadening_action():
    """PR1.7c flipped this. Saturation says counting cannot settle the *label* at this q; it says
    nothing about a neighbouring classification probe. The blanket ``if anchor.stuck: return None``
    dropped the whole anchor, so a stuck anchor with an unvisited neighbour produced no action at all
    and the controller reported ``action_inventory_exhausted`` while that broadening scan was still
    available (and ``same_q_identity_work_remains`` was False, so the invariant guard never fired).
    Both the stuck and non-stuck anchor must now offer the same broadening quicklook."""
    def grid(stuck):
        anchor = _saturated_point(index=0, hkl=(2.0, 0.0, 0.0), q=0.0)
        if not stuck:
            anchor.identity_updates = []       # remove the saturation → not stuck
        neighbor = GridQ(index=1, hkl=(2.05, 0.0, 0.0), q=0.05, tolerance=0.5)
        return [anchor, neighbor]

    not_stuck = grid(stuck=False)
    assert not_stuck[0].stuck is False
    baseline = _tracked_frontier_target(not_stuck, anchor_index=0, classify=_classify)
    assert baseline is not None and baseline[1] == "quicklook"

    stuck = grid(stuck=True)
    assert stuck[0].stuck is True
    assert stuck[0].same_q_identity_work_remains is False      # the invariant guard cannot catch this
    recovered = _tracked_frontier_target(stuck, anchor_index=0, classify=_classify)
    assert recovered is not None and recovered[1] == "quicklook"
    assert tuple(recovered[0].hkl) == tuple(baseline[0].hkl)   # the same legal neighbouring probe


def test_a_stuck_anchor_still_refuses_same_q_identity_counting():
    """The other half of the per-action contract: un-blanketing must not re-authorise the same-q
    identity action that saturation is actually about. With no neighbour to broaden to, a stuck anchor
    has no action left — an honest empty set rather than a suppressed one."""
    anchor = _saturated_point(index=0, hkl=(2.0, 0.0, 0.0), q=0.0)
    visited_neighbor = _saturated_point(index=1, hkl=(2.05, 0.0, 0.0), q=0.05)
    visited_neighbor.track_state = "confirmed"
    visited_neighbor.local_identity_clear = True
    assert anchor.same_q_identity_work_remains is False
    assert _tracked_frontier_target([anchor, visited_neighbor], anchor_index=0,
                                    classify=_classify) is None


def test_v1_baseline_stuck_frontier_is_already_per_action_and_still_bridges():
    """Contrast: a stuck *frontier* (not anchor) is already per-action — it still proposes the adjacent
    bridge/broaden actions, so only the anchor blanket-read at :681 is the PR2 target."""
    anchor = GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5, n_production=1,
                   track_state="confirmed")
    anchor.local_identity_clear = True
    frontier = _saturated_point(index=1, hkl=(2.05, 0.0, 0.0), q=0.05)
    neighbor = GridQ(index=2, hkl=(2.1, 0.0, 0.0), q=0.1, tolerance=0.5)
    result = _tracked_frontier_target([anchor, frontier, neighbor], anchor_index=0, classify=_classify)
    assert result is not None
    assert frontier.stuck is True and result[1] == "quicklook"    # bridge/broaden, not suppressed
