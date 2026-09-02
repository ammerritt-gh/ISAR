"""Regression tests for ``isar.analyze.lineshapes`` (PR #10 review findings).

The T = 0 anti-Stokes guard is fixed and pinned here. The convolution-grid centring defect is
KNOWN and deliberately retained: the internal ``arange(-span, span, dx)`` grid contains exact 0
only when the span is a dx multiple, so ``fftconvolve(mode='same')`` carries a sub-dx
(<= dx/2), window-dependent centring offset that biases fitted ω₀. The symmetric odd-grid fix
was implemented and then deferred during the PR #10 review response, because the corrected
(flatter, honestly degenerate) landscape makes k>=7 shadow candidates fail MIGRAD often enough
that shadow selection reports ``unavailable`` — an EIG-eligibility blocker — and the frozen
Slice-8 replacement candidate stops reproducing. The strict xfail below pins the defect: when
the grid is fixed in its own owner-sequenced slice it flips to XPASS and forces this module's
cleanup. (A fitted-ω₀ variant is deliberately absent: real scan grids are dx-multiples, so a
masked-window fit never exercises the misalignment — only direct model evaluation at
non-multiple window extents discriminates.)
"""
import numpy as np
import pytest
from scipy.interpolate import interp1d
from scipy.signal import fftconvolve

from isar.analyze.lineshapes import (
    KB, Stokes_DHO, Voigt, antiStokes_DHO, convolute_voigt_dho,
)

TRUE_OMEGA0 = 5.0
DX = 0.05          # the model's internal grid step (lineshapes.convolute_voigt_dho)

_GRID_DEFECT = pytest.mark.xfail(
    strict=True,
    reason="known sub-dx convolution-grid centring defect (PR #10 review, deferred): the fix "
           "destabilizes degenerate shadow candidates (EIG-eligibility regression) and "
           "invalidates the pending Slice-8 replacement candidate; see lineshapes.py comment")


@_GRID_DEFECT
def test_model_values_are_window_independent():
    # Sharper grid-identity check, no fit: the convolved model at the SAME energies must not
    # change when the requested window grows by a non-multiple-of-dx amount.
    x = np.linspace(-5.0, 8.0, 131)
    args = (TRUE_OMEGA0, 0.0, 1.0, 0.0, 0.4, 400.0, 30.0, 10.0, 300.0)
    base = convolute_voigt_dho(x, *args, peak="all")
    for pad in (0.013, 0.617, 3.33):
        wider = np.concatenate(([x[0] - pad], x, [x[-1] + pad]))
        again = convolute_voigt_dho(wider, *args, peak="all")[1:-1]
        np.testing.assert_allclose(again, base, rtol=0.0, atol=1e-9 * float(np.max(base)))


def test_astokes_at_zero_temperature_is_empty_not_an_error():
    x = np.linspace(-10.0, 10.0, 201)
    y = convolute_voigt_dho(x, TRUE_OMEGA0, 0.0, 1.0, 0.0, 0.4, 400.0, 0.0, 0.0, 0.0,
                            peak="astokes")
    assert np.all(np.isfinite(y)) and np.allclose(y, 0.0)
    # peak="all" at T=0 degrades to Stokes+elastic — finite, no ZeroDivisionError.
    y_all = convolute_voigt_dho(x, TRUE_OMEGA0, 0.0, 1.0, 0.0, 0.4, 400.0, 50.0, 5.0, 0.0,
                                peak="all")
    assert np.all(np.isfinite(y_all))


