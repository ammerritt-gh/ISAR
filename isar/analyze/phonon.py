"""Phonon-owned analysis helpers: DHO parameter conversions and fit assessment.

More DHO-specific machinery arrives in slice P2.7.
"""
from __future__ import annotations

import numpy as np


# ----------------------------------------------------------------------------
# Reparameterization helpers
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
    var_od, var_g, cov_og = cov[0, 0], cov[1, 1], cov[0, 1]
    d_od, d_g = omega_d / omega0, gamma / omega0
    var = d_od**2 * var_od + d_g**2 * var_g + 2.0 * d_od * d_g * cov_og
    return omega0, float(np.sqrt(var)) if var > 0 else float("nan")


# ----------------------------------------------------------------------------
# Success assessment
# ----------------------------------------------------------------------------
def assess_success(x, errs, at_limit, backend_success, covariance_ok, eps_omega_d,
                   rtol=1e-2) -> bool:
    """Fit succeeds iff: backend converged, covariance usable, all σ finite & >0,
    Ω_d not pinned at ε (ω₀/Γ unseparable) and its error doesn't swamp it, and Ω_d not
    at the upper bound. Γ at its floor is NOT a failure (handled via a separate flag)."""
    if not backend_success or not covariance_ok or errs is None:
        return False
    errs = np.asarray(errs, dtype=float)
    if not np.all(np.isfinite(errs)) or np.any(errs <= 0):
        return False
    omega_d = float(x[0])
    if omega_d <= eps_omega_d * (1.0 + rtol):
        return False
    if errs[0] >= omega_d:
        return False
    if at_limit.get("omega_d", False):  # low already excluded → this is the upper bound
        return False
    return True
