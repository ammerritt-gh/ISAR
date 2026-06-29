"""Parametric bootstrap — the authoritative within-model error (§5.2).

Take the best-fit DHO⊗resolution model as the generating model, draw `M` Poisson
realizations of the counts, refit each under Baker–Cousins, and let the scatter of the
refit parameters be the error. The reported ω₀ interval is built by transforming **each
draw** (ω₀=√(Ω_d²+Γ²)), not by propagating the point HESSE σ — this is the F4 fix.

It is the production promotion of the Phase-1 MC harness (``isar.synth.score``) from
MC-against-truth to MC-against-fit, and has no silent-failure mode: a degenerate fit shows
up as a wide or multimodal bootstrap, exposed by :func:`is_bimodal`, not as an exception.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from isar.analyze import loop
from isar.synth import backends, estimators


@dataclass
class BootstrapResult:
    omega0_interval: tuple[float, float, float]   # (lo, median, hi) at `level`
    gamma_interval: tuple[float, float, float]
    omega0_samples: np.ndarray                    # converged draws only
    gamma_samples: np.ndarray
    n_converged: int
    M: int
    interval_type: str
    bimodal: bool
    seed: int


def _percentile_interval(samples: np.ndarray, level: float) -> tuple[float, float, float]:
    if samples.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    lo_q, hi_q = 100.0 * (1.0 - level) / 2.0, 100.0 * (1.0 + level) / 2.0
    return (float(np.percentile(samples, lo_q)),
            float(np.median(samples)),
            float(np.percentile(samples, hi_q)))


def is_bimodal(samples, *, grid: int = 256, rel_height: float = 0.1, dip: float = 0.75) -> bool:
    """True if the sample density has two prominent modes separated by a real valley.

    A KDE is sampled on a grid; local maxima at least `rel_height` of the global peak are
    counted, and the two tallest must be separated by a valley dropping to ≤ `dip` of the
    shorter peak. Feeds §4's modality test (the two-phonon ambiguity appears here)."""
    s = np.asarray(samples, dtype=float)
    s = s[np.isfinite(s)]
    if s.size < 20 or np.ptp(s) <= 0.0:
        return False
    from scipy.stats import gaussian_kde
    try:
        kde = gaussian_kde(s)
    except Exception:                              # singular covariance, etc.
        return False
    xs = np.linspace(float(s.min()), float(s.max()), grid)
    d = np.asarray(kde(xs), dtype=float)
    dmax = float(d.max())
    if dmax <= 0.0:
        return False
    peaks = [i for i in range(1, grid - 1)
             if d[i] > d[i - 1] and d[i] >= d[i + 1] and d[i] >= rel_height * dmax]
    if len(peaks) < 2:
        return False
    a, b = sorted(sorted(peaks, key=lambda i: d[i])[-2:])
    valley = float(d[a:b + 1].min())
    return bool(valley <= dip * min(float(d[a]), float(d[b])))


def parametric_bootstrap(hyp, ctx, fit_result, *, M: int = 200, seed: int = 12345,
                         backend: str = "iminuit", interval: str = "percentile",
                         level: float = 0.68) -> BootstrapResult:
    """Bootstrap one fitted hypothesis; returns transformed ω₀/Γ intervals + raw samples.

    Generates from the best fit (`fit_result.params`), refits each Poisson draw warm-started
    at the fit (the §11 re-entrancy contract), and reduces the converged refits. Failed
    refits are counted (`n_converged`), never silently dropped."""
    if interval != "percentile":
        raise NotImplementedError(f"interval={interval!r} not implemented; BCa is D10-pending")

    prob = loop.build_problem(hyp, ctx)
    names = prob.free_names
    theta_fit = np.array([fit_result.params[n] for n in names], dtype=float)
    x = ctx.x_used
    lam = np.clip(prob.composite.evaluate(x, theta_fit, ctx), 0.0, None)
    est = estimators.get_estimator("baker_cousins")
    be_names = [loop._sanitize(n) for n in names]
    od_i = names.index(next(n for n in names if n.endswith(".Omega_d")))
    g_i = names.index(next(n for n in names if n.endswith(".Gamma")))

    rng = np.random.default_rng(seed)
    o_samples, g_samples, n_conv = [], [], 0
    for _ in range(int(M)):
        counts_b = rng.poisson(lam).astype(float)

        def costv(theta, _cb=counts_b):
            return est.cost(_cb, prob.composite.evaluate(x, theta, ctx))

        br = backends.minimize(backend, costv, theta_fit, prob.bounds, be_names, minos=False)
        if not br.success:
            continue
        n_conv += 1
        od_b, g_b = float(br.x[od_i]), float(br.x[g_i])
        o_samples.append(float(np.sqrt(od_b ** 2 + g_b ** 2)))
        g_samples.append(g_b)

    o = np.array(o_samples, dtype=float)
    g = np.array(g_samples, dtype=float)
    return BootstrapResult(
        omega0_interval=_percentile_interval(o, level),
        gamma_interval=_percentile_interval(g, level),
        omega0_samples=o, gamma_samples=g,
        n_converged=n_conv, M=int(M), interval_type=interval,
        bimodal=is_bimodal(o), seed=int(seed),
    )
