"""Cost-normalized expected-information-gain (EIG) acquisition scoring (Phase 5, LLM_HARNESS_DESIGN §7).

Stage-B (Inference) acquisition: rank scan proposals by EIG on ω₀ **per unit cost**, so the campaign
spends counts where ω₀ is least pinned per neutron and stops counting a q once one more scan barely
helps (the value-of-information stop). The acquisition unit stays a whole constant-Q SCAN (contract 7);
these are the pure scoring primitives the ranker in :mod:`isar.drive.acquisition.acquire` consumes.

**5a — counts-based diminishing-returns EIG (this module, self-contained).** With a √N precision model
(σ ∝ 1/√C, so Fisher info ``I = 1/σ² ∝`` accumulated counts ``C``), one more scan of cost ``ΔC`` scales
the info by ``(C+ΔC)/C``. The raw Gaussian EIG ``½·log(1+ΔC/C)`` captures diminishing returns but, at a
fixed production ``ΔC``, degenerates to "least-counted q" and ignores whether σ is barely over tolerance
or wildly unconstrained. So the score is **goal-weighted**: the expected reduction in the *tolerance
deficit* ``max(σ−tol, 0)`` — ~0 for a q already at tolerance, large for one far from it. The log form is
only the tie-balancer.

**5b — Poisson-Fisher forward-model screen** (added in a later commit) replaces the √N assumption with
the exact Poisson Fisher matrix, so the low-q Γ/A/elastic merge (where σ does **not** drop as √N because
ω₀ is unidentifiable) is scored honestly. The VOI primitives here take σ_now/σ_post directly so the 5b
Fisher σ can drive them without changing the stopping-rule wiring.

All functions are pure and deterministic; the driver holds no state here.
"""
from __future__ import annotations

import math

import numpy as np

# VOI stop-floor: a non-converged q whose next scan shrinks σ(ω₀) by less than this *fraction* is
# "info-saturated" — one more scan barely helps, so stop escalating it (GridQ.info_saturated → stuck).
# Conservative under the 5a √N model (which, assuming σ ∝ 1/√C, always predicts eventual convergence,
# so this mostly stays dormant behind MAX_PROD_ATTEMPTS); it gains teeth in 5b, where the Fisher σ_post
# does NOT shrink when ω₀ is unidentifiable, so the fraction goes to ~0 and the gate fires at once.
VOI_SIGMA_FRAC = 0.01

_INF = float("inf")


def _bad_sigma(sigma) -> bool:
    return sigma is None or not math.isfinite(sigma) or sigma <= 0.0


def predicted_sigma_after(sigma_now: float, c_spent: float, dc: float) -> float:
    """√N precision update: ``σ_post = σ_now·√(C/(C+ΔC))`` (adding counts can only shrink σ).

    With no measured baseline (``C ≤ 0``) or no added cost (``ΔC ≤ 0``) σ is unchanged. This is the 5a
    model; 5b computes ``σ_post`` from the Poisson Fisher matrix instead and feeds the VOI primitives.
    """
    if _bad_sigma(sigma_now):
        return _INF
    c_spent = max(float(c_spent), 0.0)
    dc = max(float(dc), 0.0)
    if c_spent <= 0.0 or dc <= 0.0:
        return float(sigma_now)
    return float(sigma_now) * math.sqrt(c_spent / (c_spent + dc))


def counts_eig(sigma_now: float, tolerance: float, c_spent: float, dc: float) -> float:
    """Goal-weighted counts-EIG: expected reduction in the tolerance deficit ``max(σ−tol, 0)`` from
    adding a scan of cost ``ΔC`` to a q that has accumulated ``C`` counts at current σ (√N model).

    A q already at/under tolerance scores ~0 (its deficit is already 0); a q far over tolerance scores
    high. Returns ``+inf`` when there is no measured baseline (``C ≤ 0``) so an un-based q is scanned
    first (baseline/anchors-first). Never negative.
    """
    if _bad_sigma(sigma_now):
        return _INF
    c_spent = max(float(c_spent), 0.0)
    if c_spent <= 0.0:
        return _INF
    tol = max(float(tolerance), 0.0)
    deficit_now = max(float(sigma_now) - tol, 0.0)
    deficit_post = max(predicted_sigma_after(sigma_now, c_spent, dc) - tol, 0.0)
    return max(deficit_now - deficit_post, 0.0)


def eig_per_cost(eig: float, dc: float) -> float:
    """EIG per unit cost (the ranking key). ``ΔC = n_points × ncount`` (the monitor-count budget, a
    proxy for wall-clock at fixed flux). An ``+inf`` EIG (no baseline yet) stays ``+inf`` so those
    candidates outrank any finite score; a non-positive cost is treated as free (``+inf``)."""
    if not math.isfinite(eig):
        return _INF
    dc = float(dc)
    if dc <= 0.0:
        return _INF
    return float(eig) / dc


