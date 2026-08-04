"""Where a target window is centred and how wide it is: the same-q master, not the global model.

EIG-4 target cores were built from the global dispersion fit even at a q whose own confirmed pooled
master had *supplied* that fit.  The core half-width is ``max(2*FWHM, 3*sigma)`` and the
tricube/WLS sigma grows to tens of meV once the query leaves the measured range, so the best-measured
points produced the widest cores — wider than the authorized domain, which refused ``target_core/1``
with ``target_core_truncated_after_domain_clip``.

These tests pin the decision at the seam that makes it (``acquire._build_proposal``) and assert on the
**emitted proposal**, not on what a generator declared: the prior regression in this function
re-derived every window from the target search band and silently discarded a declared prediction.
"""
from __future__ import annotations

import pytest

from isar.drive.acquisition import acquire as acq
from isar.drive.acquisition.scan_design import (
    FAMILY_LEGACY_UNIFORM,
    FAMILY_TARGET_CORE,
    ScanDesignContext,
    design_scan_windows,
)
from isar.drive.objective import ObjectiveSpec, shipped_hypothesis_snapshot
from isar.schema.hypotheses import content_fingerprint

PATH = [(2.0, 0.0, 0.0), (2.1, 0.0, 0.0), (2.2, 0.0, 0.0), (2.3, 0.0, 0.0)]
DOMAIN = (-2.0, 7.0)
FWHM = 0.8
OMEGA0 = 4.0
INTERVAL_HALFWIDTH = 0.6      # > tolerance, so every q still wants another production
WILD_SIGMA = 9.0              # the extrapolated dispersion sigma this slice stops trusting for geometry


class _WildModel:
    """A dispersion fit whose extrapolated sigma is far wider than the authorized domain."""

    def predict(self, q):
        return 2.0, WILD_SIGMA


def _objective():
    frozen = shipped_hypothesis_snapshot("acoustic", DOMAIN)
    # ``shadow`` is what generates (and refuses) the alternative families; the executed request is
    # still the legacy control in both modes.
    frozen["steering"] = {**frozen["steering"], "scan_window_eig_mode": "shadow"}
    frozen["hypothesis_fingerprint"] = content_fingerprint(
        frozen["hypotheses"], frozen["steering"])
    return ObjectiveSpec(
        path=PATH, tolerance_meV=0.5, budget={"kind": "neutrons", "amount": 1e12},
        target_branch="acoustic", target_search_source="compiler",
        energy_search_meV=DOMAIN, **frozen)


def _master(hkl, **overrides):
    master = {"hkl": list(hkl), "omega0": OMEGA0, "halfwidth": INTERVAL_HALFWIDTH,
              "diagnostic_halfwidth": INTERVAL_HALFWIDTH, "usable": True, "reliable": True,
              "certifiable": True, "disposition": "resolved", "pool_consistent": True,
              "n_members": 2, "members": ["1", "2"], "branch_id": "acoustic",
              "target_branch": "acoustic", "content_hash": f"master-{hkl[0]}"}
    master.update(overrides)
    return master


def _observations(points):
    return [{"hkl": list(point), "kind": "production", "disposition": "resolved", "resolved": True,
             "omega0": OMEGA0, "submitted_ncount": 10_000, "proposal_cost": 1e6}
            for point in points]


def _proposals(measured=PATH, **master_overrides):
    """Every proposal the campaign offers with ``measured`` carrying confirmed pooled masters."""
    objective = _objective()
    observations = _observations(measured)
    masters = [_master(point, **master_overrides) for point in measured]
    plan = acq.plan_actions(objective, _WildModel(), observations,
                            resolution_info={"fwhm_dE": FWHM}, budget_state={},
                            master_rows=masters)
    return list(iter(plan.next_proposal, None))


def _only(proposals):
    assert proposals, "the campaign must still offer work"
    return proposals[0]


def _family(proposal, family):
    return next((candidate for candidate in proposal.scan_design_inventory.candidates
                 if candidate.candidate_family == family), None)


