"""The narrow predicted-rival generator (PR2.7).

This is the generator AP6 needed and did not have. At H=2.9 the target sits at 5.754 meV with
P(target)=0.706 — below the 0.85 gate — and the rival that would settle the identity is predicted at
7.115 meV. Every existing generator legitimately produced nothing there: at a path endpoint there is no
unvisited neighbour to broaden to and no under-resolved behind-point to reconnect, so the campaign
stopped ``branch_ambiguity`` with beam time left.

The region is hard for a physical reason, not an accidental one. This sample has zone centres at every
even H, so H=3.0 is a zone **boundary**: the acoustic branch is at its maximum there and flattening
toward zero group velocity, which puts it closest to the optic branch and makes ω a weak discriminator
across 2.9→3.0. That is why counting longer stopped paying, and why the discriminating evidence has to
come from somewhere other than the target window.

The missing action is not more counting at the same window. ``identity_saturated`` is the point's own
statement that exposure grew and the label did not sharpen, so repeating the target window is known to
be wasted beam. What was never tried is *looking where the rival is predicted to be* — a narrow window
centred on 7.115 meV, fit independently, routed to ``auxiliary_track_node``.

Two ways this generator could be wrong, both guarded here: firing while ordinary counting could still
resolve the point (it would spend beam to answer a question the cheap path was about to answer), and
scanning outside the operator's declared search band to reach a rival they never authorised.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from isar.drive.acquisition import acquire as acq
from isar.drive.acquisition.inventory import GENERATOR_REGISTRY
from isar.drive.objective import ObjectiveSpec
from isar.schema.steering import AUXILIARY_TRACK_NODE
from isar.drive.supervisor.stopping import GridQ

RIVAL_GENERATOR = "narrow_rival_probe"


def _objective(**kw):
    base = dict(h_start=2.0, h_end=3.0, step=0.1, tolerance_meV=0.5,
                budget={"kind": "neutrons", "amount": 1e12}, target_branch="acoustic")
    base.update(kw)
    return ObjectiveSpec.along_h(**base)


def _blocked_point(index=18, hkl=(2.9, 0.0, 0.0), *, saturated=True, clear=False):
    """A point in AP6's failing state: identity ambiguous, and counting has stopped paying."""
    point = GridQ(index=index, hkl=hkl, q=hkl[0], tolerance=0.5)
    point.local_identity_clear = clear
    point.target_probability = 0.706
    point.identity_entropy = 0.61
    point.evidence_master = {"omega0": 5.754, "content_hash": "abc123"}
    if saturated:
        # Three comparable revisions, exposure up 4x, neither metric moving: the point's own evidence
        # that the label is not count-limited.
        point.identity_updates = [
            {"effective_exposure": 1.0, "target_probability": 0.700, "identity_entropy": 0.62},
            {"effective_exposure": 2.0, "target_probability": 0.703, "identity_entropy": 0.615},
            {"effective_exposure": 4.0, "target_probability": 0.706, "identity_entropy": 0.61},
        ]
    return point


def _context(grid, objective=None, rival=(7.115, 0.2), resolution_info=None):
    return acq.AcquisitionContext(
        objective=objective or _objective(), model=None, grid=tuple(grid), results_so_far=(),
        resolution_info=resolution_info, policy={}, runtime_limits={}, classify=None, fisher=None,
        target_hypothesis=None, classification_weight=1.0, opening=None, tracked_branch=True,
        # Zone centres every even H for this sample, so the period is 2.0 and H=3.0 is a zone
        # BOUNDARY. That is where the acoustic branch reaches its maximum and flattens, bringing it
        # closest to the optic branch — the physical reason index 18's identity is hard and why more
        # counting at the same window stopped separating them.
        maximum_bridge_steps=2, track_anchor_index=None, track_zone_period=2.0, branch_track={},
        ranking_cost_kind="counts", scoring_allowance_seconds=1.0,
        rival_band=(lambda g: rival))


# -- registration -----------------------------------------------------------------------------------

def test_the_generator_is_registered_and_dispatchable():
    """A generator absent from the registry never runs, and one absent from the dispatch tables makes
    an exhaustion proof cover an action class that was never enumerated."""
    assert RIVAL_GENERATOR in {spec.generator_id for spec in GENERATOR_REGISTRY}
    assert RIVAL_GENERATOR in acq.ACTION_GENERATORS
    assert RIVAL_GENERATOR in acq.ACTION_EVALUATORS


# -- the action AP6 was missing ---------------------------------------------------------------------

def test_a_blocked_saturated_point_yields_a_narrow_window_on_the_predicted_rival():
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=_objective(target_search_source="compiler")))

    assert len(result.actions) == 1
    intent = result.actions[0].intent
    assert intent.hkl == (2.9, 0.0, 0.0)
    assert intent.energy_lo < 7.115 < intent.energy_hi, "the window must contain the rival"
    assert intent.evidence_sink == AUXILIARY_TRACK_NODE


