"""The (Ω_d, Γ) reparameterized fit layer.

Vary (Ω_d ≥ ε, Γ, A, E, BG); convert ω₀ = √(Ω_d²+Γ²) and call the production model
unchanged. Ω_d is the underdamped coordinate: the Stokes peak of the full lineshape only
approximates it, not exactly. ω₀ ≥ Γ always, so this layer cannot represent Γ > ω₀
(an overdamped truth). center, resolution, and T are FIXED at their true values (baked
into the model closure) — five free parameters. The estimator and backend are pluggable.
"""
from __future__ import annotations

import numpy as np
from scipy.optimize import nnls

from . import config, noise
from isar.analyze import backends, estimators
from isar.analyze.lineshapes import convolute_voigt_dho, voigt_area_for_height
from isar.analyze.phonon import assess_success

FIT_PARAM_ORDER = config.FIT_PARAM_ORDER  # (omega_d, gamma, phonon_amplitude, elastic_amplitude, BG)


# ----------------------------------------------------------------------------
# Reparameterization helpers (this phase-1 core's own; the fitting path fits E0 directly)
# ----------------------------------------------------------------------------
def omega0_from(omega_d: float, gamma: float) -> float:
    """ω₀ = √(Ω_d² + Γ²)."""
    return float(np.sqrt(omega_d**2 + gamma**2))


def omega_d_from(omega0: float, gamma: float) -> float:
    """Ω_d = √(ω₀² − Γ²) (0 if overdamped)."""
    disc = omega0**2 - gamma**2
    return float(np.sqrt(disc)) if disc > 0 else 0.0


def omega0_value_error(omega_d: float, gamma: float, cov) -> tuple[float, float]:
    """ω₀ and its symmetric error, propagating the full (Ω_d,Γ) covariance.

    σ_ω₀² = (Ω_d/ω₀)²σ_Ωd² + (Γ/ω₀)²σ_Γ² + 2(Ω_dΓ/ω₀²)cov(Ω_d,Γ). The cross term is
    mandatory (Ω_d, Γ are anticorrelated). Returns NaN error if covariance is missing.
    """
    omega0 = omega0_from(omega_d, gamma)
    if cov is None:
        return omega0, float("nan")
    if omega0 == 0.0:
        # Ω_d = Γ = 0: both derivatives are ∂ω₀/∂· = 0/0, so the covariance cannot be
        # propagated at all. Same contract as a missing covariance — an unusable error.
        return omega0, float("nan")
    var_od, var_g, cov_og = cov[0, 0], cov[1, 1], cov[0, 1]
    d_od, d_g = omega_d / omega0, gamma / omega0
    var = d_od**2 * var_od + d_g**2 * var_g + 2.0 * d_od * d_g * cov_og
    return omega0, float(np.sqrt(var)) if var > 0 else float("nan")


# ----------------------------------------------------------------------------
# Model closure (fixed params baked in)
# ----------------------------------------------------------------------------
def make_model(x, gauss_fwhm, lorentz_fwhm, T, center=0.0):
    """Return `model(theta)` with theta = (Ω_d, Γ, A, E, BG)."""
    x = np.asarray(x, dtype=float)

    def model(theta):
        omega_d, gamma, A, E, BG = theta
        omega0 = np.sqrt(omega_d**2 + gamma**2)  # ≥ gamma by construction → guard never fires
        return convolute_voigt_dho(x, omega0, center, gauss_fwhm, lorentz_fwhm,
                                   gamma, A, E, BG, T, peak="all")

    return model