def _reference_convolute_voigt_dho(x_target, phonon_energy, center, gauss_fwhm, lorentz_fwhm,
                                   damping, phonon_amplitude, elastic_amplitude, BG, T,
                                   peak='all'):
    """Pre-caching reference: identical formulas to the original convolute_voigt_dho, built
    with interp1d directly instead of np.interp, and with no lru_cache in the loop."""
    x_target = np.asarray(x_target, dtype=float)
    dx = 0.05
    pad_range = 20
    x_span = max(abs(x_target.min()), abs(x_target.max())) + pad_range
    x_uniform = np.arange(-x_span, x_span, dx)

    peak_lower = peak.lower()
    peak_parts = ['stokes', 'astokes', 'elastic'] if peak_lower == 'all' else peak_lower.split('+')

    signal = np.zeros_like(x_uniform)
    if 'stokes' in peak_parts:
        signal += Stokes_DHO(x_uniform, amplitude=phonon_amplitude, damping=damping,
                             center=phonon_energy)
    if 'astokes' in peak_parts and T > 0:
        astokes_amp = phonon_amplitude / np.exp(phonon_energy / (KB * T))
        signal += antiStokes_DHO(x_uniform, amplitude=astokes_amp, damping=damping,
                                 center=phonon_energy)

    voigt_kernel = Voigt(x_uniform, Area=1.0, gauss_fwhm=gauss_fwhm, lorentz_fwhm=lorentz_fwhm,
                         center=0)
    convolved = fftconvolve(signal, voigt_kernel, mode='same') * dx

    interp_fn = interp1d(x_uniform, convolved, kind='linear', bounds_error=False, fill_value=0.0)
    y = interp_fn(x_target - center)

    if 'elastic' in peak_parts:
        y += Voigt(x_target, Area=elastic_amplitude, gauss_fwhm=gauss_fwhm,
                   lorentz_fwhm=lorentz_fwhm, center=center)

    y += BG
    return y, x_uniform, convolved


_REF_ARGS = (TRUE_OMEGA0, 0.0, 1.0, 0.0, 0.4, 400.0, 30.0, 10.0, 300.0)


def test_cached_convolution_matches_precache_reference():
    from isar.analyze.lineshapes import _grid_and_kernel

    x = np.linspace(-5.0, 8.0, 131)
    ref_y, x_uniform, convolved = _reference_convolute_voigt_dho(x, *_REF_ARGS, peak="all")
    got_y = convolute_voigt_dho(x, *_REF_ARGS, peak="all")

    # Cached grid + kernel (what the implementation actually convolves with) must be
    # bit-identical to the pre-change per-call computation.
    ref_kernel = Voigt(x_uniform, Area=1.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, center=0)
    x_span = max(abs(x.min()), abs(x.max())) + 20
    cached_grid, cached_kernel = _grid_and_kernel(float(x_span), 0.05, 1.0, 0.0)
    assert np.array_equal(cached_grid, x_uniform)
    assert np.array_equal(cached_kernel, ref_kernel)

    # Full model (interp1d reference vs np.interp implementation) agrees tightly.
    np.testing.assert_allclose(got_y, ref_y, rtol=0.0, atol=1e-12)


def test_repeated_calls_share_cached_kernel_and_grid():
    x = np.linspace(-5.0, 8.0, 131)
    first = convolute_voigt_dho(x, *_REF_ARGS, peak="all")
    # Same resolution/grid, different DHO parameters — kernel/grid cache must not leak
    # DHO-dependent state into results.
    second = convolute_voigt_dho(x, TRUE_OMEGA0, 0.0, 1.0, 0.0, 0.6, 250.0, 15.0, 2.0, 250.0,
                                 peak="all")
    assert not np.allclose(first, second)
    # And calling again with the original args reproduces the original result exactly.
    again = convolute_voigt_dho(x, *_REF_ARGS, peak="all")
    assert np.array_equal(first, again)


def test_cached_kernel_grid_and_elastic_profile_are_read_only():
    from isar.analyze.lineshapes import _elastic_unit_voigt, _grid_and_kernel

    x_uniform, kernel = _grid_and_kernel(28.0, 0.05, 1.0, 0.0)
    assert not x_uniform.flags.writeable
    assert not kernel.flags.writeable

    x_target = np.linspace(-5.0, 8.0, 131)
    profile = _elastic_unit_voigt(x_target.tobytes(), x_target.shape, 1.0, 0.0, 0.0)
    assert not profile.flags.writeable
