"""The tie-breaker penalty between two close modes: a pair's separability and its penalty.

Design: the inter-spectra pass, "The tie-breaker penalty between close modes". Numpy only, Qt-free.

Each mode is a Gaussian-equivalent line. The separability D is the Poisson deviance between the pair's expected
counts and the best single merged line, in closed form (method A). The penalty in nats is
cap * share_factor * fade(D, d0); the engine (``fast_pass._Engine.pair_penalty``) wires it in, and nothing here
reads a setting.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from isar.analyze.estimators import FMIN

FWHM_PER_SIGMA = 2.3548   # a Gaussian's FWHM is 2.3548 standard deviations


@dataclass(frozen=True)
class PairMode:
    centre: float   # meV
    width: float    # meV FWHM, resolution included
    counts: float   # >= 0, expected counts on the Stokes side


def in_range(a: PairMode, b: PairMode) -> bool:
    """Pairs further apart than three combined widths are not evaluated (saves time, no cutoff)."""
    return abs(a.centre - b.centre) <= 3.0 * (a.width + b.width)


def _line(E, dE, centre: float, sd: float, counts: float):
    """A Gaussian line of ``counts`` in channels of width ``dE`` at energies ``E``."""
    z = (E - centre) / sd
    return counts * dE * np.exp(-0.5 * z * z) / (sd * math.sqrt(2.0 * math.pi))


def separability(a: PairMode, b: PairMode, E, dE, mu) -> float:
    """D, in deviance units: the pair's lines against the moment-matched merged line, on the channels ``E``
    (widths ``dE``), weighted by the full model ``mu``; channels at or below FMIN are skipped."""
    # shortcut: Gaussian-equivalent closed form; against the exact reference (DHO averaged over a free-width spread)
    # it meets the 25% half-point band in 8 of 16 synthetic cells and fades early for broader pairs; upgrade if a
    # real spectrum shows the fade in the wrong place
    n = a.counts + b.counts
    if n <= 0.0:
        return 0.0
    E, dE, mu = np.asarray(E, dtype=float), np.asarray(dE, dtype=float), np.asarray(mu, dtype=float)
    sa, sb = a.width / FWHM_PER_SIGMA, b.width / FWHM_PER_SIGMA
    p = a.counts / n
    centre = p * a.centre + (1.0 - p) * b.centre
    sm = math.sqrt(p * sa * sa + (1.0 - p) * sb * sb + p * (1.0 - p) * (a.centre - b.centre) ** 2)
    diff = (_line(E, dE, a.centre, sa, a.counts) + _line(E, dE, b.centre, sb, b.counts)
            - _line(E, dE, centre, sm, n))
    ok = mu > FMIN
    return float(np.sum(diff[ok] ** 2 / mu[ok]))


def fade(D: float, d0: float) -> float:
    """Half at D = d0 ln 2, mostly gone by about 3 d0."""
    return math.exp(-D / d0)


def share_factor(a: PairMode, b: PairMode) -> float:
    """4 p (1 - p): largest for an equal pair, zero as either mode fades."""
    n = a.counts + b.counts
    if n <= 0.0:
        return 0.0
    p = a.counts / n
    return 4.0 * p * (1.0 - p)
