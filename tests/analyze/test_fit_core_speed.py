"""Fit-core speed (S1U7): the resolution kernel is transformed once per fit, the new evaluation
matches the pre-change arithmetic, and a fit with its linear amplitudes profiled out reaches the
free fit's optimum and evidence.

The reference below is the model written out with ``scipy.signal.fftconvolve`` per mode and per
call, as ``convolute_voigt_dho`` and ``convolve_tabulated`` computed it before the cached batched
convolver: the full-DHO signal (``dho_signal``) convolved alone, each mode normalised to its own
height (the production ``unit_peak`` height, so what is checked is the convolution machinery). Agreement
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
from isar.analyze.resolution import TabulatedResolution

DX = L.CONVOLUTION_DX
REL = 1e-9


def _close(new, old):
    np.testing.assert_allclose(new, old, rtol=0.0, atol=REL * float(np.max(np.abs(old))))


def _signal(grid, w0, gamma, amp, T, peak):
    return L.dho_signal(grid, w0, gamma, amp, T, peak)


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


# (omega0, Gamma, h) per mode; the third mode is broad, the second is Stokes-only.
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
        vals.update({f"{p}.omega0": od, f"{p}.Gamma": g, f"{p}.h": a})
    vals.update({"elastic.h": 400.0, "qe.QE": 70.0, "bg.BG": 2.5})
    return np.array([vals[nm] for nm in comp.param_names()])


def _old_composite(comp, x, theta, ctx):
    """Per-call CompositeModel.evaluate: each mode convolved on its own at A = h / its height,
    then summed."""
    total = np.zeros_like(x)
    for c, local in zip(comp.components, comp._split(theta)):
        if isinstance(c, PhononDHO):
            w0 = local["omega0"]
            conv = L.resolution_convolver(x, ctx.gauss_fwhm, ctx.lorentz_fwhm, ctx.resolution_curve)
            amp = local["h"] / L.unit_peak(conv, w0, local["Gamma"], ctx.T, c.peak)[1].height
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


# ------------------------------------------------------------------ amplitude profiling
@pytest.mark.parametrize("case", CASES)
def test_the_basis_is_the_old_model_linear_in_the_amplitudes(case):
    ctx, comp, theta = _case(case)
    lin = [comp.param_names().index(nm) for nm in comp.linear_names()]
    phi = comp.basis(X, theta, ctx, static=comp.static_columns(X, ctx))
    _close(phi @ theta[lin], _old_composite(comp, X, theta, ctx))
    for k, i in enumerate(lin):    # each column is that amplitude's unit response
        unit = theta.copy()
        unit[lin] = 0.0
        unit[i] = 1.0
        _close(phi[:, k], _old_composite(comp, X, unit, ctx))
    phi_nnls, names = comp.nnls_basis(X, {nm: theta[comp.param_names().index(nm)]
                                          for nm in comp.nonlinear_names()}, ctx)
    assert names == comp.linear_names()
    np.testing.assert_array_equal(phi_nnls, phi)


def _poisson_problem():
    """Five columns: two nearly collinear peaks, a peak under zero counts only, a tall narrow one
    (many decades above the rest) and a flat background."""
    x = np.linspace(-5.0, 15.0, 90)
    cols = [np.exp(-0.5 * ((x - c) / w) ** 2) for c, w in [(3.0, 0.8), (3.3, 0.9), (12.0, 0.5)]]
    cols.append(1e4 / ((x - 7.0) ** 2 + 1e-4))
    cols.append(np.ones_like(x))
    phi = np.column_stack(cols)
    lam = phi @ np.array([40.0, 25.0, 0.0, 1e-3, 1.5])
    y = np.random.default_rng(7).poisson(lam).astype(float)
    y[np.abs(x - 12.0) < 2.0] = 0.0          # zero counts where the third peak would sit
    return phi, y


def test_poisson_nnls_is_the_bounded_minimum_of_the_poisson_cost():
    from scipy.optimize import minimize

    from isar.analyze import backends
    from isar.analyze.estimators import baker_cousins_cost

    phi, y = _poisson_problem()
    lo, hi = np.zeros(5), np.full(5, 1e6)
    hi[4] = 0.5                               # the background wants more than its ceiling
    a = backends.poisson_nnls(phi, y, np.ones(5), lo, hi)
    again = backends.poisson_nnls(phi, y, np.array([100.0, 0.0, 5.0, 0.1, 0.5]), lo, hi)
    np.testing.assert_allclose(again, a, rtol=1e-6, atol=1e-9)    # one minimum, from any start
    assert a[2] == 0.0 and a[4] == 0.5        # on their bounds, exactly
    mu = phi @ a
    g = phi.T @ (1.0 - y / mu)                # KKT of the Poisson cost
    free = (a > lo) & (a < hi)
    scale = np.abs(phi).sum(axis=0)
    assert np.all(np.abs(g[free]) <= 1e-6 * scale[free])
    assert g[2] > 0 and g[4] < 0
    cost = baker_cousins_cost(y, mu)
    rng = np.random.default_rng(1)
    for start in [np.ones(5), rng.uniform(0, 50, 5), rng.uniform(0, 1, 5)]:
        start[3] *= 1e-3
        other = minimize(lambda b: baker_cousins_cost(y, phi @ b), start, method="L-BFGS-B",
                         bounds=list(zip(lo, hi)))
        assert cost <= other.fun + 1e-6


def _spectrum(modes, *, seed, x=None, bg=3.0, elastic=200.0, curve=None, exposure=None, T=150.0,
              constraints=None):
    x = np.arange(-12.0, 16.0001, 0.25) if x is None else x
    lam = bg + L.convolute_voigt_dho(x, 1.0, 0.0, 1.3, 0.2, 0.1, 0.0, elastic, 0.0, T,
                                     peak="elastic", resolution_curve=curve)
    for w, g, a in modes:
        lam = lam + L.convolute_voigt_dho(x, w, 0.0, 1.3, 0.2, g, a, 0.0, 0.0, T,
                                          resolution_curve=curve)
    if exposure is not None:
        lam = lam * exposure
    counts = np.random.default_rng(seed).poisson(lam).astype(float)
    kw = {} if exposure is None else {"exposure_scale": exposure, "exposure_reference": 1.0e6,
                                      "exposure_source": "monitor"}
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=T, center=0.0,
                       gauss_fwhm=1.3, lorentz_fwhm=0.2, instrument="BL43LXU",
                       energy_status="needs_zero", resolution_curve=curve, constraints=constraints,
                       **kw)


def _fixture(name):
    from isar.schema.steering import ParamConstraints

    one, two = [(5.0, 0.4, 80.0)], [(4.0, 0.4, 80.0), (9.0, 0.8, 50.0)]
    if name == "one_mode":
        return _spectrum(one, seed=0), "elastic+stokes+bg"
    if name == "two_modes":
        return _spectrum(two, seed=1), "elastic+stokes2+bg"
    if name == "tabulated":
        return _spectrum(one, seed=2, curve=_curve()), "elastic+stokes+bg"
    if name == "exposure":
        return (_spectrum(one, seed=3, exposure=np.linspace(0.6, 1.4, 113)), "elastic+stokes+bg")
    if name == "zero_counts":   # a weak spectrum, most points zero
        ctx = _spectrum([(5.0, 0.4, 1.0)], seed=4, bg=0.01, elastic=3.0)
        assert np.mean(ctx.counts == 0) > 0.5
        return ctx, "elastic+stokes+bg"
    if name == "soft_amplitude":   # a soft prior keeps h out of the profile: an offset column
        return (_spectrum(one, seed=5, constraints=ParamConstraints(soft={"phonon.h": (100.0, 10.0)})),
                "elastic+stokes+bg")
    if name == "hard_background":   # a hard-fixed amplitude is an offset too
        return (_spectrum(one, seed=6, constraints=ParamConstraints(hard={"bg.BG": 3.0})),
                "elastic+stokes+bg")
    raise KeyError(name)


def _hypothesis(ctx, label):
    from isar.analyze.loop import without_component
    from isar.analyze.phonon import enumerate_phonon_multimode, enumerate_phonon_structures

    # BL43LXU enumerates only elastic + central QE structures; these fits are of the reduced models
    hyps = [without_component(h, "qe") for h in enumerate_phonon_structures(ctx)
            + enumerate_phonon_multimode(ctx, 2)]
    hyps += [without_component(h, "elastic") for h in hyps if h is not None]
    return {h.label: h for h in hyps if h is not None}[label]


def test_the_optimizer_sees_only_the_nonlinear_parameters(monkeypatch):
    """Red on the pre-change code, whose MIGRAD carried every amplitude."""
    from iminuit import Minuit

    from isar.analyze import loop

    seen = []
    migrad = Minuit.migrad

    def spy(self, *a, **k):
        seen.append([p for p in self.parameters if not self.fixed[p]])
        return migrad(self, *a, **k)

    monkeypatch.setattr(Minuit, "migrad", spy)
    ctx, label = _fixture("two_modes")
    loop.fit_hypothesis(_hypothesis(ctx, label), ctx)
    assert seen == [["phonon_omega0", "phonon_Gamma", "phonon2_omega0", "phonon2_Gamma"]]


@pytest.mark.parametrize("name", ["one_mode", "two_modes", "tabulated", "exposure", "zero_counts",
                                  "soft_amplitude", "hard_background"])
def test_a_profiled_fit_reaches_the_free_fit_optimum_and_evidence(name):
    """The profiled fit against the old free fit of every parameter, from the same start: the same
    optimum to MIGRAD's tolerance (its EDM < 2e-4 bounds each fit's cost above the minimum by about
    that), the same errors and the same Laplace evidence to HESSE's own precision (the free fit's
    HESSE is about 1% off a finite-difference Hessian in a sigma)."""
    from isar.analyze import evidence as E
    from isar.analyze import loop

    from isar.analyze.phonon import greedy_multimode_ic

    ctx, label = _fixture(name)
    hyp = _hypothesis(ctx, label)
    cctx = loop.candidate_context(hyp, ctx)
    x0 = None
    if label == "elastic+stokes2+bg":   # the start production gives a two-mode fit: the residual seed
        one = loop.fit_hypothesis(_hypothesis(ctx, "elastic+stokes+bg"), ctx)
        x0 = greedy_multimode_ic(hyp.build(), cctx, one)
    prob = loop.build_problem(hyp, cctx, x0=x0)
    assert prob.profile is not None
    be = [loop._sanitize(n) for n in prob.free_names]
    fits = []
    for br in (loop._minimize_fixed("iminuit", prob.objective, prob.x0, prob.bounds, be,
                                    prob.fixed_pos, prob.fixed_vals),
               loop._minimize_profiled(prob)):
        fit = loop.result_from_backend(label, prob.composite, prob.costv, prob.n_used, prob.k, br,
                                       penalty=prob.penalty, hard_fixed=prob.is_hard_fixed, T=ctx.T)
        assert fit.optimizer_converged and fit.covariance_ok
        fits.append(fit)
    free, prof = fits
    assert abs(prof.cost - free.cost) < 2e-3
    # Modes compared in canonical (ascending omega0) order: label order is arbitrary.
    for mf, mp in zip(free.modes, prof.modes):
        for key in ("omega0", "gamma", "amplitude"):
            err = mf[f"{key}_err"] if key != "amplitude" else None
            tol = 0.05 * (err if err else abs(mf[key]) * 0.05)
            assert abs(mp[key] - mf[key]) <= tol, (key, mp[key], mf[key])
    for n in ("elastic.h", "bg.BG"):
        if free.param_errors.get(n):
            assert abs(prof.params[n] - free.params[n]) <= 0.05 * free.param_errors[n]
            assert prof.param_errors[n] == pytest.approx(free.param_errors[n], rel=0.03)
    bundle = E.candidate_bundle(hyp, ctx)
    ln_z = [E.structure_evidence(hyp, ctx, f, bundle, escalation="defer") for f in (free, prof)]
    assert ln_z[0].evidence_method == ln_z[1].evidence_method == "laplace"
    assert ln_z[1].log_evidence == pytest.approx(ln_z[0].log_evidence, abs=0.1)


def test_a_start_on_a_flat_profile_falls_back_to_the_free_fit():
    """Pure elastic data, and a start (a railed parent's, as the degenerate-limit twins get) whose
    amplitude is on zero: the profile is exactly flat along the mode, so MIGRAD on it has no
    curvature and fails (or steps to NaN). The free fit takes over from the same start and converges
    with the amplitude railed, as before profiling. Red without the fallback."""
    from isar.analyze import loop

    ctx = _spectrum([], seed=8, bg=20.0, elastic=900.0)
    start = {"phonon.omega0": 5.0, "phonon.Gamma": 0.5, "phonon.h": 0.0, "bg.BG": 20.0}
    fit = loop.fit_hypothesis(_hypothesis(ctx, "stokes+bg"), ctx, x0=start)
    assert fit.optimizer_converged
    assert all(np.isfinite(v) for v in fit.params.values())
