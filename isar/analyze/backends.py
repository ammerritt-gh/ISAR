"""Three minimizer backends behind one interface, for the empirical comparison.

All take a single scalar cost `costv(theta: ndarray) -> float` (an estimator cost of
the model prediction), start `x0`, per-parameter `bounds`, and `names`, and return a
uniform `BackendResult`. `errordef = 1` throughout (χ² and −2lnL alike), so the
covariance is `2·H⁻¹` and matches `curve_fit(absolute_sigma=True)`.

- iminuit (primary): MIGRAD → HESSE → (MINOS on demand) — the only source of
  asymmetric profile errors.
- scipy (fallback): L-BFGS-B + a carefully scaled finite-difference Hessian with a
  positive-definite guard; no MINOS.
- lmfit (comparison): scalar minimizer on the same cost; covariance where available.
  Optional — imported lazily, ships in the ``[test]`` extra.

The profiled fit (:func:`isar.analyze.loop.fit_hypothesis` with iminuit) uses the iminuit pieces
separately: :func:`iminuit_migrad` on the nonlinear parameters, with the linear amplitudes solved
inside the cost by :func:`poisson_nnls`, then :func:`iminuit_hesse` over every parameter at the
joint optimum.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class BackendResult:
    x: np.ndarray
    success: bool
    message: str
    n_eval: int
    covariance: np.ndarray | None
    covariance_ok: bool
    at_limit: dict          # {name: bool}
    merrors: dict | None    # {name: (lower<=0, upper>=0, is_valid)} or None


def _at_limit(value: float, lo: float, hi: float, rtol: float = 1e-3, atol: float = 1e-9) -> bool:
    tol_lo = atol + rtol * max(abs(lo), 1.0)
    tol_hi = atol + rtol * max(abs(hi), 1.0)
    return bool(value <= lo + tol_lo or value >= hi - tol_hi)


def _at_limit_map(x, bounds, names) -> dict:
    return {nm: _at_limit(v, lo, hi) for nm, (lo, hi), v in zip(names, bounds, x)}


# MIGRAD budget = this multiple of MINUIT's own default ceiling. See _migrad_ncall.
_MIGRAD_NCALL_MULT = 4


def _default_migrad_ncall(npar: int) -> int:
    """MINUIT's own default call ceiling for ``npar`` free parameters."""
    return 200 + 100 * npar + 5 * npar * npar


def _migrad_ncall(npar: int) -> int:
    """MIGRAD call budget for ``npar`` free parameters — a modest multiple of MINUIT's own
    default ceiling ``200 + 100·npar + 5·npar²``.

    Why raise it. The k≥7 near-degenerate multi-mode fits in the sparse wide-window regime reach
    a small-EDM minimum but exhaust the DEFAULT budget on the way (measured need ~1400-2000 calls
    for k=7-8, vs a ~1145 default at k=7). MIGRAD then sets ``has_reached_call_limit`` and
    ``m.valid`` is False even though the point estimate is good, so the fit is wrongly dropped
    from model selection and the loop falls back to 1-mode (the greedy-IC "seed robustness"
    symptom — the seed is fine, the fit was cut short).

    Why only MODEST. The budget cutoff is also a weak regularizer: a fit that needs a *large*
    multiple of its default budget is crawling along a near-flat (non-identifiable) direction —
    the signature of an unconstrained extra parameter, i.e. an overfit. On the IN8 anchor the
    spurious ``stokes2+qe+bg`` fit needs ~13k calls to limp to its minimum, whereas the physical
    ``stokes2+bg`` needs ~1400; a ~4× ceiling certifies the well-constrained fits (TAVI + IN8)
    while still cutting off that overfit so it never enters selection. Strictly greater than the
    default for all npar, so any fit that already converges inside the default is untouched."""
    return _MIGRAD_NCALL_MULT * _default_migrad_ncall(npar)


