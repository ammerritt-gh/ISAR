"""Tests for the counting-statistics layer (count scaling + Poisson draw)."""
import numpy as np

from isar.synth import config, noise


def _gt(seed=7, phonon_amplitude=10.0, elastic_amplitude=200.0, BG=2.0):
    return config.GroundTruth(
        H=2.5, omega0=6.0, gamma=0.5,
        phonon_amplitude=phonon_amplitude, elastic_amplitude=elastic_amplitude, BG=BG,
        gauss_fwhm=0.961, lorentz_fwhm=0.596, T=100.0,
        x_min=-10.0, x_max=10.0, x_step=0.25,
        target_peak_counts=1000.0, seed=seed,
    )


def test_energy_grid_is_81_channels():
    x = noise.energy_grid(-10.0, 10.0, 0.25)
    assert len(x) == 81
    assert x[0] == -10.0
    assert x[-1] == 10.0


def test_phonon_peak_height_linear_in_amplitude():
    x = noise.energy_grid(-10.0, 10.0, 0.25)
    h1 = noise.phonon_peak_height(x, omega0=6.0, gamma=0.5, phonon_amplitude=1.0,
                                  gauss_fwhm=0.961, lorentz_fwhm=0.596, T=100.0)
    h2 = noise.phonon_peak_height(x, omega0=6.0, gamma=0.5, phonon_amplitude=2.0,
                                  gauss_fwhm=0.961, lorentz_fwhm=0.596, T=100.0)
    assert h1 > 0
    assert np.isclose(h2, 2.0 * h1)


def test_scaled_model_is_nonneg_and_full_grid():
    gt = _gt()
    lam = noise.scaled_model(gt)
    assert lam.shape == (81,)
    assert np.all(lam >= 0)
    assert np.all(np.isfinite(lam))


def test_scaled_model_linear_in_amplitudes():
    # Scaling all amplitudes by k scales the whole noise-free spectrum by k.
    lam1 = noise.scaled_model(_gt(phonon_amplitude=10.0, elastic_amplitude=200.0, BG=2.0))
    lam2 = noise.scaled_model(_gt(phonon_amplitude=30.0, elastic_amplitude=600.0, BG=6.0))
    assert np.allclose(lam2, 3.0 * lam1)


def test_draw_counts_are_nonneg_integers():
    counts = noise.draw_counts(_gt())
    assert counts.shape == (81,)
    assert counts.dtype.kind in "iu"
    assert np.all(counts >= 0)


def test_draw_counts_reproducible_by_seed():
    a = noise.draw_counts(_gt(seed=123))
    b = noise.draw_counts(_gt(seed=123))
    c = noise.draw_counts(_gt(seed=124))
    assert np.array_equal(a, b)
    assert not np.array_equal(a, c)


def test_draw_counts_accepts_explicit_rng():
    gt = _gt(seed=1)
    rng = np.random.default_rng(999)
    counts = noise.draw_counts(gt, rng)
    # mean of the draw should be in the right ballpark of the model total
    assert counts.sum() > 0
