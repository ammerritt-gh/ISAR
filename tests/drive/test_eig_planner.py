"""Behaviour gates for the EIG scan planner and the offline campaign it drives.

These are the checks that fail if the planner stops being an information-per-neutron planner: the
prize measure, the cost ranking, and one end-to-end campaign against the analytic truth.
"""
from __future__ import annotations

import importlib.util
import math
import sys
from pathlib import Path

import numpy as np
import pytest

from isar.analyze.truth import _fold
from isar.drive.acquisition.eig_planner import (
    NULL_MISS_PROBABILITY, PRESENCE_PRIOR_ODDS, CONFIRMED_PROBABILITY, Candidate, ExecutedScan, PlannerInput, _admitted_anchor, _deficit,
    _deficits_batch, _pad_candidates, default_intensity_scale, hdi_halfwidth, miss_factor,
    pooled_delivery,
    plan_next, point_presence, target_posterior,
)
from isar.schema.hypotheses import preset

BRANCHES = ("acoustic", "optic")
DOMAIN = (-9.0, 9.0)
WIDTH = DOMAIN[1] - max(DOMAIN[0], 0.0)
TOLERANCE = 0.05


def belief_row(name: str, anchors=()) -> dict:
    h = preset(name, DOMAIN).to_dict()
    return {"branch_id": name, "anchors": [dict(a) for a in anchors], "n_anchors": len(anchors),
            "period": 2.0,
            "parameter_prior": {"zc_energy": h["zc_energy"], "slope_zc": h["slope_zc"],
                                "confidence": h["confidence"]}}


def planner_input(hs, *, targets=("acoustic",), beliefs=None, measured=None) -> PlannerInput:
    return PlannerInput(
        path=tuple((h, 0.0, 0.0) for h in hs), u=tuple(abs(_fold(h, 2.0)) for h in hs),
        targets=targets, beliefs=beliefs or {b: belief_row(b) for b in BRANCHES},
        measured=measured or {}, energy_domain_meV=DOMAIN,
        fwhm_meV=tuple(0.8 for _ in hs), temperature_K=200.0,
        scale=default_intensity_scale(BRANCHES), tolerance_meV=TOLERANCE)


def deficit_of(cands, mus, sigs, target_index=0, presence=None) -> tuple[float, float]:
    hw, _identified, deficit = _deficits_batch(
        _pad_candidates([tuple(cands)]), [[m] for m in mus], [[s] for s in sigs], target_index,
        WIDTH, TOLERANCE, [False], presence=None if presence is None else np.array([presence]))
    return float(hw[0]), float(deficit[0])


# ------------------------------------------------------------------ the prize measure

def test_a_sharp_bimodal_posterior_fails_the_bar_however_narrow_each_mode_is():
    """Two rival peaks 2 meV apart, equally credible: the energy is NOT known to +-0.05 meV."""
    cands = [Candidate(4.0, 0.02), Candidate(6.0, 0.02)]
    hw, deficit = deficit_of(cands, [5.0, 40.0], [3.0, 3.0])
    assert hw > 0.9        # both modes are in the interval, so it spans both
    assert deficit > 0.0


def test_one_sharp_candidate_under_a_tight_belief_passes_despite_non_zero_none_mass():
    cands = [Candidate(4.0, 0.01)]
    hw, deficit = deficit_of(cands, [4.0, 40.0], [0.3, 3.0])
    assert hw < TOLERANCE
    assert deficit == 0.0


def test_completion_needs_identity_as_well_as_width():
    """A 0.02 meV posterior on a peak that is only 70% likely to BE the target is not the answer
    the scientist asked for; the same 0.85 the anchor gate uses means the same thing here."""
    narrow = float(np.log(0.02 / TOLERANCE))            # width front alone: already satisfied
    assert narrow < 0.0
    assert _deficit(np.array([0.02]), np.array([0.70]), TOLERANCE)[0] > 0.0
    assert _deficit(np.array([0.02]), np.array([0.95]), TOLERANCE)[0] == 0.0
    # and the identity front alone can hold a point open however narrow the peak is
    assert _deficit(np.array([1e-6]), np.array([0.70]), TOLERANCE)[0] == pytest.approx(
        float(np.log(CONFIRMED_PROBABILITY / 0.70)))


def test_an_unmeasured_point_is_half_the_positive_domain_wide():
    hw, deficit = deficit_of([], [4.0, 8.0], [0.3, 0.3])
    assert hw == pytest.approx(0.5 * WIDTH)
    assert deficit > 0.0


