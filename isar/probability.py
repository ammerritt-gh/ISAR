"""Small probability primitives shared across the Qt-free analysis and drive layers."""

from __future__ import annotations

import math

from scipy.special import erfcx, ndtr


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


def truncated_normal_moments(mu: float, sigma: float, lo: float, hi: float) -> tuple[float, float]:
    """Mean/std of ``N(mu, sigma**2)`` conditioned on ``lo <= x <= hi``.

    The closed form rather than ``scipy.stats.truncnorm``: this sits under every belief prediction,
    and the frozen-distribution machinery costs ~100 us a call.  ``hi = inf`` delegates to
    :func:`lower_truncated_normal_moments`, whose cancellation-free asymptotics beat this expression
    far into the tail.  When the interval holds essentially no mass the distribution has collapsed
    onto the nearer edge, and that is what is returned.
    """
    mu, lo, hi = float(mu), float(lo), float(hi)
    sigma = max(float(sigma), 1e-12)
    if not hi > lo:
        raise ValueError("truncation interval must have hi > lo")
    if math.isinf(hi):
        mean, std = lower_truncated_normal_moments(mu - lo, sigma)
        return mean + lo, std
    alpha, beta = (lo - mu) / sigma, (hi - mu) / sigma
    mass = float(ndtr(beta) - ndtr(alpha))
    if mass < 1e-12:
        # The interval holds essentially no mass: the distribution has collapsed onto the
        # nearer edge.  Report the ORIGINAL sigma with it, never 0.0 -- a belief that claims a
        # branch energy to +-0 makes every likelihood against it degenerate, and the honest
        # statement is "pinned at the band edge, to within the spread the fit had".
        return (lo if mu < lo else hi), sigma
    phi_a = math.exp(-0.5 * alpha * alpha) / math.sqrt(2.0 * math.pi)
    phi_b = math.exp(-0.5 * beta * beta) / math.sqrt(2.0 * math.pi)
    ratio = (phi_a - phi_b) / mass
    mean = mu + sigma * ratio
    variance = sigma * sigma * (1.0 + (alpha * phi_a - beta * phi_b) / mass - ratio * ratio)
    return min(max(mean, lo), hi), math.sqrt(max(variance, 0.0))


def extrapolation_model_sigma(distance: float, period: float | None,
                              domain_hi: float | None) -> float:
    """Model error of carrying a LINEAR branch prediction ``distance`` in reduced q past its data.

    This is a DECLARED SMOOTHNESS ASSUMPTION, not a derived bound.  Periodicity plus a bounded band
    do not bound curvature -- a branch may stay inside the band and still turn arbitrarily sharply
    over a short interval.  What is assumed is that the dispersion's curvature is set by its lowest
    harmonic, ``|omega''| <~ domain_hi * (pi / period) ** 2``, which is the quantitative form of the
    "roughly linear nearby" clause of the class prior: a smoothness scale the scientist declares,
    on the same footing as a slope prior, and wrong for a branch with structure finer than one zone.
    A straight line carried a distance ``d`` beyond its nearest anchor is then taken to be wrong by
    up to ``0.5 * |omega''| * d ** 2``.  Added in quadrature to the fitted spread, it is what stops
    an extrapolated acoustic belief from claiming the optic's peak on the WLS spread alone.

    Lives here because the drive and session layers each carry their own belief implementation and
    the two must agree exactly.  Returns 0 when the period or the band is unknown, or at an anchor.
    """
    if period is None or domain_hi is None or float(distance) <= 0.0:
        return 0.0
    curvature = float(domain_hi) * (math.pi / float(period)) ** 2
    return 0.5 * curvature * float(distance) ** 2
