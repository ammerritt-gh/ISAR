"""The width coupling prior and the lifted width ceiling of the coupled refit (S4U3).

A matched slot whose branch has a link prediction carries a proper density over its width, centred on the
neighbours' predicted ``matching_width``; a slot on a settled link has the 20 meV ceiling, every other slot the lone
``min(4, omega0)``. Every spectrum is synthetic and generated at test time (hard rule 10).
"""
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from scipy.integrate import quad

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_coupled_refit import FWHM, S, _kept, spectrum, t_pair  # noqa: E402

from isar.analyze import prior  # noqa: E402
from isar.analyze.coupled import engine as cc  # noqa: E402
from isar.analyze.coupled import refit as R  # noqa: E402
from isar.analyze.coupled.linking import Change, Prediction, matching_width  # noqa: E402
from isar.analyze.coupled.search import gamma_start  # noqa: E402
from isar.analyze.coupled.types import PricingJob, SnapshotLinks, Start  # noqa: E402

K, GLO = 0.12, 1e-3
SETTINGS = prior.PriorSettings()
EP = prior.EnergyPrior(lo=0.5, hi=30.0, start=2.0, end=0.75, ceiling=7.5, corner=0.05)


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


def _slot(w=1.0, R_=1.0, tau=0.7, C=12.0):
    return cc.MatchedDensity(EP, "me", [], {}, coupling=cc.SlotCoupling(w, R_, tau, C, K, GLO))


# (a) the density is proper on [g_lo, c] and reduces to the base density at C = 0 ---------------------------------
@pytest.mark.parametrize("c, F", [(4.0, 1.6417), (2.0, 1.6394), (20.0, 1.6457)])
def test_the_normaliser_matches_quad_and_the_worked_values(c, F):
    d = _slot()
    cp = d.coupling
    ref = quad(lambda v: math.exp(float(d._g(math.exp(v) - K)[1])), math.log(GLO + K), math.log(c + K),
               epsabs=0, epsrel=1e-12)[0]
    assert math.exp(d.ln_f(c)) == pytest.approx(ref, rel=1e-9)
    assert ref == pytest.approx(F, abs=5e-4)
    assert d.ln_width(0.5, 4.0) == pytest.approx(-0.7098, abs=5e-4)
    assert cp.g_lo == GLO


@pytest.mark.parametrize("c", [4.0, 2.0, 20.0])
@pytest.mark.parametrize("w", [0.5, 1.0, 3.0])
def test_the_width_density_integrates_to_one(c, w):
    d = _slot(w=w, tau=0.3)
    # break points at the prediction's damping, where the potential turns
    pts = [x for x in (gamma_start(w, 1.0), 0.1, 1.0) if GLO < x < c]
    tot = sum(quad(lambda g: math.exp(d.ln_width(g, c)), a, b, epsabs=0, epsrel=1e-10, limit=200)[0]
              for a, b in zip([GLO] + sorted(pts), sorted(pts) + [c]))
    assert tot == pytest.approx(1.0, rel=1e-8)


def test_a_zero_cap_is_the_base_density():
    d = _slot(C=0.0)
    for c in (4.0, 2.0, 20.0):
        for g in (GLO, 0.3, 1.9):
            assert d.ln_width(g, c) == pytest.approx(prior.ln_width_given(g, c, GLO, SETTINGS), abs=1e-9)


def test_a_linked_mode_at_its_prediction_is_not_charged_more_than_an_unlinked_one():
    # the property that a candidate with one more linked mode pays no arbitrary extra cost: the proper density
    # at the predicted width is at least the base density there (F <= the base normaliser)
    for R_ in (0.5, 1.5):
        for w in (R_, 2.0 * R_, 5.0):
            G = gamma_start(w, R_)
            assert matching_width(G, R_) == pytest.approx(w)          # gamma_start inverts matching_width
            d = _slot(w=w, R_=R_, tau=0.3)
            for c in (2.0, 4.0, 20.0):
                if GLO < G < c:
                    assert d.ln_width(max(G, GLO), c) >= prior.ln_width_given(max(G, GLO), c, GLO, SETTINGS) - 1e-12


# (c) the derivatives -----------------------------------------------------------------------------------------------
def _cd(f, x, h=1e-6):
    return (f(x + h) - f(x - h)) / (2.0 * h)


@pytest.mark.parametrize("c", [2.0, 4.0, 20.0])
def test_the_slot_derivatives_match_central_differences(c):
    d = _slot(w=1.3, tau=0.4)
    for g in (0.05, 0.4, 1.7):
        assert d.d_ln_width(g, c) == pytest.approx(_cd(lambda x: d.ln_width(x, c), g), rel=1e-5)
    assert d.d_ln_width_dc(0.4, c) == pytest.approx(_cd(lambda x: d.ln_width(0.4, x), c), rel=1e-5)


# ---- an engine on a snapshot with a link prediction ---------------------------------------------------------------
def _pred(branch="b1", E=3.0, w=1.2, A=1.0):
    return Prediction(branch, 0, E, w, A, 0.1, ("x", "z"))


