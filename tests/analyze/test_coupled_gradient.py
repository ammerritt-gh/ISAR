"""The coupled pricing's joint objective and gradient (S1U3): the gradient against a central difference of the value,
each prior term's derivative, the same prices as the finite-difference path, the fast pass untouched, and the split
node's one-pass polish at a weak/strong tie.

Every spectrum is synthetic and generated at test time (hard rule 10): the hidden-pair case of
``tests/inter_hidden_case.py``, an ordinary three-mode and an on-a-bound case built from its spectrum builders, and a
BL43LXU spectrum with a central QE from ``qe_rail_recipe``.
"""
import math
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import minimize

sys.path.insert(0, str(Path(__file__).resolve().parent))
import qe_rail_recipe as recipe  # noqa: E402
from inter_hidden_case import Prepared, _ctx, _expected, hidden_snapshot  # noqa: E402
from test_coupled_width_floor import snapshot as qe_snapshot  # noqa: E402

from isar.analyze import catalogue, fast_pass, prior  # noqa: E402
from isar.analyze.coupled import engine as cc  # noqa: E402
from isar.analyze.coupled import refit as R  # noqa: E402
from isar.analyze.coupled.types import PricingJob, Start  # noqa: E402


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


# ---- the cases ------------------------------------------------------------------------------------------------
def _counts_case(modes, k, seed=3, prior_settings=None):
    counts = np.random.default_rng(seed).poisson(_expected(modes)).astype(float)
    prep = Prepared(_ctx(counts), k)
    if prior_settings is not None:
        prep.settings = replace(prep.settings, prior=prior_settings)
    return replace(hidden_snapshot(3.0), context=prep)


def _hidden(prior_settings=None):
    snap = hidden_snapshot(3.0, asimov=True)
    if prior_settings is not None:
        prep = Prepared(snap.context.ctx, 2)
        prep.settings = replace(prep.settings, prior=prior_settings)
        snap = replace(snap, context=prep)
    return snap


def _case(name):
    """(snapshot, slots, start modes): an ordinary three-mode spectrum, one with a mode railed on a bound (its
    energy at the window top: the box correction and the railed gradient), and the hidden pair."""
    if name == "ordinary":
        return (_counts_case([(3.5, 0.5, 25.0), (8.0, 0.4, 40.0), (15.0, 0.6, 20.0)], 3), (None, "b1", None),
                [(3.5, 0.5, 20.0), (8.0, 0.4, 40.0), (15.0, 0.6, 15.0)])
    if name == "bound":
        return (_counts_case([(8.0, 0.4, 43.0)], 2), ("b1", None), [(8.0, 0.4, 40.0), (15.0, 0.5, 2.0)])
    snap = _hidden()
    spec = R._Spec(snap)
    nb = next(m for m in spec.held_all["x"].modes if m.branch == "b2")
    own = spec.own.modes[0]
    return snap, ("b1", "b2"), [(own.omega0, own.gamma, own.height), (float(np.mean(spec.pred["b2"])), nb.gamma,
                                                                       nb.height)]


CASES = ("ordinary", "bound", "hidden")


def _job(snap, slots, start):
    return PricingJob(snap.unit, snap.snapshot_id, len(slots), tuple(slots), f"{slots}", (Start("own", tuple(start)),))


def _engine(snap, slots, **kw):
    spec = R._Spec(snap)
    return R._Labelled(spec.ctx, R._ceiling(spec.fs, len(slots)), spec.slot_objects(slots), structures=spec.structures,
                       **kw)


def _fd(f, z, rel=1e-6):
    """Central difference of the value alone."""
    g = np.zeros(z.size)
    for i in range(z.size):
        h = rel * max(abs(z[i]), 1.0)
        e = np.zeros(z.size)
        e[i] = h
        g[i] = (f(z + e) - f(z - e)) / (2.0 * h)
    return g


def _agree(g, ref, where):
    """Relative 1e-4 on every component above 1e-3 of the gradient's norm."""
    big = np.abs(ref) > 1e-3 * np.linalg.norm(ref)
    rel = np.abs(g - ref)[big] / np.abs(ref)[big]
    assert big.any() and np.all(rel <= 1e-4), (where, g, ref)


def _inside(z, lo, hi, rel=0.02, seed=0):
    """A perturbed point, kept inside the box."""
    rng = np.random.default_rng(seed)
    span = hi - lo
    return np.clip(z * (1.0 + rel * rng.uniform(-1.0, 1.0, z.size)), lo + 1e-3 * span, hi - 1e-3 * span)


