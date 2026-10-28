"""Synthetic regressions for Laplace stencils and truncated Gaussian support."""
import math

import numpy as np
import pytest
from scipy.integrate import quad

from isar.analyze import prior


@pytest.mark.parametrize("lo, hi, theta", [
    ([0.0], [1.0], [0.99999]),
    ([-1.0], [0.0], [-0.99999]),
    ([1000.0], [1000.001], [1000.0005]),
])
def test_near_edge_stencil_stays_inside_support(lo, hi, theta):
    lo, hi, theta = (np.asarray(v) for v in (lo, hi, theta))
    sigma = 0.05
    evaluated = []

    def objective(x):
        evaluated.append(x.copy())
        if np.any(x < lo) or np.any(x > hi):
            return math.inf
        return 0.5 * float(np.sum(((x - theta) / sigma) ** 2))

    result = prior.laplace_log_evidence(objective, theta, lo, hi)
    exact = quad(lambda x: math.exp(-0.5 * ((x - theta[0]) / sigma) ** 2), lo[0], hi[0])[0]

    assert result.status == "ok_boundary", result.reason
    assert np.all((np.asarray(evaluated) >= lo) & (np.asarray(evaluated) <= hi))
    assert result.ln_z == pytest.approx(math.log(exact), abs=1e-7)


def test_near_edges_keep_mixed_hessian_stencil_inside_support():
    lo, hi = np.array([-1.0, 0.0]), np.array([0.0, 1.0])
    theta = np.array([-0.99999, 0.99999])
    covariance = np.array([[0.0025, 0.0008], [0.0008, 0.0025]])
    precision = np.linalg.inv(covariance)
    evaluated = []

    def objective(x):
        evaluated.append(x.copy())
        if np.any(x < lo) or np.any(x > hi):
            return math.inf
        delta = x - theta
        return 0.5 * float(delta @ precision @ delta)

    result = prior.laplace_log_evidence(objective, theta, lo, hi)

    assert result.status == "ok_boundary", result.reason
    assert np.all((np.asarray(evaluated) >= lo) & (np.asarray(evaluated) <= hi))
    np.testing.assert_allclose(result.cov, covariance, rtol=1e-7)
