"""The intensity coupling prior of the coupled refit (S4U5).

A matched slot whose branch has a link prediction with a known intrinsic intensity A* carries a proper density over
its height, induced from a Welsch density on the mode's intrinsic intensity m = h b (b = area per unit height over
``unit_stokes_area``, the quantity ``linking.matching_intensity`` compares). Every spectrum is synthetic and generated
at test time (hard rule 10).
"""
import math
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from scipy.integrate import quad

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_coupled_coupling import EP, GLO, K, _cd, _engine, _links, _model, _pred, _snap, _theta  # noqa: E402
from test_coupled_refit import FWHM, S, _kept, spectrum, t_pair  # noqa: E402

from isar.analyze import prior  # noqa: E402
from isar.analyze.coupled import engine as cc  # noqa: E402
from isar.analyze.coupled.linking import matching_intensity, matching_width, unit_stokes_area  # noqa: E402

TAU, C = 0.7, 12.0


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


def _slot(a_star=100.0, tau=TAU, C_=C):
    return cc.MatchedDensity(EP, "me", [], {}, coupling=cc.SlotCoupling(1.0, 1.0, 0.3, C_, K, GLO, a_star=a_star,
                                                                         tau_a=tau))


def _g(m, a_star, tau=TAU):
    """The intensity density's unnormalised Welsch factor exp(-P(u)/2) at intrinsic intensity m."""
    if m <= 0:
        return math.exp(-0.5 * C)
    u = math.log(m / a_star) / tau
    return math.exp(0.5 * C * math.expm1(-0.5 * u * u))


# (a) the table, the normaliser and the Jacobian ---------------------------------------------------------------------
@pytest.mark.parametrize("X, worked", [(1.0, 0.30520), (10.0, 0.84346), (0.01, 2.4788e-5), (1e-5, None),
                                       (1e5, None), (2.7, None)])
def test_the_gf_table_matches_quadrature_and_the_worked_values(X, worked):
    ref = quad(lambda v: math.exp(v) * _g(math.exp(v), 1.0), -60.0, math.log(X), points=[0.0] if X > 1 else None,
               epsabs=0, epsrel=1e-13, limit=400)[0]
    got = cc.intensity_gf(TAU, C, X)[0]
    assert got == pytest.approx(ref, rel=1e-10)
    if worked is not None:
        assert got == pytest.approx(worked, rel=2e-5)


def test_the_worked_density_value():
    d = _slot(100.0)
    assert d.ln_height(50.0, 2.0, 500.0) == pytest.approx(-3.741, abs=1e-3)
    assert -math.log(500.0) == pytest.approx(-6.215, abs=5e-4)                  # the flat base it replaces


@pytest.mark.parametrize("b", [0.05, 2.0, 30.0])
def test_the_height_density_integrates_to_one_and_is_the_intensity_density_with_its_jacobian(b):
    A, H = 100.0, 500.0
    d = _slot(A)
    hs = A / b
    pts = sorted(x for x in (hs * math.exp(-2 * TAU), hs, hs * math.exp(2 * TAU)) if 0 < x < H)
    tot = sum(quad(lambda h: math.exp(d.ln_height(h, b, H)), lo, hi, epsabs=0, epsrel=1e-11, limit=200)[0]
              for lo, hi in zip([0.0] + pts, pts + [H]))
    assert tot == pytest.approx(1.0, rel=1e-8)
    # the density of m = h b on [0, H b], normalised by direct quadrature, is p_h's with ln b removed
    mpts = [x * b for x in pts]
    Zm = sum(quad(lambda m: _g(m, A), lo, hi, epsabs=0, epsrel=1e-12, limit=200)[0]
             for lo, hi in zip([0.0] + mpts, mpts + [H * b]))
    for h in (x for x in (0.0, 0.3 * hs, hs, 3.0 * hs, H) if x <= H):
        assert d.ln_height(h, b, H) == pytest.approx(math.log(_g(h * b, A) / Zm) + math.log(b), abs=1e-9)
    assert d.ln_height(H * 1.0001, b, H) == -math.inf


def test_a_zero_or_unknown_prediction_keeps_todays_height_term():
    assert not _slot(0.0).has_area and _slot(0.0).status == "ok"
    bad = _slot(100.0, tau=0.0)
    assert bad.status == "no_evidence"                                         # never a silent flat fallback


# (d) a linked mode at its prediction is not charged more than the proper-density bound ---------------------------------
def test_a_linked_mode_at_its_prediction_is_not_charged_more_than_the_flat_height_prior():
    for tau in (0.2, 0.7, 2.0):
        for b in (0.01, 1.0, 50.0):
            for A in (0.5, 100.0, 2e4):
                for H in (10.0, 500.0, 1e5):
                    h = A / b
                    if h <= H:
                        assert _slot(A, tau).ln_height(h, b, H) >= -math.log(H) - 1e-12


