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

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

import numpy as np

from isar.analyze import loop
from isar.synth import backends, estimators

# Only spin up a process pool for the M-replicate loop when there is enough work to amortize the
# (Windows-spawn) worker startup. Below this, the serial path is faster.
_BOOT_PARALLEL_MIN_M = 16


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


@dataclass
class KdeModes:
    """KDE peak/valley summary of a sample density. ``split`` is the x-location of the valley
    between the two tallest peaks (the fork boundary) when ``bimodal``, else ``None``."""

    xs: np.ndarray
    density: np.ndarray
    peaks: list          # grid indices of qualifying local maxima
    bimodal: bool
    split: float | None


def kde_modes(samples, *, weights=None, grid: int = 256, rel_height: float = 0.1,
              dip: float = 0.75) -> KdeModes:
    """Summarise a (optionally weighted) sample density: a KDE is sampled on a grid, local
    maxima at least `rel_height` of the global peak are counted, and the two tallest are
    bimodal when separated by a valley dropping to ≤ `dip` of the shorter peak; `split` is
    that valley's location. Weighting is what lets §4's pooled across-model mixture be
    evaluated honestly — each survivor's samples carry its Akaike weight."""
    s = np.asarray(samples, dtype=float)
    finite = np.isfinite(s)
    s = s[finite]
    w = np.asarray(weights, dtype=float)[finite] if weights is not None else None
    if s.size < 20 or np.ptp(s) <= 0.0:
        return KdeModes(np.empty(0), np.empty(0), [], False, None)
    from scipy.stats import gaussian_kde
    try:
        kde = gaussian_kde(s, weights=w)
    except Exception:                              # singular covariance, etc.
        return KdeModes(np.empty(0), np.empty(0), [], False, None)
    xs = np.linspace(float(s.min()), float(s.max()), grid)
    d = np.asarray(kde(xs), dtype=float)
    dmax = float(d.max())
    if dmax <= 0.0:
        return KdeModes(xs, d, [], False, None)
    peaks = [i for i in range(1, grid - 1)
             if d[i] > d[i - 1] and d[i] >= d[i + 1] and d[i] >= rel_height * dmax]
    if len(peaks) < 2:
        return KdeModes(xs, d, peaks, False, None)
    a, b = sorted(sorted(peaks, key=lambda i: d[i])[-2:])
    valley_i = a + int(np.argmin(d[a:b + 1]))
    bimodal = bool(float(d[valley_i]) <= dip * min(float(d[a]), float(d[b])))
    return KdeModes(xs, d, peaks, bimodal, float(xs[valley_i]) if bimodal else None)


def is_bimodal(samples, *, weights=None, grid: int = 256, rel_height: float = 0.1,
               dip: float = 0.75) -> bool:
    """True if the sample density has two prominent modes separated by a real valley.
    Thin wrapper over :func:`kde_modes`; feeds §4's modality test (the two-phonon ambiguity
    appears here as a double-peaked bootstrap)."""
    return kde_modes(samples, weights=weights, grid=grid, rel_height=rel_height, dip=dip).bimodal


def _refit_replicate(counts_b, composite, x, ctx, theta_fit, bounds, be_names,
                     phonon_i, prim_rank, est, backend, ncall):
    """Refit one Poisson draw and reduce it to (ω₀, Γ), or ``None`` if the refit failed.

    Warm-starts at the fit; a failed refit is counted (not fatal), matching the serial contract.
    """
    def costv(theta):
        return est.cost(counts_b, composite.evaluate(x, theta, ctx))

    # Bootstrap refits warm-start at the fit and only need a point estimate — a failed refit
    # is counted (`n_converged`), never fatal — so they run on MINUIT's plain default budget,
    # not the raised certification budget the main fit uses. This keeps the per-scan bootstrap
    # cost unchanged from before the FIX-1 budget bump (which is per-fit, not per-refit).
    br = backends.minimize(backend, costv, theta_fit, bounds, be_names, minos=False, ncall=ncall)
    if not br.success:
        return None
    w0 = np.array([np.sqrt(float(br.x[i]) ** 2 + float(br.x[i + 1]) ** 2) for i in phonon_i])
    sel_pos = phonon_i[int(np.argsort(w0)[prim_rank])]  # global index of the primary mode
    od_b, g_b = float(br.x[sel_pos]), float(br.x[sel_pos + 1])
    return (float(np.sqrt(od_b ** 2 + g_b ** 2)), g_b)