# ---- 1. the gradient check -----------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", CASES)
def test_the_joint_gradient_is_the_derivative_of_the_value(name):
    snap, slots, start = _case(name)
    eng = _engine(snap, slots)
    for v in eng.variants[len(slots)]:
        eng.fit(v, [(*m, False) for m in start], "own")
    basins = [b for b in eng.basins() if b.status == "pending"]
    for b in basins:
        eng.price(b)
    priced = [b for b in basins if b.status in ("ok", "ok_boundary") and b.boundary_method != "split"]
    assert priced
    for b in priced:
        f, lo, hi = eng.objective(b.model, b.theta)
        fg, _lo, _hi = eng.objective_and_gradient(b.model, b.theta)
        for where, z in (("mode", b.theta_map[b.model.free]), ("perturbed", _inside(b.theta_map[b.model.free], lo, hi))):
            v, g = fg(z)
            assert v == f(z)                                       # one value: the objective's own, bit for bit
            if where == "perturbed":
                _agree(g, _fd(f, z), (name, b.model.hyp.label, where))
            else:                                                  # at the mode: the railed coordinates and the rest
                ref = _fd(f, z)
                assert np.all(np.abs(g - ref) <= 1e-4 * np.abs(ref) + 1e-5), (name, where, g, ref)


@pytest.mark.parametrize("width_kind", ["softlog", "flat"])
def test_a_split_node_gradient_is_the_derivative_of_its_value(width_kind):
    # the flat (untransformed) width prior: the ridge acts on Gamma itself, so a missing ridge term shows here
    snap = _hidden(prior.PriorSettings(width_kind=width_kind))
    eng = _engine(snap, ("b1", "b2"))
    model = eng.model(eng.variant(2, True, True), ())
    theta = eng._start(model, [(8.0, 0.4, 40.0, False), (8.2, 0.5, 3.0, False)], {"elastic": 100.0, "bg": 2.0})
    obj, xi0, _fj, lo, _theta_of = eng.split_problem(model, theta, (0, 1))
    for f in (0.03, 0.5, 0.97):
        g, gv, h2 = obj(f)
        x = _inside(xi0, lo, h2, seed=int(100 * f))
        v, d = gv(x)
        assert v == g(x)
        _agree(d, _fd(g, x), (width_kind, f))


def test_the_qe_width_and_height_gradient():
    # BL43LXU with a free central QE width (its column difference) and the QE width prior's derivative
    lo_w, _fwhm = recipe.qe_floor()
    truth = [(5.0, 0.4, 60.0), (11.0, 0.8, 40.0)]
    snap = qe_snapshot(recipe.widened(1, qe_h=30.0, qe_width=3.0 * lo_w), truth)
    eng = _engine(snap, ("b1", "b2"))
    model = eng.model(eng.variant(2, True, True, True), ())
    assert model.qe_w is not None and model.qe_w in model.free
    theta = eng._start(model, [(5.0, 0.4, 60.0, False), (11.0, 0.8, 40.0, False)],
                       {"elastic": 200.0, "bg": 2.0, "qe_h": 30.0, "qe_width": 3.0 * lo_w})
    f, lo, hi = eng.objective(model, theta)
    fg, _lo, _hi = eng.objective_and_gradient(model, theta)
    for seed in (0, 1):
        z = _inside(theta[model.free], lo, hi, seed=seed)
        v, g = fg(z)
        assert v == f(z)
        _agree(g, _fd(f, z), ("qe", seed))


def _grid_check(fun, dfun, xs, h=1e-6):
    for x in xs:
        ref = (fun(x + h) - fun(x - h)) / (2.0 * h)
        assert abs(dfun(x) - ref) <= 1e-5 * (1.0 + abs(ref)), (x, dfun(x), ref)


