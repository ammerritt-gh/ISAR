"""O44 — an acoustic branch is exempt from precision coverage *at* the reduced-q zone centre.

Two halves, and the PR #9 review found both broken:

* the exemption must be **published only where the physics holds**.  ``anchor_index`` returns the
  *nearest* path point to the zone centre, with no test that the winner is actually there, and
  falls back to path index 0 whenever the period is absent, non-finite or non-positive — which the
  driver reaches whenever ``[dispersion] zone_period`` is missing from the hand-edited
  ``card.policy.toml``.  Publishing that as ``reduced_q_origin_index`` exempted a measurable q
  (reduced q=0.05 predicts ω ≈ 0.6 meV, resolvable at FWHM 0.3 and 0.5), and on a descending path
  with no period it exempted the point *farthest* from the zone centre.
* the exemption must be **honoured on every acquisition path**.  ``precision_coverage_eligible``
  gated the general ranking path but neither the tracked-branch frontier walk nor the
  rival-reconnect picker, and because O44 makes ``GridQ.visited`` return True for the exempt origin
  the frontier walk skipped its cheap quicklook rung and spent *production* scans there.
"""
from __future__ import annotations

import math

from isar.drive.supervisor.stopping import GridQ
from isar.session.branch_tracking import track_path


HYPOTHESES = [
    {"name": "optic", "zc_energy": {"mean": 6.0, "sigma": 1.0},
     "slope_zc": {"mean": 0.0, "sigma": 8.0}},
    {"name": "acoustic", "zc_energy": {"mean": 0.0, "sigma": 0.2},
     "slope_zc": {"mean": 12.0, "sigma": 12.0}},
]


def _candidate(ref, energy, error=0.12):
    return {"candidate_ref": ref, "energy_meV": energy, "omega0_err": error,
            "sigma_meV": error, "gamma": 0.4, "amplitude": 1.0, "reliable": True}


def _acoustic_track(path, period):
    """Track an acoustic branch over ``path`` with one clean acoustic candidate per point."""
    observations = [{"gauss_fwhm": 0.3,
                     "candidates": [_candidate(f"a{index}", 12.0 * abs(point[0] - 2.0))]}
                    for index, point in enumerate(path)]
    return track_path(path, observations, period=period, hypotheses=HYPOTHESES,
                      target_branch="acoustic", assignment_entropy_stop_nats=1.0)


# ------------------------------------------------------- which q the tracker may declare exempt
def test_an_exact_zone_centre_on_the_path_is_published_as_the_origin():
    path = [[2.0, 0.0, 0.0], [2.05, 0.0, 0.0], [2.1, 0.0, 0.0], [2.15, 0.0, 0.0]]

    track = _acoustic_track(path, period=2.0)

    assert track["reduced_q_origin_index"] == 0


def test_a_path_that_never_reaches_the_zone_centre_exempts_nothing():
    """Nearest is not *at*: reduced q=0.05 carries a measurable acoustic ω, so exempting it
    discards beam time's worth of real signal rather than saving it."""
    path = [[2.05, 0.0, 0.0], [2.1, 0.0, 0.0], [2.15, 0.0, 0.0]]

    track = _acoustic_track(path, period=2.0)

    assert track["reduced_q_origin_index"] is None
    assert track["inferred_endpoint_indices"] == []


def test_an_absent_or_unusable_period_exempts_nothing():
    """``_dispersion_period`` returns None whenever ``[dispersion] zone_period`` is absent from the
    hand-edited card, and the driver passes that straight through.  Without a period there is no
    reduced-zone coordinate at all, so no point can be shown to sit at the zone centre."""
    path = [[2.05, 0.0, 0.0], [2.1, 0.0, 0.0], [2.15, 0.0, 0.0]]

    for period in (None, 0.0, float("nan")):
        track = _acoustic_track(path, period=period)
        assert track["reduced_q_origin_index"] is None, period


def test_a_descending_path_exempts_the_zone_centre_and_never_the_far_end():
    """Path order is not physics.  With no period the old fallback picked index 0, which on a
    descending path is the point *farthest* from the zone centre — the most measurable q there is."""
    path = [[2.15, 0.0, 0.0], [2.1, 0.0, 0.0], [2.05, 0.0, 0.0], [2.0, 0.0, 0.0]]

    assert _acoustic_track(path, period=2.0)["reduced_q_origin_index"] == 3
    assert _acoustic_track(path, period=None)["reduced_q_origin_index"] is None


def test_an_optic_branch_at_the_exact_zone_centre_stays_measurable():
    """An optic branch has a finite ω at the zone centre.  The exemption keys on the *branch*, not
    on the geometry, or an optic campaign would stop demanding the one q it most wants."""
    path = [[2.0, 0.0, 0.0], [2.05, 0.0, 0.0], [2.1, 0.0, 0.0]]
    observations = [{"gauss_fwhm": 0.3, "candidates": [_candidate(f"o{index}", 6.0)]}
                    for index in range(len(path))]

    track = track_path(path, observations, period=2.0, hypotheses=HYPOTHESES,
                       target_branch="optic", assignment_entropy_stop_nats=1.0)

    assert track["reduced_q_origin_index"] == 0
    origin = GridQ(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5,
                   acoustic_target=False, is_reduced_q_origin=True, track_state="confirmed")
    assert origin.precision_coverage_eligible is True


# ------------------------------------------------------- which q acquisition may name in an action
def _exempt_origin(**overrides) -> GridQ:
    """An acoustic reduced-q origin, as ``summarize_grid`` builds it from the tracker geometry."""
    fields = dict(index=0, hkl=(2.0, 0.0, 0.0), q=0.0, tolerance=0.5,
                  acoustic_target=True, is_reduced_q_origin=True, track_state="ambiguous")
    fields.update(overrides)
    point = GridQ(**fields)
    assert point.precision_coverage_eligible is False       # the premise of every test below
    return point


def test_the_origin_tolerance_is_a_floating_point_tolerance_not_a_neighbourhood():
    """O44's physics is "at the zone centre", exactly.  Widening the exemption to *near* the origin
    is a separate, priced decision — this pins the tolerance so it cannot drift into one."""
    from isar.session import branch_tracking

    assert math.isclose(branch_tracking._ORIGIN_TOL, 1e-9, rel_tol=0.0, abs_tol=1e-12)
