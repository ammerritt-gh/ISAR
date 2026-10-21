"""
lineshapes.py  —  Shared IXS lineshape library for FeSeS analysis
==================================================================
Canonical version used by both FeSeS_1 and FeSeS_2 analysis pipelines.

Provides:
  - Gaussian, Lorentzian, Voigt profiles (FWHM-parameterised, integrate to Area)
  - Pseudo-Voigt (for elastic-line / K=0 energy-zero calibration)
  - DHO (Damped Harmonic Oscillator) Stokes / anti-Stokes lineshapes
  - Voigt ⊗ DHO convolution model with elastic Voigt peak + flat background
  - The same model with a tabulated (measured) resolution curve in place of the Voigt
  - Helper: voigt_area_for_height

Constants
---------
  KB  : Boltzmann constant in meV/K
"""

from functools import lru_cache

import numpy as np
from scipy.special import wofz
from scipy.signal import fftconvolve

# ============================================================
# Constants
# ============================================================
KB = 0.086173324   # Boltzmann constant (meV / K)

#: Internal convolution grid step (meV) for :func:`convolute_voigt_dho`. Tests that rebuild the
#: grid must read it from here, never re-declare the number.
CONVOLUTION_DX = 0.05
kB = KB            # alias


# ============================================================
# Basic lineshapes
# ============================================================

def Gaussian(x, Area, Width, Center):
    """Gaussian lineshape (FWHM-parameterised, integrates to Area)."""
    return (Area * np.sqrt(4 * np.log(2) / np.pi) / Width
            * np.exp(-4 * np.log(2) * (np.asarray(x) - Center) ** 2 / Width ** 2))


def Lorentzian(x, Area, Width, Center):
    """Lorentzian lineshape (FWHM-parameterised, integrates to Area)."""
    return 2 * Area / np.pi * Width / (4 * (np.asarray(x) - Center) ** 2 + Width ** 2)


def Voigt(x, Area, gauss_fwhm, lorentz_fwhm, center):
    """Voigt profile via Faddeeva function (integrates to Area)."""
    sigma = gauss_fwhm / (2 * np.sqrt(2 * np.log(2)))
    gamma = lorentz_fwhm / 2
    z = ((np.asarray(x) - center) + 1j * gamma) / (sigma * np.sqrt(2))
    profile = np.real(wofz(z)) / (sigma * np.sqrt(2 * np.pi))
    return Area * profile


def Voigt_wBG(x, center, gauss_fwhm, lorentz_fwhm, elastic_amplitude, BG):
    """Voigt profile + flat background (for Bragg / elastic-line fitting)."""
    return Voigt(x, elastic_amplitude, gauss_fwhm, lorentz_fwhm, center) + BG


def pseudo_voigt(x, I, W, x0, eta):
    """Pseudo-Voigt: eta * Lorentzian + (1-eta) * Gaussian (for K=0 calibration)."""
    return eta * Lorentzian(x, I, W, x0) + (1 - eta) * Gaussian(x, I, W, x0)


# ============================================================
# Area / height conversion utilities
# ============================================================

def voigt_area_for_height(height, gauss_fwhm, lorentz_fwhm):
    """Return the Area that gives a Voigt peak of a given height at its centre."""
    sigma = gauss_fwhm / (2 * np.sqrt(2 * np.log(2)))
    gamma = lorentz_fwhm / 2
    z = (1j * gamma) / (sigma * np.sqrt(2))
    voigt_peak = np.real(wofz(z)) / (sigma * np.sqrt(2 * np.pi))
    return height / voigt_peak if voigt_peak > 0 else 0.0


# ============================================================
# DHO (Damped Harmonic Oscillator)
# ============================================================

def Stokes_DHO(x, amplitude, damping, center):
    """Stokes (phonon creation) DHO lineshape.
    Returns large penalty if overdamped (centre² ≤ damping²).
    """
    x = np.asarray(x, dtype=float)
    damping_sq = center ** 2 - damping ** 2
    if damping_sq <= 0:
        return np.full_like(x, 1e6 * (1 + abs(damping_sq)))
    return amplitude / ((x - np.sqrt(damping_sq)) ** 2 + damping ** 2)


def antiStokes_DHO(x, amplitude, damping, center):
    """Anti-Stokes (phonon annihilation) DHO lineshape."""
    x = np.asarray(x, dtype=float)
    damping_sq = center ** 2 - damping ** 2
    if damping_sq <= 0:
        return np.full_like(x, 1e6 * (1 + abs(damping_sq)))
    return amplitude / ((x + np.sqrt(damping_sq)) ** 2 + damping ** 2)


# ============================================================
# Voigt ⊗ DHO convolution model
# ============================================================