@pytest.mark.parametrize("energies,sigmas,weights,w_none", [
    ([3.0], [0.20], [0.97], 0.03),
    ([3.0, 5.5], [0.10, 0.30], [0.55, 0.40], 0.05),
])
def test_level_set_halfwidth_matches_a_grid_scan_of_the_same_mixture(energies, sigmas, weights,
                                                                    w_none):
    """The closed-form level set is checked against the textbook 2000-point HDI scan."""
    grid = np.linspace(0.0, WIDTH, 2001)
    step = grid[1] - grid[0]
    density = np.full_like(grid, w_none / WIDTH)
    for e, s, w in zip(energies, sigmas, weights):
        density += w * np.exp(-0.5 * ((grid - e) / s) ** 2) / (s * math.sqrt(2.0 * math.pi))
    order = np.sort(density)[::-1]
    cells = int(np.searchsorted(np.cumsum(order) * step, 0.6827) + 1)
    inside = grid[density >= order[cells - 1]]
    reference = 0.5 * (inside.max() - inside.min())
    got = float(hdi_halfwidth(np.array([energies]), np.array([sigmas]), np.array([weights]),
                              np.array([w_none]), WIDTH)[0])
    assert got == pytest.approx(reference, rel=0.15, abs=2.0 * step)


def test_the_target_posterior_is_a_proper_distribution_over_which_peak_is_the_target():
    e = np.array([[1.0, 6.0]])
    s = np.array([[0.03, 0.03]])
    w, w_none = target_posterior(e, s, np.ones(e.shape, dtype=bool),
                                 [np.array([1.0]), np.array([6.0])],
                                 [np.array([0.3]), np.array([0.3])], 0)
    assert float(w.sum() + w_none.sum()) == pytest.approx(1.0)
    assert w[0, 0] > 0.95 and w[0, 1] < 0.05


@pytest.mark.parametrize("mu,sigma", [(0.0, 0.2), (4.5, 2.6), (110.0, 83.0), (-5.0, 2.0),
                                      (4.5, 0.01), (0.0, 18.0)])
def test_band_truncated_moments_match_scipy(mu, sigma):
    """The class prior's third clause: a branch energy is bounded by the authorized band, so the
    predictive is truncated at BOTH ends.  Closed form here, checked against the reference."""
    from scipy.stats import truncnorm

    from isar.probability import truncated_normal_moments

    lo, hi = 0.0, 9.0
    mean, var = truncnorm.stats((lo - mu) / sigma, (hi - mu) / sigma, loc=mu, scale=sigma,
                                moments="mv")
    got = truncated_normal_moments(mu, sigma, lo, hi)
    assert got[0] == pytest.approx(float(mean))
    assert got[1] == pytest.approx(math.sqrt(float(var)))
    assert lo <= got[0] <= hi


def test_a_cold_branch_far_from_the_fold_predicts_somewhere_in_the_band():
    from isar.drive.acquisition.classify_eig import belief_predict

    mu, sigma = belief_predict(belief_row("optic"), 1.0, domain_hi=DOMAIN[1])
    assert mu == pytest.approx(0.5 * DOMAIN[1], abs=0.1)          # hi / 2
    assert sigma == pytest.approx(DOMAIN[1] / math.sqrt(12.0), abs=0.1)
    assert belief_predict(belief_row("optic"), 1.0)[0] > 60.0     # untruncated, for contrast


def test_a_dispersion_is_continuous_in_q():
    """The owner's clause: "from q to q, the change in energy reduces to zero as deltaQ becomes 0".
    These are the anchors of the run that stalled at 17/20, where the acoustic extrapolated its
    fitted slope onto the optic's peak and the optic, on one anchor, was pulled onto the
    acoustic's -- crossed beliefs, identified mass 0.00, unresolvable by any measurement."""
    from isar.drive.acquisition.classify_eig import belief_predict

    acoustic = belief_row("acoustic", [{"u": u, "omega0": w, "weight": 1.0} for u, w in
                                       ((0.1, 0.93), (0.3, 2.74), (0.45, 3.93), (0.6, 4.81),
                                        (0.7, 5.32))])
    optic = belief_row("optic", [{"u": 0.7, "omega0": 7.68, "weight": 1.0}])

    assert belief_predict(acoustic, 0.90, domain_hi=DOMAIN[1])[0] == pytest.approx(5.93, abs=0.3)
    assert belief_predict(acoustic, 1.00, domain_hi=DOMAIN[1])[0] == pytest.approx(6.00, abs=0.3)
    assert belief_predict(optic, 1.00, domain_hi=DOMAIN[1])[0] >= 7.0
    # and the two no longer cross anywhere along the boundary
    for u in (0.80, 0.85, 0.90, 0.95, 1.00):
        assert (belief_predict(optic, u, domain_hi=DOMAIN[1])[0]
                > belief_predict(acoustic, u, domain_hi=DOMAIN[1])[0])