def _refusals(proposal):
    return [refusal.reason_code for refusal in proposal.scan_design_inventory.refusals]


# -- the same-q path ---------------------------------------------------------------------------------

def test_a_confirmed_same_q_master_overrides_the_global_dispersion_for_window_geometry():
    proposal = _only(_proposals())
    context = proposal.scan_design_inventory.context

    assert proposal.kind == "production"
    # The scorer's world is untouched: the target prediction is still the global ensemble's.
    assert context.target_prediction_mean_meV == pytest.approx(2.0)
    assert context.target_prediction_sigma_meV == pytest.approx(WILD_SIGMA)
    # The design — what the window is actually built from — is this q's own answer.
    assert context.design_prediction_mean_meV == pytest.approx(OMEGA0)
    assert context.design_prediction_sigma_meV == pytest.approx(INTERVAL_HALFWIDTH)
    assert context.target_band == pytest.approx((OMEGA0, INTERVAL_HALFWIDTH))


def test_the_emitted_proposal_carries_the_same_q_values_it_was_designed_from():
    """The honoured prediction is asserted on the *emitted* proposal. ``e_pred`` is persisted as
    ``acquisition_target_energy`` and is the centre effective exposure is weighted around, so a
    proposal designed on the master while still advertising the extrapolation would attribute its
    counts to an energy it never centred on."""
    proposal = _only(_proposals())

    assert proposal.e_pred == pytest.approx(OMEGA0)
    assert proposal.meta["design_prediction_source"] == acq.SAME_Q_SOURCE_MODEL_INTERVAL
    assert proposal.meta["design_prediction_mean"] == pytest.approx(OMEGA0)
    assert proposal.meta["design_prediction_sigma"] == pytest.approx(INTERVAL_HALFWIDTH)
    assert proposal.meta["e_sigma"] == pytest.approx(INTERVAL_HALFWIDTH)
    assert proposal.e_lo <= proposal.e_pred <= proposal.e_hi
    assert proposal.fwhm_dE == pytest.approx(FWHM)


def test_a_wide_global_sigma_no_longer_refuses_the_contained_target_core():
    proposal = _only(_proposals())
    core = _family(proposal, FAMILY_TARGET_CORE)
    legacy = _family(proposal, FAMILY_LEGACY_UNIFORM)

    assert "target_core_truncated_after_domain_clip" not in _refusals(proposal)
    assert core is not None, "the core must exist, not be refused"
    # max(2*FWHM, 3*sigma) = max(1.6, 1.8) = 1.8 around the measured 4.0 meV.
    assert (core.e_lo, core.e_hi) == pytest.approx((OMEGA0 - 1.8, OMEGA0 + 1.8))
    assert DOMAIN[0] <= core.e_lo and core.e_hi <= DOMAIN[1]
    assert (core.e_hi - core.e_lo) < (legacy.e_hi - legacy.e_lo)


def test_the_global_prediction_is_what_produced_the_refusal_this_slice_removes():
    """The control for the test above: the identical action designed from the global prediction is
    refused, so the contained core is the consequence of the basis change and not of anything else
    that moved."""
    proposal = _only(_proposals())
    from dataclasses import replace

    global_context = replace(
        proposal.scan_design_inventory.context,
        design_prediction_mean_meV=proposal.scan_design_inventory.context
        .target_prediction_mean_meV,
        design_prediction_sigma_meV=proposal.scan_design_inventory.context
        .target_prediction_sigma_meV,
        target_band=(2.0, WILD_SIGMA))

    refused = design_scan_windows(global_context)
    assert "target_core_truncated_after_domain_clip" in [
        refusal.reason_code for refusal in refused.refusals]


