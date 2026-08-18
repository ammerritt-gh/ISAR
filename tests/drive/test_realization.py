"""The discovery realization defaults (SCENE-4.3 C5): the energy grid rule and its refusals.

Pure unit scope on purpose — the grid rule is the one place in the discovery lane where a
mis-rounded number silently changes what was measured, and it is cheap enough to pin exhaustively.
"""
from __future__ import annotations

import pytest

from isar.schema.action_identity import MeasurementIntent
from isar.drive.steering.realization import (
    DiscoveryWindowUnrealizable, aligned_window, proposal_from_intent, snap_energy_step)


# ---------------------------------------------------------------------------- the step
@pytest.mark.parametrize("fwhm, expected", [
    (0.9, 0.5),        # the owner's worked examples
    (0.3, 0.25),
    (1.7, 1.0),
    (0.2, 0.2),        # exactly on a grid value keeps it
    (1.0, 1.0),
    (2.6, 2.5),        # 0.25 x 10^1 — the grid repeats every decade
    (0.05, 0.05),
    (0.021, 0.02),
    (12.0, 10.0),
    (0.7, 0.5),
])
def test_the_step_snaps_to_the_largest_nice_number_at_or_below_the_fwhm(fwhm, expected):
    assert snap_energy_step(fwhm) == pytest.approx(expected)


def test_the_step_rule_is_monotonic_in_the_resolution():
    """A coarser instrument never gets a finer step. A max over a fixed candidate set gives this
    for free; the test exists because the obvious alternative implementations (decade arithmetic
    with a rounding fudge) do not."""
    steps = [snap_energy_step(0.05 + 0.013 * i) for i in range(200)]
    assert steps == sorted(steps)


def test_a_step_never_exceeds_the_resolution_it_was_sized_from():
    for i in range(400):
        width = 0.01 + 0.037 * i
        assert snap_energy_step(width) <= width + 1e-12


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_a_nonsense_resolution_refuses_rather_than_picking_a_step(bad):
    with pytest.raises(ValueError):
        snap_energy_step(bad)


# ---------------------------------------------------------------------------- the grid
def test_a_non_integer_window_still_yields_integer_aligned_points():
    """The owner's worked example: [0.37, 2.9] at 0.25 realizes 0.50 ... 2.75, through 1.0 and 2.0.

    Prevents: anchoring the grid at ``e_lo``, which would put every point at an arbitrary phase
    (0.37, 0.62, 0.87, ...) and never touch an integer energy.
    """
    first, last, n_points = aligned_window(0.37, 2.9, 0.25)
    assert (first, last, n_points) == (0.5, 2.75, 10)
    points = [round(first + i * 0.25, 9) for i in range(n_points)]
    assert 1.0 in points and 2.0 in points
    assert all(abs(p / 0.25 - round(p / 0.25)) < 1e-9 for p in points)


def test_the_realized_endpoints_are_the_clipped_aligned_points_not_the_window_bounds():
    assert aligned_window(-3.3, 7.1, 1.0) == (-3.0, 7.0, 11)
    assert aligned_window(0.05, 0.95, 0.2) == (0.2, 0.8, 4)


def test_an_endpoint_that_is_aligned_up_to_floating_error_is_kept():
    """0.1 + 0.2 is not 0.3, and a bare floor/ceil would drop that last point."""
    e_hi = 0.1 + 0.2
    assert e_hi != 0.3
    assert aligned_window(0.0, e_hi, 0.1) == (0.0, 0.3, 4)
    # And the same at the low end: an ``e_lo`` a hair BELOW an aligned value must not silently
    # gain a point outside the window, nor lose the aligned one.
    assert aligned_window(0.1 + 0.2, 1.0, 0.1) == (0.3, 1.0, 8)


