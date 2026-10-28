"""Synthetic Gaussian-tail regressions for the one- and multi-coordinate box rule."""
import math

import numpy as np
import pytest
from scipy.integrate import quad
from scipy.special import ndtr

from isar.analyze import prior


@pytest.mark.parametrize("a, b", [(9.0, 9.001), (-9.001, -9.0)])
def test_narrow_same_tail_interval_matches_quadrature(a, b):
    exact = quad(lambda x: math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi), a, b)[0]
    assert prior._ln_interval_mass(a, b) == pytest.approx(math.log(exact), abs=1e-10)


@pytest.mark.parametrize("direction", [1.0, -1.0])
def test_thin_support_beyond_gaussian_centre_keeps_both_limits(direction):
    lo, hi = (0.0, 0.001) if direction > 0 else (-0.001, 0.0)
    objective = lambda theta: direction * 9.0 * theta[0] + 0.5 * theta[0] ** 2
    result = prior.laplace_log_evidence(objective, [0.0], [lo], [hi])
    exact = quad(lambda x: math.exp(-objective([x])), lo, hi)[0]

    assert result.status == "ok_boundary"
    assert result.ln_z == pytest.approx(math.log(exact), abs=1e-6)


@pytest.mark.parametrize("direction", [1.0, -1.0])
def test_multivariate_same_tail_box_keeps_finite_limits(direction):
    # The nonzero correlation exercises Genz rather than a product of marginal masses.
    # This mass stays above the existing 1e-10 threshold for the marginal fallback.
    rho = 0.15
    covariance = np.array([[1.0, rho], [rho, 1.0]])
    a, b = (6.01, 6.1) if direction > 0 else (-6.1, -6.01)
    lo, hi = np.array([a, -2.0]), np.array([b, 2.0])
    conditional_sd = math.sqrt(1 - rho * rho)

    def integrand(x):
        conditional_mass = ndtr((2.0 - rho * x) / conditional_sd) - ndtr(
            (-2.0 - rho * x) / conditional_sd)
        return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi) * conditional_mass

    exact = quad(integrand, a, b, epsabs=1e-16)[0]
    assert exact > 1e-10
    actual = prior._ln_box_mass(np.zeros(2), covariance, lo, hi, np.ones(2))
    assert actual == pytest.approx(math.log(exact), abs=1e-5)
