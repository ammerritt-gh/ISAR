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
    Candidate, PlannerInput, _deficits_batch, _pad_candidates, default_intensity_scale,
    hdi_halfwidth, plan_next, target_posterior,
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


def deficit_of(cands, mus, sigs, target_index=0) -> tuple[float, float]:
    hw, deficit = _deficits_batch(_pad_candidates([tuple(cands)]), [[m] for m in mus],
                                  [[s] for s in sigs], target_index, WIDTH, TOLERANCE, [False])
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


def test_extrapolating_a_branch_widens_it_by_the_bounded_curvature():
    """The curvature clause: |omega''| <= hi*(pi/period)^2, so a line carried d past its last anchor
    can be wrong by 0.5*|omega''|*d^2, and the belief must say so."""
    from isar.drive.acquisition.classify_eig import belief_predict
    from isar.probability import extrapolation_model_sigma

    assert extrapolation_model_sigma(0.0, 2.0, 9.0) == 0.0
    assert extrapolation_model_sigma(0.2, 2.0, 9.0) == pytest.approx(0.5 * 9.0 * (math.pi / 2) ** 2
                                                                    * 0.04)
    assert extrapolation_model_sigma(0.2, None, 9.0) == 0.0      # period unknown: term skipped
    assert extrapolation_model_sigma(0.2, 2.0, None) == 0.0      # band unknown: term skipped

    anchors = [{"u": 0.50, "omega0": 4.24, "weight": 1.0}, {"u": 0.55, "omega0": 4.56, "weight": 1.0},
               {"u": 0.60, "omega0": 4.85, "weight": 1.0}]
    row = belief_row("acoustic", anchors)
    at_anchor = belief_predict(dict(row), 0.60, domain_hi=DOMAIN[1])[1]
    far = belief_predict(dict(row), 0.95, domain_hi=DOMAIN[1])[1]
    assert far > 1.0 and far > 3.0 * at_anchor   # 0.35 rlu of extrapolation, band-truncated
    no_period = dict(row); no_period.pop("period")
    assert belief_predict(no_period, 0.95, domain_hi=DOMAIN[1])[1] < far


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
    window = ExecutedScan(3.0, 7.0, 10_000_000, seen=frozenset({"acoustic"}))
    base = planner_input(hs, targets=("optic",))
    seen_input = PlannerInput(**{**base.__dict__, "scans": {0: (window,)}})
    branches = tuple(sorted(BRANCHES))
    optic, acoustic = branches.index("optic"), branches.index("acoustic")

    def mass_inside(inp):
        rows = {b: dict(inp.beliefs[b]) for b in branches}
        om, wt, _tr, _marg = _scenarios(0, inp, branches, _Predictor(rows, DOMAIN[1]),
                               {k: tuple(v) for k, v in inp.scans.items()})
        return {b: float(wt[(om[:, i] >= window.e_lo) & (om[:, i] <= window.e_hi)].sum())
                for b, i in (("optic", optic), ("acoustic", acoustic))}

    before, after = mass_inside(base), mass_inside(seen_input)
    assert after["optic"] < 0.5 * before["optic"]          # contradicted where it was not found
    assert after["acoustic"] == pytest.approx(before["acoustic"])   # it WAS found: no evidence


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
    assert len(scans) == 22
    first = [(line.split("h=")[1].split()[0], line.split("n=")[1].split()[0]) for line in scans[:3]]
    assert first == [("2.05", "14"), ("2.10", "13"), ("2.15", "12")]
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
