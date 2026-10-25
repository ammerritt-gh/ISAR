"""Component-model layer: each lineshape is a Component; a CompositeModel sums them.

The load-bearing test is `test_composite_equals_convolute_voigt_dho_all` — the composite
of [PhononDHO, Elastic, ConstantBackground] must be numerically identical to the production
lineshape `convolute_voigt_dho(peak="all")` once the fitted heights are turned into the
lineshape's intrinsic amplitude, proving the generalization is faithful.
"""
from types import SimpleNamespace

import numpy as np

from isar.analyze import components as C
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import KB, Voigt, convolute_voigt_dho, voigt_area_for_height


def _ctx(gauss_fwhm=1.2, lorentz_fwhm=0.3, center=-0.5, T=200.0, x=None, counts=None,
         quasielastic_fwhm=1.5):
    """Duck-typed stand-in for ScanContext (only the fields components read); ``x_used`` is the
    window the phonon's unit peak is read on."""
    return SimpleNamespace(gauss_fwhm=gauss_fwhm, lorentz_fwhm=lorentz_fwhm,
                           center=center, T=T, x=x, x_used=x, counts=counts,
                           quasielastic_fwhm=quasielastic_fwhm)


def test_constant_background_is_flat():
    x = np.linspace(-10, 10, 41)
    out = C.ConstantBackground().evaluate(x, {"BG": 7.5}, _ctx())
    assert np.allclose(out, 7.5)
    assert out.shape == x.shape


def test_elastic_height_is_the_peak_of_the_voigt():
    x = np.linspace(-10, 10, 81)
    ctx = _ctx(gauss_fwhm=1.1, lorentz_fwhm=0.2, center=-0.3)
    out = C.Elastic().evaluate(x, {"h": 42.0}, ctx)
    area = 42.0 * voigt_area_for_height(1.0, ctx.gauss_fwhm, ctx.lorentz_fwhm)
    assert np.allclose(out, Voigt(x, area, ctx.gauss_fwhm, ctx.lorentz_fwhm, ctx.center))
    at_center = C.Elastic().evaluate(np.array([ctx.center]), {"h": 42.0}, ctx)[0]
    assert np.isclose(at_center, 42.0) and np.isclose(out.max(), 42.0, rtol=2e-2)


def _phonon_amplitude(local, ctx):
    """The lineshape's intrinsic A behind a fitted height (the library's own bookkeeping)."""
    return C.PhononDHO().intrinsic_amplitude(local, ctx)


def test_phonon_matches_stokes_astokes_convolution():
    x = np.linspace(-10, 10, 81)
    ctx = _ctx(x=x)
    local = {"omega0": 4.0, "Gamma": 0.5, "h": 300.0}
    out = C.PhononDHO().evaluate(x, local, ctx)
    expected = convolute_voigt_dho(x, 4.0, ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                                   0.5, _phonon_amplitude(local, ctx), 0.0, 0.0, ctx.T,
                                   peak="stokes+astokes")
    assert np.allclose(out, expected)


def test_phonon_tied_antistokes_grows_with_T():
    """The Bose-tied anti-Stokes (gain side) is ~0 as T->0 and grows with T — the
    step-1 form of the §2 detailed-balance guard (no free anti-Stokes parameter)."""
    x = np.linspace(-12, 12, 241)
    local = {"omega0": 4.0, "Gamma": 0.4, "h": 500.0}
    cold = C.PhononDHO().evaluate(x, local, _ctx(center=0.0, T=5.0, x=x))
    hot = C.PhononDHO().evaluate(x, local, _ctx(center=0.0, T=300.0, x=x))
    gain = x < -1.0  # anti-Stokes (energy-gain) side
    assert hot[gain].sum() > 10.0 * cold[gain].sum()
    assert cold[gain].sum() >= 0.0


