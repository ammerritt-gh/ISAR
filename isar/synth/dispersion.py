"""Acoustic test dispersion for the (2,0,0) → (3,0,0) Q-series.

ω₀(H) = 6·|sin(π(H−2))| meV: zero at the zone centers (H = 2, 3), maximum 6 meV at
the zone boundary (H = 2.5). Walking H sweeps the fit from easy (mid-zone, ω₀ ≫ Γ,
clear of the elastic line) to hard (near zone center, ω₀ → 0 → Stokes–elastic merge
and the overdamped boundary).
"""
from __future__ import annotations

import numpy as np

# (2,0,0) → (3,0,0): H from 2.00 to 3.00, step 0.05.
H_START = 2.0
H_STOP = 3.0
H_STEP = 0.05

# Dispersion amplitude (meV): maximum ω₀ at the zone boundary.
OMEGA0_MAX = 6.0


def omega0_of_H(H):
    """Undamped phonon frequency ω₀(H) = 6·|sin(π(H−2))| meV. Scalar or array."""
    H = np.asarray(H, dtype=float)
    out = OMEGA0_MAX * np.abs(np.sin(np.pi * (H - H_START)))
    return out if out.ndim else float(out)


def h_values():
    """The 21-point H grid, 2.00 → 3.00 inclusive at step 0.05 (rounded to 2 dp)."""
    n = int(round((H_STOP - H_START) / H_STEP)) + 1
    return np.round(H_START + H_STEP * np.arange(n), 2)


def classify_cell(H, gamma, eps_omega_d):
    """Label a cell by its position on the dispersion.

    - ``unmeasurable``: ω₀ < ε (zone-center/Bragg; phonon buried in the elastic line).
    - ``boundary``: ε ≤ ω₀ ≲ 2Γ (Stokes within ~2 linewidths of the elastic line).
    - ``midzone``: ω₀ ≳ 2Γ (the happy path).
    """
    omega0 = omega0_of_H(H)
    if omega0 < eps_omega_d:
        return "unmeasurable"
    if omega0 < 2.0 * gamma:
        return "boundary"
    return "midzone"
