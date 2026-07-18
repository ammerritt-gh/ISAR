"""Small probability primitives shared across the Qt-free analysis and drive layers."""

from __future__ import annotations

import math

from scipy.special import erfcx


def lower_truncated_normal_moments(mu: float, sigma: float) -> tuple[float, float]:
    """Mean/std of ``N(mu, sigma**2)`` conditioned on values greater than or equal to zero.

    The ordinary inverse-Mills expression loses both the survival probability and the variance in
    the far negative tail. ``erfcx`` keeps the moderate-tail ratio finite; beyond eight standard
    deviations cancellation-free asymptotic moments avoid subtracting nearly equal values.
    """
    mu = float(mu)
    sigma = max(float(sigma), 1e-12)
    alpha = -mu / sigma

    if alpha > 8.0:
        inv = 1.0 / alpha
        inv2 = inv * inv
        mean_scaled = inv * (1.0 - 2.0 * inv2 + 10.0 * inv2 ** 2 - 74.0 * inv2 ** 3)
        var_scaled = inv2 * (1.0 - 6.0 * inv2 + 50.0 * inv2 ** 2 - 518.0 * inv2 ** 3)
        return max(sigma * mean_scaled, 0.0), sigma * math.sqrt(max(var_scaled, 0.0))

    if alpha <= 0.0:
        survival = 0.5 * math.erfc(alpha / math.sqrt(2.0))
        ratio = math.exp(-0.5 * alpha * alpha) / math.sqrt(2.0 * math.pi) / survival
    else:
        ratio = math.sqrt(2.0 / math.pi) / float(erfcx(alpha / math.sqrt(2.0)))
    mean = mu + sigma * ratio
    variance = sigma * sigma * max(1.0 + alpha * ratio - ratio * ratio, 0.0)
    return max(mean, 0.0), math.sqrt(variance)