def test_continuity_bounds_the_change_and_vanishes_at_an_anchor():
    from isar.probability import CONTINUITY_LENGTH, MAX_GROUP_VELOCITY, continuity_adjust

    # at an anchor: no distance, no reversion, the fit stands
    assert continuity_adjust(5.0, 0.2, 4.0, 0.3, 0.0, 0.0) == (5.0, 0.2)
    # beyond the anchors the mean reverts toward the anchored level over CONTINUITY_LENGTH
    mu, _ = continuity_adjust(9.0, 0.3, 6.0, 0.3, CONTINUITY_LENGTH, 0.1)
    assert 6.0 < mu < 9.0 and mu == pytest.approx(6.0 + 3.0 * math.exp(-1.0))
    # the sigma is a bound: a line whose own sigma diverges is not a wider belief
    _mu, sigma = continuity_adjust(9.0, 9.3, 6.0, 0.3, 0.3, 0.3)
    assert sigma == pytest.approx(math.hypot(0.3, MAX_GROUP_VELOCITY * 0.3))
    # where the fit is genuinely tighter than the bound, the fit wins
    assert continuity_adjust(5.0, 0.05, 5.0, 0.3, 0.0, 0.01)[1] == 0.05


# ------------------------------------------------------------------ ranking behaviour

def test_the_cold_start_scan_is_where_the_gapless_prior_is_tightest_and_is_cheap():
    """No placement rule: the acoustic prior is narrowest just off the zone centre, so that is
    where one scan buys the most per neutron.  The exact zone centre is exempt (energy fixed at 0)."""
    hs = [2.0 + 0.05 * i for i in range(21)]
    decision = plan_next(planner_input(hs))
    assert not decision.stop
    chosen = decision.chosen
    u = abs(_fold(chosen.hkl[0], 2.0))
    assert 0.0 < u <= 0.30
    assert chosen.n_points <= 40
    assert decision.per_point[0].exempt and decision.per_point[0].within_bar


def test_a_point_already_within_the_bar_is_worth_nothing_more():
    hs = [2.2, 2.4]
    anchors = [{"u": 0.15, "omega0": 1.40, "weight": 1.0}, {"u": 0.20, "omega0": 1.85, "weight": 1.0},
               {"u": 0.25, "omega0": 2.30, "weight": 1.0}, {"u": 0.30, "omega0": 2.72, "weight": 1.0}]
    beliefs = {"acoustic": belief_row("acoustic", anchors), "optic": belief_row("optic")}
    measured = {0: (Candidate(1.85, 0.01),)}
    decision = plan_next(planner_input(hs, beliefs=beliefs, measured=measured))
    assert decision.per_point[0].within_bar
    assert not decision.per_point[1].within_bar
    assert all(d.grid_index == 1 for d in decision.ranked)


def test_two_grid_points_with_identical_rates_score_identically():
    hs = [2.3, 1.7]           # |fold| is 0.3 for both, so every input is bit-identical
    decision = plan_next(planner_input(hs))
    best = {}
    for design in decision.ranked:
        best.setdefault(design.grid_index, design.score)
    assert len(best) == 2
    assert best[0] == best[1]


def test_malformed_input_is_refused():
    with pytest.raises(ValueError):
        plan_next(planner_input([2.1], targets=("phonon",)))
    with pytest.raises(ValueError):
        plan_next(PlannerInput(**{**planner_input([2.1]).__dict__, "tolerance_meV": 0.0}))