def voi_saturated_from_sigmas(sigma_now, sigma_post, tolerance, *, frac: float = VOI_SIGMA_FRAC) -> bool:
    """Value-of-information stop, given the current and predicted-post-scan σ(ω₀): True when the q is
    NOT yet at tolerance but the next scan shrinks σ by less than ``frac`` (diminishing returns /
    systematically floored). Needs a finite σ over tolerance; returns False otherwise (never abandons
    blind). 5a feeds ``sigma_post`` from :func:`predicted_sigma_after` (√N); 5b feeds the Fisher σ."""
    if _bad_sigma(sigma_now) or sigma_post is None:
        return False
    tol = max(float(tolerance), 0.0)
    if float(sigma_now) <= tol:
        return False                       # already at tolerance → converged, not saturated
    if not math.isfinite(sigma_post):      # scan cannot pin ω₀ at all (5b unidentifiable) → saturated
        return True
    if float(sigma_post) >= float(sigma_now):
        return True
    return (1.0 - float(sigma_post) / float(sigma_now)) < float(frac)


def voi_saturated(sigma_now, tolerance, c_spent, dc, *, frac: float = VOI_SIGMA_FRAC) -> bool:
    """Convenience 5a wrapper: is this q info-saturated under the √N model? Needs a measured baseline
    (``C > 0``, ``ΔC > 0``); returns False otherwise (never abandons a q with no cost history)."""
    if _bad_sigma(sigma_now) or float(c_spent) <= 0.0 or float(dc) <= 0.0:
        return False
    sigma_post = predicted_sigma_after(sigma_now, c_spent, dc)
    return voi_saturated_from_sigmas(sigma_now, sigma_post, tolerance, frac=frac)


# ============================================================================================
# 5b — Poisson-Fisher forward-model screen (identifiability-aware σ(ω₀))
# ============================================================================================
# The 5a counts-EIG assumes σ ∝ 1/√C. That is wrong exactly where it matters most — the low-q merge,
# where the Stokes peak overlaps Γ/A/the elastic line and ω₀ is (nearly) unidentifiable, so σ(ω₀) does
# NOT shrink with counts. 5b computes the exact Poisson Fisher information of a *proposed* scan on the
# forward model the fit will actually use, adds the branch soft-constraint precision the fit will use,
# marginalizes the nuisances honestly (Schur complement, never a bare pinv that fakes small variance in
# null directions), and projects to σ(ω₀) via the exact ∂ω₀/∂θ Jacobian — returning ∞ when ω₀ is
# unidentifiable from that scan + prior, never a falsely-tight number.
#
# Parameter order is the production fit's: θ = (Ω_d, Γ, A, E, BG)  (isar.synth.config.FIT_PARAM_ORDER).
# ω₀ = √(Ω_d²+Γ²) is derived; the primary block is (Ω_d, Γ) [indices 0,1], nuisances (A, E, BG) [2,3,4].

_PRIMARY = (0, 1)                       # Ω_d, Γ — the ω₀-bearing params
_NUISANCE = (2, 3, 4)                   # A, E, BG
# Finite-difference steps (convolute_voigt_dho is non-differentiable — numpy FFT + interpolation).
_FD_REL = 1e-3
_FD_ABS = (1e-3, 1e-3, None, None, None)  # position params get an absolute floor; amplitudes scale


def predicted_lambda(theta, x_grid, gauss_fwhm, lorentz_fwhm, T, center=0.0):
    """Expected counts λ(E) of a proposed scan, from the exact production forward model.

    Reuses :func:`isar.synth.fit.make_model` (the same DHO⊗Voigt convolution the fit runs) so the
    Fisher screen sees the model it is screening. ``theta = (Ω_d, Γ, A, E, BG)``. λ is clipped ≥0
    (the Poisson rate; mirrors the bootstrap).
    """
    from isar.synth.fit import make_model
    model = make_model(np.asarray(x_grid, dtype=float), gauss_fwhm, lorentz_fwhm, T, center=center)
    return np.clip(np.asarray(model(np.asarray(theta, dtype=float)), dtype=float), 0.0, None)


def _fd_step(theta) -> np.ndarray:
    theta = np.asarray(theta, dtype=float)
    h = np.empty_like(theta)
    for j in range(len(theta)):
        floor = _FD_ABS[j] if j < len(_FD_ABS) and _FD_ABS[j] is not None else _FD_REL
        h[j] = max(_FD_REL * abs(theta[j]), floor)
    return h


