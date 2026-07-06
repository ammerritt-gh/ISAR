"""The ω(q) model over ISAR's accumulated results (LLM_HARNESS_DESIGN §2.2, §9.2).

Contract: ``fit(points) -> model`` then ``model.predict(q) -> (mean, sigma)`` — a predictive model of
ω₀ along the objective path with an uncertainty the acquisition function can act on.

This is the **local-linear windowed** form (open question §9.2 resolved: start local, GP-swappable):
for each query ``q`` we take the ``window_k`` nearest observed points, fit a weighted line
(weights = measurement precision ``1/σ²`` × a tricube locality kernel), and read the predictive mean
and its standard error off the weighted-least-squares parameter covariance. Two honesty properties fall
out for free and are what the acquisition function relies on:

- **interpolation shrinks with data** — near dense, precise points σ is small;
- **extrapolation grows away from data** — the WLS prediction variance carries a ``(q − q̄)² / Sₓₓ``
  term that grows quadratically past the data centroid, and an explicit extra penalty once ``q`` leaves
  the observed range, so proposing a scan far from anything measured is honestly flagged as uncertain.

The input ``points`` are ISAR's delivered results: ``(q, omega0, sigma)`` where ``sigma`` is the
**calibrated** interval half-width (never ISAR's floored prior sigma — §4 WARNING). ``q`` is any scalar
path coordinate (the driver uses arclength along the objective path). The model never produces a number
for the record; it only guides WHERE to measure next.

**GP-swappable interface.** ``fit(points)`` returns an object exposing ``predict(q) -> (mean, sigma)``.
A Gaussian-process implementation can drop in behind the same two-method contract without touching the
acquisition function.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

# Defaults (module-level so they are easy to tune / override in a subclass).
WINDOW_K = 5                 # how many nearest points enter each local fit
SIGMA_FLOOR = 1e-3           # meV; guards 1/σ² against a zero-width interval
EXTRAPOLATION_PENALTY = 1.0  # extra σ fraction per data-span stepped beyond the observed q-range
COLD_SIGMA = 1e3             # meV; the "I have no idea" σ returned with no data


@dataclass
class LocalLinearModel:
    """A fitted local-linear ω(q) model. Build via :func:`fit`; query via :meth:`predict`.

    Holds the observed points sorted by ``q``. ``predict`` is a pure function of them.
    """

    qs: np.ndarray
    ys: np.ndarray
    sigmas: np.ndarray
    window_k: int = WINDOW_K
    extrapolation_penalty: float = EXTRAPOLATION_PENALTY

    def predict(self, q: float) -> tuple[float, float]:
        """Predict ``(mean, sigma)`` of ω₀ at path coordinate ``q``.

        With no data returns ``(0.0, COLD_SIGMA)``; with one point a constant offset whose σ grows
        linearly with distance; with two or more a windowed weighted-least-squares line whose σ is the
        WLS prediction standard error plus an out-of-range extrapolation penalty.
        """
        q = float(q)
        n = len(self.qs)
        if n == 0:
            return 0.0, COLD_SIGMA
        if n == 1:
            sig0 = max(float(self.sigmas[0]), SIGMA_FLOOR)
            mean = float(self.ys[0])
            sigma = sig0 + sig0 * abs(q - float(self.qs[0]))
            return mean, sigma

        qs, ys, sg = self.qs, self.ys, self.sigmas
        # --- window: the k nearest points to q -----------------------------------
        d = np.abs(qs - q)
        k = min(n, self.window_k)
        idx = np.argsort(d, kind="stable")[:k]
        wq, wy, wsg = qs[idx], ys[idx], sg[idx]

        # --- weights: measurement precision × tricube locality -------------------
        w_meas = 1.0 / np.maximum(wsg, SIGMA_FLOOR) ** 2
        h = max(float(d[idx].max()), 1e-12)
        u = np.clip(d[idx] / h, 0.0, 1.0)
        k_loc = (1.0 - u ** 3) ** 3
        w = w_meas * np.maximum(k_loc, 1e-6)

        qref = float(np.average(wq, weights=w))
        X = np.column_stack([np.ones_like(wq), wq - qref])
        WX = w[:, None] * X
        XtWX = X.T @ WX
        XtWy = X.T @ (w * wy)
        try:
            cov = np.linalg.inv(XtWX)
            beta = cov @ XtWy
        except np.linalg.LinAlgError:
            # Degenerate window (e.g. all points at one q): fall back to a weighted constant.
            mean = float(np.average(wy, weights=w))
            sigma = float(np.sqrt(1.0 / np.sum(w)))
            return mean, self._penalize(q, sigma)

        xq = np.array([1.0, q - qref])
        mean = float(xq @ beta)
        var = float(xq @ cov @ xq)
        sigma = float(np.sqrt(max(var, 0.0)))
        # Never claim to know ω better than the best local scan measured it.
        sigma = max(sigma, SIGMA_FLOOR)
        return mean, self._penalize(q, sigma)

    def _penalize(self, q: float, sigma: float) -> float:
        """Inflate σ when ``q`` lies outside the observed range — honest extrapolation growth."""
        qmin, qmax = float(self.qs.min()), float(self.qs.max())
        span = max(qmax - qmin, 1e-9)
        if q < qmin:
            frac = (qmin - q) / span
        elif q > qmax:
            frac = (q - qmax) / span
        else:
            return sigma
        return sigma * (1.0 + self.extrapolation_penalty * frac)


def fit(points, *, window_k: int = WINDOW_K,
        extrapolation_penalty: float = EXTRAPOLATION_PENALTY) -> LocalLinearModel:
    """Fit a predictive ω(q) model to ISAR's delivered results.

    Args:
        points: an iterable of ``(q, omega0, sigma)`` — ``q`` a scalar path coordinate, ``omega0`` the
            fitted energy, ``sigma`` the **calibrated** interval half-width (never a prior sigma).
            Points with a non-finite ``q``/``omega0`` are dropped; a non-finite or non-positive
            ``sigma`` is floored (:data:`SIGMA_FLOOR`).
        window_k: how many nearest points enter each local fit.
        extrapolation_penalty: extra σ fraction per data-span beyond the observed range.

    Returns a :class:`LocalLinearModel` (``predict(q) -> (mean, sigma)``).
    """
    qs: list[float] = []
    ys: list[float] = []
    sg: list[float] = []
    for p in points:
        q, y, s = p
        q, y = float(q), float(y)
        if not (np.isfinite(q) and np.isfinite(y)):
            continue
        s = float(s)
        if not np.isfinite(s) or s <= 0.0:
            s = SIGMA_FLOOR
        qs.append(q)
        ys.append(y)
        sg.append(s)
    order = np.argsort(np.asarray(qs, dtype=float), kind="stable") if qs else np.array([], dtype=int)
    return LocalLinearModel(
        qs=np.asarray(qs, dtype=float)[order],
        ys=np.asarray(ys, dtype=float)[order],
        sigmas=np.asarray(sg, dtype=float)[order],
        window_k=int(window_k),
        extrapolation_penalty=float(extrapolation_penalty),
    )