def test_each_prior_term_derivative_matches_a_difference_of_that_term():
    ep = prior.EnergyPrior(0.75, 24.0, 1.5, 0.375, 7.5, 0.075)
    xs = [x for x in np.linspace(0.76, 23.99, 157)]              # the ramp, both rounded corners and the flat part
    _grid_check(ep.log_pdf, ep.d_log_pdf, xs)
    qe = prior.qe_width_prior(1.5)
    _grid_check(qe.log_pdf, qe.d_log_pdf, np.linspace(qe.lo + 1e-3, qe.hi - 1e-3, 157))
    snap = _hidden()
    spec = R._Spec(snap)
    for st in (prior.PriorSettings(), prior.PriorSettings(width_kind="flat")):
        sp = prior.resolve_prior_for(spec.ctx, st)
        _grid_check(sp.ln_width, sp.d_ln_width, np.linspace(1e-3, 19.9, 97))
    support = np.linspace(spec.lo + 1e-3, spec.hi - 1e-3, 233)
    for entry in (spec.entry, replace(spec.entry, physical_hi=True)):    # the smoothstep band at each edge, its middle
        _grid_check(entry.ln_weight, entry.d_ln_weight, support)
    md = spec.slot_objects(("b1", "b2"))[1]                       # the ramp and both neighbours' factors
    _grid_check(md.ln_pdf, md.d_ln_pdf, support)
    for family, d in (("welsch", 0.0), ("quartic", 0.0), ("welsch", 2.0), ("quartic", 3.0)):
        fac = cc.Factor("t", "b", ("y", "x"), (1.0, -1.0), 0.3, 12.0, "dT", d=d, family=family)
        _grid_check(lambda u: cc.potential(fac, u), lambda u: cc.d_potential(fac, u), np.linspace(-2.0, 2.0, 81) + 1e-3)
    for p in (catalogue.ResolvedPrior("g", "gaussian", 0.4, 0.1, 0.0, 2.0, "transport"),
              catalogue.ResolvedPrior("l", "lognormal", 0.4, 0.3, 0.01, 2.0, "transport"),
              catalogue.ResolvedPrior("s", "softlog", 0.12, None, 0.0, 20.0, "transport"),
              catalogue.ResolvedPrior("f", "flat", None, None, 0.0, 2.0, "transport")):
        _grid_check(p.logpdf, lambda v, p=p: cc._d_logpdf(p, v), np.linspace(p.lo + 0.02, p.hi - 0.02, 41))


# ---- 2. the same results as the finite-difference path ------------------------------------------------------------
def _priced(snap, job, gradients):
    engines = []

    class Spy(R._Labelled):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            engines.append(self)
    orig = R._Labelled
    R._Labelled = Spy
    try:
        t = time.perf_counter()
        p = R.price(snap, job, gradients=gradients)
        return p, engines[0], time.perf_counter() - t
    finally:
        R._Labelled = orig


@pytest.mark.parametrize("name", CASES)
def test_the_gradient_path_prices_as_the_finite_difference_path(name):
    snap, slots, start = _case(name)
    spec = R._Spec(snap)
    job = _job(snap, slots, start)
    old, e_old, s_old = _priced(snap, job, False)
    new, e_new, s_new = _priced(snap, job, True)
    print(f"[S1U3] {name}: finite differences {s_old:.2f} s, gradients {s_new:.2f} s; "
          f"delta ln Z {new.ln_z - old.ln_z:+.5f}")
    assert new.status == old.status and new.mode[3] == old.mode[3]
    assert abs(new.ln_z - old.ln_z) <= 0.02
    assert len(new.basins) == len(old.basins)
    for a in old.basins:                                     # the same basin set, each within 0.02 nat
        b = next(b for b in new.basins if R._same_basin(spec, a, b))
        assert abs(a[0] - b[0]) <= 0.02, (a[1], a[0], b[0])
    # the posterior modes, within 1e-3 posterior sd per free coordinate (the Laplace basins)
    pairs = [(x, y) for x in e_old.basins() for y in e_new.basins()
             if x.model.hyp.label == y.model.hyp.label and x.index == y.index and x.cov_free is not None
             and y.cov_free is not None]
    assert pairs
    for x, y in pairs:
        assert x.status == y.status
        free = x.model.free
        sd = np.sqrt(np.diag(x.cov_free))
        assert np.all(np.abs(x.theta_map[free] - y.theta_map[free]) <= 1e-3 * sd), (name, x.model.hyp.label)


# ---- 3. the fast pass is untouched ------------------------------------------------------------------------------------
def test_the_fast_pass_has_no_joint_gradient_and_the_switch_turns_it_off():
    snap, slots, start = _case("ordinary")
    spec = R._Spec(snap)
    base = fast_pass._Engine(spec.ctx, R._ceiling(spec.fs, 3), structures=spec.structures)
    model = base.model(base.variant(3, True, True), ())
    theta = base._start(model, [(*m, False) for m in start], None)
    assert base.objective_and_gradient(model, theta) is None
    off = _engine(snap, slots, gradients=False)
    assert off.objective_and_gradient(off.model(off.variant(3, True, True), ()), theta) is None
    on = _engine(snap, slots)
    assert on.objective_and_gradient(on.model(on.variant(3, True, True), ()), theta) is not None


