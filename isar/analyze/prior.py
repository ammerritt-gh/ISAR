"""The dispersion-prior expectation injected into step-4 verdict/confidence.

`PhononPrior` is the seam the future *running* dispersion prior (built from already-fitted
neighbours, §11) will fill. Step 4 only ever consumes it — it is an input, never built here.
`ScanContext.prior_omega` is the scalar shortcut (= `PhononPrior.omega0`); the richer object
adds the expected peak strength the detection-power test needs and the spread the confidence
distance term uses.

The second half of the module is the **mode prior recipe** (S1U1): the one place a mode's
parameter bounds, and so its flat priors, come from. Every quantity is declared (the fit window,
the resolution width, the spectrum's exposure) and none is read from the observed counts, so the
Occam penalty a candidate pays does not depend on how bright the spectrum happens to be. Starting
values may still come from the data; priors and bounds may not. Derivation of the constants:
``docs/STATS_PRIMER.md``, "The mode prior recipe".
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from isar.analyze.phonon import EPS_OMEGA_D, GAMMA_FLOOR, GAMMA_MAX


@dataclass(frozen=True)
class PhononPrior:
    """Expected phonon at a scan's Q.

    omega0: expected peak position (meV).
    amplitude: expected peak strength (counts above background) for the detection-power test;
        ``None`` = unknown, so detection power abstains (no underpowered verdict can be raised).
    omega0_sigma: prior spread on omega0 (meV) for the confidence distance term.
    """

    omega0: float
    amplitude: float | None = None
    omega0_sigma: float = 1.0


# ----------------------------------------------------------------------------
# The mode prior recipe
# ----------------------------------------------------------------------------
# Bump the version whenever a constant or a rule below changes: it is hashed into the guidelines
# signature (so the affected spectra refit) and recorded with every fit.
PRIOR_RECIPE_VERSION = "isar.mode-prior-recipe/3"

# Amplitude ceiling: a fitted area A (counts at the reference exposure, integrated over meV) is
# bounded by scale x exposure x width, i.e. a resolution-limited peak may reach `scale x exposure`
# counts per point. The scale depends on what the exposure counts: monitor counts (one count per
# monitor count) or seconds (a count rate per second). Agent-proposed from order-of-magnitude
# instrument figures, no measured value: docs/STATS_PRIMER.md. No card override; the S6 sensitivity
# check is how the values reach the operator.
AMPLITUDE_SCALE_PER_MONITOR = 1.0
# Counts per second: the top of the 1e4-1e5 counts/s an analyzer detector saturates at (the primer's
# figure), so a seconds-normalised ceiling never clips a real line.
AMPLITUDE_SCALE_PER_SECOND = 1.0e5

# Exposure assumed for a spectrum that declares none (the legacy uniform-exposure path): a declared
# nominal figure, never a function of the counts.
NOMINAL_EXPOSURE = 1.0e6

# A mode width may fall to this fraction of the resolution width before the absolute GAMMA_FLOOR
# (kept: the H8 boundary-pin tolerance reads it) is reached; it only bites for a resolution sharper
# than GAMMA_FLOOR / WIDTH_FLOOR_FRACTION = 0.1 meV.
WIDTH_FLOOR_FRACTION = 1.0e-2

# A mode's energy (Omega_d, the coordinate the recipe bounds and orders) may not fall below this
# fraction of the resolution FWHM: a DHO nearer than that to the elastic line is not separable from
# it, and the quasi-elastic component is untested (ruling R13), so a central residual is flagged,
# never fitted by a "phonon" standing in for the elastic line. Agent-proposed; undo with 0 (and a
# version bump). The Stokes side only: the anti-Stokes partner is derived from it.
MODE_ENERGY_FLOOR_FRACTION = 0.5


def recipe_exposure(ctx) -> float:
    """The spectrum's declared exposure (``ctx.exposure_reference``: the median kept monitor, or
    counting time where there is no monitor), else :data:`NOMINAL_EXPOSURE`. Never the counts."""
    ref = getattr(ctx, "exposure_reference", None)
    if ref is not None and np.isfinite(float(ref)) and float(ref) > 0.0:
        return float(ref)
    return NOMINAL_EXPOSURE


def resolution_fwhm(ctx) -> float:
    """Total resolution FWHM (meV): the Voigt of ``gauss_fwhm`` and ``lorentz_fwhm`` by the
    Olivero-Longbothum approximation (exact in either limit); a tabulated curve has put its own
    FWHM in ``gauss_fwhm`` with ``lorentz_fwhm`` 0. A spectrum with no usable resolution is held
    upstream, so this refuses rather than defaults."""
    g = float(getattr(ctx, "gauss_fwhm", 0.0) or 0.0)
    lor = float(getattr(ctx, "lorentz_fwhm", 0.0) or 0.0)
    w = 0.5346 * lor + math.sqrt(0.2166 * lor * lor + g * g)
    if not (np.isfinite(w) and w > 0.0):
        raise ValueError(f"mode prior recipe needs a positive resolution width, got "
                         f"gauss_fwhm={g!r} lorentz_fwhm={lor!r}")
    return w


def amplitude_scale(ctx) -> float:
    """The amplitude ceiling's scale for this spectrum, by the source its exposure was counted in.
    A spectrum with no declared exposure keeps the monitor scale on the nominal exposure."""
    if getattr(ctx, "exposure_reference", None) is None:
        return AMPLITUDE_SCALE_PER_MONITOR
    source = getattr(ctx, "exposure_source", None)
    if source == "monitor":
        return AMPLITUDE_SCALE_PER_MONITOR
    if source == "time":
        return AMPLITUDE_SCALE_PER_SECOND
    raise ValueError(f"exposure reference {ctx.exposure_reference!r} has no declared source "
                     f"('monitor' or 'time'), got {source!r}: the amplitude scale cannot be chosen")


def mode_bounds(names, ctx) -> list[tuple[float, float]]:
    """(lo, hi) for each global parameter name, in order: the recipe's bounds and, since every
    parameter's prior is flat over them, its priors.

    * ``.Omega_d`` (energy): uniform over the fit window above the elastic line,
      ``(MODE_ENERGY_FLOOR_FRACTION x resolution width, max fitted energy)``; a window with no room
      above that floor keeps ``(EPS_OMEGA_D, max fitted energy)``.
    * ``.Gamma`` (width): from ``WIDTH_FLOOR_FRACTION`` of the resolution width (never above the
      absolute ``GAMMA_FLOOR``) to ``GAMMA_MAX``.
    * ``.A`` / ``.E`` / ``.QE`` (areas): ``(0, amplitude_scale x exposure x resolution width)``.
    * ``.BG`` (flat background, counts per point): ``(0, amplitude_scale x exposure)``.
    """
    x_max = float(np.max(ctx.x_used))
    exposure = recipe_exposure(ctx)
    width = resolution_fwhm(ctx)
    scale = amplitude_scale(ctx)
    area_hi = scale * exposure * width
    floor = MODE_ENERGY_FLOOR_FRACTION * width
    energy_lo = floor if EPS_OMEGA_D < floor < x_max else EPS_OMEGA_D
    out = []
    for name in names:
        if name.endswith(".Omega_d"):
            out.append((energy_lo, x_max))
        elif name.endswith(".Gamma"):
            out.append((min(GAMMA_FLOOR, WIDTH_FLOOR_FRACTION * width), GAMMA_MAX))
        elif name.endswith((".A", ".E", ".QE")):
            out.append((0.0, area_hi))
        elif name.endswith(".BG"):
            out.append((0.0, scale * exposure))
        else:
            raise ValueError(f"mode prior recipe has no bound for parameter {name!r}")
    return out


def recipe_provenance(ctx) -> dict:
    """What a fit records about the recipe it was bounded by: the version and the declared
    quantities it resolved to for this spectrum (JSON-able)."""
    return {"version": PRIOR_RECIPE_VERSION, "amplitude_scale": amplitude_scale(ctx),
            "exposure_source": getattr(ctx, "exposure_source", None),
            "exposure": recipe_exposure(ctx), "resolution_fwhm": resolution_fwhm(ctx),
            "width_floor_fraction": WIDTH_FLOOR_FRACTION,
            "mode_energy_floor_fraction": MODE_ENERGY_FLOOR_FRACTION}


# ----------------------------------------------------------------------------
# Ordered mode energies
# ----------------------------------------------------------------------------
# N modes of one structure are exchangeable: swapping two labels gives the same curve, so the
# likelihood has N! identical copies of every solution. A flat prior per label makes the two evidence
# lanes disagree by ln N!: nested sampling finds all N! copies, a Laplace expansion at one fitted
# point sees one. The recipe orders the energy coordinate (Omega_d, the sampled one, so the
# normalisation below is exact) and puts one joint density on the ordered region: N!/W^N. The Laplace
# evidence (ordered_energy_log_prior) and the nested-sampling prior transform
# (ordered_energy_transform) are the same prior, so each lane gets the N! exactly once and agrees.
def ordered_energy_log_prior(energies, lo: float, hi: float) -> float:
    """``ln(N!/W^N)`` for N mode energies inside the window ``[lo, hi]`` (W = hi - lo), else -inf.

    A function of the *set* of energies, so every label permutation gives the same value: it is the
    density on the ordered region ``e_1 < ... < e_N``, which integrates to 1 there."""
    e = np.asarray(energies, dtype=float).ravel()
    if not hi > lo:
        raise ValueError(f"energy window needs lo < hi, got ({lo!r}, {hi!r})")
    if e.size and (e.min() < lo or e.max() > hi):
        return -math.inf
    return math.lgamma(e.size + 1) - e.size * math.log(hi - lo)


def ordered_energy_transform(u, lo: float, hi: float) -> np.ndarray:
    """Nested-sampling prior transform for the ordered energies: unit-cube ``u`` (length N) to the
    N order statistics of uniforms on ``[lo, hi]``, ascending. Sampling this is sampling
    :func:`ordered_energy_log_prior`."""
    u = np.asarray(u, dtype=float).ravel()
    n = u.size
    t = np.empty(n)
    if n:
        t[-1] = u[-1] ** (1.0 / n)                  # the largest of n uniforms
        for i in range(n - 2, -1, -1):              # then the largest of the i + 1 left below it
            t[i] = t[i + 1] * u[i] ** (1.0 / (i + 1))
    return lo + (hi - lo) * t
