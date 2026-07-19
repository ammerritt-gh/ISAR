"""Component-model layer: each lineshape is a Component; a CompositeModel sums them.

The load-bearing test is `test_composite_equals_convolute_voigt_dho_all` — the composite
of [PhononDHO, Elastic, ConstantBackground] must be numerically identical to the frozen
production model `convolute_voigt_dho(peak="all")`, proving the generalization is faithful.
"""
from types import SimpleNamespace

import numpy as np

from isar.analyze import components as C
from isar.analyze.context import ScanContext
from isar.synth._ff import Voigt, convolute_voigt_dho
from isar.synth.fit import omega0_from, omega0_value_error


def _ctx(gauss_fwhm=1.2, lorentz_fwhm=0.3, center=-0.5, T=200.0, x=None, counts=None,
         quasielastic_fwhm=1.5):
    """Duck-typed stand-in for ScanContext (only the fields components read)."""
    return SimpleNamespace(gauss_fwhm=gauss_fwhm, lorentz_fwhm=lorentz_fwhm,
                           center=center, T=T, x=x, counts=counts,
                           quasielastic_fwhm=quasielastic_fwhm)


def test_constant_background_is_flat():
    x = np.linspace(-10, 10, 41)
    out = C.ConstantBackground().evaluate(x, {"BG": 7.5}, _ctx())
    assert np.allclose(out, 7.5)
    assert out.shape == x.shape


def test_elastic_matches_voigt():
    x = np.linspace(-10, 10, 81)
    ctx = _ctx(gauss_fwhm=1.1, lorentz_fwhm=0.2, center=-0.3)
    out = C.Elastic().evaluate(x, {"E": 42.0}, ctx)
    expected = Voigt(x, 42.0, ctx.gauss_fwhm, ctx.lorentz_fwhm, ctx.center)
    assert np.allclose(out, expected)


def test_phonon_matches_stokes_astokes_convolution():
    x = np.linspace(-10, 10, 81)
    ctx = _ctx()
    od, g, A = 4.0, 0.5, 300.0
    out = C.PhononDHO().evaluate(x, {"Omega_d": od, "Gamma": g, "A": A}, ctx)
    omega0 = omega0_from(od, g)
    expected = convolute_voigt_dho(x, omega0, ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                                   g, A, 0.0, 0.0, ctx.T, peak="stokes+astokes")
    assert np.allclose(out, expected)


def test_phonon_tied_antistokes_grows_with_T():
    """The Bose-tied anti-Stokes (gain side) is ~0 as T->0 and grows with T — the
    step-1 form of the §2 detailed-balance guard (no free anti-Stokes parameter)."""
    x = np.linspace(-12, 12, 241)
    od, g, A = 4.0, 0.4, 500.0
    cold = C.PhononDHO().evaluate(x, {"Omega_d": od, "Gamma": g, "A": A}, _ctx(center=0.0, T=5.0))
    hot = C.PhononDHO().evaluate(x, {"Omega_d": od, "Gamma": g, "A": A}, _ctx(center=0.0, T=300.0))
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
    el = C.Elastic().evaluate(x, {"E": 1.0}, ctx)[0]
    assert qe > 1000.0 * el


def test_composite_equals_convolute_voigt_dho_all():
    x = np.linspace(-10, 10, 81)
    ctx = _ctx(gauss_fwhm=1.2, lorentz_fwhm=0.3, center=-0.5, T=200.0)
    comp = C.CompositeModel([C.PhononDHO(), C.Elastic(), C.ConstantBackground()])
    theta = np.array([4.0, 0.5, 300.0, 50.0, 10.0])  # (Omega_d, Gamma, A, E, BG)
    out = comp.evaluate(x, theta, ctx)
    omega0 = omega0_from(theta[0], theta[1])
    expected = convolute_voigt_dho(x, omega0, ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                                   theta[1], theta[2], theta[3], theta[4], ctx.T, peak="all")
    assert np.allclose(out, expected)


def test_composite_param_names_and_kinds():
    comp = C.CompositeModel([C.PhononDHO(), C.Elastic(), C.ConstantBackground()])
    assert comp.param_names() == ["phonon.Omega_d", "phonon.Gamma", "phonon.A",
                                  "elastic.E", "bg.BG"]
    assert comp.nonlinear_names() == ["phonon.Omega_d", "phonon.Gamma"]
    assert comp.linear_names() == ["phonon.A", "elastic.E", "bg.BG"]


def test_composite_param_names_drop_elastic():
    comp = C.CompositeModel([C.PhononDHO(), C.ConstantBackground()])
    assert comp.param_names() == ["phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG"]
    assert comp.linear_names() == ["phonon.A", "bg.BG"]


