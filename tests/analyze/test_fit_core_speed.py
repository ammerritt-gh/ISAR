"""Fit-core speed (S1U7): the resolution kernel is transformed once per fit, and the new evaluation
matches the pre-change arithmetic.

The reference below is the pre-change model, written out with ``scipy.signal.fftconvolve`` per mode
and per call, exactly as ``convolute_voigt_dho`` and ``convolve_tabulated`` computed it. Agreement
is asserted to 1e-9 of the curve's peak: FFT round-off is absolute on the scale of the peak
(about 1e-16 of it), so a point-by-point relative test in a far tail would be testing round-off, not
the model.
"""
import numpy as np
import pytest
from scipy.signal import fftconvolve

from isar.analyze import lineshapes as L
from isar.analyze.components import (CompositeModel, ConstantBackground, Elastic, PhononDHO,
                                     QuasiElastic)
from isar.analyze.context import ScanContext
from isar.analyze.phonon import bose_occupation, omega0_from
from isar.analyze.resolution import TabulatedResolution

DX = L.CONVOLUTION_DX
REL = 1e-9


def _close(new, old):
    np.testing.assert_allclose(new, old, rtol=0.0, atol=REL * float(np.max(np.abs(old))))


def _signal(grid, w0, gamma, amp, T, peak):
    parts = ["stokes", "astokes"] if peak == "all" else peak.split("+")
    s = np.zeros_like(grid)
    if "stokes" in parts:
        s += L.Stokes_DHO(grid, amp, gamma, w0)
    if "astokes" in parts and T > 0:
        s += L.antiStokes_DHO(grid, amp / np.exp(w0 / (L.KB * T)), gamma, w0)
    return s


def _old_voigt_dho(x, w0, center, g, lor, gamma, amp, T, peak="stokes+astokes"):
    """Pre-change Voigt path of convolute_voigt_dho (phonon part only)."""
    x_span = max(abs(x.min()), abs(x.max())) + 20
    grid = np.arange(-x_span, x_span, DX)
    kernel = L.Voigt(grid, 1.0, g, lor, 0)
    conv = fftconvolve(_signal(grid, w0, gamma, amp, T, peak), kernel, mode="same") * DX
    return np.interp(x - center, grid, conv, left=0.0, right=0.0)


def _old_tabulated(x, center, curve, signal):
    """Pre-change convolve_tabulated."""
    x_span = max(abs(x.min()), abs(x.max())) + 20
    n_half = int(np.ceil(x_span / DX))
    nodes = np.arange(-n_half, n_half + 1) * DX
    kern, centre = curve.kernel(DX)
    full = fftconvolve(signal(nodes), kern, mode="full") * DX
    x_full = nodes[0] + (np.arange(full.size) - centre) * DX
    return np.interp(x - center, x_full, full, left=0.0, right=0.0)


def _curve():
    e = np.round(np.arange(-8.0, 12.0 + 1e-9, 0.1), 10)
    core = np.where(e < 0, np.exp(-0.5 * (e / 0.4) ** 2), np.exp(-0.5 * (e / 0.7) ** 2))
    return TabulatedResolution(e, core + 0.02 * np.exp(-np.abs(e) / 2.0), "d1", None, "generated")


def _ctx(x, *, curve=None, exposure=None, qe=None, T=150.0, center=0.13):
    kw = {}
    if exposure is not None:
        kw = {"exposure_scale": exposure, "exposure_reference": 1.0e5, "exposure_source": "monitor"}
    return ScanContext(x=x, counts=np.zeros_like(x), mask=np.ones_like(x, dtype=bool), T=T,
                       center=center, gauss_fwhm=1.1, lorentz_fwhm=0.3, instrument="BL43LXU",
                       energy_status="needs_zero", quasielastic_fwhm=qe, resolution_curve=curve,
                       **kw)


# (Omega_d, Gamma, A) per mode; the third mode is broad, the second is Stokes-only.
MODES = [(3.2, 0.3, 50.0), (6.5, 0.6, 30.0), (11.0, 1.4, 20.0)]


def _composite(n, *, elastic=True, qe=False):
    comps = [PhononDHO(prefix="phonon" if i == 0 else f"phonon{i + 1}",
                       peak="stokes" if i == 1 else "stokes+astokes") for i in range(n)]
    if elastic:
        comps.append(Elastic())
    if qe:
        comps.append(QuasiElastic())
    comps.append(ConstantBackground())
    return CompositeModel(comps)


def _theta(comp, n):
    vals = {}
    for i, (od, g, a) in enumerate(MODES[:n]):
        p = "phonon" if i == 0 else f"phonon{i + 1}"
        vals.update({f"{p}.Omega_d": od, f"{p}.Gamma": g, f"{p}.A": a})
    vals.update({"elastic.E": 400.0, "qe.QE": 70.0, "bg.BG": 2.5})
    return np.array([vals[nm] for nm in comp.param_names()])


