"""MINOS↔bootstrap agreement — the frequentist half of §5.5.

MINOS is the asymptotic limit the bootstrap converges to; where their interval widths
diverge, the scan is out of the asymptotic regime (low counts, weak peak, Γ→0). The
divergence is a reported diagnostic and a §9 confidence input. Compared on Γ (where MINOS
is native and the Γ→0 corner bites) and, if a profiled ω₀ interval is supplied, on ω₀.
This is the production generalization of ``score.plot_minos_hesse``.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class AgreementResult:
    gamma_ratio: float        # MINOS half-width / bootstrap half-width on Γ
    omega0_ratio: float       # profile half-width / bootstrap half-width on ω₀ (nan if none)
    regime: str               # "asymptotic" | "sparse" | "unknown"
    paradigm_divergence: bool


def _half_width(interval) -> float:
    return (float(interval[-1]) - float(interval[0])) / 2.0


def agreement(boot, minos, *, profile_omega0=None, lo: float = 0.5, hi: float = 2.0
              ) -> AgreementResult:
    """Compare MINOS vs bootstrap interval widths. A ratio outside [`lo`, `hi`] on any
    available parameter flags the sparse (non-asymptotic) regime."""
    g_boot = _half_width(boot.gamma_interval)
    gamma_ratio = float("nan")
    me = (minos or {}).get("phonon.Gamma")
    if me is not None and me[2] and g_boot > 0:           # me = (lo, hi, valid)
        gamma_ratio = _half_width((me[0], me[1])) / g_boot

    omega0_ratio = float("nan")
    if profile_omega0 is not None:
        p_lo, p_hi = profile_omega0
        o_boot = _half_width(boot.omega0_interval)
        if np.isfinite(p_lo) and np.isfinite(p_hi) and o_boot > 0:
            omega0_ratio = _half_width((p_lo, p_hi)) / o_boot

    ratios = [r for r in (gamma_ratio, omega0_ratio) if np.isfinite(r)]
    if not ratios:
        return AgreementResult(gamma_ratio, omega0_ratio, "unknown", False)
    sparse = any(r < lo or r > hi for r in ratios)
    return AgreementResult(gamma_ratio, omega0_ratio,
                           "sparse" if sparse else "asymptotic", bool(sparse))
