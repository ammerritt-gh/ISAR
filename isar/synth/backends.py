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
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


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
    import lmfit

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
        cov = np.asarray(covar, dtype=float)
        cov_ok = bool(np.all(np.isfinite(cov)))
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