def _old_composite(comp, x, theta, ctx):
    """Pre-change CompositeModel.evaluate: each mode convolved on its own, then summed."""
    total = np.zeros_like(x)
    for c, local in zip(comp.components, comp._split(theta)):
        if isinstance(c, PhononDHO):
            w0 = omega0_from(local["Omega_d"], local["Gamma"])
            amp = local["A"] * bose_occupation(w0, ctx.T)
            if ctx.resolution_curve is not None:
                y = _old_tabulated(x, ctx.center, ctx.resolution_curve,
                                   lambda g: _signal(g, w0, local["Gamma"], amp, ctx.T, c.peak))
            else:
                y = _old_voigt_dho(x, w0, ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                                   local["Gamma"], amp, ctx.T, c.peak)
        elif isinstance(c, QuasiElastic) and ctx.resolution_curve is not None:
            y = _old_tabulated(x, ctx.center, ctx.resolution_curve,
                               lambda g: L.Lorentzian(g, local["QE"], ctx.quasielastic_fwhm, 0.0))
        else:
            y = c.evaluate(x, local, ctx)
        total = total + y
    exposure = ctx.exposure_for(x)
    return total if exposure is None else total * exposure


X = np.arange(-16.0, 20.0001, 0.25)


def test_the_kernel_is_transformed_once_per_resolution_never_per_call(monkeypatch):
    """Red on the pre-change code, which ran one fftconvolve (both transforms) per mode per call."""
    calls = []
    monkeypatch.setattr(L, "fftconvolve", lambda *a, **k: calls.append(1) or fftconvolve(*a, **k),
                        raising=False)
    curve = _curve()

    def evaluate_25():
        for i in range(25):
            L.convolute_voigt_dho(X, 4.0 + 0.1 * i, 0.1, 1.1, 0.3, 0.4, 50.0, 30.0, 2.0, 150.0)
            L.convolute_voigt_dho(X, 4.0 + 0.1 * i, 0.1, 1.1, 0.3, 0.4, 50.0, 30.0, 2.0, 150.0,
                                  resolution_curve=curve)

    evaluate_25()
    assert len(calls) == 0
    L._voigt_convolver.cache_clear()
    L._tabulated_convolver.cache_clear()
    evaluate_25()
    assert L._voigt_convolver.cache_info().misses == 1
    assert L._tabulated_convolver.cache_info().misses == 1
    assert L._voigt_convolver.cache_info().hits == 24
    assert not L._voigt_convolver(40.0, DX, 1.1, 0.3).kernel_fft.flags.writeable


@pytest.mark.parametrize("peak", ["stokes+astokes", "stokes", "astokes", "all"])
def test_voigt_dho_matches_the_per_call_fft(peak):
    new = L.convolute_voigt_dho(X, 4.3, 0.13, 1.1, 0.3, 0.45, 60.0, 0.0, 0.0, 150.0, peak=peak)
    old = _old_voigt_dho(X, 4.3, 0.13, 1.1, 0.3, 0.45, 60.0, 150.0, peak)
    _close(new, old)


def test_tabulated_dho_and_qe_match_the_per_call_fft():
    curve = _curve()
    new = L.convolute_voigt_dho(X, 4.3, 0.13, 0.0, 0.0, 0.45, 60.0, 0.0, 0.0, 150.0,
                                resolution_curve=curve)
    old = _old_tabulated(X, 0.13, curve, lambda g: _signal(g, 4.3, 0.45, 60.0, 150.0, "all"))
    _close(new, old)
    lor = lambda g: L.Lorentzian(g, 70.0, 3.0, 0.0)
    _close(L.convolve_tabulated(X, 0.13, curve, lor), _old_tabulated(X, 0.13, curve, lor))


CASES = ["single", "multi", "multi_qe_exposure", "tabulated_multi", "zero_temperature"]


def _case(case):
    n = 1 if case == "single" else 3
    exposure = np.linspace(0.7, 1.3, X.size) if "exposure" in case else None
    curve = _curve() if case.startswith("tabulated") else None
    qe = 3.0 if "qe" in case else None
    ctx = _ctx(X, curve=curve, exposure=exposure, qe=qe,
               T=0.0 if case == "zero_temperature" else 150.0)
    comp = _composite(n, qe=qe is not None)
    return ctx, comp, _theta(comp, n)


@pytest.mark.parametrize("case", CASES)
def test_composite_evaluate_matches_the_old_evaluation(case):
    ctx, comp, theta = _case(case)
    _close(comp.evaluate(X, theta, ctx), _old_composite(comp, X, theta, ctx))