def test_a_window_with_fewer_than_two_aligned_points_is_refused_never_widened():
    """The honest choice (reported with C5): a window holding no scan is refused, not widened.

    Widening to the nearest aligned point OUTSIDE the window would measure energies the question
    did not ask about; and a one-point scan is not a scan anyway — the acquisition layer calls it
    ``degenerate_window`` and TAVI refuses the command outright.
    """
    with pytest.raises(DiscoveryWindowUnrealizable) as excinfo:
        aligned_window(0.31, 0.44, 1.0)                       # no multiple of 1.0 inside
    assert "not widened" in str(excinfo.value)
    with pytest.raises(DiscoveryWindowUnrealizable):
        aligned_window(0.9, 1.4, 1.0)                         # exactly one aligned point (1.0)


# ---------------------------------------------------------------------------- the proposal
def _intent(lo=0.37, hi=2.9, hkl=(2.5, 0.0, 0.0), hypotheses_fingerprint=None):
    """The declared physics rides on the INTENT (review finding 2026-08-18) — the generator puts it
    there before dedup and scheduling, and ``proposal_from_intent`` only verifies it."""
    return MeasurementIntent(action_class="discovery_survey", hkl=hkl, energy_lo=lo, energy_hi=hi,
                             window_family="discovery",
                             hypotheses_fingerprint=hypotheses_fingerprint)


def test_the_proposal_carries_the_snapped_step_the_aligned_window_and_the_given_exposure():
    proposal = proposal_from_intent(
        _intent(hypotheses_fingerprint="hf"), fwhm_meV=0.3,
        provenance={"topology_role": "canonical_path"},
        hypotheses_fingerprint="hf", reason="discovery: step-1", ncount=int(1e8))

    assert (proposal.e_lo, proposal.e_hi, proposal.e_step) == (0.5, 2.75, 0.25)
    assert proposal.n_points == 10
    # C10: the exposure is the caller's — the step's frozen discovery_monitor_ncount — and this
    # module holds no default of its own to fall back to.
    assert proposal.ncount == 100_000_000
    assert proposal.kind == "quicklook"
    assert proposal.scan_command() == "deltaE 0.5 2.75 0.25"
    assert proposal.meta["topology_role"] == "canonical_path"
    assert proposal.meta["hypotheses_fingerprint"] == "hf"
    assert proposal.meta["scientific_action_fingerprint"] == \
        _intent(hypotheses_fingerprint="hf").scientific_action_fingerprint
    # The hypothesis set is an INPUT to scientific identity, so an intent built without it is a
    # different action, not the same one missing a field.
    assert proposal.intent.hypotheses_fingerprint == "hf"
    # The request fingerprint is stamped by ``_finalize_proposal`` AFTER the instrument clamps,
    # never here — so it can never describe a window that was not the one submitted.
    assert "proposal_fingerprint" not in proposal.meta


def test_an_intent_with_no_energy_window_is_refused():
    intent = MeasurementIntent(action_class="discovery_survey", hkl=(2.0, 0.0, 0.0),
                               energy_lo=None, energy_hi=None, window_family="discovery")
    with pytest.raises(DiscoveryWindowUnrealizable):
        proposal_from_intent(intent, fwhm_meV=0.5, provenance={}, hypotheses_fingerprint=None,
                             reason="r", ncount=int(1e8))


def test_a_request_built_under_different_physics_than_its_decision_refuses():
    """The declared hypothesis set is an INPUT to ``scientific_fingerprint``, so it has to be on
    the intent before dedup and scheduling — this module only VERIFIES it.

    Introducing it here instead meant the inventory, the scientific identity, the physical retry
    key, the decision basis and the scheduler all described an action WITHOUT the physics that
    would interpret its measurement, and a card edit could leave a persisted decision
    byte-identical while changing what the scan means. The check makes the disagreement loud
    instead of submitting a scan whose declared physics is not the physics it was selected under.
    """
    with pytest.raises(DiscoveryWindowUnrealizable, match="not the physics its decision"):
        proposal_from_intent(_intent(hypotheses_fingerprint="declared-at-selection"),
                             fwhm_meV=0.3, provenance={},
                             hypotheses_fingerprint="edited-since",
                             reason="discovery: step-1", ncount=int(1e8))