def fisher_information(theta, x_grid, gauss_fwhm, lorentz_fwhm, T, center=0.0, *,
                       prior_precision=None):
    """The Poisson Fisher information matrix of a proposed scan on θ = (Ω_d, Γ, A, E, BG).

    ``I_scan[j,k] = Σ_E (∂λ/∂θ_j)(∂λ/∂θ_k)/max(λ,1)`` with central finite-difference derivatives.
    ``prior_precision`` (a full 5×5 matrix or a ``{index: precision}`` dict) is **added** — this is the
    branch soft-constraint precision (``1/σ_Γ²`` on Γ, ``1/σ_A²`` on A) the low-q fit will actually use,
    so ``σ(ω₀)`` reflects "how much does this scan improve ω₀ *given the prior*", not a standalone scan
    that is usually singular at low q. Returns the 5×5 ``I = I_scan + I_prior``.
    """
    theta = np.asarray(theta, dtype=float)
    n = len(theta)
    lam = predicted_lambda(theta, x_grid, gauss_fwhm, lorentz_fwhm, T, center=center)
    lam_safe = np.maximum(lam, 1.0)
    h = _fd_step(theta)
    d = np.empty((n, len(lam)), dtype=float)
    for j in range(n):
        tp = theta.copy(); tp[j] += h[j]
        tm = theta.copy(); tm[j] -= h[j]
        lp = predicted_lambda(tp, x_grid, gauss_fwhm, lorentz_fwhm, T, center=center)
        lm = predicted_lambda(tm, x_grid, gauss_fwhm, lorentz_fwhm, T, center=center)
        d[j] = (lp - lm) / (2.0 * h[j])
    info = (d / lam_safe) @ d.T
    info = 0.5 * (info + info.T)         # symmetrize away finite-difference asymmetry
    if prior_precision is not None:
        info = info + _as_precision_matrix(prior_precision, n)
    return info


def _as_precision_matrix(prior, n) -> np.ndarray:
    if isinstance(prior, dict):
        m = np.zeros((n, n), dtype=float)
        for idx, prec in prior.items():
            m[int(idx), int(idx)] += float(prec)
        return m
    return np.asarray(prior, dtype=float)


def sigma_omega0(theta, info) -> float:
    """σ(ω₀) from the Fisher matrix, via nuisance Schur-complement marginalization + delta method.

    Marginalizes (A, E, BG) by the Schur complement onto the (Ω_d, Γ) block (the honest cost of not
    knowing the nuisances), then projects with the exact Jacobian ``g = (Ω_d/ω₀, Γ/ω₀)`` (the same
    weights as :func:`isar.synth.fit.omega0_value_error`). Returns ``+inf`` — never a falsely-small
    value — when ω₀ is unidentifiable: a bare Moore-Penrose inverse would zero the null directions and
    report a tight σ, so instead the ω₀-direction precision ``gᵀ I_marg g`` is rank-checked and any
    singular/non-finite/non-positive variance maps to ∞.
    """
    theta = np.asarray(theta, dtype=float)
    od, gam = float(theta[0]), float(theta[1])
    w0 = math.hypot(od, gam)
    if w0 <= 0.0:
        return _INF
    gp = np.array([od / w0, gam / w0])
    info = np.asarray(info, dtype=float)
    pp = info[np.ix_(_PRIMARY, _PRIMARY)]
    pn = info[np.ix_(_PRIMARY, _NUISANCE)]
    nn = info[np.ix_(_NUISANCE, _NUISANCE)]
    npp = info[np.ix_(_NUISANCE, _PRIMARY)]
    # Schur complement onto (Ω_d, Γ): marginal precision after integrating out the nuisances.
    try:
        nn_inv = np.linalg.inv(nn)
    except np.linalg.LinAlgError:
        nn_inv = np.linalg.pinv(nn)     # nuisance block only — safe here (not the ω₀ direction)
    i_marg = pp - pn @ nn_inv @ npp
    i_marg = 0.5 * (i_marg + i_marg.T)
    # Rank guard on the ω₀ direction: no curvature along g ⇒ ω₀ unidentifiable ⇒ σ = ∞ (never 0).
    prec_along_g = float(gp @ i_marg @ gp)
    scale = float(np.trace(np.abs(i_marg))) or 1.0
    if not math.isfinite(prec_along_g) or prec_along_g <= 1e-12 * scale:
        return _INF
    try:
        cov = np.linalg.inv(i_marg)
    except np.linalg.LinAlgError:
        return _INF
    var = float(gp @ cov @ gp)
    if not math.isfinite(var) or var <= 0.0:
        return _INF
    return math.sqrt(var)


def fisher_eig(sigma_prior, sigma_scan) -> float:
    """EIG on ω₀ from combining the current belief precision with the scan's predicted precision:
    ``½·log(1 + σ_prior²/σ_scan²)``. A scan that cannot pin ω₀ (``σ_scan = ∞``) yields 0; an infinitely
    uncertain prior yields ``+inf``. Both σ in meV."""
    if sigma_scan is None or not math.isfinite(sigma_scan) or sigma_scan <= 0.0:
        return 0.0
    if sigma_prior is None or sigma_prior <= 0.0:
        return 0.0
    if not math.isfinite(sigma_prior):
        return _INF
    return 0.5 * math.log1p((float(sigma_prior) / float(sigma_scan)) ** 2)