def test_the_window_is_narrow_rather_than_the_whole_identity_domain():
    """The point is to observe one predicted mode, not to re-survey the spectrum. A window as wide as
    the identity domain would cost a survey's beam time to answer a yes/no question."""
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=_objective(target_search_source="compiler")))
    intent = result.actions[0].intent

    assert (intent.energy_hi - intent.energy_lo) < 3.0


def test_a_wider_rival_prediction_gets_a_wider_window():
    """Resolution- and uncertainty-relative: a poorly localized rival needs more room, or the probe
    misses the mode it was fired to find and returns a false null."""
    objective = _objective(target_search_source="compiler")
    tight = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=objective, rival=(7.115, 0.1))).actions[0].intent
    loose = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=objective, rival=(7.115, 0.8))).actions[0].intent

    assert (loose.energy_hi - loose.energy_lo) > (tight.energy_hi - tight.energy_lo)


def test_a_broad_local_resolution_widens_the_window():
    """The window is resolution-relative to the *local* instrument resolution, not a fixed fallback.
    At a q where the resolution is broad the probe must open wide enough to catch the mode, or a valid
    rival is smeared below the window and reported as a false null."""
    objective = _objective(identity_search_meV=(-2.0, 20.0), identity_search_source="operator")
    tight_pred = (7.115, 0.05)   # sigma small so the resolution term dominates the half-width
    broad = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=objective, rival=tight_pred,
                 resolution_info={"fwhm_dE": 3.0})).actions[0].intent
    narrow = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=objective, rival=tight_pred,
                 resolution_info={"fwhm_dE": 0.3})).actions[0].intent

    assert (broad.energy_hi - broad.energy_lo) > (narrow.energy_hi - narrow.energy_lo)


def test_the_widened_compiler_domain_is_sized_by_the_rivals_own_resolution():
    """``identity_domain`` widens a compiler band by ``3 x fwhm_meV`` around the rival, so it has to be
    told the resolution *at the rival*. It was called before the local FWHM was resolved, spending the
    fixed default: at a q where the instrument is broad the authorised domain was too tight to hold the
    window the generator then built, and the probe's shoulders were clamped off."""
    objective = _objective(target_search_source="compiler")
    tight_pred = (7.115, 0.05)      # sigma small, so the resolution term decides both sizes
    broad = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=objective, rival=tight_pred,
                 resolution_info={"fwhm_dE": 2.0})).actions[0].intent
    narrow = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=objective, rival=tight_pred,
                 resolution_info={"fwhm_dE": 0.2})).actions[0].intent

    # 1.5 x fwhm each side, and the domain (target_hi=7.0 widened by 3 x fwhm) must be roomy enough to
    # leave that intact rather than clipping it back to the band edge.
    assert broad.energy_hi == pytest.approx(7.115 + 1.5 * 2.0, abs=1e-6)
    assert narrow.energy_hi == pytest.approx(7.115 + 1.5 * 0.2, abs=1e-6)


def test_the_declared_probe_centre_is_the_prediction_not_the_surviving_midpoint():
    """Realization persists this as the acquisition target energy. When the domain clips one shoulder
    the midpoint of the surviving bounds is not the rival, so the prediction has to travel explicitly."""
    objective = _objective(identity_search_meV=(-2.0, 7.115 + 0.3),
                           identity_search_source="operator")
    intent = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=objective)).actions[0].intent

    assert intent.window_center_meV == pytest.approx(7.115)
    assert 0.5 * (intent.energy_lo + intent.energy_hi) < intent.window_center_meV


def test_the_probe_centre_is_not_part_of_the_scientific_identity():
    """``energy_lo``/``energy_hi`` already carry the scientific window. Folding the centre into the
    fingerprint would re-mint every fingerprint ever computed — invalidating the frozen replay
    artifacts — without distinguishing any two actions the bounds do not already distinguish."""
    base = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=_objective(target_search_source="compiler"))
    ).actions[0].intent
    moved = replace(base, window_center_meV=(base.window_center_meV or 0.0) + 1.0)

    assert moved.scientific_action_fingerprint == base.scientific_action_fingerprint


# -- when it must NOT fire --------------------------------------------------------------------------

def test_a_point_whose_identity_is_clear_produces_nothing():
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point(clear=True)], objective=_objective(target_search_source="compiler")))
    assert result.actions == []


def test_a_point_where_counting_could_still_help_produces_nothing():
    """The guard against spending identity beam on a question the cheap path was about to answer."""
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point(saturated=False)],
                 objective=_objective(target_search_source="compiler")))
    assert result.actions == []