# ----------------------------------------------------------------------------
# Initial guess
# ----------------------------------------------------------------------------
def initial_guess(x, counts, gauss_fwhm, lorentz_fwhm, T, *, center=0.0,
                  eps_omega_d=1e-3, x_max=10.0, mode="grid",
                  estimator="baker_cousins", truth=None, gamma0=0.3):
    """Initial (Ω_d, Γ, A, E, BG).

    mode='grid' (default): scan Ω_d candidates; at each, fix Γ₀ and solve (A,E,BG) by
      non-negative linear least squares (the model is exactly linear in them); pick the
      candidate with the lowest estimator cost. Robust where the raw argmax is noisy.
    mode='data': cmax / edge-median BG / argmax heuristic.
    mode='truth': perturb the supplied truth (Ω_d, Γ, A, E, BG) by ±10%.
    """
    x = np.asarray(x, dtype=float)
    counts = np.asarray(counts, dtype=float)
    cmax = max(float(counts.max()), 1.0)

    if mode == "truth":
        if truth is None:
            raise ValueError("mode='truth' requires `truth`")
        t = np.asarray(truth, dtype=float)
        return t * np.array([1.05, 0.95, 1.1, 0.9, 1.1])

    if mode == "grid":
        est = estimators.get_estimator(estimator)
        ones = np.ones_like(x)
        candidates = np.arange(max(eps_omega_d, x[1] - x[0]), x_max, 0.25)
        best_cost, best = np.inf, None
        for od in candidates:
            omega0 = omega0_from(od, gamma0)
            ph = convolute_voigt_dho(x, omega0, center, gauss_fwhm, lorentz_fwhm,
                                     gamma0, 1.0, 0.0, 0.0, T, peak="stokes+astokes")
            el = convolute_voigt_dho(x, omega0, center, gauss_fwhm, lorentz_fwhm,
                                     gamma0, 0.0, 1.0, 0.0, T, peak="elastic")
            phi = np.column_stack([ph, el, ones])
            coef, _ = nnls(phi, counts)  # A, E, BG ≥ 0
            cost = est.cost(counts, phi @ coef)
            if cost < best_cost:
                best_cost, best = cost, (od, coef)
        od, (A, E, BG) = best
        return np.array([od, gamma0, A, E, max(BG, 0.0)])

    # mode == 'data'
    BG0 = float(np.median(np.concatenate([counts[:5], counts[-5:]])))
    elastic_mask = np.abs(x - center) > 2.0 * gauss_fwhm
    if np.any(elastic_mask):
        off = np.where(elastic_mask)[0]
        peak_idx = off[np.argmax(counts[off])]
        od0 = min(max(abs(x[peak_idx] - center), eps_omega_d), x_max)
        height = max(counts[peak_idx] - BG0, 1.0)
    else:
        od0, height = 3.0, cmax / 40.0
    A0 = max(height * gamma0**2, cmax / 40.0)
    E0 = voigt_area_for_height(max(cmax - BG0, 1.0), gauss_fwhm, lorentz_fwhm)
    return np.array([od0, gamma0, A0, E0, max(BG0, 0.0)])


# ----------------------------------------------------------------------------
# Fit
# ----------------------------------------------------------------------------
def _build_problem(x, counts, gauss_fwhm, lorentz_fwhm, T, center,
                   eps_omega_d, gamma_floor, gamma_max, estimator, ic_mode, ic_truth):
    """Assemble (costv, x0, bounds) for one scan — shared by `fit` and `backend_compare`."""
    x = np.asarray(x, dtype=float)
    counts = np.asarray(counts, dtype=float)
    x_max = float(np.max(x))
    cmax = max(float(counts.max()), 1.0)

    est = estimators.get_estimator(estimator)
    model = make_model(x, gauss_fwhm, lorentz_fwhm, T, center=center)

    def costv(theta):
        return est.cost(counts, model(theta))

    x0 = initial_guess(x, counts, gauss_fwhm, lorentz_fwhm, T, center=center,
                       eps_omega_d=eps_omega_d, x_max=x_max, mode=ic_mode,
                       estimator=estimator, truth=ic_truth)
    bounds = [
        (eps_omega_d, x_max),         # Ω_d
        (gamma_floor, gamma_max),     # Γ
        (0.0, 1e4 * cmax),            # A
        (0.0, 1e4 * cmax),            # E
        (0.0, 5.0 * cmax + 10.0),     # BG
    ]
    return costv, x0, bounds