def test_quasielastic_is_broad_voigt_at_center():
    """A quasi-elastic Lorentzian of width ctx.quasielastic_fwhm convolved with the Gaussian
    resolution — a Voigt with the QE width as its Lorentzian part, at ctx.center."""
    x = np.linspace(-10, 10, 81)
    ctx = _ctx(gauss_fwhm=0.6, lorentz_fwhm=0.0, center=0.0, quasielastic_fwhm=1.5)
    out = C.QuasiElastic().evaluate(x, {"QE": 12.0}, ctx)
    expected = Voigt(x, 12.0, ctx.gauss_fwhm, ctx.quasielastic_fwhm, ctx.center)
    assert np.allclose(out, expected)


def test_quasielastic_param_is_a_single_linear_amplitude():
    qe = C.QuasiElastic()
    assert qe.prefix == "qe"
    assert qe.param_names() == ["QE"]
    assert qe.linear_params() == ["QE"]
    assert qe.nonlinear_params() == []


def test_quasielastic_unit_column_is_unit_amplitude_response():
    x = np.linspace(-8, 8, 65)
    ctx = _ctx(gauss_fwhm=0.6, lorentz_fwhm=0.0, center=0.5, quasielastic_fwhm=1.8)
    unit = C.QuasiElastic().unit_columns(x, {}, ctx)
    assert set(unit) == {"QE"}
    assert np.allclose(unit["QE"], Voigt(x, 1.0, ctx.gauss_fwhm, ctx.quasielastic_fwhm, ctx.center))


def test_quasielastic_has_in_window_leverage_where_sharp_elastic_has_none():
    """The reason QE is admissible on IN8 (EN>=1) where the resolution-limited elastic is not:
    2 meV off centre, the broad QE column still carries amplitude; the sharp elastic is ~0."""
    x = np.array([2.0])
    ctx = _ctx(gauss_fwhm=0.6, lorentz_fwhm=0.0, center=0.0, quasielastic_fwhm=1.5)
    qe = C.QuasiElastic().evaluate(x, {"QE": 1.0}, ctx)[0]
    el = C.Elastic().evaluate(x, {"h": 1.0}, ctx)[0]
    assert qe > 1000.0 * el


def test_composite_equals_convolute_voigt_dho_all():
    x = np.linspace(-10, 10, 81)
    ctx = _ctx(gauss_fwhm=1.2, lorentz_fwhm=0.3, center=-0.5, T=200.0, x=x)
    comp = C.CompositeModel([C.PhononDHO(), C.Elastic(), C.ConstantBackground()])
    theta = np.array([4.0, 0.5, 300.0, 50.0, 10.0])  # (omega0, Gamma, h, elastic.h, BG)
    out = comp.evaluate(x, theta, ctx)
    # The phonon's height is turned into the lineshape's intrinsic A; the elastic height into
    # the Voigt area that has that peak value; background is occupation-free.
    expected = convolute_voigt_dho(
        x, theta[0], ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm, theta[1],
        _phonon_amplitude({"omega0": theta[0], "Gamma": theta[1], "h": theta[2]}, ctx),
        theta[3] * voigt_area_for_height(1.0, ctx.gauss_fwhm, ctx.lorentz_fwhm), theta[4], ctx.T,
        peak="all")
    assert np.allclose(out, expected)


def test_composite_param_names_and_kinds():
    comp = C.CompositeModel([C.PhononDHO(), C.Elastic(), C.ConstantBackground()])
    assert comp.param_names() == ["phonon.omega0", "phonon.Gamma", "phonon.h",
                                  "elastic.h", "bg.BG"]
    assert comp.nonlinear_names() == ["phonon.omega0", "phonon.Gamma"]
    assert comp.linear_names() == ["phonon.h", "elastic.h", "bg.BG"]


def test_composite_param_names_drop_elastic():
    comp = C.CompositeModel([C.PhononDHO(), C.ConstantBackground()])
    assert comp.param_names() == ["phonon.omega0", "phonon.Gamma", "phonon.h", "bg.BG"]
    assert comp.linear_names() == ["phonon.h", "bg.BG"]


