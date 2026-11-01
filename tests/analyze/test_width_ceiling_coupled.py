"""The coupled refit under the width ceiling (recipe /7): the coupled engine fits, prices and takes covariance in the
fast pass's measure (each width proper on [g_lo, min(4, omega0)], fitted as t in [0, 1]); a lifted slot's ceiling is
the old 20 meV; the coupled record marks a width at its ceiling.

Every spectrum is synthetic and generated at test time (hard rule 10).
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import inter_hidden_case as H  # noqa: E402
from test_coupled_refit import _kept, _Lifted, spectrum, t_pair  # noqa: E402

from isar.analyze.coupled import engine as cc  # noqa: E402
from isar.analyze.coupled import refit as R  # noqa: E402
from isar.analyze.coupled.settings import InterSettings  # noqa: E402
from isar.analyze.coupled.types import PricingJob, Snapshot, Start, TermInput  # noqa: E402
from isar.session.inter_spectra.graph import Term  # noqa: E402


def test_runs_from_the_worktree():
    import isar
    here = __file__.replace("\\", "/").rsplit("/tests/", 1)[0]
    print("import root:", isar.__file__)
    assert isar.__file__.replace("\\", "/").startswith(here)


def _low_pair(e=2.5, w=3.0, dl=0.8):
    """``inter_hidden_case.hidden_snapshot`` with the pair at ``e`` meV, below the 4 meV cap: each pair width's
    range ends at its own energy. Noise free."""
    own_true = [(e, 0.4, 40.0 + w)]
    prep = H.Prepared(H._ctx(H._expected(own_true)), 2)
    xm, zm = [(e, 0.4, 40.0), (e - dl, 0.4, w)], [(e, 0.4, 40.0), (e + dl, 0.4, w)]
    terms = (TermInput(Term("T-change", ("x", "y"), (100.0,), None, None), (H._held("x", xm, ["b1", "b2"], T=100.0),)),
             TermInput(Term("T-change", ("y", "z"), (100.0,), None, None), (H._held("z", zm, ["b1", "b2"], T=300.0),)))
    return Snapshot(snapshot_id="b1:round1", stage_id="b1:round1", unit="y", fit_id="f00000000000a", context=prep,
                    own=H._held("y", own_true, ["b1"], T=200.0), own_record=H._record(own_true), terms=terms,
                    settings=InterSettings(), batch_kind="anchored")


def _engine(snap, slots, **kw):
    spec = R._Spec(snap)
    return spec, R._Labelled(spec.ctx, R._ceiling(spec.fs, len(slots)), spec.slot_objects(slots),
                             structures=spec.structures, **kw)


def _fd(f, z, rel=1e-6):
    g = np.zeros(z.size)
    for i in range(z.size):
        h = rel * max(abs(z[i]), 1.0)
        e = np.zeros(z.size)
        e[i] = h
        g[i] = (f(z + e) - f(z - e)) / (2.0 * h)
    return g


# (a) the gradient path prices as the finite-difference path at a pair below the cap -------------------------------
def test_the_gradient_path_prices_a_low_pair_as_the_finite_difference_path():
    snap = _low_pair()
    spec = R._Spec(snap)
    nb = next(m for m in spec.held_all["x"].modes if m.branch == "b2")
    own = spec.own.modes[0]
    start = ((own.omega0, own.gamma, own.height), (float(np.mean(spec.pred["b2"])), nb.gamma, nb.height))
    job = PricingJob(snap.unit, snap.snapshot_id, 2, ("b1", "b2"), "low pair", (Start("own", start),))
    old, new = R.price(snap, job, gradients=False), R.price(snap, job, gradients=True)
    assert any(b[1].endswith("|split") for b in new.basins)          # the pair's integral over (omega0, s) ran
    assert new.status == old.status and abs(new.ln_z - old.ln_z) <= 0.02, (old.ln_z, new.ln_z)


@pytest.mark.parametrize("lifted", [(False, False), (True, False)])
def test_the_joint_gradient_carries_the_ceiling_terms_at_2_5_and_7_meV(lifted):
    snap = H.hidden_snapshot(3.0, asimov=True)
    _spec, eng = _engine(snap, (None, "b1"), lifted=lifted)
    model = eng.model(eng.variant(2, True, True), ())
    theta = eng._start(model, [(2.5, 0.6, 30.0, False), (7.0, 0.5, 40.0, False)], {"elastic": 100.0, "bg": 2.0})
    f, lo, hi = eng.objective(model, theta)
    fg, _lo, _hi = eng.objective_and_gradient(model, theta)
    zm = eng.zmap(model)
    assert [zm.ceiling(theta, m) for m in (0, 1)] == [20.0 if lifted[0] else 2.5, 4.0]
    rng = np.random.default_rng(0)
    for _ in range(2):
        z = np.clip(zm.to_z(theta) * (1 + 0.02 * rng.uniform(-1, 1, lo.size)), lo + 1e-3 * (hi - lo), hi - 1e-3 * (hi - lo))
        v, g = fg(z)
        assert v == f(z)
        ref = _fd(f, z)
        big = np.abs(ref) > 1e-3 * np.linalg.norm(ref)
        assert np.all(np.abs(g - ref)[big] <= 1e-4 * np.abs(ref)[big]), (g, ref)


def test_a_soft_width_cut_at_its_ceiling_has_the_energy_gradient_of_its_normaliser():
    from dataclasses import replace

    from isar.analyze import fast_pass, prior
    from isar.schema.steering import ParamConstraints
    ctx = replace(H._ctx(H._expected([(2.5, 0.8, 40.0)])),
                  constraints=ParamConstraints(soft={"phonon.Gamma": (3.0, 1.0)}))
    s = fast_pass.FastPassSettings(mode_ceiling=1)
    sp = prior.resolve_prior_for(ctx, prior.PriorSettings())
    eng = cc.CoupledEngine(ctx, s, [cc.Entry(sp.energy, band=1.0, b_edge=0.0, b_mid=0.0)])
    model = eng.model(eng.variant(1, True, True), ())
    a, g, _c = model.modes[0]
    assert any(i == g for i, _p in model.soft_priors) and eng.zmap(model).mapped_mode(0)
    theta = eng._start(model, [(2.5, 0.8, 40.0, False)], {"elastic": 100.0, "bg": 2.0})
    d = eng.d_ln_prior(model, theta)
    h = 1e-6
    up, dn = theta.copy(), theta.copy()
    up[a] += h
    dn[a] -= h
    assert d[a] == pytest.approx((eng.ln_prior(model, up) - eng.ln_prior(model, dn)) / (2 * h), rel=1e-5)


# (c) a planted broad mode stops at the ceiling and is marked --------------------------------------------------------
BROAD = [(10.0, 8.0, 40.0)]
INCOMING = [(10.0, 4.0, 40.0)]


def test_a_broad_mode_stops_at_the_lone_ceiling_and_is_marked():
    r = _kept(t_pair(spectrum(BROAD, scale=4.0, seed=5), INCOMING, ["b1"], INCOMING, ["b1"]))
    peak, = r.fit["peaks"]
    assert peak["Gamma"] <= 4.0 + 1e-12 and peak["at_width_cap"] is True


# (d) a lifted slot follows its own data past 4 meV with the lifted density ------------------------------------------
def test_a_lifted_slot_has_the_lifted_ceiling_and_density():
    snap = t_pair(spectrum(BROAD, scale=4.0, seed=5), INCOMING, ["b1"], INCOMING, ["b1"])
    spec = R._Spec(snap)
    for lifted in (False, True):
        e = R._Labelled(spec.ctx, spec.fs, spec.slot_objects(("b1",)), structures=spec.structures,
                        lifted=(lifted,))
        model = e.model(e.variant(1, True, True), ())
        theta = e._start(model, [(10.0, 4.0, 40.0, False)], {"elastic": 100.0, "bg": 2.0})
        g = model.modes[0][1]
        zm = e.zmap(model)
        theta[g] = 8.0
        if lifted:          # 20 meV, and the density is SpectrumPrior.ln_width's, normalised on [g_lo, 20]
            assert zm.ceiling(theta, 0) == 20.0 and not zm.at_cap(theta, 0)
            assert e._ln_width(model, theta, 0) == pytest.approx(e.sp.ln_width(8.0))
        else:
            assert zm.ceiling(theta, 0) == 4.0 and e._ln_width(model, theta, 0) == -np.inf
        cap = zm.ceiling(theta, 0)
        theta[g] = cap
        assert zm.at_cap(theta, 0)


def test_a_lifted_slot_goes_past_4_meV_on_its_own_data(monkeypatch):
    monkeypatch.setattr(R, "_Labelled", _Lifted)
    r = _kept(t_pair(spectrum(BROAD, scale=4.0, seed=5), INCOMING, ["b1"], INCOMING, ["b1"]))
    peak, = r.fit["peaks"]
    assert 4.0 < peak["Gamma"] < 20.0 and peak["at_width_cap"] is False      # its ceiling is 20, not reached


def test_lifted_flags_must_match_the_slots():
    spec = R._Spec(H.hidden_snapshot(3.0, asimov=True))
    with pytest.raises(ValueError, match="lifted flags"):
        cc.CoupledEngine(spec.ctx, spec.fs, spec.slot_objects(("b1", "b2")), lifted=(True,))