def test_a_scan_that_saw_only_the_acoustic_argues_against_the_optic_in_its_window():
    """Negative evidence is per branch: the scan delivered the acoustic, so it is evidence against
    the OPTIC lying inside the same window, and against nothing about the acoustic."""
    from isar.drive.acquisition.eig_planner import ExecutedScan, _Predictor, _scenarios

    hs = [2.5]
    window = ExecutedScan(3.0, 7.0, 40_000_000)           # bright enough to see the optic
    # What the master delivered, and WHERE, is read off the pooled fit's own candidates.
    delivered = {0: (Candidate(5.70, 0.02, {"acoustic": 1.0}),)}
    base = planner_input(hs, targets=("optic",))
    seen_input = PlannerInput(**{**base.__dict__, "scans": {0: (window,)},
                                 "measured": delivered})
    branches = tuple(sorted(BRANCHES))
    optic, acoustic = branches.index("optic"), branches.index("acoustic")

    def mass_inside(inp):
        rows = {b: dict(inp.beliefs[b]) for b in branches}
        om, wt, _tr, _marg = _scenarios(0, inp, branches, _Predictor(rows, DOMAIN[1]),
                               {k: tuple(v) for k, v in inp.scans.items()},
                               inp.measured.get(0, ()))
        return {b: float(wt[(om[:, i] >= window.e_lo) & (om[:, i] <= window.e_hi)].sum())
                for b, i in (("optic", optic), ("acoustic", acoustic))}

    before, after = mass_inside(base), mass_inside(seen_input)
    assert after["optic"] < 0.5 * before["optic"]          # contradicted where it was not found
    assert after["acoustic"] == pytest.approx(before["acoustic"])   # found there: no evidence


def test_the_anchor_a_design_leaves_is_one_its_own_outcomes_produce():
    """After negative evidence removes the middle of a branch's set, a window over one lump must
    not be credited with an anchor in the contradicted gap -- an energy no outcome delivers."""
    energies = np.array([1.0, 1.1, 1.2, 5.0, 5.1, 5.2])
    weights = np.array([0.05, 0.05, 0.05, 0.30, 0.30, 0.25])   # heavier lump at ~5.1
    energy, mass = _admitted_anchor(energies, weights, sres=0.34)
    assert 5.0 <= energy <= 5.2                     # inside the lump, not the 1..5 gap
    assert mass == pytest.approx(0.85)
    # a design whose outcomes never admit leaves no anchor at all
    assert _admitted_anchor(energies, np.zeros_like(weights), sres=0.34) == (0.0, 0.0)


def test_five_hundred_identical_nulls_stay_finite():
    """Repeated identical scans are independent observations and are not deduplicated, so the
    weights are accumulated in log space; a linear product would underflow to a NaN posterior."""
    from isar.drive.acquisition.eig_planner import _Predictor, _scenarios

    hs = [2.5]
    null = ExecutedScan(0.0, 9.0, 5_000_000)
    base = planner_input(hs, targets=("acoustic",))
    inp = PlannerInput(**{**base.__dict__, "scans": {0: tuple([null] * 500)}})
    branches = tuple(sorted(BRANCHES))
    rows = {b: dict(inp.beliefs[b]) for b in branches}
    _om, wt, _tr, _marg = _scenarios(0, inp, branches, _Predictor(rows, DOMAIN[1]),
                                     {k: tuple(v) for k, v in inp.scans.items()})
    assert np.all(np.isfinite(wt))
    assert float(wt.sum()) == pytest.approx(1.0)


def test_a_scan_records_only_what_it_asked_of_the_instrument():
    """Delivery is a property of the pooled master, so it is not carried on a single scan."""
    scan = ExecutedScan(3.0, 7.0, 10_000_000)
    assert (scan.e_lo, scan.e_hi, scan.ncount) == (3.0, 7.0, 10_000_000)
    assert not hasattr(scan, "seen") and not hasattr(scan, "probability")
    assert hash(scan) == hash(ExecutedScan(3.0, 7.0, 10_000_000))

def test_a_rival_looked_for_and_not_found_loses_its_claim():
    """The missing half of "a null is evidence".  A narrow acoustic candidate under a broad,
    unanchored optic is NOT the answer -- the optic could be that peak.  A master deep enough to
    have detected the optic there, whose fit delivers only the acoustic, settles it, without
    measuring the acoustic again."""
    cands = [Candidate(5.70, 0.030, {"acoustic": 1.0})]
    mus, sigs = [5.71, 5.50], [0.30, 1.00]              # a broad optic sitting over the same peak
    _hw, deficit = deficit_of(cands, mus, sigs)
    assert deficit > 0.0                                # width is fine; identity is not

    looked = ExecutedScan(4.0, 7.4, 40_000_000)
    presence = point_presence(("acoustic", "optic"), mus, (looked,) * 3, step=0.2, fwhm_meV=0.8,
                              temperature_K=200.0, scale=default_intensity_scale(BRANCHES),
                              candidates=cands)
    assert presence[0] > 0.98                           # the master delivers it
    assert presence[1] < 0.55                           # looked into, never delivered
    _hw, after = deficit_of(cands, mus, sigs, presence=presence)
    assert after == 0.0