def fit(x, counts, *, gauss_fwhm, lorentz_fwhm, T, center=0.0,
        eps_omega_d=1e-3, gamma_floor=1e-3, gamma_max=5.0,
        estimator="baker_cousins", backend="iminuit", minos=False,
        ic_mode="grid", ic_truth=None, seed=-1) -> config.FitResult:
    """Fit one scan and return a populated FitResult."""
    counts = np.asarray(counts, dtype=float)
    costv, x0, bounds = _build_problem(x, counts, gauss_fwhm, lorentz_fwhm, T, center,
                                       eps_omega_d, gamma_floor, gamma_max,
                                       estimator, ic_mode, ic_truth)

    br = backends.minimize(backend, costv, x0, bounds, FIT_PARAM_ORDER, minos=minos)

    omega_d, gamma, A, E, BG = (float(v) for v in br.x)
    errs = np.sqrt(np.diag(br.covariance)) if br.covariance_ok else None

    res = config.FitResult(estimator=estimator, seed=int(seed),
                           message=br.message, n_eval=br.n_eval,
                           covariance_ok=br.covariance_ok)
    res.omega_d, res.gamma, res.phonon_amplitude, res.elastic_amplitude, res.BG = \
        omega_d, gamma, A, E, BG

    if errs is not None:
        res.omega_d_err, res.gamma_err, res.phonon_amplitude_err, \
            res.elastic_amplitude_err, res.BG_err = (float(e) for e in errs)

    res.omega0, res.omega0_err = omega0_value_error(omega_d, gamma, br.covariance)

    if br.merrors:
        if "omega_d" in br.merrors:
            res.omega_d_err_lo, res.omega_d_err_hi, _ = br.merrors["omega_d"]
        if "gamma" in br.merrors:
            res.gamma_err_lo, res.gamma_err_hi, _ = br.merrors["gamma"]

    res.cost = costv(br.x)
    res.ndof = len(counts) - len(FIT_PARAM_ORDER)
    res.cost_per_dof = res.cost / res.ndof if res.ndof > 0 else float("nan")

    res.gamma_at_floor = gamma <= gamma_floor * (1.0 + 1e-2)
    res.success = assess_success(br.x, errs, br.at_limit, br.success,
                                 br.covariance_ok, eps_omega_d)
    return res


def backend_compare(cells, *, estimator="baker_cousins",
                    backend_names=("iminuit", "scipy", "lmfit"),
                    recover_tol=0.3) -> dict:
    """Empirical backend comparison (the handoff deliverable): run each backend on a list
    of (GroundTruth, counts) cells and report, separately,

      - converged_rate    : the optimizer found a minimum (BackendResult.success),
      - covariance_rate    : a usable parameter covariance was produced,
      - asymmetric_errors  : whether profile (MINOS) errors are available,
      - recovered_rate     : |ω₀−truth| and |Γ−truth| both < recover_tol meV,
      - mean_time_s        : mean wall time per fit.

    Convergence and covariance are reported apart on purpose: a backend can converge to
    the right optimum yet yield no error bars through this scalar-cost interface
    (e.g. lmfit), which is the kind of distinction that picks the production backend.
    iminuit runs MINOS so its `asymmetric_errors` reflects real profile intervals.
    """
    from time import perf_counter

    report = {}
    for be in backend_names:
        use_minos = be == "iminuit"
        n = len(cells)
        n_conv = n_cov = n_asym = n_rec = 0
        t0 = perf_counter()
        for gt, counts in cells:
            costv, x0, bounds = _build_problem(
                noise.energy_grid(gt.x_min, gt.x_max, gt.x_step), counts,
                gt.gauss_fwhm, gt.lorentz_fwhm, gt.T, gt.center,
                1e-3, 1e-3, 5.0, estimator, "grid", None)
            br = backends.minimize(be, costv, x0, bounds, FIT_PARAM_ORDER, minos=use_minos)
            n_conv += int(br.success)
            n_cov += int(br.covariance_ok)
            if br.merrors and "gamma" in br.merrors and br.merrors["gamma"][2]:
                n_asym += 1
            omega0 = omega0_from(br.x[0], br.x[1])
            if abs(omega0 - gt.omega0) < recover_tol and abs(br.x[1] - gt.gamma) < recover_tol:
                n_rec += 1
        dt = perf_counter() - t0
        report[be] = {
            "n": n,
            "converged_rate": n_conv / n if n else float("nan"),
            "covariance_rate": n_cov / n if n else float("nan"),
            "asymmetric_errors": n_asym > 0,
            "recovered_rate": n_rec / n if n else float("nan"),
            "mean_time_s": dt / n if n else float("nan"),
        }
    return report


def fit_truth(gt: config.GroundTruth, counts, *, estimator="baker_cousins",
              backend="iminuit", minos=False, ic_mode="grid",
              eps_omega_d=1e-3, gamma_floor=1e-3) -> config.FitResult:
    """Fit a scan with the fixed (resolution, T, center) context taken from its truth."""
    x = noise.energy_grid(gt.x_min, gt.x_max, gt.x_step)
    return fit(x, counts, gauss_fwhm=gt.gauss_fwhm, lorentz_fwhm=gt.lorentz_fwhm,
               T=gt.T, center=gt.center, eps_omega_d=eps_omega_d, gamma_floor=gamma_floor,
               estimator=estimator, backend=backend, minos=minos, ic_mode=ic_mode,
               seed=gt.seed)
