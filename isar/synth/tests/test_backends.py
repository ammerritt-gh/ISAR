"""Tests for the three minimizer backends behind one interface.

Uses a self-contained Gaussian-peak-on-background model (3 params) so the backend
mechanics — minimize, covariance, MINOS — are tested independently of the DHO fit.
"""
import numpy as np
import pytest

from isar.analyze import backends, estimators

SIGMA = 1.0
X = np.linspace(-10.0, 10.0, 81)
NAMES = ("A", "mu", "BG")
BOUNDS = [(1e-3, 1e6), (-10.0, 10.0), (0.0, 1e6)]


def model(theta):
    A, mu, BG = theta
    return A * np.exp(-((X - mu) ** 2) / (2 * SIGMA**2)) + BG


def make_data(A, mu, BG, seed):
    lam = model((A, mu, BG))
    return np.random.default_rng(seed).poisson(lam).astype(float)


def make_cost(y, estimator):
    est = estimators.get_estimator(estimator)
    return lambda theta: est.cost(y, model(theta))


ALL_BACKENDS = ["iminuit", "scipy", "lmfit"]
ALL_ESTIMATORS = ["neyman", "pearson", "baker_cousins"]


@pytest.mark.parametrize("backend", ALL_BACKENDS)
@pytest.mark.parametrize("estimator", ALL_ESTIMATORS)
def test_backend_recovers_highcount(backend, estimator):
    truth = (2000.0, 2.0, 20.0)
    y = make_data(*truth, seed=11)
    cost = make_cost(y, estimator)
    res = backends.minimize(backend, cost, x0=[1500.0, 1.0, 10.0], bounds=BOUNDS, names=NAMES)
    assert res.success
    assert np.isclose(res.x[0], truth[0], rtol=0.10)   # A
    assert abs(res.x[1] - truth[1]) < 0.1               # mu
    assert np.isclose(res.x[2], truth[2], rtol=0.30)    # BG


@pytest.mark.parametrize("estimator", ALL_ESTIMATORS)
def test_iminuit_scipy_covariance_agree(estimator):
    truth = (2000.0, 2.0, 20.0)
    y = make_data(*truth, seed=22)
    cost = make_cost(y, estimator)
    r_im = backends.minimize("iminuit", cost, [2000.0, 2.0, 20.0], BOUNDS, NAMES)
    r_sp = backends.minimize("scipy", cost, [2000.0, 2.0, 20.0], BOUNDS, NAMES)
    assert r_im.covariance_ok and r_sp.covariance_ok
    err_im = np.sqrt(np.diag(r_im.covariance))
    err_sp = np.sqrt(np.diag(r_sp.covariance))
    assert np.allclose(err_im, err_sp, rtol=0.05), (estimator, err_im, err_sp)


def test_estimator_errors_agree_at_high_counts():
    # Gaussian limit: all three estimators give ~equal parameter errors.
    truth = (5000.0, 2.0, 50.0)
    y = make_data(*truth, seed=33)
    errs = {}
    for est in ALL_ESTIMATORS:
        r = backends.minimize("iminuit", make_cost(y, est), [5000.0, 2.0, 50.0], BOUNDS, NAMES)
        errs[est] = np.sqrt(np.diag(r.covariance))
    # A-error agreement across estimators within 10% (factor-of-2 trap would blow this up).
    a_errs = np.array([errs[e][0] for e in ALL_ESTIMATORS])
    assert a_errs.max() / a_errs.min() < 1.10, errs


def test_minos_symmetric_at_high_counts():
    truth = (5000.0, 2.0, 50.0)
    y = make_data(*truth, seed=44)
    r = backends.minimize("iminuit", make_cost(y, "baker_cousins"),
                          [5000.0, 2.0, 50.0], BOUNDS, NAMES, minos=True)
    assert r.merrors is not None
    lo, hi, valid = r.merrors["A"]
    assert valid
    assert np.isclose(abs(lo), hi, rtol=0.10)   # parabolic regime → symmetric


def test_minos_asymmetric_at_low_counts():
    # Low amplitude near the A>=0 bound → asymmetric likelihood profile.
    truth = (12.0, 2.0, 2.0)
    y = make_data(*truth, seed=55)
    r = backends.minimize("iminuit", make_cost(y, "baker_cousins"),
                          [12.0, 2.0, 2.0], BOUNDS, NAMES, minos=True)
    assert r.merrors is not None
    lo, hi, valid = r.merrors["A"]
    assert valid
    # upper error should exceed the magnitude of the lower error (skew away from 0)
    assert hi > abs(lo)


def test_scipy_returns_no_minos():
    y = make_data(2000.0, 2.0, 20.0, seed=66)
    r = backends.minimize("scipy", make_cost(y, "baker_cousins"),
                          [2000.0, 2.0, 20.0], BOUNDS, NAMES, minos=True)
    assert r.merrors is None  # scipy has no MINOS; asymmetric errors come from iminuit


def test_lmfit_scalar_bc_does_not_raise():
    y = make_data(2000.0, 2.0, 20.0, seed=77)
    r = backends.minimize("lmfit", make_cost(y, "baker_cousins"),
                          [1800.0, 1.5, 15.0], BOUNDS, NAMES)
    assert r.success
    assert np.isclose(r.x[0], 2000.0, rtol=0.10)


def test_at_limit_flag_detects_pinned_param():
    # Force BG to sit at its lower bound by fitting data with true BG=0.
    y = make_data(2000.0, 2.0, 0.0, seed=88)
    r = backends.minimize("iminuit", make_cost(y, "baker_cousins"),
                          [2000.0, 2.0, 0.5], BOUNDS, NAMES)
    assert "BG" in r.at_limit and "A" in r.at_limit
    assert r.at_limit["BG"] is True
    assert r.at_limit["A"] is False
def test_psd_covariance_rejects_finite_but_indefinite():
    """A reported covariance that is finite but not positive-definite carries negative
    variances along some direction — the lmfit path used to accept it while the scipy path
    rejected it. Both now reject, returning (None, False)."""
    cov, ok = backends._psd_covariance(np.array([[1.0, 2.0], [2.0, 1.0]]))
    assert cov is None and ok is False

    cov, ok = backends._psd_covariance(np.eye(2))
    assert ok is True and np.allclose(cov, np.eye(2))