def test_presence_is_one_pooled_question_asked_once():
    """Detectability is a property of the pooled master, backwards as well as forwards.  A stack of
    scans too small to have seen the branch alone still constitutes a look once their counts are in
    one master -- the fault that stalled the optic campaign -- and, being one look, the odds move
    once however many scans built the pool."""
    branches = ("acoustic",)
    kw = dict(step=0.2, fwhm_meV=0.8, temperature_K=200.0,
              scale=default_intensity_scale(branches))
    delivered = [Candidate(3.0, 0.02, {"acoustic": 1.0})]
    solo = ExecutedScan(0.0, 9.0, 300_000)   # 0.46 sigma alone; fifty of them pool to 3.3
    deep = ExecutedScan(0.0, 9.0, 40_000_000)

    unexamined = float(point_presence(branches, [3.0], (), **kw)[0])
    one_small = float(point_presence(branches, [3.0], (solo,), **kw)[0])
    pooled = float(point_presence(branches, [3.0], (solo,) * 50, **kw)[0])
    seen = float(point_presence(branches, [3.0], (deep,), candidates=delivered, **kw)[0])

    assert unexamined == pytest.approx(PRESENCE_PRIOR_ODDS / (1.0 + PRESENCE_PRIOR_ODDS))
    assert one_small == pytest.approx(unexamined)   # below the floor alone: no evidence either way
    assert pooled < unexamined                      # the same scans, pooled, ARE a look
    assert seen > unexamined                        # and a master that delivers it argues for it
    # one pooled fit is one observation, so a deeper pool of the same silence does not compound it
    assert float(point_presence(branches, [3.0], (deep,) * 5, **kw)[0]) == pytest.approx(pooled)


def test_a_scan_measures_a_branch_only_when_the_fitter_would_see_it():
    """The planner mirrors ``isar.analyze.verdict.detection_power``: expected significance
    ``A / sqrt(A + B)`` on the peak HEIGHT over the flat background, against DETECT_FLOOR.  Its
    previous gate -- 5 integrated counts anywhere in the window -- said the counts sufficed where
    the real fitter then abstained, and the planner re-bought the identical design."""
    from isar.drive.acquisition.eig_planner import DETECT_FLOOR, detection_significance

    kw = dict(e_lo=4.0, e_step=0.2, n_points=25, fwhm_meV=0.8, temperature_K=200.0,
              branches=BRANCHES, scale=default_intensity_scale(BRANCHES))
    faint = detection_significance([1.0, 6.0], ncount=2_000_000, **kw)[1]
    bright = detection_significance([1.0, 6.0], ncount=200_000_000, **kw)[1]
    assert faint < DETECT_FLOOR < bright                 # the same peak, two exposures
    # significance grows as sqrt(exposure) once the peak dominates its background
    assert bright / faint == pytest.approx(10.0, rel=0.25)
    # a branch whose peak is outside the window is never measured, however long the count
    assert detection_significance([1.0, 6.0], ncount=200_000_000, **kw)[0] < DETECT_FLOOR


def test_detectability_is_a_property_of_the_pooled_master():
    """ISAR pools every scan at a q into a master and fits the MASTER, so the forward question --
    can this design deliver a fitted peak -- is asked of what is banked here plus what the design
    adds.  Both directions: a top-up that finishes an almost-there point is admitted, and the same
    small scan at an untouched point is still refused."""
    from isar.drive.acquisition.eig_planner import (DETECT_FLOOR, banked_exposure,
                                                    detection_significance)

    kw = dict(e_lo=4.0, e_step=0.2, n_points=25, fwhm_meV=0.8, temperature_K=200.0,
              branches=BRANCHES, scale=default_intensity_scale(BRANCHES))
    top_up = 5_000_000
    banked = (ExecutedScan(4.0, 9.0, 20_000_000), ExecutedScan(4.0, 9.0, 20_000_000))

    # only scans whose window covers the peak count toward its master
    assert banked_exposure(banked, 6.0) == pytest.approx(4.0e7)
    assert banked_exposure(banked, 1.0) == 0.0
    assert banked_exposure((), 6.0) == 0.0

    alone = detection_significance([1.0, 6.0], ncount=top_up, **kw)[1]
    pooled = detection_significance([1.0, 6.0],
                                    ncount=top_up + banked_exposure(banked, 6.0), **kw)[1]
    assert alone < DETECT_FLOOR                      # too small a first scan: still refused
    assert pooled >= DETECT_FLOOR                    # the same scan on a pool: finishes the point


