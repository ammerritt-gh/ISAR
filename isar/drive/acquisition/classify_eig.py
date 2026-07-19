"""Deterministic branch-classification mutual information for drive-side acquisition.

The driver cannot import :mod:`isar.session`, so this module consumes the flat belief rows persisted
in ``card.state.json``.  Mixture entropy is evaluated by applying the committed GH32 rule *within
each Gaussian component*, then averaging by branch probability.  This is the composite rule frozen
as ``isar.branch-mi/gh32-component-v1`` in the objective fingerprint.
"""
from __future__ import annotations

import math
from typing import Iterable

import numpy as np

from isar.probability import lower_truncated_normal_moments

from isar.schema.hypotheses import ACQUISITION_ESTIMATOR_VERSION


GH32_NODES = np.array((-7.125813909830728, -6.409498149269661, -5.812225949515914,
    -5.2755509865158805, -4.777164503502596, -4.305547953351199, -3.853755485471445,
    -3.417167492818571, -2.992490825002374, -2.5772495377323175, -2.169499183606112,
    -1.7676541094632015, -1.3703764109528718, -0.9765004635896828, -0.5849787654359324,
    -0.19484074156939934, 0.19484074156939934, 0.5849787654359324, 0.9765004635896828,
    1.3703764109528718, 1.7676541094632015, 2.169499183606112, 2.5772495377323175,
    2.992490825002374, 3.417167492818571, 3.853755485471445, 4.305547953351199,
    4.777164503502596, 5.2755509865158805, 5.812225949515914, 6.409498149269661,
    7.125813909830728), dtype=float)
GH32_WEIGHTS = np.array((7.310676427384096e-23, 9.231736536518258e-19,
    1.1973440170928503e-15, 4.2150102113264155e-13, 5.933291463396676e-11,
    4.098832164770879e-09, 1.5741677925455882e-07, 3.650585129562378e-06,
    5.416584061819991e-05, 0.000536268365527972, 0.003654890326654426,
    0.017553428831573438, 0.06045813095591269, 0.15126973407664232,
    0.27745814230252996, 0.37523835259280247, 0.37523835259280247,
    0.27745814230252996, 0.15126973407664232, 0.06045813095591269,
    0.017553428831573438, 0.003654890326654426, 0.000536268365527972,
    5.416584061819991e-05, 3.650585129562378e-06, 1.5741677925455882e-07,
    4.098832164770879e-09, 5.933291463396676e-11, 4.2150102113264155e-13,
    1.1973440170928503e-15, 9.231736536518258e-19, 7.310676427384096e-23), dtype=float)
ESTIMATOR_VERSION = ACQUISITION_ESTIMATOR_VERSION
SIGMA_SLACK = 0.3
LOCALITY_BW = 0.35
WINDOW_K = 5


def _normal_pdf(x: np.ndarray, mean: float, sigma: float) -> np.ndarray:
    sigma = max(float(sigma), 1e-12)
    z = (x - float(mean)) / sigma
    return np.exp(-0.5 * z * z) / (sigma * math.sqrt(2.0 * math.pi))


def belief_predict(row: dict, u: float, *, data_only: bool = False) -> tuple[float, float]:
    """Qt/session-free mirror of parameter-prior branch prediction from a persisted belief row."""
    anchors = list(row.get("anchors") or [])
    if anchors:
        us = np.array([float(a["u"]) for a in anchors])
        ys = np.array([float(a["omega0"]) for a in anchors])
        ws = np.array([max(float(a.get("weight", 1.0)), 0.0) for a in anchors])
        distances = np.abs(us - float(u))
        idx = np.argsort(distances, kind="stable")[:min(len(us), WINDOW_K)]
        X = np.column_stack([np.ones(len(idx)), us[idx]])
        weights = (ws[idx] / (SIGMA_SLACK ** 2)
                   * np.exp(-0.5 * (distances[idx] / LOCALITY_BW) ** 2))
        precision = X.T @ (weights[:, None] * X)
        rhs = X.T @ (weights * ys[idx])
    else:
        precision = np.zeros((2, 2), dtype=float)
        rhs = np.zeros(2, dtype=float)
    prior = dict(row.get("parameter_prior") or {})
    if prior and not data_only:
        confidence = max(float(prior.get("confidence", 0.0)), 0.0)
        gap, slope = prior.get("zc_energy") or {}, prior.get("slope_zc") or {}
        means = np.array([float(gap.get("mean", gap.get("mode", 0.0))),
                          float(slope.get("mean", 0.0))])
        sigmas = np.array([max(float(gap.get("sigma", 1e3)), 1e-9),
                           max(float(slope.get("sigma", 1e3)), 1e-9)])
        prior_precision = confidence / np.square(sigmas)
        precision += np.diag(prior_precision)
        rhs += prior_precision * means
    if not np.any(precision):
        return 0.0, 1e3
    cov = np.linalg.pinv(precision, hermitian=True)
    xq = np.array([1.0, float(u)])
    mu = float(xq @ (cov @ rhs))
    sigma = max(math.sqrt(max(float(xq @ cov @ xq), 0.0)), SIGMA_SLACK)
    if data_only or not prior:
        return mu, sigma
    return lower_truncated_normal_moments(mu, sigma)


