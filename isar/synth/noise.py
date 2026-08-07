"""Counting-statistics layer: count scaling + Poisson draw.

The physically interpretable knob is **target peak counts** — the Stokes phonon
maximum above background, mapping onto "how long we counted." Because the model is
strictly linear in `phonon_amplitude`, `elastic_amplitude`, and `BG`, scaling all
three by one factor scales the whole spectrum, so the count scale is solved exactly
in one shot (see `generate.make_ground_truth`).
"""
from __future__ import annotations

import numpy as np

from scipy.interpolate import interp1d
from scipy.signal import fftconvolve

from . import config
from isar.analyze.lineshapes import KB, Voigt, convolute_voigt_dho

# Convolution grid, matched to isar.analyze.lineshapes.convolute_voigt_dho so the unknown
# lineshape is smeared by exactly the same resolution treatment as the DHO it competes with
# (including that function's known sub-dx centring offset — a mismatch world must differ in
# its lineshape, not in its numerics).
_CONV_DX = 0.05
_CONV_PAD = 20.0


def energy_grid(x_min: float, x_max: float, x_step: float) -> np.ndarray:
    """Inclusive energy axis [x_min, x_max] at x_step (±10 meV / 0.25 → 81 channels)."""
    n = int(round((x_max - x_min) / x_step)) + 1
    return x_min + x_step * np.arange(n)


def phonon_peak_height(x, omega0, gamma, phonon_amplitude,
                       gauss_fwhm, lorentz_fwhm, T) -> float:
    """Maximum of the (Stokes + anti-Stokes) component, excluding elastic and BG.

    This is the 'phonon maximum above background' that `target_peak_counts` sets.
    Excluding the elastic line ensures the maximum is the phonon, not the elastic peak.
    """
    comp = convolute_voigt_dho(
        x, omega0, 0.0, gauss_fwhm, lorentz_fwhm, gamma,
        phonon_amplitude, 0.0, 0.0, T, peak="stokes+astokes",
    )
    return float(np.max(comp))


# ----------------------------------------------------------------------------
# The unknown lineshape (SCENE model-mismatch world)
# ----------------------------------------------------------------------------
def two_horn_profile(x, center, width, asymmetry, horn_fwhm: float = 0.25) -> np.ndarray:
    """Asymmetric flat-top profile: a plateau of ``width`` meV with a horn at each edge.

    Deliberately neither a DHO nor a Voigt — a flat interior between two maxima of unequal
    height (``asymmetry`` in [−1, 1] shifts weight to the low-energy horn), falling off as
    a Gaussian outside. A DHO fit of this can only ever be wrong, which is the point: the
    mismatch world exists to check that the pipeline notices rather than reporting a tidy ω₀.

    Height-parameterized (plateau height 1), so the caller scales it to counts.
    """
    x = np.asarray(x, dtype=float)
    lo, hi = center - 0.5 * width, center + 0.5 * width
    plateau = np.where((x >= min(lo, hi)) & (x <= max(lo, hi)), 1.0, 0.0)
    k = 4.0 * np.log(2.0) / horn_fwhm**2
    horn_lo = np.exp(-k * (x - lo) ** 2)
    horn_hi = np.exp(-k * (x - hi) ** 2)
    return plateau + (1.0 + asymmetry) * horn_lo + (1.0 - asymmetry) * horn_hi


def convolute_voigt_unknown(x_target, omega0, center, gauss_fwhm, lorentz_fwhm,
                            width, asymmetry, amplitude, BG, T,
                            peak: str = "stokes+astokes") -> np.ndarray:
    """`two_horn_profile` ⊗ Voigt resolution + flat background, on the same terms as the DHO.

    The anti-Stokes side is the mirrored Stokes profile times the Bose factor
    exp(−ω₀/k_BT) — derived, never independent (AGENTS.md hard rule 2). At T = 0 it is
    exactly empty.
    """
    x_target = np.asarray(x_target, dtype=float)
    x_span = max(abs(x_target.min()), abs(x_target.max())) + _CONV_PAD
    x_uniform = np.arange(-x_span, x_span, _CONV_DX)

    parts = ["stokes", "astokes"] if peak.lower() == "all" else peak.lower().split("+")
    unknown_parts = set(parts) - {"stokes", "astokes"}
    if unknown_parts or not parts:
        # A mistyped side would silently return a partial model — and a partial model of
        # this component is a different physical claim, not a smaller one.
        raise ValueError(f"peak {peak!r} names no side of this component: {sorted(unknown_parts)}")
    signal = np.zeros_like(x_uniform)
    if "stokes" in parts:
        signal += amplitude * two_horn_profile(x_uniform, omega0, width, asymmetry)
    if "astokes" in parts and T > 0:
        bose = np.exp(-omega0 / (KB * T))
        signal += amplitude * bose * two_horn_profile(x_uniform, -omega0, width, -asymmetry)

    kernel = Voigt(x_uniform, Area=1.0, gauss_fwhm=gauss_fwhm,
                   lorentz_fwhm=lorentz_fwhm, center=0)
    convolved = fftconvolve(signal, kernel, mode="same") * _CONV_DX
    interp_fn = interp1d(x_uniform, convolved, kind="linear",
                         bounds_error=False, fill_value=0.0)
    return interp_fn(x_target - center) + BG


def model_on_grid(gt: config.GroundTruth, x) -> np.ndarray:
    """Noise-free model evaluated at arbitrary energies x (used for smooth plot curves)."""
    return convolute_voigt_dho(
        x, gt.omega0, gt.center, gt.gauss_fwhm, gt.lorentz_fwhm, gt.gamma,
        gt.phonon_amplitude, gt.elastic_amplitude, gt.BG, gt.T, peak=gt.peak,
    )


def scaled_model(gt: config.GroundTruth) -> np.ndarray:
    """Noise-free expected counts λ on the scan (data) grid, using the truth parameters."""
    x = energy_grid(gt.x_min, gt.x_max, gt.x_step)
    return model_on_grid(gt, x)


def draw_counts(gt: config.GroundTruth, rng: np.random.Generator | None = None) -> np.ndarray:
    """One Poisson(λ) realization. Reproducible from gt.seed unless rng is given."""
    if rng is None:
        rng = np.random.default_rng(gt.seed)
    lam = np.clip(scaled_model(gt), 0.0, None)
    return rng.poisson(lam)