def test_composite_nnls_basis_matches_initial_guess_columns():
    x = np.linspace(-9.99, 9.99, 41)
    ctx = _ctx(gauss_fwhm=1.0, lorentz_fwhm=0.0, center=0.0, T=100.0, x=x)
    comp = C.CompositeModel([C.PhononDHO(), C.Elastic(), C.ConstantBackground()])
    e0, g0 = 4.5, 0.3
    phi, names = comp.nnls_basis(x, {"phonon.omega0": e0, "phonon.Gamma": g0}, ctx)
    assert names == ["phonon.h", "elastic.h", "bg.BG"]
    assert phi.shape == (x.size, 3)
    # The phonon's unit column is the unit-HEIGHT response: the lineshape at A = 1 over its height.
    from isar.analyze import lineshapes as L
    conv = L.resolution_convolver(x, ctx.gauss_fwhm, ctx.lorentz_fwhm)
    height = L.unit_peak(conv, e0, g0, ctx.T)[1].height
    ph = convolute_voigt_dho(x, e0, ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                             g0, 1.0, 0.0, 0.0, ctx.T, peak="stokes+astokes") / height
    el = Voigt(x, voigt_area_for_height(1.0, ctx.gauss_fwhm, ctx.lorentz_fwhm), ctx.gauss_fwhm,
               ctx.lorentz_fwhm, ctx.center)
    assert np.allclose(phi[:, 0], ph)
    assert np.allclose(phi[:, 1], el)
    assert np.allclose(phi[:, 2], np.ones_like(x))


def test_composite_omega0_and_error_are_the_fitted_energy():
    comp = C.CompositeModel([C.PhononDHO(), C.Elastic(), C.ConstantBackground()])
    theta = np.array([4.0, 0.5, 300.0, 50.0, 10.0])
    cov = np.zeros((5, 5))
    cov[0, 0], cov[1, 1], cov[0, 1] = 0.01, 0.04, -0.005
    cov[1, 0] = cov[0, 1]
    omega0, err = comp.omega0(theta, cov)
    assert omega0 == 4.0
    assert np.isclose(err, 0.1)                 # E0 is fitted directly: sigma = sqrt(cov[0, 0])


def test_composite_omega0_error_nan_without_covariance():
    comp = C.CompositeModel([C.PhononDHO(), C.ConstantBackground()])
    theta = np.array([4.0, 0.5, 300.0, 10.0])
    omega0, err = comp.omega0(theta, None)
    assert omega0 == 4.0
    assert np.isnan(err)


def test_instance_prefix_override_namespaces_two_phonons():
    """Two PhononDHO instances with distinct prefixes share no parameter names (the N-mode
    namespace) — and the default class-instantiated PhononDHO still carries ``phonon``."""
    assert C.PhononDHO().prefix == "phonon"                 # class default unchanged
    p1, p2 = C.PhononDHO(prefix="phonon1"), C.PhononDHO(prefix="phonon2")
    assert p1.prefix == "phonon1" and p2.prefix == "phonon2"
    assert C.PhononDHO().prefix == "phonon"                 # instance override did not mutate class
    comp = C.CompositeModel([p1, p2, C.ConstantBackground()])
    assert comp.param_names() == ["phonon1.omega0", "phonon1.Gamma", "phonon1.h",
                                  "phonon2.omega0", "phonon2.Gamma", "phonon2.h", "bg.BG"]
    assert len(set(comp.param_names())) == 7               # no collision


def test_phonon_indices_and_omega0_all_sorted():
    comp = C.CompositeModel([C.PhononDHO(prefix="phonon1"), C.PhononDHO(prefix="phonon2"),
                             C.ConstantBackground()])
    # phonon1 is the HIGH-E0 mode; phonon2 the low one — omega0_all must canonicalize ascending.
    theta = np.array([9.0, 0.3, 100.0,   3.0, 0.3, 400.0,   10.0])
    assert comp.phonon_indices() == [0, 3]
    w = comp.omega0_all(theta, None)
    assert [o for o, _ in w] == [3.0, 9.0]                  # low mode first