def _links(settled=(), changes=(), preds=None, labels=("b1",)):
    return SnapshotLinks("l0123456789ab", "ring1", tuple(labels), (True,) * len(labels), frozenset(settled),
                         tuple(preds if preds is not None else [_pred()]), tuple(changes), ())


def _snap(links, modes=((3.0, 0.4, 40.0),)):
    modes = list(modes)
    snap = t_pair(spectrum(modes, scale=2.0, seed=3), modes, ["b1"], modes, ["b1"])
    return replace(snap, links=links)


def _engine(snap, slots, **kw):
    spec = R._Spec(snap)
    return spec, R._Labelled(spec.ctx, R._ceiling(spec.fs, len(slots)), spec.slot_objects(slots),
                             structures=spec.structures, **kw)


def _model(eng, k=1):
    model = eng.model(eng.variant(k, True, True), ())
    return model


def _theta(eng, model, e, g):
    return eng._start(model, [(e, g, 40.0, False)], {"elastic": 100.0, "bg": 2.0})


def test_the_slot_carries_the_prediction_in_the_snapshots_settings():
    spec, eng = _engine(_snap(_links()), ("b1",))
    cp = eng.slots[0].coupling
    assert (cp.w_star, cp.fwhm, cp.tau_w, cp.C, cp.knee, cp.g_lo) == (
        1.2, spec.fwhm, S.width_tolerance_ln, S.penalty_cap, K, spec.sp.g_lo)
    _spec, bare = _engine(_snap(None), ("b1",))
    assert bare.slots[0].coupling is None                                   # no links: today's terms


@pytest.mark.parametrize("lifted", [False, True])
@pytest.mark.parametrize("e", [2.5, 3.5, 7.0])
def test_the_coupled_prior_differs_from_the_base_only_in_the_width_term_and_its_gradient_is_exact(e, lifted):
    snap = _snap(_links(preds=[_pred(A=0.0)]))                              # no intensity: the height term stays (S4U5)
    spec, eng = _engine(snap, ("b1",), lifted=(lifted,))
    _spec, bare = _engine(replace(snap, links=None), ("b1",), lifted=(lifted,))
    model, bmodel = _model(eng), _model(bare)
    theta = _theta(eng, model, e, 0.6)
    a, g, _c = model.modes[0]
    c = prior.width_ceiling(e, lifted=lifted, settings=eng.prior_s)
    diff = eng.ln_prior(model, theta) - bare.ln_prior(bmodel, theta)
    assert diff == pytest.approx(eng.slots[0].ln_width(0.6, c) - eng.sp.ln_width_at(0.6, e, lifted=lifted))
    d = eng.d_ln_prior(model, theta)
    for pos in (g, a):
        def f(x, pos=pos):
            t = theta.copy()
            t[pos] = x
            return eng.ln_prior(model, t)
        assert d[pos] == pytest.approx(_cd(f, theta[pos]), rel=1e-5)
    # the unchanged coordinates carry the base gradient
    db = bare.d_ln_prior(bmodel, theta)
    other = [i for i in range(len(theta)) if i not in (a, g)]
    assert np.allclose(d[other], db[other])


# (e) the lifted ceiling ----------------------------------------------------------------------------------------------
def test_only_a_settled_link_lifts_the_ceiling_and_an_applied_add_or_split_keeps_the_cap():
    add = Change("add", None, ("b2",), (_pred("b2", 7.0),))
    split = Change("split", 0, ("b3", "b4"), (_pred("b3", 2.9), _pred("b4", 3.1)))
    snap = _snap(_links(settled=["b1", "b2", "b3"], changes=[add, split], preds=[_pred(), _pred("b2", 7.0)]))
    spec = R._Spec(snap)
    assert [spec.slot_lifted(b, ()) for b in ("b1", "b2", "b3", None)] == [True, True, True, False]   # the incoming fit
    assert [spec.slot_lifted(b, ("b2", "b3", "b4")) for b in ("b1", "b2", "b3", "b4", None)] == [True, False, False, False, False]
    assert not R._Spec(_snap(_links(settled=[]))).slot_lifted("b1", ())         # a tie group: unsettled
    assert not R._Spec(_snap(None)).slot_lifted("b1", ())                       # no links
    job = PricingJob(snap.unit, snap.snapshot_id, 2, ("b1", "b2"), "k", (Start("own", ((3.0, 0.4, 40.0), (7.0, 0.5, 20.0))),),
                     unlifted=("b2",))
    eng = R._engine_for(spec, spec.slot_objects(job.slots), job, True)
    assert eng.lifted == (True, False)
    model = eng.model(eng.variant(2, True, True), ())
    theta = eng._start(model, [(3.0, 0.4, 40.0, False), (7.0, 0.5, 20.0, False)], {"elastic": 100.0, "bg": 2.0})
    assert [eng.zmap(model).ceiling(theta, m) for m in (0, 1)] == [20.0, 4.0]