def _bootstrap_chunk(args):
    """A worker-process chunk: rebuild the problem from plain data (``hyp``/``ctx`` are picklable;
    iminuit closures are not), then refit each pre-drawn ``counts_b`` in order. Returns a list of
    (ω₀, Γ) tuples / ``None``, in the same order as the chunk — so concatenating chunks in
    submission order reproduces the serial sample sequence exactly (bit-for-bit determinism)."""
    (hyp, ctx, theta_fit, counts_chunk, phonon_i, prim_rank, be_names, backend, ncall) = args
    prob = loop.build_problem(hyp, ctx)
    est = estimators.get_estimator("baker_cousins")
    x = ctx.x_used
    return [_refit_replicate(cb, prob.composite, x, ctx, theta_fit, prob.bounds, be_names,
                             phonon_i, prim_rank, est, backend, ncall)
            for cb in counts_chunk]


def parametric_bootstrap(hyp, ctx, fit_result, *, M: int = 200, seed: int = 12345,
                         backend: str = "iminuit", interval: str = "percentile",
                         level: float = 0.68, workers: int = 1) -> BootstrapResult:
    """Bootstrap one fitted hypothesis; returns transformed ω₀/Γ intervals + raw samples.

    Generates from the best fit (`fit_result.params`), refits each Poisson draw warm-started
    at the fit (the §11 re-entrancy contract), and reduces the converged refits. Failed
    refits are counted (`n_converged`), never silently dropped.

    ``workers`` > 1 spreads the M embarrassingly-parallel refits over a process pool. The M
    Poisson draws are pre-generated from a single RNG and dispatched in order, so the sample
    sequence — and therefore every interval — is identical to the serial path for a given
    ``seed`` regardless of ``workers``."""
    if interval != "percentile":
        raise NotImplementedError(f"interval={interval!r} not implemented; BCa is D10-pending")

    prob = loop.build_problem(hyp, ctx)
    names = prob.free_names
    theta_fit = np.array([fit_result.params[n] for n in names], dtype=float)
    x = ctx.x_used
    lam = np.clip(prob.composite.evaluate(x, theta_fit, ctx), 0.0, None)
    est = estimators.get_estimator("baker_cousins")
    be_names = [loop._sanitize(n) for n in names]

    # Per-mode canonicalization (kills bootstrap label-switching on close modes): each phonon's
    # (Ω_d, Γ) are at global indices (i, i+1). The PRIMARY mode is fixed from the ORIGINAL fit;
    # per replicate we sort phonons ascending by ω₀ and take the mode at the primary's sorted
    # rank. Single-phonon fits reduce to the previous behaviour (one mode, rank 0).
    phonon_i = prob.composite.phonon_indices()
    fit_w0 = [np.sqrt(theta_fit[i] ** 2 + theta_fit[i + 1] ** 2) for i in phonon_i]
    order = list(np.argsort(fit_w0))                    # component-order positions, ascending ω₀
    prim_global = prob.composite.primary_phonon_index(
        theta_fit, None, getattr(ctx, "prior_omega", None))
    prim_pos = phonon_i.index(prim_global)
    prim_rank = order.index(prim_pos)                   # primary's rank in the sorted order

    ncall = backends._default_migrad_ncall(len(be_names))
    # Draw ALL replicates up front from the single RNG — this fixes the draw sequence so the result
    # is worker-count-independent (determinism), and the draws are cheap vs the refits.
    rng = np.random.default_rng(seed)
    M = int(M)
    counts_all = [rng.poisson(lam).astype(float) for _ in range(M)]

    workers = max(1, int(workers))
    if workers > 1 and M >= _BOOT_PARALLEL_MIN_M:
        n_chunks = min(workers, M)
        chunks = [counts_all[i::n_chunks] for i in range(n_chunks)]  # round-robin, balanced
        # Reassembly must restore draw order (chunk i holds draws i, i+n_chunks, ...).
        results: list = [None] * M
        args = [(hyp, ctx, theta_fit, ch, phonon_i, prim_rank, be_names, backend, ncall)
                for ch in chunks]
        with ProcessPoolExecutor(max_workers=n_chunks) as ex:
            for ci, chunk_res in enumerate(ex.map(_bootstrap_chunk, args)):
                results[ci::n_chunks] = chunk_res
    else:
        results = [_refit_replicate(cb, prob.composite, x, ctx, theta_fit, prob.bounds, be_names,
                                    phonon_i, prim_rank, est, backend, ncall)
                   for cb in counts_all]

    o_samples = [r[0] for r in results if r is not None]
    g_samples = [r[1] for r in results if r is not None]
    n_conv = len(o_samples)

    o = np.array(o_samples, dtype=float)
    g = np.array(g_samples, dtype=float)
    return BootstrapResult(
        omega0_interval=_percentile_interval(o, level),
        gamma_interval=_percentile_interval(g, level),
        omega0_samples=o, gamma_samples=g,
        n_converged=n_conv, M=int(M), interval_type=interval,
        bimodal=is_bimodal(o), seed=int(seed),
    )
