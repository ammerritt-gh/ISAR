"""LocalLinearModel — fit/predict on a synthetic dispersion, incl. extrapolation σ growth."""
import numpy as np
import pytest

from isar.drive.acquisition import model as m


def _quadratic_dispersion(qs, sigma=0.05):
    # omega(q) = 1 + 3 q + 8 q^2 — a gently bending branch.
    return [(q, 1.0 + 3.0 * q + 8.0 * q * q, sigma) for q in qs]


def test_predict_interpolates_between_points():
    mod = m.fit(_quadratic_dispersion([0.0, 0.1, 0.2, 0.3, 0.4]))
    mean, sigma = mod.predict(0.15)
    # Local line through a bending branch: within a few tenths of the true value.
    assert abs(mean - (1.0 + 3.0 * 0.15 + 8.0 * 0.15 ** 2)) < 0.3
    assert sigma > 0.0


def test_extrapolation_sigma_grows_away_from_data():
    mod = m.fit(_quadratic_dispersion([0.0, 0.1, 0.2, 0.3]))
    s_in = mod.predict(0.15)[1]
    s_edge = mod.predict(0.3)[1]
    s_far = mod.predict(1.0)[1]
    s_farther = mod.predict(2.0)[1]
    assert s_in < s_far < s_farther            # honest growth with distance from the data
    assert s_edge < s_far


def test_more_precise_data_shrinks_interpolation_sigma():
    loose = m.fit(_quadratic_dispersion([0.0, 0.1, 0.2, 0.3], sigma=0.5))
    tight = m.fit(_quadratic_dispersion([0.0, 0.1, 0.2, 0.3], sigma=0.01))
    assert tight.predict(0.15)[1] < loose.predict(0.15)[1]


def test_zero_points_is_cold():
    mean, sigma = m.fit([]).predict(0.5)
    assert mean == 0.0 and sigma >= m.COLD_SIGMA


def test_one_point_grows_linearly():
    mod = m.fit([(0.2, 4.0, 0.1)])
    assert mod.predict(0.2) == (4.0, pytest.approx(0.1))
    far = mod.predict(1.2)
    assert far[0] == 4.0                       # constant offset
    assert far[1] > 0.1                        # σ grew with distance


def test_nonfinite_and_bad_sigma_are_handled():
    mod = m.fit([(0.0, 1.0, 0.0), (0.1, 1.3, -1.0), (float("nan"), 9.0, 0.1), (0.2, 1.9, 0.05)])
    # The NaN q is dropped; the two zero/negative sigmas are floored — still predicts finitely.
    mean, sigma = mod.predict(0.1)
    assert np.isfinite(mean) and np.isfinite(sigma)


def test_degenerate_all_same_q_falls_back_to_constant():
    mod = m.fit([(0.5, 2.0, 0.1), (0.5, 2.2, 0.1), (0.5, 1.8, 0.1)])
    mean, sigma = mod.predict(0.5)
    assert 1.8 <= mean <= 2.2 and np.isfinite(sigma)
