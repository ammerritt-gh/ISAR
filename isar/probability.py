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


#: Largest group velocity |domega/dq| a branch is assumed to reach, meV per r.l.u.  The declared
#: continuity scale: energy is a continuous function of q, so the CHANGE between two nearby q is
#: bounded by this times their separation and goes to zero with it.  Matches
#: ``branch_assignment.ACOUSTIC_MAX_SLOPE``, which named the same quantity for the acoustic branch.
MAX_GROUP_VELOCITY = 6.0
#: How far an extrapolated trend carries in r.l.u. before it has decayed to the anchored level.  A
#: fitted slope is evidence about the q where it was measured, not a promise about q further out,
#: so beyond the anchors it reverts rather than continuing without limit.
CONTINUITY_LENGTH = 0.25


def continuity_adjust(mu_fit: float, sigma_fit: float, anchored_level: float,
                      anchored_sigma: float, distance_outside: float,
                      distance_to_anchor: float) -> tuple[float, float]:
    """Apply continuity to a locally fitted prediction: ``(mean, sigma)``.

    The governing statement of the predictive, in the owner's words: "from q to q, the change in
    energy reduces to zero as deltaQ becomes 0".  A dispersion is a continuous function of q with a
    bounded group velocity, so two nearby q have nearby energies and the further apart they are the
    less one says about the other.

    Two consequences, and the local linear fit obeys neither on its own.  The MEAN beyond the
    anchored range reverts toward the level at the range's edge over :data:`CONTINUITY_LENGTH`,
    because an extrapolated trend is not a measurement -- carrying a fitted slope without limit is
    what put the acoustic at 7.26 where the truth was 6.00, landing it on the rival's peak.  The
    SIGMA is the continuity bound itself, :data:`MAX_GROUP_VELOCITY` times the distance to the
    nearest anchor, in quadrature with the fit's own uncertainty: zero at an anchor, growing with
    separation, so a prediction far from any measurement is honestly uncertain rather than
    confidently wrong.

    The sigma is a BOUND, not an addition: continuity says the energy cannot have moved further
    than the group velocity allows, so a line whose own uncertainty exceeds that is not evidence of
    a wider belief -- it is the absence of evidence, and the bound replaces it.  That is the whole
    optic case: one anchor leaves the SLOPE unconstrained, the line's own sigma 0.3 r.l.u. out is
    about 9 meV, band truncation then drags the mean from 7.7 down to 4.8, and the branch lands on
    its rival.  Where the fit is genuinely tighter than the bound -- interpolation between dense
    anchors -- the fit wins and nothing changes.

    Inside the anchored range ``distance_outside`` is zero and both are the fit unchanged.
    Supersedes the earlier curvature term, which asserted the same bounded-change idea through the
    lowest harmonic of the zone; one statement of continuity, not two.
    """
    carried = math.exp(-float(distance_outside) / CONTINUITY_LENGTH)
    mu = anchored_level + (mu_fit - anchored_level) * carried
    bound = math.hypot(float(anchored_sigma), MAX_GROUP_VELOCITY * float(distance_to_anchor))
    return mu, min(float(sigma_fit), bound)
