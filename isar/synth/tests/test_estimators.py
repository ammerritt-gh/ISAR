"""Tests for the pluggable cost functions and the constant-rate identities."""
import numpy as np
from scipy.optimize import minimize_scalar

from isar.synth import estimators


def test_registry_has_three():
    assert set(estimators.ESTIMATORS) == {"neyman", "pearson", "baker_cousins"}
    for name, est in estimators.ESTIMATORS.items():
        assert est.name == name
        assert callable(est.cost)


def test_cost_zero_when_model_equals_data():
    y = np.array([3.0, 10.0, 25.0, 7.0, 1.0])
    for est in estimators.ESTIMATORS.values():
        assert np.isclose(est.cost(y, y.copy()), 0.0, atol=1e-9)


def test_cost_nonnegative():
    rng = np.random.default_rng(0)
    y = rng.poisson(20, size=40).astype(float)
    f = np.clip(y + rng.normal(0, 3, size=40), 0.1, None)
    for est in estimators.ESTIMATORS.values():
        assert est.cost(y, f) >= 0.0


def test_baker_cousins_zero_count_channel():
    # At y=0 the BC term is f, so -2lnL contribution = 2f.
    assert np.isclose(estimators.baker_cousins_cost(np.array([0.0]), np.array([3.0])), 6.0)


def test_baker_cousins_finite_large_when_model_vanishes():
    c = estimators.baker_cousins_cost(np.array([5.0]), np.array([1e-12]))
    assert np.isfinite(c)
    assert c > 100.0  # large penalty for predicting ~0 where 5 counts were seen


def test_neyman_zero_count_floor():
    # y=0 floored to variance 1: (0-f)^2 / 1 = f^2.
    assert np.isclose(estimators.neyman_cost(np.array([0.0]), np.array([4.0])), 16.0)


def test_pearson_zero_count_finite():
    # y=0: (0-f)^2 / f = f.
    assert np.isclose(estimators.pearson_cost(np.array([0.0]), np.array([4.0])), 4.0)


def _numerical_constant_rate(counts, cost):
    res = minimize_scalar(lambda mu: cost(counts, np.full_like(counts, mu)),
                          bounds=(1e-3, 1e4), method="bounded",
                          options={"xatol": 1e-8})
    return res.x


def test_constant_rate_closed_forms_match_numerical_argmin():
    rng = np.random.default_rng(7)
    counts = rng.poisson(30, size=200).astype(float)
    counts[counts == 0] = 1.0  # keep all positive for the textbook closed forms
    for name, est in estimators.ESTIMATORS.items():
        analytic = estimators.constant_rate_estimate(counts, name)
        numeric = _numerical_constant_rate(counts, est.cost)
        assert np.isclose(analytic, numeric, rtol=1e-4), (name, analytic, numeric)


def test_constant_rate_ordering_harmonic_le_arith_le_rms():
    # Neyman→harmonic, Baker–Cousins→arithmetic, Pearson→RMS; harmonic ≤ arith ≤ RMS.
    rng = np.random.default_rng(3)
    counts = rng.poisson(15, size=300).astype(float)
    counts[counts == 0] = 1.0
    harmonic = estimators.constant_rate_estimate(counts, "neyman")
    arithmetic = estimators.constant_rate_estimate(counts, "baker_cousins")
    rms = estimators.constant_rate_estimate(counts, "pearson")
    assert harmonic <= arithmetic + 1e-9 <= rms + 1e-9
    assert np.isclose(arithmetic, counts.mean())