# ----------------------------------------------------------------------------
# iminuit (primary)
# ----------------------------------------------------------------------------
def iminuit_minimize(costv, x0, bounds, names, *, minos=False, ncall=None) -> BackendResult:
    from iminuit import Minuit

    names = list(names)

    def cost_star(*params):
        return costv(np.asarray(params, dtype=float))

    cost_star.errordef = 1.0  # 1σ at Δcost = 1, for χ² and −2lnL alike
    m = Minuit(cost_star, *x0, name=names)
    for nm, (lo, hi) in zip(names, bounds):
        m.limits[nm] = (lo, hi)

    # Default (ncall=None): the raised certification budget (_migrad_ncall) so a slow-but-good
    # k≥7 fit certifies for selection. Callers that only need a point estimate and tolerate
    # non-convergence — the parametric bootstrap, which counts failed refits — pass the plain
    # default budget to keep the per-scan refit cost unchanged from before this fix.
    m.migrad(ncall=(_migrad_ncall(len(names)) if ncall is None else int(ncall)))
    success = bool(m.valid)

    covariance = None
    covariance_ok = False
    try:
        m.hesse()
        if m.covariance is not None and m.fmin.has_covariance:
            cov = np.array(m.covariance, dtype=float)
            if np.all(np.isfinite(cov)):
                covariance = cov
                covariance_ok = True
    except Exception:
        pass

    merrors = None
    if minos and success:
        try:
            m.minos()
            merrors = {}
            for nm in names:
                if nm in m.merrors:
                    me = m.merrors[nm]
                    merrors[nm] = (float(me.lower), float(me.upper), bool(me.is_valid))
            if not merrors:
                merrors = None
        except Exception:
            merrors = None

    x = np.array([m.values[nm] for nm in names], dtype=float)
    return BackendResult(x, success, str(m.fmin.is_valid), int(m.nfcn),
                         covariance, covariance_ok, _at_limit_map(x, bounds, names), merrors)


def iminuit_migrad(costv, x0, bounds, names, *, ncall=None):
    """MIGRAD alone, with :func:`iminuit_minimize`'s call budget and no HESSE:
    ``(x, success, message, n_eval)``."""
    from iminuit import Minuit

    names = list(names)

    def cost_star(*params):
        return costv(np.asarray(params, dtype=float))

    cost_star.errordef = 1.0
    m = Minuit(cost_star, *x0, name=names)
    for nm, (lo, hi) in zip(names, bounds):
        m.limits[nm] = (lo, hi)
    m.migrad(ncall=(_migrad_ncall(len(names)) if ncall is None else int(ncall)))
    x = np.array([m.values[nm] for nm in names], dtype=float)
    return x, bool(m.valid), str(m.fmin.is_valid), int(m.nfcn)


def iminuit_hesse(costv, x, bounds, names, *, fixed=(), minos=False):
    """HESSE at the given point ``x``, with no MIGRAD before it, and MINOS on demand:
    ``(covariance, covariance_ok, merrors, n_eval)``, accepted as :func:`iminuit_minimize` accepts
    them. A ``bounds`` entry of ``None`` leaves that parameter unbounded; names in ``fixed`` are
    held, so their rows and columns of the covariance are zero."""
    from iminuit import Minuit

    names = list(names)
    if set(names) <= set(fixed):    # every parameter held: zero covariance, as for each held one
        return np.zeros((len(names), len(names))), True, None, 0

    def cost_star(*params):
        return costv(np.asarray(params, dtype=float))

    cost_star.errordef = 1.0
    m = Minuit(cost_star, *x, name=names)
    for nm, b in zip(names, bounds):
        if b is not None:
            m.limits[nm] = b
    for nm in fixed:
        m.fixed[nm] = True
    covariance, covariance_ok = None, False
    try:
        m.hesse()
        if m.covariance is not None and m.fmin.has_covariance:
            cov = np.array(m.covariance, dtype=float)
            if np.all(np.isfinite(cov)):
                covariance, covariance_ok = cov, True
    except Exception as exc:  # noqa: BLE001 - no covariance is a fit outcome, not an error
        log.warning("HESSE failed at the profiled optimum: %s: %s", type(exc).__name__, exc)
    n_eval = int(m.nfcn)
    merrors = None
    if minos and covariance_ok:
        before = int(m.nfcn)
        try:
            m.minos()
            merrors = {nm: (float(me.lower), float(me.upper), bool(me.is_valid))
                       for nm, me in ((nm, m.merrors[nm]) for nm in names if nm in m.merrors)}
            merrors = merrors or None
        except Exception as exc:  # noqa: BLE001
            log.warning("MINOS failed at the profiled optimum: %s: %s", type(exc).__name__, exc)
            merrors = None
        n_eval += int(m.nfcn) - before
    return covariance, covariance_ok, merrors, n_eval