def test_primary_phonon_index_height_then_prior():
    comp = C.CompositeModel([C.PhononDHO(prefix="phonon1"), C.PhononDHO(prefix="phonon2"),
                             C.ConstantBackground()])
    theta = np.array([9.0, 0.3, 100.0,   3.0, 0.3, 400.0,   10.0])
    # no prior → largest height (phonon2, h=400 at index 3)
    assert comp.primary_phonon_index(theta, None) == 3
    o_primary, _ = comp.omega0(theta, None)
    assert o_primary == 3.0
    # prior near the high mode → nearest-to-prior wins (phonon1 at index 0)
    assert comp.primary_phonon_index(theta, None, prior_omega=9.0) == 0
    o_prior, _ = comp.omega0(theta, None, prior_omega=9.0)
    assert o_prior == 9.0


def test_composite_evaluate_components_sum_to_total():
    x = np.linspace(-10, 10, 81)
    ctx = _ctx(x=x)
    comp = C.CompositeModel([C.PhononDHO(), C.Elastic(), C.ConstantBackground()])
    theta = np.array([4.0, 0.5, 300.0, 50.0, 10.0])
    parts = comp.evaluate_components(x, theta, ctx)
    assert [label for label, _ in parts] == ["phonon", "elastic", "bg"]
    total = np.sum([arr for _, arr in parts], axis=0)
    assert np.allclose(total, comp.evaluate(x, theta, ctx))


def test_exposure_scales_composite_mean_components_and_nnls_only():
    x = np.linspace(-4.0, 8.0, 25)
    mask = np.ones(x.size, dtype=bool)
    scale = np.linspace(0.5, 2.0, x.size)
    legacy = ScanContext(x=x, counts=np.zeros_like(x), mask=mask, T=100.0, center=0.0,
                         gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="synthetic",
                         energy_status="absolute")
    exposed = ScanContext(x=x, counts=np.zeros_like(x), mask=mask, T=100.0, center=0.0,
                          gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="synthetic",
                          energy_status="absolute", exposure_scale=scale)
    comp = C.CompositeModel([C.PhononDHO(), C.ConstantBackground()])
    theta = np.array([4.0, 0.4, 200.0, 7.0])
    base = comp.evaluate(x, theta, legacy)
    assert np.array_equal(comp.evaluate(x, theta, legacy), base)
    assert np.allclose(comp.evaluate(x, theta, exposed), base * scale)
    parts = comp.evaluate_components(x, theta, exposed)
    assert np.allclose(sum(values for _name, values in parts), base * scale)
    nonlinear = {"phonon.omega0": 4.0, "phonon.Gamma": 0.4}
    phi0, names0 = comp.nnls_basis(x, nonlinear, legacy)
    phi1, names1 = comp.nnls_basis(x, nonlinear, exposed)
    assert names0 == names1
    assert np.allclose(phi1, phi0 * scale[:, None])


def test_uniform_exposure_is_regression_identical():
    x = np.linspace(-4.0, 8.0, 25)
    mask = np.ones(x.size, dtype=bool)
    base_ctx = ScanContext(x=x, counts=np.zeros_like(x), mask=mask, T=100.0, center=0.0,
                           gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="synthetic",
                           energy_status="absolute")
    uniform_ctx = ScanContext(x=x, counts=np.zeros_like(x), mask=mask, T=100.0, center=0.0,
                              gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="synthetic",
                              energy_status="absolute", exposure_scale=np.ones_like(x))
    comp = C.CompositeModel([C.PhononDHO(), C.ConstantBackground()])
    theta = np.array([4.0, 0.4, 200.0, 7.0])
    assert np.array_equal(comp.evaluate(x, theta, base_ctx),
                          comp.evaluate(x, theta, uniform_ctx))