@lru_cache(maxsize=64)
def _grid_and_kernel(x_span, dx, gauss_fwhm, lorentz_fwhm):
    """Cached uniform grid + unit-area Voigt resolution kernel on it.

    Keyed on the quantities that are fixed within one fit (grid geometry and
    resolution), never on the varying DHO parameters. Arrays are frozen
    read-only so callers can't corrupt the cache.
    """
    x_uniform = np.arange(-x_span, x_span, dx)
    voigt_kernel = Voigt(x_uniform, Area=1.0,
                         gauss_fwhm=gauss_fwhm, lorentz_fwhm=lorentz_fwhm, center=0)
    x_uniform.flags.writeable = False
    voigt_kernel.flags.writeable = False
    return x_uniform, voigt_kernel


@lru_cache(maxsize=64)
def _elastic_unit_voigt(x_target_bytes, x_target_shape, gauss_fwhm, lorentz_fwhm, center):
    """Cached unit-area elastic Voigt profile on x_target (scale by elastic_amplitude)."""
    x_target = np.frombuffer(x_target_bytes, dtype=float).reshape(x_target_shape)
    profile = Voigt(x_target, Area=1.0,
                    gauss_fwhm=gauss_fwhm, lorentz_fwhm=lorentz_fwhm, center=center)
    profile.flags.writeable = False
    return profile


@lru_cache(maxsize=64)
def _tabulated_grid_and_kernel(n_half, dx, curve):
    """Cached node grid ``k*dx`` (k = -n_half..n_half, so 0 is a node) and the curve's kernel
    with its centre index. Keyed on the curve object (identity-hashed), never on DHO params."""
    x_nodes = np.arange(-n_half, n_half + 1) * dx
    x_nodes.flags.writeable = False
    kern, centre = curve.kernel(dx)
    return x_nodes, kern, centre


def convolve_tabulated(x_target, center, curve, signal):
    """``signal`` (a function of energy, evaluated on nodes ``k*dx``) convolved with a tabulated
    resolution curve, evaluated at ``x_target - center``.

    The convolution is aligned on the kernel's ``k = 0`` node, so the result carries no
    window-dependent centring offset (unlike the Voigt path's retained defect below). The grid
    spans the data window plus the same 20 meV pad as the Voigt path.
    shortcut: a signal with tails beyond that pad (a broad Lorentzian) loses its far-tail area;
    widen the pad if a quasi-elastic width ever approaches it.
    """
    x_target = np.asarray(x_target, dtype=float)
    dx = CONVOLUTION_DX
    x_span = max(abs(x_target.min()), abs(x_target.max())) + 20
    x_nodes, kern, centre = _tabulated_grid_and_kernel(int(np.ceil(x_span / dx)), dx, curve)
    full = fftconvolve(signal(x_nodes), kern, mode='full') * dx
    # full[p] sits at node index p - centre relative to x_nodes[0].
    x_full = x_nodes[0] + (np.arange(full.size) - centre) * dx
    return np.interp(x_target - center, x_full, full, left=0.0, right=0.0)


def _tabulated_dho(x_target, phonon_energy, center, damping, phonon_amplitude,
                   elastic_amplitude, BG, T, peak, curve):
    """The tabulated-resolution branch of :func:`convolute_voigt_dho` (same physics, same
    detailed-balance tie; the elastic line is the curve itself at ``center``)."""
    x_target = np.asarray(x_target, dtype=float)
    peak_lower = peak.lower()
    peak_parts = ['stokes', 'astokes', 'elastic'] if peak_lower == 'all' else peak_lower.split('+')

    def signal(x_nodes):
        s = np.zeros_like(x_nodes)
        if 'stokes' in peak_parts:
            s += Stokes_DHO(x_nodes, amplitude=phonon_amplitude, damping=damping,
                            center=phonon_energy)
        if 'astokes' in peak_parts and T > 0:
            astokes_amp = phonon_amplitude / np.exp(phonon_energy / (KB * T))
            s += antiStokes_DHO(x_nodes, amplitude=astokes_amp, damping=damping,
                                center=phonon_energy)
        return s

    if 'stokes' in peak_parts or 'astokes' in peak_parts:
        y = convolve_tabulated(x_target, center, curve, signal)
    else:
        y = np.zeros_like(x_target)
    if 'elastic' in peak_parts:
        y += elastic_amplitude * curve.profile(x_target - center)
    y += BG
    return y