def test_no_predicted_rival_means_no_probe():
    """Nothing to look for. Firing a window at an unpredicted energy is a survey, not a discriminator."""
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=_objective(target_search_source="compiler"),
                 rival=None))
    assert result.actions == []


def test_an_operator_declared_band_blocks_the_probe_rather_than_scanning_past_it():
    """The scientist declared 7.0 meV. Reaching 7.115 would spend their beam outside the intent they
    declared, so the action is withheld — the campaign then terminates naming a forbidden action it
    could have taken, which is a reviewable statement rather than a silent overreach."""
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=_objective(target_search_source="operator")))
    assert result.actions == []


def test_a_legacy_objective_without_provenance_is_also_blocked():
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](_context([_blocked_point()]))
    assert result.actions == []


def test_a_declared_identity_domain_admits_the_probe():
    objective = _objective(identity_search_meV=(-2.0, 9.0), identity_search_source="operator")
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](_context([_blocked_point()], objective=objective))
    assert len(result.actions) == 1


def test_a_rival_outside_even_the_declared_identity_domain_is_not_probed():
    objective = _objective(identity_search_meV=(-2.0, 7.5), identity_search_source="operator")
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=objective, rival=(20.0, 0.2)))
    assert result.actions == []


# -- a withheld action must be visible ---------------------------------------------------------------

def test_a_forbidden_probe_is_reported_not_silently_skipped():
    """The whole point of refusing rather than reaching. If the refusal is invisible, an operator sees
    a campaign that stopped ambiguous and has no way to learn that their own declared band was the only
    thing between it and the answer — the one fact that would let them decide to widen it."""
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=_objective(target_search_source="operator")))

    assert result.actions == []
    assert result.reason_code and result.reason_code.startswith("policy_forbidden:identity_domain")
    assert "7.115" in result.reason_code, "name the rival that was not pursued"


def test_a_malformed_rival_prediction_is_a_diagnostic_not_routine_silence():
    """A predictor that answered with nonsense is not one that had nothing to say. If the two look
    identical, a broken predictor is indistinguishable from a settled frontier."""
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point()], objective=_objective(target_search_source="compiler"),
                 rival=(7.115,)))   # wrong arity: a (mean,) with no sigma
    assert result.actions == []
    assert result.reason_code and result.reason_code.startswith("invalid_rival_prediction")


def test_routine_silence_keeps_the_generic_reason():
    """The other half: a campaign with nothing to probe must not look policy-blocked."""
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context([_blocked_point(saturated=False)],
                 objective=_objective(target_search_source="operator")))
    assert result.actions == [] and result.reason_code is None


def test_the_forbidden_reason_reaches_the_inventory_report():
    """It has to survive the accounting layer, which otherwise derives its own reason from the counts
    and cannot tell a withheld action from an absent one."""
    context = _context([_blocked_point()], objective=_objective(target_search_source="operator"))
    _generated, reports = acq.generate_action_intents(context)
    report = next(r for r in reports if r.generator == RIVAL_GENERATOR)

    assert report.reason_code.startswith("policy_forbidden:identity_domain")


def test_a_generator_that_produced_an_action_does_not_also_report_a_refusal():
    """A frontier that was partly served is not a blocked one, and reporting both would read as
    though the campaign had been stopped by policy when it was not."""
    grid = [_blocked_point(index=18, hkl=(2.9, 0.0, 0.0)),
            _blocked_point(index=19, hkl=(2.95, 0.0, 0.0))]
    context = _context(grid, objective=_objective(identity_search_meV=(-2.0, 9.0),
                                                  identity_search_source="operator"))
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](context)
    assert result.actions and result.reason_code is None


# -- identity and accounting ------------------------------------------------------------------------

def test_two_blocked_points_yield_two_distinct_actions():
    grid = [_blocked_point(index=18, hkl=(2.9, 0.0, 0.0)),
            _blocked_point(index=19, hkl=(2.95, 0.0, 0.0))]
    result = acq.ACTION_GENERATORS[RIVAL_GENERATOR](
        _context(grid, objective=_objective(target_search_source="compiler")))

    fingerprints = {a.intent.scientific_action_fingerprint for a in result.actions}
    assert len(result.actions) == 2 and len(fingerprints) == 2


def test_the_probe_is_not_the_same_action_as_a_target_scan_at_that_point():
    """It must not dedupe against the ordinary window: different sink, different window, and it is the
    whole reason the point is no longer stuck."""
    point = _blocked_point()
    objective = _objective(target_search_source="compiler")
    probe = acq.ACTION_GENERATORS[RIVAL_GENERATOR](_context([point], objective=objective)).actions[0].intent
    target = acq._intent_from_pick(objective, point, "production", acq.ProposalScore(method="counts"))

    assert probe.scientific_action_fingerprint != target.scientific_action_fingerprint
