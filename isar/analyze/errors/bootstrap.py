"""Parametric bootstrap — the authoritative within-model error (§5.2).

Take the best-fit DHO⊗resolution model as the generating model, draw `M` Poisson
realizations of the counts, refit each under Baker–Cousins, and let the scatter of the
refit parameters be the error. The reported ω₀ interval is built from **each draw**'s fitted
``.omega0``, not by propagating the point HESSE σ — this is the F4 fix.

It is the production promotion of the Phase-1 MC harness (``isar.synth.score``) from
MC-against-truth to MC-against-fit, and has no silent-failure mode: a degenerate fit shows
up as a wide or multimodal bootstrap, exposed by :func:`is_bimodal`, not as an exception.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
import logging

import numpy as np

from isar.analyze import loop
from isar.analyze import backends, estimators

# Only spin up a process pool for the M-replicate loop when there is enough work to amortize the
# (Windows-spawn) worker startup. Below this, the serial path is faster.
_BOOT_PARALLEL_MIN_M = 16
log = logging.getLogger(__name__)


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
    parameter_names: tuple[str, ...]
    parameter_samples: np.ndarray


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
                     phonon_i, prim_rank, est, backend, ncall, penalty=None,
                     fixed_pos=(), fixed_vals=()):
    """Refit one Poisson draw and reduce it to (ω₀, Γ), or ``None`` if the refit failed.

    Warm-starts at the fit; a failed refit is counted (not fatal), matching the serial contract.
    When the fit was soft-constrained, ``penalty`` is added to the refit objective too (contract 1),
    so the replicate scatter — and thus the reported interval — reflects the prior, not a penalised
    point estimate with unpenalised scatter. When the fit was hard-fixed (branch-conditional), the
    same params are frozen in every replicate (``fixed_pos``/``fixed_vals`` via
    :func:`loop._minimize_fixed`), so the interval reflects the conditioned model, not free Γ scatter.
    """
    def objective(theta):
        c = est.cost(counts_b, composite.evaluate(x, theta, ctx))
        return c + penalty(theta) if penalty is not None else c

    # Bootstrap refits warm-start at the fit and only need a point estimate — a failed refit
    # is counted (`n_converged`), never fatal — so they run on MINUIT's plain default budget,
    # not the raised certification budget the main fit uses. This keeps the per-scan bootstrap
    # cost unchanged from before the FIX-1 budget bump (which is per-fit, not per-refit).
    br = loop._minimize_fixed(backend, objective, theta_fit, bounds, be_names,
                              list(fixed_pos), list(fixed_vals), minos=False, ncall=ncall)
    if not br.success:
        return None
    w0 = np.array([float(br.x[i]) for i in phonon_i])
    sel_pos = phonon_i[int(np.argsort(w0)[prim_rank])]  # global index of the primary mode
    return (float(br.x[sel_pos]), float(br.x[sel_pos + 1]), np.asarray(br.x, dtype=float))


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
                             phonon_i, prim_rank, est, backend, ncall, prob.penalty,
                             prob.fixed_pos, prob.fixed_vals)
            for cb in counts_chunk]


def _omega0_halfwidth(results, level: float) -> float:
    """The ω₀ percentile half-width of the converged draws so far (adaptive-M stability metric)."""
    o = np.array([r[0] for r in results if r is not None], dtype=float)
    lo, _med, hi = _percentile_interval(o, level)
    return (hi - lo) / 2.0 if np.isfinite(hi) and np.isfinite(lo) else float("inf")


def parametric_bootstrap(hyp, ctx, fit_result, *, M: int = 200, seed: int = 12345,
                         backend: str = "iminuit", interval: str = "percentile",
                         level: float = 0.68, workers: int = 1, executor=None,
                         adaptive: bool = False, block: int = 32, m_min: int = 64,
                         rel_eps: float = 0.05) -> BootstrapResult:
    """Bootstrap one fitted hypothesis; returns transformed ω₀/Γ intervals + raw samples.

    Generates from the best fit (`fit_result.params`), refits each Poisson draw warm-started
    at the fit (the §11 re-entrancy contract), and reduces the converged refits. Failed
    refits are counted (`n_converged`), never silently dropped.

    ``workers`` > 1 spreads the embarrassingly-parallel refits over a process pool. The Poisson
    draws come off a single seeded RNG in order and are reassembled in draw order, so the sample
    sequence — and therefore every interval — is identical to the serial path for a given
    ``seed`` regardless of ``workers`` or where the refits run.

    ``executor`` (a shared ``ProcessPoolExecutor``) runs the refit chunks on an already-open pool
    instead of spawning one per call; the callee never closes it. Pass ``workers`` = the pool's
    size so chunking matches. ``adaptive`` draws in blocks of ``block`` and stops once the ω₀
    half-width is stable (relative change < ``rel_eps`` for two consecutive blocks) with
    ``≥ m_min`` draws in, capped at ``M`` — a no-early-stop adaptive run is bit-identical to the
    fixed path (same RNG order). Defaults keep every existing caller bit-identical."""
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
    # (ω₀, Γ) are at global indices (i, i+1). The PRIMARY mode is fixed from the ORIGINAL fit;
    # per replicate we sort phonons ascending by ω₀ and take the mode at the primary's sorted
    # rank. Single-phonon fits reduce to the previous behaviour (one mode, rank 0).
    phonon_i = prob.composite.phonon_indices()
    fit_w0 = [theta_fit[i] for i in phonon_i]
    order = list(np.argsort(fit_w0))                    # component-order positions, ascending ω₀
    prim_global = prob.composite.primary_phonon_index(
        theta_fit, None, getattr(ctx, "prior_omega", None), getattr(ctx, "acoustic_omega", None),
        T=getattr(ctx, "T", None))
    prim_pos = phonon_i.index(prim_global)
    prim_rank = order.index(prim_pos)                   # primary's rank in the sorted order

    ncall = backends._default_migrad_ncall(len(be_names))
    rng = np.random.default_rng(seed)
    M = int(M)
    workers = max(1, int(workers))

    def _refit_block(counts_list):
        """Refit a list of draws → (ω₀,Γ)/None in draw order; chunked onto the shared/created pool
        when it pays, else serial. Any chunking gives identical per-draw results (pure refits);
        only the draw-order reassembly matters, so this is bit-identical to the serial path."""
        n = len(counts_list)
        if n == 0:
            return []
        n_chunks = min(workers, n)
        if n_chunks <= 1 or n < _BOOT_PARALLEL_MIN_M:
            return [_refit_replicate(cb, prob.composite, x, ctx, theta_fit, prob.bounds, be_names,
                                     phonon_i, prim_rank, est, backend, ncall, prob.penalty,
                                     prob.fixed_pos, prob.fixed_vals)
                    for cb in counts_list]
        chunks = [counts_list[i::n_chunks] for i in range(n_chunks)]  # round-robin, balanced
        args = [(hyp, ctx, theta_fit, ch, phonon_i, prim_rank, be_names, backend, ncall)
                for ch in chunks]
        out: list = [None] * n
        if executor is not None:                       # shared pool: never closed by the callee
            try:
                chunk_results = list(executor.map(_bootstrap_chunk, args))
            except Exception as exc:  # noqa: BLE001 — pure chunks are safe to repeat
                log.warning("parallel bootstrap failed; retrying serially: %s: %s",
                            type(exc).__name__, exc)
                chunk_results = [_bootstrap_chunk(arg) for arg in args]
            for ci, chunk_res in enumerate(chunk_results):
                out[ci::n_chunks] = chunk_res
        else:
            try:
                with ProcessPoolExecutor(max_workers=n_chunks) as ex:
                    chunk_results = list(ex.map(_bootstrap_chunk, args))
            except Exception as exc:  # noqa: BLE001 — pure chunks are safe to repeat
                log.warning("parallel bootstrap failed; retrying serially: %s: %s",
                            type(exc).__name__, exc)
                chunk_results = [_bootstrap_chunk(arg) for arg in args]
            for ci, chunk_res in enumerate(chunk_results):
                out[ci::n_chunks] = chunk_res
        return out

    if adaptive:
        # Draw in blocks off the same RNG; stop once the ω₀ half-width has stabilised. Because the
        # block draws come off the RNG in the same order as the fixed path, a no-early-stop run is
        # bit-identical — early stop only truncates once the interval has converged.
        results = []
        prev_hw = None
        stable = 0
        total = 0
        while total < M:
            n = min(int(block), M - total)
            block_counts = [rng.poisson(lam).astype(float) for _ in range(n)]
            results.extend(_refit_block(block_counts))
            total += n
            hw = _omega0_halfwidth(results, level)
            if total >= int(m_min) and prev_hw is not None and hw > 0.0 and \
                    abs(hw - prev_hw) / hw < rel_eps:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
            prev_hw = hw
    else:
        # Draw ALL replicates up front from the single RNG (fixes the sequence → worker-independent).
        counts_all = [rng.poisson(lam).astype(float) for _ in range(M)]
        results = _refit_block(counts_all)

    o_samples = [r[0] for r in results if r is not None]
    g_samples = [r[1] for r in results if r is not None]
    parameter_samples = [r[2] for r in results if r is not None]
    n_conv = len(o_samples)

    o = np.array(o_samples, dtype=float)
    g = np.array(g_samples, dtype=float)
    return BootstrapResult(
        omega0_interval=_percentile_interval(o, level),
        gamma_interval=_percentile_interval(g, level),
        omega0_samples=o, gamma_samples=g,
        n_converged=n_conv, M=len(results), interval_type=interval,
        bimodal=is_bimodal(o), seed=int(seed),
        parameter_names=tuple(names),
        parameter_samples=(np.asarray(parameter_samples, dtype=float)
                           if parameter_samples else np.empty((0, len(names)), dtype=float)),
    )