# ----------------------------------------------------------------------------
# Poisson NNLS: the profiled linear amplitudes
# ----------------------------------------------------------------------------
def _box_qp(h, g, lo, hi):
    """``argmin g.d + d.h.d/2`` over ``lo <= d <= hi`` (``lo <= 0 <= hi``, ``h`` positive
    definite): a primal active-set method from ``d = 0``. Each pass solves the free coordinates
    with the held ones on their bounds, walks towards that solution as far as the first bound it
    meets (and holds it), or, once it is reached, releases the held coordinate whose multiplier has
    the wrong sign; a strictly convex QP ends in finitely many passes."""
    n = g.size
    d = np.zeros(n)
    held = np.zeros(n, dtype=np.int8)           # -1 on lo, +1 on hi, 0 free
    held[(lo >= 0) & (g > 0)] = -1
    held[(hi <= 0) & (g < 0)] = 1
    for _ in range(4 * n + 10):
        free = np.flatnonzero(held == 0)
        if free.size:
            grad = g + h @ d
            step = np.linalg.solve(h[np.ix_(free, free)], -grad[free])
            with np.errstate(divide="ignore", invalid="ignore"):
                room = np.where(step > 0, (hi[free] - d[free]) / step,
                                np.where(step < 0, (lo[free] - d[free]) / step, np.inf))
            j = int(np.argmin(room))
            if room[j] < 1.0:
                d[free] += room[j] * step
                k = free[j]
                d[k], held[k] = (hi[k], 1) if step[j] > 0 else (lo[k], -1)
                continue
            d[free] += step
        grad = g + h @ d
        wrong = np.where(held == -1, -grad, np.where(held == 1, grad, 0.0))
        j = int(np.argmax(wrong))
        if wrong[j] <= 0.0:
            break
        held[j] = 0
    return d, held