def assignment_mi(components: Iterable[dict], *, meas_sigma: float,
                  differentiation_tolerance_meV: float | None = None) -> float:
    """Mutual information ``I(branch; measured energy)`` in nats.

    Each component is ``{mean, sigma, probability}``.  MI naturally self-throttles because it cannot
    exceed current label entropy; the optional differentiation gate zeros it once every rival is
    already known more tightly than required.
    """
    rows = [dict(c) for c in components if float(c.get("probability", 0.0)) > 0.0]
    if len(rows) < 2:
        return 0.0
    if differentiation_tolerance_meV is not None and all(
            float(c.get("sigma", math.inf)) <= float(differentiation_tolerance_meV) for c in rows):
        return 0.0
    probabilities = np.array([float(c["probability"]) for c in rows], dtype=float)
    probabilities /= probabilities.sum()
    means = np.array([float(c["mean"]) for c in rows], dtype=float)
    sigmas = np.sqrt(np.square([max(float(c.get("sigma", 0.0)), 0.0) for c in rows])
                     + float(meas_sigma) ** 2)
    mixture_entropy = 0.0
    for p_i, mean_i, sigma_i in zip(probabilities, means, sigmas):
        y = mean_i + math.sqrt(2.0) * sigma_i * GH32_NODES
        density = np.zeros_like(y)
        for p_j, mean_j, sigma_j in zip(probabilities, means, sigmas):
            density += p_j * _normal_pdf(y, mean_j, sigma_j)
        expected_log = float(np.dot(GH32_WEIGHTS, np.log(np.maximum(density, 1e-300)))
                             / math.sqrt(math.pi))
        mixture_entropy -= p_i * expected_log
    conditional_entropy = float(np.dot(probabilities,
        0.5 * np.log(2.0 * math.pi * math.e * np.square(sigmas))))
    return max(mixture_entropy - conditional_entropy, 0.0)


def information_per_cost(target_information: float, classification_information: float, *,
                         classification_weight: float, cost: float) -> float:
    """O6 ranking: dimensionless information divided by positive measurement cost."""
    if float(cost) <= 0.0:
        return math.inf
    return (max(float(target_information), 0.0)
            + max(float(classification_weight), 0.0) * max(float(classification_information), 0.0)) / float(cost)


def lambda_eff(classification_weight: float, label_probabilities: Iterable[float]) -> float:
    """Explicit confidence throttle in addition to MI's intrinsic ``MI <= H`` bound."""
    p = np.array([max(float(v), 0.0) for v in label_probabilities], dtype=float)
    if not p.size or p.sum() <= 0.0:
        return 0.0
    p /= p.sum()
    entropy = -float(np.sum(p[p > 0.0] * np.log(p[p > 0.0])))
    return max(float(classification_weight), 0.0) * entropy / math.log(max(len(p), 2))


def widen_factor(absent_signal_count: int, *, step: float = 0.5, max_steps: int = 3) -> float:
    """Crash-safe O11 widening schedule; its input is a persisted decline counter."""
    return 1.0 + float(step) * min(max(int(absent_signal_count), 0), int(max_steps))


def opening_order(grid_us: Iterable[float], target_hypothesis: dict | None, *,
                  resolution_fwhm: float, predictions: dict[float, tuple[float, float]] | None = None) -> list[float]:
    """Character-aware opening order; absent/explicit-null uses largest-predicted-sigma survey."""
    us = [float(u) for u in grid_us]
    predictions = predictions or {}
    name = str((target_hypothesis or {}).get("name", ""))
    if name == "optic":
        return sorted(us)
    if name == "acoustic":
        resolvable = [u for u in sorted(us) if predictions.get(u, (0.0, 0.0))[0] >= resolution_fwhm]
        unresolved = [u for u in sorted(us) if u not in resolvable]
        return resolvable + unresolved
    return sorted(us, key=lambda u: (-predictions.get(u, (0.0, math.inf))[1], u))