def test_the_conservative_local_diagnostic_is_the_fallback_when_no_interval_certifies():
    proposal = _only(_proposals(certifiable=False, reliable=False, halfwidth=None,
                                diagnostic_halfwidth=0.9))
    context = proposal.scan_design_inventory.context

    assert proposal.meta["design_prediction_source"] == acq.SAME_Q_SOURCE_DIAGNOSTIC_INTERVAL
    assert context.design_prediction_mean_meV == pytest.approx(OMEGA0)
    assert context.design_prediction_sigma_meV == pytest.approx(0.9)


# -- everything that must keep the global prediction ------------------------------------------------

def test_an_unvisited_survey_action_still_uses_the_global_dispersion_prediction():
    """The regression guard on the other side. An unmeasured q has no same-q answer, and a tight
    window there would miss a concave dispersion sitting above the local-linear prediction."""
    proposal = next(p for p in _proposals(measured=PATH[:3])
                    if tuple(p.scan_design_inventory.context.hkl) == PATH[3])
    context = proposal.scan_design_inventory.context

    assert proposal.kind == "quicklook"
    assert proposal.meta["design_prediction_source"] == "dispersion_model"
    assert context.design_prediction_mean_meV == pytest.approx(
        context.target_prediction_mean_meV)
    assert context.design_prediction_sigma_meV == pytest.approx(WILD_SIGMA)
    assert proposal.e_pred == pytest.approx(2.0)


def test_an_ambiguous_same_q_master_keeps_the_global_prediction():
    """A fork master has no single answer to be authoritative with, so the fallback is the global
    model exactly as before — the same-q path is a narrow override, not a general preference for
    local numbers."""
    proposal = _only(_proposals(modality="fork"))
    context = proposal.scan_design_inventory.context

    assert proposal.meta["design_prediction_source"] == "dispersion_model"
    assert context.design_prediction_sigma_meV == pytest.approx(WILD_SIGMA)


def test_a_master_with_no_local_spread_evidence_keeps_the_global_prediction():
    """No interval and no diagnostic half-width is no local spread evidence at all, so there is
    nothing to size a window with and the global prediction stands. Asserted at the decision helper:
    such a campaign has no eligible action left to emit a proposal from."""
    from isar.drive.acquisition.acquire import _same_q_design_prediction
    from isar.drive.supervisor.stopping import GridQ

    pick = GridQ(index=0, hkl=PATH[0], q=0.0, tolerance=0.5,
                 master=_master(PATH[0], certifiable=False, reliable=False, halfwidth=None,
                                diagnostic_halfwidth=None))
    intent = acq.MeasurementIntent(
        action_class="production", hkl=PATH[0], energy_lo=DOMAIN[0], energy_hi=DOMAIN[1],
        window_family="target")

    assert _same_q_design_prediction(pick, intent, driver_inserted=False) is None


def test_a_declared_rival_window_is_still_honoured_over_the_same_q_master():
    """The KNOWN TRAP, from the other direction: a rival probe declares its own window and centre on
    a deliberately different energy. A confirmed same-q target master must not re-centre it."""
    from isar.drive.acquisition.acquire import _same_q_design_prediction
    from isar.drive.supervisor.stopping import GridQ
    from isar.schema.steering import AUXILIARY_TRACK_NODE

    pick = GridQ(index=0, hkl=PATH[0], q=0.0, tolerance=0.5, master=_master(PATH[0]))
    target_intent = acq.MeasurementIntent(
        action_class="production", hkl=PATH[0], energy_lo=DOMAIN[0], energy_hi=DOMAIN[1],
        window_family="target")
    rival_intent = acq.MeasurementIntent(
        action_class="identity_rival_probe", hkl=PATH[0], energy_lo=5.9, energy_hi=8.3,
        window_family="rival", evidence_sink=AUXILIARY_TRACK_NODE)

    assert _same_q_design_prediction(pick, target_intent, driver_inserted=False) == (
        OMEGA0, INTERVAL_HALFWIDTH, acq.SAME_Q_SOURCE_MODEL_INTERVAL)
    assert _same_q_design_prediction(pick, rival_intent, driver_inserted=False) is None
    assert _same_q_design_prediction(pick, target_intent, driver_inserted=True) is None
