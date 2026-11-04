"""The tie-breaker penalty in the coupled engine (S1U5): J, the TRF residuals, the prior and the release fit carry the
same pair penalty as the solo fast pass. Synthetic channels built at test time; no measured data.

The gradient of the penalty is checked against the joint objective's gradient in test_coupled_gradient.py.
"""
import numpy as np
import pytest

from isar.analyze import prior
from isar.analyze.context import ScanContext
from isar.analyze.coupled import engine as cc
from isar.analyze.fast_pass import FastPassSettings

X = np.linspace(-6.0, 24.0, 241)
COINCIDENT = [(8.0, 0.5, 50.0), (8.0, 0.5, 50.0)]     # two identical modes: D = 0, share 1, so the penalty is the cap
FAR = [(5.0, 0.5, 50.0), (20.0, 0.5, 50.0)]           # 15 meV apart: beyond three combined widths (about 11 meV)


def _engine(cap, release=False):
    """The continuous coupled engine; ``release`` gives the release fit's slots (a ramp, no coupling)."""
    # a peak under the pair: the height prior's ceiling is the largest count, so a 50-count mode is inside it
    counts = 1.0 + 200.0 * np.exp(-0.5 * ((X - 8.0) / 1.0) ** 2)
    ctx = ScanContext(x=X, counts=counts, mask=np.ones(X.size, bool), T=300.0, center=0.0,
                      gauss_fwhm=1.5, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute")
    s = FastPassSettings(mode_ceiling=2, pair_penalty_cap=cap)
    sp = prior.resolve_prior_for(ctx, s.prior or prior.PriorSettings())
    slot = (cc.MatchedDensity(sp.energy, "me", [], {}) if release
            else cc.Entry(sp.energy, band=1.0, b_edge=0.0, b_mid=0.0))
    return cc.CoupledEngine(ctx, s, [slot, slot], continuous=True)


def _at(eng, modes):
    """(model, theta) of the two-mode, elastic and background variant at ``modes``."""
    model = eng.model(eng.variant(2, True, True), ())
    theta = np.zeros(len(model.names))
    for (a, g, c), (e, gam, h) in zip(model.modes, modes):
        theta[a], theta[g], theta[c] = e, gam, h
    theta[model.el], theta[model.bg] = 100.0, 2.0
    return model, theta


def test_a_coincident_equal_pair_pays_twice_the_cap_in_j_and_is_listed():
    on, off = _engine(1.0), _engine(0.0)
    model, theta = _at(on, COINCIDENT)
    [(i, j, D, _d_eq, nats)] = on.pair_terms(model, theta)
    assert (i, j) == (0, 1) and D == pytest.approx(0.0, abs=1e-9) and nats == pytest.approx(1.0)
    terms = {t: p for t, p, _s in on.penalty_terms(model, theta)}
    assert terms["pair:0-1"] == pytest.approx(2.0)                     # deviance units: 2 nats
    assert on.J(model, theta) == pytest.approx(on.cost(model, theta) + sum(terms.values()))
    off_model, _ = _at(off, COINCIDENT)
    assert on.J(model, theta) - off.J(off_model, theta) == pytest.approx(2.0, abs=1e-6)


def test_the_prior_and_the_release_fit_carry_the_same_penalty():
    on, off = _engine(1.0), _engine(0.0)
    model, theta = _at(on, COINCIDENT)
    off_model, _ = _at(off, COINCIDENT)
    assert np.isfinite(on.ln_prior(model, theta))                    # else the comparison is -inf against -inf
    assert on.ln_prior(model, theta) == pytest.approx(off.ln_prior(off_model, theta) - 1.0, abs=1e-9)
    rel, rel_off = _engine(1.0, release=True), _engine(0.0, release=True)
    rel_model, rel_theta = _at(rel, COINCIDENT)
    rel_off_model, _ = _at(rel_off, COINCIDENT)
    assert rel.J(rel_model, rel_theta) - rel_off.J(rel_off_model, rel_theta) == pytest.approx(2.0, abs=1e-6)


def test_a_pair_beyond_three_combined_widths_is_not_evaluated():
    on, off = _engine(1.0), _engine(0.0)
    model, theta = _at(on, FAR)
    assert on.pair_terms(model, theta) == []
    assert dict((t, p) for t, p, _s in on.penalty_terms(model, theta))["pair:0-1"] == 0.0   # listed at zero
    off_model, _ = _at(off, FAR)
    assert on.J(model, theta) == pytest.approx(off.J(off_model, theta), abs=1e-12)


def test_the_fit_residuals_square_to_j_with_the_pair_term(monkeypatch):
    # the TRF residuals come from penalty_terms, so their squares are J, the pair's 2 nats included
    eng = _engine(1.0)
    model, theta = _at(eng, COINCIDENT)
    seen = {}
    monkeypatch.setattr(eng, "_least_squares", lambda residual, z0, lo, hi, family: seen.update(residual=residual))
    eng._trf(model, theta, "coupled")
    zm = eng.zmap(model)
    r = seen["residual"](zm.to_z(theta))
    assert float(r @ r) == pytest.approx(eng.J(model, theta), rel=1e-9)
    assert any(t.startswith("pair:") for t, _p, _s in eng.penalty_terms(model, theta))


def test_the_residual_count_holds_while_a_pair_leaves_the_range(monkeypatch):
    # SciPy's least_squares needs one residual count for a whole fit: a pair leaving the range keeps its zero term
    eng = _engine(1.0)
    model, theta_in = _at(eng, COINCIDENT)
    _, theta_far = _at(eng, FAR)
    seen = {}
    monkeypatch.setattr(eng, "_least_squares", lambda residual, z0, lo, hi, family: seen.update(residual=residual))
    eng._trf(model, theta_in, "coupled")
    zm = eng.zmap(model)
    assert seen["residual"](zm.to_z(theta_far)).size == seen["residual"](zm.to_z(theta_in)).size
