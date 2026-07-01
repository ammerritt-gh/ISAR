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
    # §5.5 three-method error-extraction cross-check on ω₀ (HESSE / bootstrap / posterior).
    # Raw half-widths are carried as data (a later report renders them); the ratios and flags
    # are the derived diagnosis. All default to neutral so the frequentist-only path is unchanged.
    hesse_half_width: float = float("nan")
    boot_half_width: float = float("nan")
    bayes_half_width: float = float("nan")
    bayes_freq_ratio: float = float("nan")   # bootstrap half-width / bayes half-width
    hesse_freq_ratio: float = float("nan")   # HESSE σ / bootstrap half-width
    prior_driven: bool = False               # freq ≫ bayes with an informative prior present
    fork_suspected: bool = False             # bootstrap multimodal but bayes unimodal
    unconstrained: bool = False              # both wide AND disagreeing
    bayes_available: bool = False


def _half_width(interval) -> float:
    return (float(interval[-1]) - float(interval[0])) / 2.0


def agreement(boot, minos, *, profile_omega0=None, bayes=None, prior=None,
              hesse_omega0_sigma=None, lo: float = 0.5, hi: float = 2.0,
              wide_rel: float = 0.5) -> AgreementResult:
    """Compare the paradigms' interval widths. The frequentist half (unchanged) flags the
    sparse regime when MINOS and the bootstrap disagree. The §5.5 extension carries the ω₀
    half-widths from all three error-extraction roads (HESSE, bootstrap, posterior) and derives
    the divergence flags: ``prior_driven`` (freq ≫ bayes with a prior), ``fork_suspected``
    (bootstrap multimodal but bayes unimodal), ``unconstrained`` (both wide and disagreeing).
    ``bayes`` / ``hesse_omega0_sigma`` absent ⇒ identical to the frequentist-only behaviour."""
    # --- frequentist MINOS↔bootstrap regime (unchanged) ---
    g_boot = _half_width(boot.gamma_interval)
    o_boot = _half_width(boot.omega0_interval)
    gamma_ratio = float("nan")
    me = (minos or {}).get("phonon.Gamma")
    if me is not None and me[2] and g_boot > 0:           # me = (lo, hi, valid)
        gamma_ratio = _half_width((me[0], me[1])) / g_boot

    omega0_ratio = float("nan")
    if profile_omega0 is not None:
        p_lo, p_hi = profile_omega0
        if np.isfinite(p_lo) and np.isfinite(p_hi) and o_boot > 0:
            omega0_ratio = _half_width((p_lo, p_hi)) / o_boot

    ratios = [r for r in (gamma_ratio, omega0_ratio) if np.isfinite(r)]
    if not ratios:
        regime, divergence = "unknown", False
    else:
        divergence = any(r < lo or r > hi for r in ratios)
        regime = "sparse" if divergence else "asymptotic"

    res = AgreementResult(gamma_ratio, omega0_ratio, regime, bool(divergence))

    # --- §5.5 three-method cross-check on the reported quantity, ω₀ ---
    res.boot_half_width = o_boot
    if hesse_omega0_sigma is not None and np.isfinite(hesse_omega0_sigma):
        res.hesse_half_width = float(hesse_omega0_sigma)
        if o_boot > 0:
            res.hesse_freq_ratio = res.hesse_half_width / o_boot

    if bayes is not None and getattr(bayes, "converged", False):
        b_hw = _half_width(bayes.omega0_interval)
        res.bayes_half_width = b_hw
        res.bayes_available = True
        if b_hw > 0:
            res.bayes_freq_ratio = o_boot / b_hw
        # freq ≫ bayes with an informative prior ⇒ the prior is doing the work (§5.5 bullet 1)
        if prior is not None and np.isfinite(res.bayes_freq_ratio) and res.bayes_freq_ratio > 1.0 / lo:
            res.prior_driven = True
        # bootstrap multimodal but bayes unimodal ⇒ structural ambiguity → §4 fork (bullet 2)
        if getattr(boot, "bimodal", False) and not getattr(bayes, "bimodal", False):
            res.fork_suspected = True
        # both wide AND disagreeing ⇒ genuinely uninformative scan (bullet 3)
        ref = abs(float(boot.omega0_interval[1]))
        if ref > 0 and np.isfinite(res.bayes_freq_ratio):
            both_wide = (o_boot / ref > wide_rel) and (b_hw / ref > wide_rel)
            disagree = res.bayes_freq_ratio < lo or res.bayes_freq_ratio > hi
            res.unconstrained = bool(both_wide and disagree)

    return res
