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
    CONVOLUTION_DX, KB, Stokes_DHO, Voigt, antiStokes_DHO, convolute_voigt_dho,
)

TRUE_OMEGA0 = 5.0
DX = CONVOLUTION_DX   # the model's internal grid step (lineshapes.convolute_voigt_dho)

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
    dx = DX
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
    cached_grid, cached_kernel = _grid_and_kernel(float(x_span), DX, 1.0, 0.0)
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

    x_uniform, kernel = _grid_and_kernel(28.0, DX, 1.0, 0.0)
    assert not x_uniform.flags.writeable
    assert not kernel.flags.writeable

    x_target = np.linspace(-5.0, 8.0, 131)
    profile = _elastic_unit_voigt(x_target.tobytes(), x_target.shape, 1.0, 0.0, 0.0)
    assert not profile.flags.writeable


# --- a resolution-limited mode on the convolution grid -------------------------------------------
def _bl43_like_curve():
    """A tabulated curve of about 1.3 meV FWHM (a Voigt of 0.96 meV Gaussian and 0.6 meV Lorentzian
    FWHM, as the endpoint world's), tabulated every 0.02 meV."""
    from isar.analyze.resolution import TabulatedResolution

    e = np.round(np.arange(-8.0, 12.0 + 1e-9, 0.02), 10)
    profile = Voigt(e, 1.0, 0.96, 0.6, 0.0)
    return TabulatedResolution(e, profile / profile.max(), "d", None, "generated")


@pytest.mark.parametrize("path", ["voigt", "tabulated"])
def test_a_resolution_limited_mode_keeps_its_area_wherever_it_sits_on_the_grid(path):
    """A mode at the Γ floor (the delta-function twin) is the resolution curve with the mode's
    area: moving its energy between two 0.05 meV grid nodes must not move that area or the peak
    (both sides, the anti-Stokes partner at its Bose weight). The peak keeps about 0.1%: the linear
    interpolation of the 0.05 meV output grid, which an ordinary 0.1 meV-wide mode shows too."""
    from isar.analyze.phonon import GAMMA_FLOOR, omega0_from

    curve = _bl43_like_curve() if path == "tabulated" else None
    x = np.arange(-12.0, 18.0 + 1e-9, 0.01)
    T = 300.0
    stokes_area, peaks, bose = [], [], []
    for f in np.linspace(0.0, 1.0, 11)[:-1]:
        w0 = omega0_from(5.0 + f * DX, GAMMA_FLOOR)
        args = (x, w0, 0.0, 0.96, 0.6, GAMMA_FLOOR, 1.0, 0.0, 0.0, T)
        stokes = convolute_voigt_dho(*args, peak="stokes", resolution_curve=curve)
        anti = convolute_voigt_dho(*args, peak="astokes", resolution_curve=curve)
        stokes_area.append(np.trapezoid(stokes, x))
        peaks.append(stokes.max())
        bose.append(np.trapezoid(anti, x) / stokes_area[-1] / np.exp(-w0 / (KB * T)))
    assert max(stokes_area) / min(stokes_area) - 1.0 < 1e-3
    assert np.mean(stokes_area) == pytest.approx(np.pi / GAMMA_FLOOR, rel=0.02)   # the mode's area
    assert max(peaks) / min(peaks) - 1.0 < 1.5e-3
    assert max(bose) / min(bose) - 1.0 < 1e-3


def test_the_narrow_rendering_is_continuous_in_the_width_and_leaves_ordinary_widths_alone():
    """The B-spline average blends into the point samples between the two thresholds, so the model
    has no step in Γ, and above them a mode is point-sampled exactly as before."""
    from isar.analyze.lineshapes import NARROW_GAMMA_STEPS, dho_signal

    grid = np.arange(-30.0, 30.0, DX)
    lo, hi = (s * DX for s in NARROW_GAMMA_STEPS)
    for edge in (lo, hi):
        below, above = (dho_signal(grid, 5.0, g, 1.0, 300.0) for g in (edge * (1 - 1e-7),
                                                                         edge * (1 + 1e-7)))
        assert np.max(np.abs(below - above)) < 1e-5 * np.max(above)
    broad = 1.5 * hi
    point = (Stokes_DHO(grid, 1.0, broad, 5.0)
             + antiStokes_DHO(grid, np.exp(-5.0 / (KB * 300.0)), broad, 5.0))
    np.testing.assert_allclose(dho_signal(grid, 5.0, broad, 1.0, 300.0), point, rtol=1e-14)