def test_composite_nnls_basis_matches_initial_guess_columns():
    x = np.linspace(-9.99, 9.99, 41)
    ctx = _ctx(gauss_fwhm=1.0, lorentz_fwhm=0.0, center=0.0, T=100.0)
    comp = C.CompositeModel([C.PhononDHO(), C.Elastic(), C.ConstantBackground()])
    od, g0 = 4.5, 0.3
    phi, names = comp.nnls_basis(x, {"phonon.Omega_d": od, "phonon.Gamma": g0}, ctx)
    assert names == ["phonon.A", "elastic.E", "bg.BG"]
    assert phi.shape == (x.size, 3)
    omega0 = omega0_from(od, g0)
    ph = convolute_voigt_dho(x, omega0, ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                             g0, 1.0, 0.0, 0.0, ctx.T, peak="stokes+astokes")
    el = convolute_voigt_dho(x, omega0, ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm,
                             g0, 0.0, 1.0, 0.0, ctx.T, peak="elastic")
    assert np.allclose(phi[:, 0], ph)
    assert np.allclose(phi[:, 1], el)
    assert np.allclose(phi[:, 2], np.ones_like(x))


def test_composite_omega0_and_error_match_helper():
    comp = C.CompositeModel([C.PhononDHO(), C.Elastic(), C.ConstantBackground()])
    theta = np.array([4.0, 0.5, 300.0, 50.0, 10.0])
    cov = np.zeros((5, 5))
    cov[0, 0], cov[1, 1], cov[0, 1] = 0.01, 0.04, -0.005
    cov[1, 0] = cov[0, 1]
    omega0, err = comp.omega0(theta, cov)
    exp_o, exp_e = omega0_value_error(theta[0], theta[1], cov[:2, :2])
    assert np.isclose(omega0, exp_o)
    assert np.isclose(err, exp_e)


def test_composite_omega0_error_nan_without_covariance():
    comp = C.CompositeModel([C.PhononDHO(), C.ConstantBackground()])
    theta = np.array([4.0, 0.5, 300.0, 10.0])
    omega0, err = comp.omega0(theta, None)
    assert np.isclose(omega0, omega0_from(4.0, 0.5))
    assert np.isnan(err)


def test_instance_prefix_override_namespaces_two_phonons():
    """Two PhononDHO instances with distinct prefixes share no parameter names (the N-mode
    namespace) — and the default class-instantiated PhononDHO still carries ``phonon``."""
    assert C.PhononDHO().prefix == "phonon"                 # class default unchanged
    p1, p2 = C.PhononDHO(prefix="phonon1"), C.PhononDHO(prefix="phonon2")
    assert p1.prefix == "phonon1" and p2.prefix == "phonon2"
    assert C.PhononDHO().prefix == "phonon"                 # instance override did not mutate class
    comp = C.CompositeModel([p1, p2, C.ConstantBackground()])
    assert comp.param_names() == ["phonon1.Omega_d", "phonon1.Gamma", "phonon1.A",
                                  "phonon2.Omega_d", "phonon2.Gamma", "phonon2.A", "bg.BG"]
    assert len(set(comp.param_names())) == 7               # no collision


def test_phonon_indices_and_omega0_all_sorted():
    comp = C.CompositeModel([C.PhononDHO(prefix="phonon1"), C.PhononDHO(prefix="phonon2"),
                             C.ConstantBackground()])
    # phonon1 is the HIGH-ω₀ mode; phonon2 the low one — omega0_all must canonicalize ascending.
    theta = np.array([9.0, 0.3, 100.0,   3.0, 0.3, 400.0,   10.0])
    assert comp.phonon_indices() == [0, 3]
    w = comp.omega0_all(theta, None)
    assert [round(o, 3) for o, _ in w] == sorted(round(o, 3) for o, _ in w)
    assert np.isclose(w[0][0], omega0_from(3.0, 0.3))       # low mode first
    assert np.isclose(w[1][0], omega0_from(9.0, 0.3))


def test_primary_phonon_index_amplitude_then_prior():
    comp = C.CompositeModel([C.PhononDHO(prefix="phonon1"), C.PhononDHO(prefix="phonon2"),
                             C.ConstantBackground()])
    theta = np.array([9.0, 0.3, 100.0,   3.0, 0.3, 400.0,   10.0])
    # no prior → largest amplitude (phonon2, A=400 at index 3)
    assert comp.primary_phonon_index(theta, None) == 3
    o_primary, _ = comp.omega0(theta, None)
    assert np.isclose(o_primary, omega0_from(3.0, 0.3))
    # prior near the high mode → nearest-to-prior wins (phonon1 at index 0)
    assert comp.primary_phonon_index(theta, None, prior_omega=9.0) == 0
    o_prior, _ = comp.omega0(theta, None, prior_omega=9.0)
    assert np.isclose(o_prior, omega0_from(9.0, 0.3))


def test_composite_evaluate_components_sum_to_total():
    x = np.linspace(-10, 10, 81)
    ctx = _ctx()
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
    nonlinear = {"phonon.Omega_d": 4.0, "phonon.Gamma": 0.4}
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