# ---- 4. the tie retry ---------------------------------------------------------------------------------------------------
def _one_fd_pass(g, lo, hi, x):
    x = np.clip(x, lo, hi)
    sc = np.maximum(np.abs(x), 1.0)
    r = minimize(lambda u: (lambda v: v if math.isfinite(v) else cc.SPLIT_OUTSIDE)(g(u * sc)), x / sc,
                 method="L-BFGS-B", bounds=list(zip(lo / sc, hi / sc)),
                 options={"maxiter": 500, "ftol": 1e-12, "gtol": 1e-6})
    return g(np.clip(r.x * sc, lo, hi))


def test_the_one_pass_polish_reaches_the_two_pass_value_at_a_weak_strong_tie(monkeypatch):
    """The weak/strong tie: a hidden pair with a weak mode (3 counts) beside a strong one (40), noisy counts, its
    nodes continued from their neighbours. The finite-difference path's single pass stops short there (the reason
    it takes two); the gradient pass reaches the two-pass node value or better at every node."""
    snap = hidden_snapshot(0.5, seed=2, asimov=False)
    eng = _engine(snap, ("b1", "b2"), gradients=False)
    model = eng.model(eng.variant(2, True, True), ())
    theta = eng._start(model, [(8.0, 0.4, 40.0, False), (8.0, 0.4, 3.0, False)], {"elastic": 100.0, "bg": 2.0})
    b = eng._place(model, theta, eng.cost(model, theta), "x", 0, "")
    assert b.status == "pending" and eng.hidden_pairs(model, b.theta)
    anchor = eng._anchor(model, b.theta)
    starts, f_of, orig, orig_problem = [], {}, cc._polish, eng.split_problem

    def problem(*a):
        objective, *rest = orig_problem(*a)

        def tagged(f):
            got = objective(f)
            f_of[id(got[0])] = f
            return got
        return (tagged, *rest)

    def spy(g, lo, hi, x, gv=None):
        starts.append((g, lo.copy(), hi.copy(), np.array(x, float)))
        return orig(g, lo, hi, x, gv)
    monkeypatch.setattr(cc, "_polish", spy)
    monkeypatch.setattr(eng, "split_problem", problem)
    eng.split_evidence(b, anchor, (0, 1), sets=(6, 12))          # the finite-difference run: its node starts
    monkeypatch.setattr(cc, "_polish", orig)
    eng.gradients = True                                         # the same node objectives, now with their gradient
    obj = orig_problem(model, anchor, (0, 1))[0]
    worst_fd1, worst_new = -math.inf, -math.inf
    for g, lo, hi, x in starts:
        _g, gv, _h2 = obj(f_of[id(g)])
        two = orig(g, lo, hi, x)[0]
        worst_fd1 = max(worst_fd1, _one_fd_pass(g, lo, hi, x) - two)
        worst_new = max(worst_new, orig(g, lo, hi, x, gv)[0] - two)
    print(f"[S1U3] tie: finite-difference one pass worse than two by up to {worst_fd1:.2e} nat; "
          f"the gradient polish by up to {worst_new:+.2e} nat")
    assert worst_fd1 > 1e-6                                  # the failure the second pass exists for
    assert worst_new <= 1e-6                                 # the gradient polish reaches the two-pass value


def test_the_polish_retries_only_when_a_pass_fails(monkeypatch):
    """A retry costs a second pass: at the tie's nodes every first pass succeeds, so none may retry."""
    snap = hidden_snapshot(0.5, seed=2, asimov=False)
    eng = _engine(snap, ("b1", "b2"))
    model = eng.model(eng.variant(2, True, True), ())
    theta = eng._start(model, [(8.0, 0.4, 40.0, False), (8.0, 0.4, 3.0, False)], {"elastic": 100.0, "bg": 2.0})
    b = eng._place(model, theta, eng.cost(model, theta), "x", 0, "")
    anchor = eng._anchor(model, b.theta)
    passes, polishes = [], []
    real_min, real_pol = cc.minimize, cc._polish
    monkeypatch.setattr(cc, "minimize", lambda *a, **k: (passes.append(1), real_min(*a, **k))[1])
    monkeypatch.setattr(cc, "_polish", lambda *a: (polishes.append(1), real_pol(*a))[1])
    eng.split_evidence(b, anchor, (0, 1), sets=(6, 12))
    assert polishes and len(passes) == len(polishes)