# --- the intrinsic amplitude behind a height, and the Bose tie --------------------------------

def test_phonon_height_is_the_intrinsic_amplitude_times_the_unit_peak_height():
    """evaluate(h) == the lineshape at A = h / UnitPeak.height — the height coordinate's relation."""
    x = np.linspace(-10.0, 10.0, 161)
    ctx = _ctx(center=0.0, T=150.0, x=x)
    local = {"omega0": 3.0, "Gamma": 0.5, "h": 40.0}
    comp = C.PhononDHO()
    a = comp.intrinsic_amplitude(local, ctx)
    assert np.isclose(a * comp.unit_peak(local, ctx).height, 40.0, rtol=1e-12)
    expected = convolute_voigt_dho(x, 3.0, 0.0, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                                   0.5, a, 0.0, 0.0, ctx.T, peak="stokes+astokes")
    assert np.allclose(comp.evaluate(x, local, ctx), expected)


def test_bose_occupation_limits():
    from isar.analyze.phonon import bose_occupation, KB

    assert bose_occupation(5.0, 0.0) == 1.0                     # T=0: no occupation
    assert bose_occupation(5.0, -10.0) == 1.0                   # defensive
    assert abs(bose_occupation(50.0, 10.0) - 1.0) < 1e-20       # Bose-dead limit
    # classical limit: (n+1) ≈ kT/ω + 1/2
    w, T = 0.05, 300.0
    assert abs(bose_occupation(w, T) - (KB * T / w + 0.5)) < 0.01


def test_same_intrinsic_amplitude_gives_the_occupation_ratio_in_height():
    """One intrinsic A produces a narrow Stokes peak scaled by the occupation ratio — the decoupling
    that makes amplitude transport physical (H4). Narrow (Γ = 0.05) so the Bose factor is flat
    across the line to 3%."""
    x = np.linspace(-10.0, 10.0, 401)
    e0, g = 4.0, 0.05
    heights = {}
    for T in (30.0, 600.0):
        ctx = _ctx(center=0.0, gauss_fwhm=0.6, lorentz_fwhm=0.0, T=T, x=x)
        heights[T] = C.PhononDHO(peak="stokes").unit_peak({"omega0": e0, "Gamma": g}, ctx).height
    ratio = heights[600.0] / heights[30.0]
    n1 = lambda T: 1.0 / (1.0 - np.exp(-e0 / (KB * T)))
    assert abs(ratio / (n1(600.0) / n1(30.0)) - 1.0) < 0.03


def test_detailed_balance_ratio_survives_reparameterization():
    """The anti-Stokes/Stokes ratio stays exp(−E0/kT) — the tie is untouched by the height."""
    x = np.linspace(-12.0, 12.0, 961)
    ctx = _ctx(center=0.0, gauss_fwhm=0.6, lorentz_fwhm=0.0, T=250.0, x=x)
    y = C.PhononDHO().evaluate(x, {"omega0": 5.0, "Gamma": 0.3, "h": 80.0}, ctx)
    stokes = float(np.max(y[x > 1.0]))
    astokes = float(np.max(y[x < -1.0]))
    assert abs(astokes / stokes - np.exp(-5.0 / (KB * ctx.T))) < 0.02


def test_primary_phonon_index_is_the_largest_height_whatever_the_temperature():
    """The fitted h is already the measured strength, so there is no Bose weighting: the strongest
    mode is the largest h with or without T."""
    comp = C.CompositeModel([C.PhononDHO(), C.PhononDHO(prefix="phonon2"),
                             C.ConstantBackground()])
    theta = np.array([1.0, 0.1, 60.0, 8.0, 0.1, 100.0, 5.0])
    assert comp.primary_phonon_index(theta) == 3
    assert comp.primary_phonon_index(theta, T=300.0) == 3