def test_the_release_fit_uses_the_same_flag_and_no_coupling():
    modes = [(3.0, 0.4, 40.0)]
    snap = _snap(_links(settled=["b1"]))
    r = _kept(snap)
    assert r.status in ("accepted", "unchanged")
    assert "width:0" not in r.P                                             # the width term is this block's unary


# (g) the continuous stage honours the prior -------------------------------------------------------------------------
def test_the_continuous_stage_pulls_a_weakly_determined_width_toward_the_prediction():
    true = [(5.0, 1.6, 6.0)]
    near = gamma_start(0.9, FWHM)                                           # resolution-limited prediction
    res = {}
    for name, links in (("linked", _links(preds=[_pred("b1", 5.0, 0.9)])), ("bare", None)):
        modes = true
        snap = t_pair(spectrum(modes, scale=0.15, seed=4), modes, ["b1"], [(5.0, near, 6.0)], ["b1"])
        res[name] = _kept(replace(snap, links=links)).modes[0].gamma
    assert abs(res["linked"] - near) < abs(res["bare"] - near)


# (h) the continuous objective carries the full density -------------------------------------------------------------
@pytest.mark.parametrize("lifted", [False, True])
def test_the_width_term_of_j_is_the_density_less_its_floor_with_an_exact_gradient(lifted):
    snap = _snap(_links())
    _spec, eng = _engine(snap, ("b1",), continuous=True, lifted=(lifted,))
    model = _model(eng)
    slot = eng.slots[0]
    a, g, _c = model.modes[0]
    theta = _theta(eng, model, 5.0, 0.5)
    terms = {t: p for t, p, _s in eng.penalty_terms(model, theta)}
    c = prior.width_ceiling(5.0, lifted=lifted, settings=eng.prior_s)
    B = eng._width_floor(model, 0, slot)
    assert terms["width:0"] == pytest.approx(-2.0 * slot.ln_width(0.5, c) - 2.0 * B)
    assert eng.J(model, theta) == pytest.approx(eng.cost(model, theta) + sum(terms.values()))
    # d V_w / d(G, omega0) = -2 d ln p_w (the floor is a constant of the box)
    d = eng.d_ln_prior(model, theta)
    for pos in (g, a):
        def v(x, pos=pos):
            t = theta.copy()
            t[pos] = x
            return dict((i, p) for i, p, _ in eng.penalty_terms(model, t))["width:0"]
        assert _cd(v, theta[pos]) == pytest.approx(
            -2.0 * _width_partial(eng, model, theta, pos, a), rel=1e-5)
        assert d[pos] != 0.0


def _width_partial(eng, model, theta, pos, a):
    """d ln p_w / d theta[pos] by the slot's own derivatives (the engine's d_ln_prior less the other terms)."""
    slot = eng.slots[0]
    c = prior.width_ceiling(float(theta[a]), lifted=eng.lifted_of(model, 0), settings=eng.prior_s)
    G = float(theta[model.modes[0][1]])
    if pos != a:
        return slot.d_ln_width(G, c)
    return slot.d_ln_width_dc(G, c) * prior.d_width_ceiling(float(theta[a]), lifted=eng.lifted_of(model, 0),
                                                            settings=eng.prior_s)


@pytest.mark.parametrize("lifted", [False, True])
def test_the_width_term_is_never_negative_over_the_box_so_the_residual_needs_no_clamp(lifted):
    _spec, eng = _engine(_snap(_links()), ("b1",), continuous=True, lifted=(lifted,))
    model = _model(eng)
    slot = eng.slots[0]
    a, g, _c = model.modes[0]
    rng = np.random.default_rng(7)
    theta = _theta(eng, model, 5.0, 0.5)
    lo_e, hi_e = float(model.lo[a]), min(float(model.hi[a]), 12.0)
    worst = math.inf
    for _ in range(200):
        t = theta.copy()
        t[a] = rng.uniform(lo_e, hi_e) if rng.random() < 0.7 else rng.choice([lo_e, 3.999, 4.0, 4.001])
        c = prior.width_ceiling(float(t[a]), lifted=lifted, settings=eng.prior_s)
        t[g] = GLO + (c - GLO) * rng.uniform(0.0, 1.0) ** 2
        v = dict((i, p) for i, p, _ in eng.penalty_terms(model, t))["width:0"]
        worst = min(worst, v)
        assert v >= 0.0
        assert math.sqrt(v) ** 2 == pytest.approx(v, rel=1e-12)
    assert worst < math.inf
    assert slot.has_width


def test_the_refits_unary_holds_the_width_term_and_p_does_not():
    r = _kept(_snap(_links()))
    assert r.status in ("accepted", "unchanged")
    assert not any(k.startswith("width:") for k in r.P)
    assert r.J == pytest.approx(r.D + sum(r.P.values()) + r.unary, rel=1e-9, abs=1e-9)
    bare = _kept(_snap(None))
    assert r.unary != bare.unary                                          # the width term is in the unary