def test_the_miss_factor_is_soft_and_the_master_says_what_it_delivered_and_where():
    """One factor, read by the scenario weights and by the point's presence alike, and the delivery
    probability behind it is the pooled master's.  Presence asks WHETHER the branch is here at all,
    so it reads the best claim any peak has; the scenario weights ask whether it is here AT an
    energy, so they read only the peaks near it -- a distant part-claimed peak must not protect a
    band the master looked into and found empty."""
    assert miss_factor(1.0) == 1.0
    assert miss_factor(0.0) == pytest.approx(NULL_MISS_PROBABILITY)
    assert miss_factor(0.5) == pytest.approx(0.5 + 0.5 * NULL_MISS_PROBABILITY)
    cands = (Candidate(6.0, 0.02, {"optic": 0.5, "acoustic": 0.4}),
             Candidate(1.0, 0.02, {"acoustic": 1.0}))
    assert pooled_delivery(cands, "optic") == pytest.approx(0.5)
    assert pooled_delivery(cands, "acoustic") == pytest.approx(1.0)
    assert pooled_delivery((), "optic") == 0.0
    assert pooled_delivery(cands, "optic", near=6.1, within=0.7) == pytest.approx(0.5)
    assert pooled_delivery(cands, "optic", near=3.0, within=0.7) == 0.0
    # a master that could not have detected the branch says nothing either way
    faint = point_presence(("optic",), [8.0], (ExecutedScan(7.5, 8.5, 1),), step=0.2,
                           fwhm_meV=0.8, temperature_K=200.0,
                           scale=default_intensity_scale(("optic",)))
    assert faint[0] == pytest.approx(PRESENCE_PRIOR_ODDS / (1.0 + PRESENCE_PRIOR_ODDS))


# ------------------------------------------------------------------ the campaign

def _load_sim():
    path = Path(__file__).resolve().parents[2] / "tools" / "eig_sim.py"
    spec = importlib.util.spec_from_file_location("eig_sim_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.slow
def test_the_seeded_acoustic_campaign_reaches_the_prize_deterministically(capsys):
    """The end-to-end gate: 21 grid points from (2,0,0) to (3,0,0), acoustic class prior only."""
    sim = _load_sim()
    import argparse
    args = argparse.Namespace(target="acoustic", seed=1, h_start=2.0, h_end=3.0, step_h=0.05,
                              tolerance=0.05, plot=None, inject_null=False)
    assert sim.run(args) == 0
    lines = capsys.readouterr().out.splitlines()
    scans = [line for line in lines if line.strip().startswith(tuple("0123456789"))
             and " h=" in line]
    assert len(scans) == 21
    first = [(line.split("h=")[1].split()[0], line.split("n=")[1].split()[0]) for line in scans[:3]]
    # Re-pinned 2026-09-03 (PR #37 review round: banked exposure per scenario energy, anchors
    # admitted per outcome): the campaign opens at the point nearest the fold, where the class
    # prior is tightest, with a 14-point window, tops it up once, and moves on. Still 21 scans.
    assert first == [("2.05", "14"), ("2.05", "14"), ("2.10", "21")]
    stop = next(line for line in lines if line.startswith("stop="))
    assert "stop=True reason=goal_met" in stop and "bar 20/20" in stop


@pytest.mark.slow
def test_a_missed_peak_is_recovered_because_a_null_is_evidence_not_proof(capsys):
    """The world hides a detectable peak on the first scan.  Negative evidence down-weights that
    band, it does not forbid it, so the campaign comes back to it and still reaches the prize."""
    sim = _load_sim()
    import argparse
    args = argparse.Namespace(target="acoustic", seed=1, h_start=2.0, h_end=3.0, step_h=0.05,
                              tolerance=0.05, plot=None, inject_null=True)
    assert sim.run(args) == 0          # the sim asserts no identical (grid, window, ncount) repeat
    lines = capsys.readouterr().out.splitlines()
    scans = [line for line in lines if line.strip().startswith(tuple("0123456789"))
             and " h=" in line]
    assert len(scans) <= 40
    assert any("HIDDEN" in line for line in scans)
    stop = next(line for line in lines if line.startswith("stop="))
    assert "stop=True reason=goal_met" in stop and "bar 20/20" in stop