# (b) the slot's derivatives -------------------------------------------------------------------------------------------
@pytest.mark.parametrize("b", [0.05, 2.0, 30.0])
def test_the_slot_derivatives_match_central_differences(b):
    d, H = _slot(100.0), 500.0
    for h in (x for x in (1.0, 50.0, 100.0 / b, 400.0) if x < H):
        assert d.d_ln_height_dh(h, b, H) == pytest.approx(_cd(lambda x: d.ln_height(x, b, H), h, 1e-5), rel=1e-6,
                                                          abs=1e-9)
        assert d.d_ln_height_db(h, b, H) == pytest.approx(_cd(lambda x: d.ln_height(h, x, H), b, 1e-6 * b), rel=1e-6,
                                                          abs=1e-9)
    assert d.d_ln_height_dh(0.0, b, H) == 0.0


# ---- an engine on a snapshot with an intensity prediction ------------------------------------------------------------
def _intensity_snap(A, *, T=200.0, settings=S, modes=((3.0, 0.4, 40.0),)):
    snap = _snap(_links(preds=[_pred(A=A)]), modes=modes)
    snap = replace(snap, own=replace(snap.own, T=T), settings=settings)
    return snap


def _b_ref(eng, model, theta, m, T):
    """b from its definition: the fast pass's area per unit height over ``unit_stokes_area``."""
    a_, g, _c = model.modes[m]
    e, G = float(theta[a_]), float(theta[g])
    return eng._area_per_h(model.variant.peaks[m], e, G)[0] / unit_stokes_area(e, G, T)


CASES = [(3.0, 0.6, None, False), (3.0, 0.6, 100.0, False), (9.7, 9.6, 100.0, True), (9.7, 9.6, None, True)]


@pytest.mark.parametrize("e, G, T, lifted", CASES)
def test_the_coupled_prior_gradient_matches_central_differences(e, G, T, lifted):
    snap = _intensity_snap(500.0, T=T)
    _spec, eng = _engine(snap, ("b1",), lifted=(lifted,))
    model = _model(eng)
    theta = _theta(eng, model, e, G)
    assert eng.slots[0].has_area and eng.slots[0].coupling.T == T
    b, db = eng._intensity_per_height(model, theta, 0, grad=True)
    assert b == pytest.approx(_b_ref(eng, model, theta, 0, T), rel=1e-12)
    d = eng.d_ln_prior(model, theta)
    for pos in model.modes[0]:
        def f(x, pos=pos):
            t = theta.copy()
            t[pos] = x
            return eng.ln_prior(model, t)
        assert d[pos] == pytest.approx(_cd(f, theta[pos], 1e-6 * max(1.0, abs(theta[pos]))), rel=1e-5, abs=1e-8)
    for pos in model.modes[0][:2]:                                           # b's own partials, through U too
        def fb(x, pos=pos):
            t = theta.copy()
            t[pos] = x
            return _b_ref(eng, model, t, 0, T)
        assert db[pos] == pytest.approx(_cd(fb, theta[pos], 1e-6 * max(1.0, abs(theta[pos]))), rel=1e-5)


# (c) only the matched slot's height term changes ------------------------------------------------------------------------
def test_the_coupled_prior_differs_from_the_width_coupling_only_in_the_matched_height_term():
    snap = _intensity_snap(500.0)
    _spec, eng = _engine(snap, ("b1", None))
    _spec, wid = _engine(_intensity_snap(0.0), ("b1", None))                  # S4U3: the width coupling alone
    model, wmodel = _model(eng, 2), _model(wid, 2)
    starts = [(3.0, 0.6, 40.0, False), (8.0, 0.5, 20.0, False)]
    theta = eng._start(model, starts, {"elastic": 100.0, "bg": 2.0})
    assert not wid.slots[0].has_area and wid.slots[0].has_width and isinstance(eng.slots[1], cc.Entry)
    c = model.modes[0][2]
    b = eng._intensity_per_height(model, theta, 0)
    H = eng.sp.height_hi
    diff = eng.ln_prior(model, theta) - wid.ln_prior(wmodel, theta)
    assert diff == pytest.approx(eng.slots[0].ln_height(float(theta[c]), b, H) - eng.sp.ln_height(float(theta[c])),
                                 rel=1e-12)
    d, dw = eng.d_ln_prior(model, theta), wid.d_ln_prior(wmodel, theta)
    other = [i for i in range(len(theta)) if i not in model.modes[0]]        # the unmatched slot, the QE, the rest
    assert np.array_equal(d[other], dw[other])
    assert not np.allclose(d[list(model.modes[0])], dw[list(model.modes[0])])


