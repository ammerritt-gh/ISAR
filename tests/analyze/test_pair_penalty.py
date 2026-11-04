"""The tie-breaker penalty between close modes: worked examples 1-4 on the pure module, the engine's wiring
and the card's range check. Synthetic channels only (never real_data/). The half-point band against the exact
separability is not asserted: the operator accepted the closed form's known limit (see pair_penalty.separability).
"""
import math

import numpy as np
import pytest

from isar.analyze import fast_pass, pair_penalty as pp
from isar.analyze.context import ScanContext
from isar.analyze.fast_pass import FastPassSettings
from isar.schema import card as card_mod
from isar.session import guidelines as G

FLAT_E = np.linspace(-20.0, 40.0, 601)            # worked examples: 0.1 meV channels, flat mu
FLAT_DE, FLAT_MU = np.full(FLAT_E.size, 0.1), np.full(FLAT_E.size, 100.0)


def test_worked_examples_and_the_limits():
    a = pp.PairMode(10.0, 2.0, 500.0)
    assert pp.separability(a, a, FLAT_E, FLAT_DE, FLAT_MU) == pytest.approx(0.0, abs=1e-9)   # ex 1
    assert pp.share_factor(a, a) == 1.0
    empty = pp.PairMode(10.5, 2.0, 0.0)                                                      # ex 2
    assert pp.separability(a, empty, FLAT_E, FLAT_DE, FLAT_MU) == pytest.approx(0.0, abs=1e-9)
    assert pp.share_factor(a, empty) == 0.0
    half = [pp.PairMode(10.0, 2.0, 250.0), pp.PairMode(10.5, 2.0, 250.0)]
    d_eq = pp.separability(*half, FLAT_E, FLAT_DE, FLAT_MU)
    assert math.isfinite(d_eq) and pp.fade(d_eq, 4.0) >= fast_pass.CLOSE_FADE
    assert not pp.in_range(pp.PairMode(5.0, 1.0, 500.0), pp.PairMode(12.0, 1.0, 500.0))    # ex 3

    # ex 4: the separation whose separability is 4 ln 2 halves the penalty (share 1/2, cap 1)
    def sep(d, n=500.0):
        return pp.separability(a, pp.PairMode(10.0 + d, 2.0, n), FLAT_E, FLAT_DE, FLAT_MU)
    lo, hi = 0.0, 3.0 * (2.0 + 2.0)
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if sep(mid) < 4.0 * math.log(2.0) else (lo, mid)
    b = pp.PairMode(10.0 + 0.5 * (lo + hi), 2.0, 500.0)
    assert pp.share_factor(a, b) * pp.fade(sep(0.5 * (lo + hi)), 4.0) == pytest.approx(0.5, abs=1e-9)
    grid = np.linspace(0.05, 8.0, 40)
    assert np.all(np.diff([sep(d) for d in grid]) > 0)                  # rises with separation

    def sep_n(n):   # both modes' counts move together, the separation fixed
        return pp.separability(pp.PairMode(10.0, 2.0, n), pp.PairMode(11.0, 2.0, n), FLAT_E, FLAT_DE, FLAT_MU)
    assert all(sep_n(n) < sep_n(1.5 * n) for n in (100.0, 300.0, 900.0))       # and rises with counts


# --------------------------------------------------------------------------- the engine's wiring
X = np.linspace(-8.0, 20.0, 113)


@pytest.fixture(scope="module")
def pair_engine():
    ctx = ScanContext(x=X, counts=np.ones(X.size), mask=np.ones(X.size, bool), T=300.0, center=0.0,
                      gauss_fwhm=1.5, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute")
    return fast_pass._Engine(ctx, FastPassSettings(mode_ceiling=2))


def _theta(model, modes, bg=5.0):
    theta = np.zeros(len(model.names))
    for (a, g, c), (e, gam, h) in zip(model.modes, modes):
        theta[a], theta[g], theta[c] = e, gam, h
    theta[model.el], theta[model.bg] = 0.0, bg
    return theta


def test_engine_penalty_is_the_cap_for_a_coincident_pair_and_absent_when_far(pair_engine):
    eng = pair_engine
    model = eng.model(eng.variant(2, True, True), ())
    same = _theta(model, [(10.0, 0.2, 100.0), (10.0, 0.2, 100.0)])
    assert eng.pair_terms(model, same)[0][:2] == (0, 1)
    assert eng.pair_penalty(model, same) == pytest.approx(eng.s.pair_penalty_cap, rel=1e-9)
    assert eng.close_pairs(model, same) == [(0, 1)]
    far = _theta(model, [(5.0, 0.2, 100.0), (15.0, 0.2, 100.0)])
    assert eng.pair_terms(model, far) == [] and eng.pair_penalty(model, far) == 0.0
    off = fast_pass._Engine(eng.ctx, FastPassSettings(mode_ceiling=2, pair_penalty_cap=0.0))
    assert off.pair_penalty(model, same) == 0.0


# --------------------------------------------------------------------------- the card's range check
def _card(fast_pass_table):
    pol = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True),
                   card_mod.DetectorSpec("ana5", 8, "Ana5_3std", False)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan",
        temperature_rule={}, collation={}, fitting={"fast_pass": fast_pass_table},
    )
    return card_mod.Card(pol, card_mod.CardState())


def test_card_penalty_keys_warn_and_keep_defaults_when_out_of_range():
    with pytest.warns(UserWarning) as caught:
        s = G.fast_pass_settings(_card({"pair_penalty_cap": -1.0, "pair_penalty_d0": float("nan")}))
    text = " ".join(str(w.message) for w in caught)
    assert "pair_penalty_cap" in text and "pair_penalty_d0" in text
    assert (s.pair_penalty_cap, s.pair_penalty_d0) == (1.0, 4.0)
    s = G.fast_pass_settings(_card({"pair_penalty_cap": 0.0, "pair_penalty_d0": 2.5}))
    assert (s.pair_penalty_cap, s.pair_penalty_d0) == (0.0, 2.5)                  # 0 is off, a legal cap