def poisson_nnls(phi, y, a0, lo, hi, *, offset=None, max_iter=50):
    """The amplitudes ``a`` in ``[lo, hi]`` that minimise the Baker-Cousins cost of the mean
    ``offset + phi @ a`` against the counts ``y``.

    The cost is convex in ``a`` (the mean is linear in it), so Newton's method with the box kept
    exactly converges to its one minimum, quadratically once the bound amplitudes are known: each
    step is the minimum of the cost's quadratic model inside the box (:func:`_box_qp`), backtracked
    until the cost falls (Armijo). ``a0`` warm-starts it. It stops when the step's predicted
    decrease is at the cost's round-off, when no step lowers the cost, or after ``max_iter`` steps.
    A zero-count point contributes its mean only (no floor, convention D3)."""
    phi = np.asarray(phi, dtype=float)
    y = np.asarray(y, dtype=float)
    lo = np.asarray(lo, dtype=float)
    hi = np.asarray(hi, dtype=float)
    c = np.zeros(y.size) if offset is None else np.asarray(offset, dtype=float)
    if not (np.all(np.isfinite(phi)) and np.all(np.isfinite(c))):
        return np.clip(np.asarray(a0, dtype=float), lo, hi)   # the caller's cost reads non-finite
    pos = y > 0
    yp = y[pos]

    def half_cost(mu):
        # cost/2 up to a constant; infinite where a counted point has no mean
        if np.any(mu < 0) or np.any(mu[pos] <= 0):
            return np.inf
        return float(np.sum(mu) - yp @ np.log(mu[pos]))

    a = np.clip(np.asarray(a0, dtype=float), lo, hi)
    mu = c + phi @ a
    f = half_cost(mu)
    # A second start, the variance-weighted least-squares fit, for a warm start the optimiser's
    # last jump left far away: from a mean near zero Newton's step only doubles it.
    from scipy.optimize import nnls

    s = 1.0 / np.sqrt(np.maximum(y, 1.0))
    try:
        ls = np.clip(nnls(phi * s[:, None], (y - c) * s)[0], lo, hi)
        mu_ls = c + phi @ ls
        f_ls = half_cost(mu_ls)
        if f_ls < f:
            a, mu, f = ls, mu_ls, f_ls
    except RuntimeError as exc:     # scipy's iteration cap; the warm start stands
        log.debug("least-squares start unavailable: %s", exc)
    if not np.isfinite(f):
        # Start inside: give every column an equal share of the counts.
        col = phi.sum(axis=0)
        share = np.divide(y.sum() / max(phi.shape[1], 1), col, out=np.zeros_like(col),
                          where=col > 0)
        a = np.clip(np.maximum(a, share), lo, hi)
        mu = c + phi @ a
        f = half_cost(mu)
        if not np.isfinite(f):
            return a
    r = np.zeros_like(y)
    w = np.zeros_like(y)
    for _ in range(max_iter):
        r[pos] = yp / mu[pos]
        w[pos] = r[pos] / mu[pos]
        g = phi.T @ (1.0 - r)
        h = phi.T @ (phi * w[:, None])
        # A relative ridge per column (the columns' scales differ by many decades: a width at its
        # floor makes a tall narrow one); a column with no counts under it gets a tiny absolute one.
        diag = np.diag(h)
        h = h + np.diag(np.where(diag > 0, 1e-12 * diag, 1e-24 * max(float(np.max(diag)), 1.0)))
        try:
            d, held = _box_qp(h, g, lo - a, hi - a)
        except np.linalg.LinAlgError:
            break
        slope = float(g @ d)
        if not -slope > 1e-14 * max(1.0, abs(f)):
            # Converged; still take the last step if it costs nothing, so a bound amplitude ends
            # exactly on its bound rather than a round-off above it.
            trial = np.clip(a + d, lo, hi)
            trial[held == -1] = lo[held == -1]
            trial[held == 1] = hi[held == 1]
            f_t = half_cost(c + phi @ trial)
            if f_t <= f + 1e-12 * max(1.0, abs(f)):
                a = trial
            break
        t, moved = 1.0, False
        for _ in range(50):
            trial = np.clip(a + t * d, lo, hi)
            if t == 1.0:    # the QP's bound amplitudes sit exactly on their bounds
                trial[held == -1] = lo[held == -1]
                trial[held == 1] = hi[held == 1]
            mu_t = c + phi @ trial
            f_t = half_cost(mu_t)
            if f_t <= f + 1e-4 * t * slope:
                moved = True
                break
            t *= 0.5
        if not moved:
            break
        a, mu, f = trial, mu_t, f_t
    return a


# ----------------------------------------------------------------------------
# scipy (fallback)
# ----------------------------------------------------------------------------
def _fd_hessian(costv, x, rel=1e-3):
    """Central-difference Hessian with per-parameter relative steps."""
    x = np.asarray(x, dtype=float)
    n = x.size
    h = rel * np.maximum(np.abs(x), 1.0)
    f0 = costv(x)
    H = np.zeros((n, n))
    for i in range(n):
        xp = x.copy(); xp[i] += h[i]
        xm = x.copy(); xm[i] -= h[i]
        H[i, i] = (costv(xp) - 2.0 * f0 + costv(xm)) / (h[i] ** 2)
        for j in range(i + 1, n):
            xpp = x.copy(); xpp[i] += h[i]; xpp[j] += h[j]
            xpm = x.copy(); xpm[i] += h[i]; xpm[j] -= h[j]
            xmp = x.copy(); xmp[i] -= h[i]; xmp[j] += h[j]
            xmm = x.copy(); xmm[i] -= h[i]; xmm[j] -= h[j]
            H[i, j] = H[j, i] = (costv(xpp) - costv(xpm) - costv(xmp) + costv(xmm)) / (4.0 * h[i] * h[j])
    return 0.5 * (H + H.T)