# (f) the continuous objective carries the full density ------------------------------------------------------------------
@pytest.mark.parametrize("e, G, T, lifted", CASES[:3])
def test_the_area_term_of_j_is_the_density_less_its_floor_with_an_exact_gradient(e, G, T, lifted):
    snap = _intensity_snap(500.0, T=T)
    _spec, eng = _engine(snap, ("b1",), continuous=True, lifted=(lifted,))
    model = _model(eng)
    slot = eng.slots[0]
    theta = _theta(eng, model, e, G)
    a, g, c = model.modes[0]
    H = eng.sp.height_hi
    terms = {t: p for t, p, _s in eng.penalty_terms(model, theta)}
    b, db = eng._intensity_per_height(model, theta, 0, grad=True)
    assert terms["area:0"] == pytest.approx(-2.0 * slot.ln_height(40.0, b, H) - 2.0 * (math.log(H) - 0.5 * C),
                                            rel=1e-12)
    assert eng.J(model, theta) == pytest.approx(eng.cost(model, theta) + sum(terms.values()))
    part = {c: slot.d_ln_height_dh(40.0, b, H)}
    part.update({pos: slot.d_ln_height_db(40.0, b, H) * v for pos, v in db.items()})
    for pos in (a, g, c):
        def v(x, pos=pos):
            t = theta.copy()
            t[pos] = x
            return dict((i, p) for i, p, _ in eng.penalty_terms(model, t))["area:0"]
        assert _cd(v, theta[pos], 1e-6 * max(1.0, abs(theta[pos]))) == pytest.approx(-2.0 * part[pos], rel=1e-5)


def test_the_area_term_is_never_negative_over_the_box_so_the_residual_needs_no_clamp():
    _spec, eng = _engine(_intensity_snap(500.0), ("b1",), continuous=True)
    model = _model(eng)
    a, g, c = model.modes[0]
    rng = np.random.default_rng(11)
    theta = _theta(eng, model, 5.0, 0.5)
    lo_e, hi_e = float(model.lo[a]), min(float(model.hi[a]), 12.0)
    H = eng.sp.height_hi
    for n in range(200):
        t = theta.copy()
        t[a] = rng.uniform(lo_e, hi_e) if rng.random() < 0.7 else rng.choice([lo_e, 3.999, 4.0, 4.001])
        cmax = prior.width_ceiling(float(t[a]), lifted=False, settings=eng.prior_s)
        t[g] = GLO + (cmax - GLO) * rng.uniform(0.0, 1.0) ** 2
        t[c] = 0.0 if n % 20 == 0 else H * rng.uniform(0.0, 1.0) ** 3
        v = dict((i, p) for i, p, _ in eng.penalty_terms(model, t))["area:0"]
        assert v >= 0.0
        assert math.sqrt(v) ** 2 == pytest.approx(v, rel=1e-12)


def test_the_refits_unary_holds_the_area_term_and_p_does_not():
    r = _kept(_intensity_snap(500.0))
    assert r.status in ("accepted", "unchanged")
    assert not any(k.startswith(("area:", "width:")) for k in r.P)
    assert r.J == pytest.approx(r.D + sum(r.P.values()) + r.unary, rel=1e-9, abs=1e-9)
    width_only = _kept(_intensity_snap(0.0))
    assert r.unary != width_only.unary                                      # the area term is in the unary


# (e) the continuous stage honours the prior -----------------------------------------------------------------------------
def test_the_continuous_stage_pulls_a_weakly_determined_intensity_toward_the_prediction():
    true = [(5.0, 0.6, 6.0)]
    target = 18.0
    settings = replace(S, intensity_tolerance_ln=0.2)
    snap0 = t_pair(spectrum(true, scale=0.15, seed=4), true, ["b1"], true, ["b1"], settings=settings)
    _spec, eng = _engine(replace(snap0, links=_links(preds=[_pred("b1", 5.0, matching_width(0.6, FWHM))])), ("b1",))
    model = _model(eng)
    b = eng._intensity_per_height(model, _theta(eng, model, 5.0, 0.6), 0)
    res = {}
    for name, A in (("linked", target * b), ("bare", None)):
        links = None if A is None else _links(preds=[_pred("b1", 5.0, matching_width(0.6, FWHM), A)])
        res[name] = _kept(replace(snap0, links=links)).modes[0].height
    assert abs(res["linked"] - target) < abs(res["bare"] - target)


# (g) the engine and linking price one quantity ---------------------------------------------------------------------------
def test_the_engines_intensity_is_the_stored_area_over_the_unit_stokes_area():
    true = [(4.0, 0.5, 30.0)]
    snap = _intensity_snap(500.0, modes=tuple(true))
    _spec, eng = _engine(snap, ("b1",))
    for v in eng.variants[1]:
        eng.fit(v, [(*true[0], False)], "own")
    basins = [x for x in eng.basins() if x.status == "pending"]
    for x in basins:
        eng.price(x)
    best = next(x for x in basins if x.status in ("ok", "ok_boundary"))
    model, t = best.model, best.theta_map
    a_, g, c = model.modes[0]
    pk = eng._peaks(best)[0][0]
    b = eng._intensity_per_height(model, t, 0)
    U = unit_stokes_area(float(t[a_]), float(t[g]), 200.0)
    assert t[c] * b * U == pytest.approx(pk.area_stokes, rel=1e-9)            # the engine's h a is the stored area
    assert t[c] * b == pytest.approx(matching_intensity(pk.area_stokes, omega0=pk.omega0, gamma=pk.Gamma, T=200.0),
                                     rel=1e-9)
