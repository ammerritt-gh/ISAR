"""A width at its floor never gets a candidate refused (change 6) and a QE at its narrowest is the elastic
line (change 8), seen through ``refit.price`` and ``refit.assign`` on synthetic spectra made at test time
(hard rule 10: every number is made up; the committed resolution curve a06 is the only measured thing).

W1-W3: the railed QE ridge and the six-mode case; W1F: the optimiser class (F1); W4-W5: a free width at the
floor is never priced as a model of its own; W6-W7: a mask change alone becomes the saved fit.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qe_rail_recipe as recipe  # noqa: E402
from test_coupled_refit import t_pair  # noqa: E402

from isar.analyze import fast_pass, loop
from isar.analyze.components import CentralQE, CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.analyze.coupled import refit as R
from isar.analyze.coupled.types import PricingJob
from isar.analyze.phonon import GAMMA_FLOOR

TOL = loop.DEGENERATE_LIMIT_TOL * max(GAMMA_FLOOR, 1.0)


class Prepared:                    # the prepared context shape the core reads (ctx, settings, structures)
    def __init__(self, ctx, ceiling):
        self.ctx, self.settings, self.structures = ctx, fast_pass.FastPassSettings(mode_ceiling=ceiling), None


def snapshot(ctx, modes, *, ceiling=None):
    """The spectrum y with its own fast fit (``fast:y#i``) and one T-neighbour holding every mode as b1, b2, ..."""
    n = len(modes)
    names = [f"b{i + 1}" for i in range(n)]
    return t_pair(Prepared(ctx, ceiling or n), modes, [f"fast:y#{i}" for i in range(n)], modes, names,
                  rec={"count_probabilities": {str(n): 0.9}})


def job(snap, slots):
    return PricingJob("y", snap.snapshot_id, len(slots), tuple(slots), R._Spec(snap).key(len(slots), slots))


def free_at_floor(rec):
    return [m for m in rec[2] if not m[4] and m[2] <= GAMMA_FLOOR + TOL]


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


# W1 --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [1, 2, 3])
@pytest.mark.parametrize("slots", [("b1", "b2"), (None, None)])
def test_a_width_driven_to_the_floor_on_the_qe_ridge_still_gets_a_price(seed, slots):
    lo, _ = recipe.qe_floor()
    truth = [(5.0, GAMMA_FLOOR, 60.0), (11.0, 0.8, 40.0)]
    snap = snapshot(recipe.widened(seed, gamma1=GAMMA_FLOOR), truth)
    p = R.price(snap, job(snap, slots))
    assert p.status in ("ok", "ok_boundary") and p.ln_z is not None and np.isfinite(p.ln_z), (p.status, p.reason)
    assert p.basins
    assert any(tuple(rec[4][3]) == (0,) for rec in p.basins)                    # the resolution-limited alternative
    assert all(rec[3][1] is None or rec[3][1] > lo + TOL for rec in p.basins)   # no QE at its lower bound
    assert not any(free_at_floor(rec) for rec in p.basins)


# W2 --------------------------------------------------------------------------------------------------------
def test_the_six_mode_case_is_priced_with_both_floor_widths_resolution_limited():
    truth = [(2.0, 0.2, 300.0), (5.5, 0.5, 150.0), (9.5, GAMMA_FLOOR, 60.0), (12.5, 0.6, 80.0),
             (14.5, 1.5, 50.0), (17.0, GAMMA_FLOOR, 50.0)]
    x = np.arange(-5.0, 22.0, 0.25)
    lo, fwhm = recipe.qe_floor()
    comp = CompositeModel([PhononDHO() for _ in truth] + [Elastic(), CentralQE(), ConstantBackground()])
    theta = [v for m in truth for v in m] + [200.0, 1.05 * lo, 0.0, 2.0]
    mean = comp.evaluate(x, np.asarray(theta, float), recipe.context(x=x, curve=None, gauss_fwhm=1.6 * fwhm))
    ctx = recipe.context(np.random.default_rng(9).poisson(mean).astype(float), x=x)
    snap = snapshot(ctx, truth, ceiling=6)
    p = R.price(snap, job(snap, tuple(f"b{i + 1}" for i in range(6))))
    assert p.status in ("ok", "ok_boundary") and p.ln_z is not None, (p.status, p.reason)
    assert any(tuple(rec[4][3]) == (2, 5) for rec in p.basins)
    assert not any(free_at_floor(rec) for rec in p.basins)


# W3 --------------------------------------------------------------------------------------------------------
def test_a_qe_clear_of_its_bound_keeps_its_width_through_price():
    lo, _ = recipe.qe_floor()
    truth = [(5.0, 0.4, 60.0), (11.0, 0.8, 40.0)]
    snap = snapshot(recipe.widened(1, wide=1.0, qe_h=80.0, qe_width=2.5 * lo), truth)
    p = R.price(snap, job(snap, ("b1", "b2")))
    assert p.status in ("ok", "ok_boundary") and p.ln_z is not None, (p.status, p.reason)
    assert any(rec[3][1] is not None and rec[3][1] > 1.5 * lo for rec in p.basins)


# W4 --------------------------------------------------------------------------------------------------------
F1_TRUTH = [(5.0, GAMMA_FLOOR, 60.0), (11.0, 0.8, 40.0)]
F1_CASES = [(3, 1.2), (2, 1.15)]      # (seed, elastic widening): the optimiser class (W1F), no QE ridge (QE height zero)


@pytest.mark.parametrize("seed, wide", F1_CASES[:1])     # seed 2 fails in the optimiser first (W1F): its basin is priced only with decision 5
def test_a_free_width_pegged_at_the_floor_is_never_priced(seed, wide, monkeypatch):
    # the delta twin cannot be fitted: the railed free-width basin must not stand in for it
    orig = R._Labelled.fit

    def fit(self, variant, modes, family, linear=None):
        return None if any(m[3] for m in modes) else orig(self, variant, modes, family, linear)
    monkeypatch.setattr(R._Labelled, "fit", fit)
    snap = snapshot(recipe.widened(seed, wide=wide, gamma1=GAMMA_FLOOR, bg=0.0), F1_TRUTH)
    p = R.price(snap, job(snap, ("b1", "b2")))
    assert not any(free_at_floor(rec) for rec in p.basins)
    assert p.status == "no_evidence" and p.ln_z is None
    assert "free width at the floor" in p.reason, p.reason


# W5 --------------------------------------------------------------------------------------------------------
def test_a_broad_mode_keeps_its_free_width_and_the_mask_prior_sits_in_the_basin_term():
    truth = [(5.0, 0.8, 60.0), (11.0, 0.8, 40.0)]
    ctx = recipe.widened(1, wide=1.0, gamma1=0.8)
    snap = snapshot(ctx, truth)                    # its own modes are unmatched fast ids: the discrete stage runs
    r = R.refit(snap, stage="both")
    a = r.assignment
    assert a.discrete == "priced" and not a.mask_only          # whatever the count and labels do, no width is fixed
    assert a.selected_model[3] == () and R._selected_model(r.fit)["delta_modes"] == []
    weights = {tuple(w["delta"]): w["p"] for w in a.coverage["decision"]["mask"]["weights"]}
    assert all(p < weights[()] for dl, p in weights.items() if dl)       # a delta mask, if priced at all, is behind
    spec = R._Spec(snap)
    eng = R._Labelled(spec.ctx, spec.fs, spec.slot_objects(("b1", "b2")), structures=spec.structures)
    v = eng.variants[2][0]
    eng.fit(v, [(5.0, GAMMA_FLOOR, 60.0, True), (11.0, 0.8, 40.0, False)], "delta_start")
    delta = [b for b in eng.basins() if b.model.delta]
    assert delta
    for b in delta:                                 # the mask prior is in the basin term; nothing adds a second one
        assert b.ln_model_prior == pytest.approx(-np.log(len(eng.variants[2]) * 2 ** 2))


# W6 --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [2, 6])
def test_a_mask_change_alone_becomes_the_saved_fit_and_stays(seed):
    from dataclasses import replace

    from test_coupled_refit import spectrum
    true = [(9.0, GAMMA_FLOOR, 40.0)]
    prep = spectrum(true, seed=seed)
    start = [(9.0, 0.3, 40.0)]
    base = t_pair(prep, start, ["b1"], [(9.0, 0.05, 40.0)], ["b1"])
    r0 = R.refit(base, stage="continuous", assignment=R._Spec(base).skipped())      # the converged free-width state
    assert r0.status == "accepted" and R._selected_model(r0.fit)["delta_modes"] == []
    assert r0.fit["peaks"][0]["Gamma"] < 0.1                                         # its width has railed
    own = R.held_from_fit(r0.fit, unit="y", fit=base.fit_id, inputs=None, context=base.context, T=200.0)
    snap = replace(t_pair(prep, start, ["b1"], [(9.0, 0.05, 40.0), (14.0, 0.3, 40.0)], ["b1", "b2"]),
                   own=own, own_record=r0.fit, fit_id="f00000000000b")
    r = R.refit(snap, stage="both")
    a = r.assignment
    # measured: the free mask of the incoming fit is priced here (far behind), so the eps comparison carries the
    # change; the unpriced-incoming clause of decide is pinned by R4/R5
    assert not any(x.startswith("mask_unpriced") for x in a.reasons)
    assert (a.count, a.slots) == (1, ("b1",)) and a.changed and a.mask_only
    assert r.status == "accepted" and r.reason == "evidence"
    assert R._selected_model(r.fit)["delta_modes"] == [0]
    assert "resolution limited" in r.why_changed
    own2 = R.held_from_fit(r.fit, unit="y", fit=snap.fit_id, inputs=None, context=snap.context, T=200.0)
    sweep2 = replace(snap, snapshot_id="b1:sweep2", own=own2, own_record=r.fit, fit_id="f00000000000c")
    r2 = R.refit(sweep2, stage="continuous", assignment=a)
    assert r2.status in ("accepted", "unchanged") and R._selected_model(r2.fit)["delta_modes"] == [0]


# W7 --------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("seed", [2, 6])
def test_an_incoming_resolution_limited_mode_keeps_its_mask_without_a_start_of_its_own(seed):
    from test_coupled_refit import _names, record, spectrum
    true = [(9.0, GAMMA_FLOOR, 40.0)]
    rec = record(true)
    rec["models"][0].update(delta_modes=[0], free=[n for n in _names(1) if n != "phonon.Gamma"])
    snap = t_pair(spectrum(true, seed=seed), true, ["b1"], [(9.0, 0.05, 40.0), (14.0, 0.3, 40.0)], ["b1", "b2"],
                  rec=rec)
    a, _priced = R.assign(snap)
    incoming = next(c for c in a.candidates if c["count"] == 1 and tuple(c["slots"]) == ("b1",))
    assert incoming["ln_z"] is not None and np.isfinite(incoming["ln_z"])
    assert a.selected_model[3] == (0,) and not a.changed
    assert "incomplete_comparison" not in a.reasons and not any(x.startswith("mask_unpriced") for x in a.reasons)
    r = R.refit(snap, stage="both")
    assert r.status in ("accepted", "unchanged") and R._selected_model(r.fit)["delta_modes"] == [0]


# W1F (F1's class; removed with decision 5) -------------------------------------------------------------------
@pytest.mark.parametrize("seed, wide, slots", [(3, 1.2, ("b1", "b2")), (3, 1.2, (None, None)), (2, 1.15, ("b1", "b2"))])
def test_an_abnormal_posterior_optimiser_is_retried_with_a_three_point_gradient(seed, wide, slots):
    # a true resolution-limited model with no QE ridge (QE height zero): change 8 does nothing here, the failure is
    # the optimiser stopping ABNORMAL on the two-point gradient
    snap = snapshot(recipe.widened(seed, wide=wide, gamma1=GAMMA_FLOOR, bg=0.0), F1_TRUTH)
    p = R.price(snap, job(snap, slots))
    assert p.status in ("ok", "ok_boundary") and p.ln_z is not None, (p.status, p.reason)
    assert p.mode[3] == (0,)
    assert not any(free_at_floor(rec) for rec in p.basins)


def test_the_retry_gives_the_optimiser_back_its_two_point_gradient():
    orig = fast_pass.minimize
    snap = snapshot(recipe.widened(3, wide=1.2, gamma1=GAMMA_FLOOR, bg=0.0), F1_TRUTH)
    R.price(snap, job(snap, ("b1", "b2")))
    assert fast_pass.minimize is orig