def convolute_voigt_dho(x_target, phonon_energy, center, gauss_fwhm, lorentz_fwhm,
                        damping, phonon_amplitude, elastic_amplitude, BG, T,
                        peak='all', resolution_curve=None):
    """
    Voigt-convolved DHO model + elastic Voigt peak + flat background.

    With ``resolution_curve`` (an :class:`isar.analyze.resolution.TabulatedResolution`) the
    tabulated curve replaces the Voigt for both the DHO convolution and the elastic line, and
    ``gauss_fwhm``/``lorentz_fwhm`` are ignored. Without it the Voigt path below is unchanged.

    Parameters
    ----------
    x_target        : array-like — energy grid (meV)
    phonon_energy   : float      — undamped phonon frequency ω₀ (meV)
    center          : float      — energy-zero offset (meV)
    gauss_fwhm      : float      — Gaussian FWHM of Voigt resolution (meV)
    lorentz_fwhm    : float      — Lorentzian FWHM of Voigt resolution (meV)
    damping         : float      — DHO damping Γ (meV)
    phonon_amplitude: float      — Stokes DHO amplitude
    elastic_amplitude: float     — elastic Voigt area
    BG              : float      — flat background
    T               : float      — temperature (K)
    peak            : str        — 'all', 'stokes', 'astokes', 'elastic',
                                   or '+'-separated combination

    Returns
    -------
    y : ndarray — model intensity at x_target
    """
    if resolution_curve is not None:
        return _tabulated_dho(x_target, phonon_energy, center, damping, phonon_amplitude,
                              elastic_amplitude, BG, T, peak, resolution_curve)
    x_target = np.asarray(x_target, dtype=float)
    # The convolution grid (CONVOLUTION_DX). DO NOT make this finer without a live campaign to
    # show for it: 0.01 and 0.02 were both tried on 2026-09-03, on the same six-point path
    # ("map the acoustic phonon from (2,0,0) to (2.3,0,0)", 0.1 meV bar), and neither finished.
    # At 0.01 the campaign ran 93 scans, then 49 after an unrelated planner fix, and timed out
    # both times (30-50 min); at 0.02 it ran 77 scans and timed out. At 0.05, identical code
    # otherwise, it reached `met` in 60 scans and 22.6 minutes with every point within 0.041 meV
    # of truth. The fine grids also produce verdicts the coarse one never does (`no_signal`,
    # `signal_unmodeled`, and an `elastic_only` abstention on a real near-fold peak), so they
    # degrade model selection, not just arithmetic. The offline accuracy case for 0.01/0.02 (a
    # refit of 82 scans, median error 0.021 -> 0.012 meV at 0.02) does not survive contact with a
    # live campaign, and the 0.1 meV per-point bar already covers the residual error.
    dx = CONVOLUTION_DX
    pad_range = 20

    x_span = max(abs(x_target.min()), abs(x_target.max())) + pad_range
    # KNOWN DEFECT, deliberately retained (PR #10 review, deferred): this grid contains exact 0
    # only when x_span is a dx multiple, so fftconvolve(mode='same') carries a sub-dx (<= dx/2),
    # window-dependent centring offset. The symmetric odd-grid fix (arange(-n, n+1)*dx) is correct
    # but removes the accidental tie-breaking this offset provides to degenerate multi-mode
    # candidates: with it, k>=7 shadow candidates fail MIGRAD often enough that shadow selection
    # reports `unavailable` (an EIG-eligibility blocker), and the frozen Slice-8 replacement
    # candidate no longer reproduces. Fixing it needs its own owner-sequenced slice (fit-robustness
    # + candidate renewal). tests/analyze/test_lineshapes.py pins the defect as strict xfails.
    x_uniform, voigt_kernel = _grid_and_kernel(float(x_span), dx, float(gauss_fwhm), float(lorentz_fwhm))

    peak_lower = peak.lower()
    if peak_lower == 'all':
        peak_parts = ['stokes', 'astokes', 'elastic']
    else:
        peak_parts = peak_lower.split('+')

    signal = np.zeros_like(x_uniform)
    if 'stokes' in peak_parts:
        signal += Stokes_DHO(x_uniform, amplitude=phonon_amplitude,
                             damping=damping, center=phonon_energy)
    if 'astokes' in peak_parts and T > 0:
        # Detailed balance is only defined for T > 0: at T = 0 the anti-Stokes side is exactly
        # empty (no phonons to annihilate), and a negative T would invert the Bose weight.
        astokes_amp = phonon_amplitude / np.exp(phonon_energy / (KB * T))
        signal += antiStokes_DHO(x_uniform, amplitude=astokes_amp,
                                 damping=damping, center=phonon_energy)

    convolved = fftconvolve(signal, voigt_kernel, mode='same') * dx

    y = np.interp(x_target - center, x_uniform, convolved, left=0.0, right=0.0)

    if 'elastic' in peak_parts:
        unit_profile = _elastic_unit_voigt(x_target.tobytes(), x_target.shape,
                                           float(gauss_fwhm), float(lorentz_fwhm), float(center))
        y += elastic_amplitude * unit_profile

    y += BG
    return y
