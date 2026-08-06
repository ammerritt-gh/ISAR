"""Counting-statistics layer: count scaling + Poisson draw.

The physically interpretable knob is **target peak counts** — the Stokes phonon
maximum above background, mapping onto "how long we counted." Because the model is
strictly linear in `phonon_amplitude`, `elastic_amplitude`, and `BG`, scaling all
three by one factor scales the whole spectrum, so the count scale is solved exactly
in one shot (see `generate.make_ground_truth`).
"""
from __future__ import annotations

import numpy as np

from . import config
from isar.analyze.lineshapes import convolute_voigt_dho


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