def _psd_covariance(c):
    """A reported covariance is usable only if it is finite AND positive-definite. Returns
    ``(cov, True)`` or ``(None, False)`` — the same acceptance test :func:`_fd_covariance`
    applies, so the scipy and lmfit paths reject a non-PSD covariance identically.
    (``iminuit_minimize`` still accepts on finiteness alone; tightening it would move
    production numbers and needs its own review.)"""
    cov = np.asarray(c, dtype=float)
    if not np.all(np.isfinite(cov)):
        return None, False
    try:
        w = np.linalg.eigvalsh(cov)
    except np.linalg.LinAlgError:
        return None, False
    if not np.all(np.isfinite(w)) or np.any(w <= 0):
        return None, False
    return cov, True


def _fd_covariance(costv, x):
    H = _fd_hessian(costv, x)
    try:
        w, V = np.linalg.eigh(H)
    except np.linalg.LinAlgError:
        return None, False
    if not np.all(np.isfinite(w)) or np.any(w <= 0):
        return None, False
    cov = 2.0 * (V / w) @ V.T  # 2·H⁻¹ via eigendecomposition (errordef=1)
    if not np.all(np.isfinite(cov)):
        return None, False
    return cov, True


def scipy_minimize(costv, x0, bounds, names, *, minos=False) -> BackendResult:
    from scipy.optimize import minimize as _minimize

    res = _minimize(costv, np.asarray(x0, dtype=float), method="L-BFGS-B", bounds=list(bounds))
    x = np.asarray(res.x, dtype=float)
    cov, cov_ok = _fd_covariance(costv, x) if res.success else (None, False)
    # scipy has no MINOS; asymmetric errors come from iminuit.
    return BackendResult(x, bool(res.success), str(res.message), int(res.nfev),
                         cov, cov_ok, _at_limit_map(x, bounds, names), None)


# ----------------------------------------------------------------------------
# lmfit (comparison)
# ----------------------------------------------------------------------------
def lmfit_minimize(costv, x0, bounds, names, *, minos=False) -> BackendResult:
    try:
        import lmfit
    except ImportError as exc:  # pragma: no cover - depends on install extras
        raise ImportError(
            "the 'lmfit' backend requires the optional lmfit package; "
            "install it with the [test] extra (pip install -e \".[test]\")"
        ) from exc

    names = list(names)
    params = lmfit.Parameters()
    for nm, v, (lo, hi) in zip(names, x0, bounds):
        params.add(nm, value=float(v), min=lo, max=hi)

    def objective(p):
        theta = np.array([p[nm].value for nm in names], dtype=float)
        return costv(theta)

    mini = lmfit.Minimizer(objective, params, calc_covar=True, scale_covar=False)
    res = mini.minimize(method="lbfgsb")
    x = np.array([res.params[nm].value for nm in names], dtype=float)

    cov = None
    cov_ok = False
    covar = getattr(res, "covar", None)
    if covar is not None:
        cov, cov_ok = _psd_covariance(covar)
    # conf_interval needs a residual/χ²-like objective; not valid for a scalar −2lnL → skip.
    return BackendResult(x, bool(res.success), str(getattr(res, "message", "")),
                         int(res.nfev), cov, cov_ok, _at_limit_map(x, bounds, names), None)


BACKENDS = {
    "iminuit": iminuit_minimize,
    "scipy": scipy_minimize,
    "lmfit": lmfit_minimize,
}


def minimize(backend: str, costv, x0, bounds, names, *, minos=False, ncall=None) -> BackendResult:
    try:
        fn = BACKENDS[backend]
    except KeyError:
        raise KeyError(f"unknown backend {backend!r}; choices: {sorted(BACKENDS)}") from None
    if backend == "iminuit":
        return fn(costv, x0, bounds, names, minos=minos, ncall=ncall)
    return fn(costv, x0, bounds, names, minos=minos)
