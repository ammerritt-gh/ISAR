"""The full DHO lineshape (McGuinness et al. 2026 Eq. 2) against independent references.

Reference values are ``scipy.integrate.quad`` integrals of the formula
``s(E) = A (E0/pi) 4 Gamma y(E) / ((E^2 - E0^2)^2 + 4 Gamma^2 E^2)``, ``y = E/(-expm1(-E/kT))``
(``d1_dho_examples.py``, 2026-10-25), kT = 0.086173324 T. The formula is rewritten here from the
paper, not imported, so a mistake in the library cannot agree with itself.

Required agreements (tolerances are requirements, not fits to the output):
- grid sums against the exact integrals: 2e-3 relative (the grid sum's own error at dx = 0.05 is
  below 1e-3; the Poisson sigma of the best-measured real bin is 3.3% of its counts);
- pointwise values on a grid node: 1e-8 relative (the reference is printed to ten digits).

The narrow-pole checks (steps 2-5, 7) name the rendering that keeps them true: the area-preserving
correction of ``lineshapes.NARROW_GAMMA_STEPS`` on top of the formula's point samples. Point
samples alone alias (a Gamma = 1e-3 meV mode's area moved by a factor 250 between grid nodes), and
a column that switches branch at a tiny Gamma locks the fast pass's optimiser.
"""
import math

import numpy as np
import pytest
from scipy.integrate import quad

from isar.analyze import lineshapes as L
from isar.analyze.components import Elastic, PhononDHO, phonon_unit_columns
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import KB, bose_numerator, dho_signal
from isar.analyze.phonon import GAMMA_FLOOR
from isar.synth import bl43_curves

DX = L.CONVOLUTION_DX
NARROW = ("the area-preserving narrow-pole rendering (lineshapes.NARROW_GAMMA_STEPS) is what keeps "
          "this true; point samples of the formula alone alias")


def _y(E, T):
    if T is None or not T > 0:
        return max(E, 0.0)
    kT = KB * T
    return kT if E == 0.0 else E / (-math.expm1(-E / kT))


def _s(E, E0, G, T):
    """The formula, A = 1, one energy."""
    return (E0 / math.pi) * 4.0 * G * _y(E, T) / ((E * E - E0 * E0) ** 2 + 4.0 * G * G * E * E)


def _exact_area(E0, G, T, lo, hi):
    pts = sorted({p for p in (-E0, 0.0, E0) if lo < p < hi})
    edges = [lo] + pts + [hi]
    return sum(quad(lambda e: _s(e, E0, G, T), a, b, limit=4000, epsabs=0, epsrel=1e-10)[0]
               for a, b in zip(edges, edges[1:]))


def _grid(dx=DX, half=200.0):
    n = int(round(half / dx))
    return np.arange(-n, n + 1) * dx


def _sides(E0, G, T, dx=DX):
    grid = _grid(dx)
    return tuple(float(np.sum(dho_signal(grid, E0, G, 1.0, T, peak=p)) * dx)
                 for p in ("stokes", "astokes"))


def _n1(E, T):
    return 1.0 / (-math.expm1(-E / (KB * T)))


# --- bose_numerator ----------------------------------------------------------------------------
def test_bose_numerator_is_kT_at_zero_and_stable_in_both_tails():
    import warnings

    T = 300.0
    kT = KB * T
    with warnings.catch_warnings():
        warnings.simplefilter("error")   # no overflow warning from the deep anti-Stokes tail
        y = bose_numerator(np.array([-1e5, -50.0, -1e-12, 0.0, 1e-12, 50.0, 4000.0]), T)
    assert y[3] == kT
    assert y[0] == 0.0 and y[-1] == pytest.approx(4000.0, rel=1e-12)
    assert y[2] == pytest.approx(kT, rel=1e-9) and y[4] == pytest.approx(kT, rel=1e-9)
    assert y[1] == pytest.approx(-50.0 / (-math.expm1(50.0 / kT)), rel=1e-12)
    assert float(bose_numerator(7.0, T)) == pytest.approx(7.0 / (-math.expm1(-7.0 / kT)), rel=1e-14)


@pytest.mark.parametrize("T", [None, 0.0, -5.0, float("nan")])
def test_unknown_temperature_is_the_zero_temperature_limit(T):
    np.testing.assert_array_equal(bose_numerator(np.array([-3.0, 0.0, 2.5]), T), [0.0, 0.0, 2.5])


# --- worked examples 1-8 (C-dho) ---------------------------------------------------------------
def test_example_1_pointwise_detailed_balance_and_zero_energy():
    E0, G, T = 10.0, 0.5, 300.0
    od = math.sqrt(E0 * E0 - G * G)
    grid = np.arange(-400, 401) * (od / 200.0)       # +-od and 0 are nodes
    s = dho_signal(grid, E0, G, 1.0, T)
    at = {name: int(np.argmin(abs(grid - e))) for name, e in (("+", od), ("-", -od), ("0", 0.0))}
    assert s[at["+"]] == pytest.approx(1.987847473, rel=1e-8)
    assert s[at["-"]] == pytest.approx(1.350829565, rel=1e-8)
    assert s[at["-"]] / s[at["+"]] == pytest.approx(0.6795438701, rel=1e-8)
    assert s[at["-"]] / s[at["+"]] == pytest.approx(math.exp(-od / (KB * T)), rel=1e-12)
    assert s[at["0"]] == pytest.approx(0.01645789257, rel=1e-8)
    assert s[at["0"]] == pytest.approx((E0 / math.pi) * 4 * G * KB * T / E0 ** 4, rel=1e-12)


def test_example_2_a_finite_width_area_is_not_the_narrow_limit_value():
    stokes, anti = _sides(10.0, 0.5, 300.0)
    assert stokes == pytest.approx(3.101895209, rel=2e-3)
    assert anti == pytest.approx(2.132527007, rel=2e-3)
    assert abs(stokes - _n1(10.0, 300.0)) > 0.01          # n(E0)+1 = 3.117354394 is not it


@pytest.mark.parametrize("G, stokes_ref", [(0.3, 5.6682214), (0.1, 5.6802245), (0.03, 5.6846015),
                                           (0.01, 5.6858697), (0.001, 5.686443)])
def test_example_3_the_narrow_limit_is_n_plus_one_and_n(G, stokes_ref):
    stokes, anti = _sides(5.0, G, 300.0)
    assert stokes == pytest.approx(stokes_ref, rel=2e-3), NARROW
    if G <= 0.001:
        assert stokes == pytest.approx(_n1(5.0, 300.0), rel=2e-3), NARROW
        assert anti == pytest.approx(_n1(5.0, 300.0) - 1.0, rel=2e-3), NARROW


def test_example_4_overdamped_mode_is_representable():
    E0, G, T = 2.0, 5.0, 97.0
    grid = _grid()
    s = dho_signal(grid, E0, G, 1.0, T)
    assert s[int(np.argmin(abs(grid)))] == pytest.approx(6.651731581, rel=1e-8)
    stokes, anti = _sides(E0, G, T)
    assert stokes == pytest.approx(4.414622419, rel=2e-3)
    assert anti == pytest.approx(3.979297205, rel=2e-3)
    assert np.all(np.isfinite(s)) and np.all(s >= 0.0)


def test_example_5_continuous_through_critical_damping():
    grid = _grid()
    at = int(np.argmin(abs(grid - 1.0)))
    vals = [dho_signal(grid, 3.0, G, 1.0, 300.0)[at] for G in (3.0 - 1e-6, 3.0, 3.0 + 1e-6)]
    for got, want in zip(vals, (3.02008553484, 3.02008581671, 3.02008609859)):
        assert got == pytest.approx(want, rel=1e-8)


def test_example_6_unknown_temperature_is_the_zero_temperature_mode():
    E0, G = 6.0, 0.4
    grid = _grid()
    assert np.all(dho_signal(grid, E0, G, 1.0, None, peak="astokes") == 0.0)
    assert dho_signal(grid, E0, G, 1.0, None)[int(np.argmin(abs(grid + 3.0)))] == 0.0
    stokes, anti = _sides(E0, G, None)
    od = math.sqrt(E0 * E0 - G * G)
    closed = (E0 / math.pi) / od * (math.pi / 2 + math.atan((E0 * E0 - 2 * G * G) / (2 * G * od)))
    assert closed == pytest.approx(0.9596621381, rel=1e-9)
    assert stokes == pytest.approx(closed, rel=2e-3) and anti == 0.0


def test_example_7_classical_sum_rule():
    stokes, anti = _sides(1.0, 0.5, 3000.0)
    assert stokes + anti == pytest.approx(517.0405884, rel=2e-3)
    assert stokes + anti == pytest.approx(2 * KB * 3000.0 / 1.0, rel=2e-3)


def test_example_8_the_bose_factor_moves_the_maximum_off_the_damped_energy():
    E0, G, T = 10.3, 1.0, 302.0
    grid = np.arange(-3000, 3001) * 0.01
    s = dho_signal(grid, E0, G, 1.0, T)
    half = grid > 0
    i = int(np.argmax(np.where(half, s, -1.0)))
    assert s[i] == pytest.approx(0.98150561, rel=2e-3)
    assert grid[i] == pytest.approx(10.2115, abs=0.01)
    assert abs(grid[i] - math.sqrt(E0 * E0 - G * G)) > 0.03         # Ω_d = 10.2513


# --- numerical validation: the convolved model (design 2.1) ------------------------------------
CURVE = bl43_curves.curve()          # a06
X = np.arange(-16.0, 20.0001, 0.2)


def _model(E0, G, T=300.0, x=X, A=1.0):
    return L.convolute_voigt_dho(x, E0, 0.0, 0.0, 0.0, G, A, 0.0, 0.0, T, peak="stokes+astokes",
                                 resolution_curve=CURVE)


def _clear_convolver_caches():
    for f in (L._tabulated_convolver, L._tabulated_grid_and_kernel, L._voigt_convolver,
              L._grid_and_kernel):
        f.cache_clear()


@pytest.mark.parametrize("E0", [5.0, 0.3])
def test_step_2_gamma_to_zero_approaches_two_resolution_curves_at_plus_and_minus_E0(E0):
    """The reference is built from the curve alone: A [(n+1) R(x-E0) + n R(x+E0)]."""
    T = 300.0
    x = np.arange(-12.0, 14.0001, 0.05)
    ref = _n1(E0, T) * CURVE.profile(x - E0) + (_n1(E0, T) - 1.0) * CURVE.profile(x + E0)
    gammas = [0.1, 0.03, 0.01, GAMMA_FLOOR]
    err = [float(np.max(np.abs(_model(E0, g, T, x) - ref)) / ref.max()) for g in gammas]
    assert all(b < a for a, b in zip(err, err[1:])), (err, NARROW)
    # Required agreement 5e-3 of the peak, the sum of two measured parts (E0 = 5 on a node, a06):
    # the DHO at GAMMA_FLOOR is not a delta (1.9e-3, converged on a 0.0025 meV grid), and the
    # area-preserving average smooths a delta by a quadratic B-spline of the grid step, lowering
    # a06's peak by (R(dx)+R(-dx)-2R(0))/(8R(0)) = 2.7e-3, since a06's core is twice as sharp as a
    # Gaussian of its FWHM (R''/R = -8.7 against -4.4). Poisson sigma is >= 3.3%.
    assert err[-1] <= 5e-3, (err, NARROW)


@pytest.mark.parametrize("E0", [3.0, 0.3, 0.06])
def test_step_3_continuous_through_critical_damping(E0):
    lo, hi = (_model(E0, E0 * (1 + e)) for e in (-1e-6, 1e-6))
    assert np.max(np.abs(hi - lo)) <= 1e-3 * max(lo.max(), hi.max()), NARROW


def _row_area(E0, G, T=300.0, x=X):
    conv = L.resolution_convolver(x, 0.0, 0.0, CURVE)
    return L.unit_peak(conv, E0, G, T)[1].area_both


@pytest.mark.parametrize("G", [0.5, 2.0, 5.0])
def test_step_4_e0_to_zero_is_finite_with_the_exact_area_and_no_spike(G):
    span = 36.0     # the data window plus the pad: the grid the model integrates over
    for E0 in (2.0, 0.5, 0.1, 0.03, 0.01, 0.003):
        assert _row_area(E0, G) == pytest.approx(_exact_area(E0, G, 300.0, -span, span), rel=1e-3), \
            (E0, G, NARROW)
    sweep = np.arange(0.003, 0.2001, 1e-3)[::-1]          # 0.2 meV down to 0.003, 1e-3 meV steps
    cols = np.array([_model(E0, G) for E0 in sweep])
    assert np.all(np.isfinite(cols))
    cols = cols / cols.max(axis=1, keepdims=True)
    steps = np.max(np.abs(np.diff(cols, axis=0)), axis=1)
    assert steps.max() <= 10.0 * np.median(steps), (steps.max(), np.median(steps), NARROW)


@pytest.mark.parametrize("E0, G", [(6.0, 0.3), (6.0, 2.0), (6.0, 5.0), (6.0, 20.0), (0.5, 5.0)])
def test_step_5_halving_the_step_and_doubling_the_support_changes_nothing(E0, G, monkeypatch):
    base = _model(E0, G)
    monkeypatch.setattr(L, "CONVOLUTION_DX", DX / 2)
    monkeypatch.setattr(L, "CONVOLUTION_PAD", 2 * L.CONVOLUTION_PAD)
    _clear_convolver_caches()
    try:
        refined = _model(E0, G)
    finally:
        monkeypatch.undo()
        _clear_convolver_caches()
    assert np.max(np.abs(refined - base)) <= 1e-3 * base.max(), (E0, G, NARROW)


# --- smooth through Gamma = 0 (C-dho requirement; the fast pass takes Hessians there) ----------
XS = np.arange(-16.0, 20.0001, 0.2)
ETA = 2.5e-4


def _column(E0, G):
    """The PhononDHO unit column on XS (height 1), as the fast pass's optimiser sees it."""
    return PhononDHO().unit_columns(XS, {"omega0": E0, "Gamma": G}, _ctx(XS))["h"]


def _gamma_points():
    edges = [k * DX for k in L.NARROW_GAMMA_STEPS]
    pts = {0.0, 1e-8, 1e-6, 1e-5}
    pts.update(np.arange(2 * ETA, 3 * DX, ETA))
    pts.update(e + d for e in edges for d in (-1e-6, 1e-6))
    return sorted(pts)


@pytest.mark.parametrize("E0", [5.013, 0.317, 5.0])
def test_step_7a_the_column_is_smooth_in_gamma_through_zero(E0):
    pts = _gamma_points()
    D = np.array([(_column(E0, g + ETA) - _column(E0, g)) / ETA for g in pts])
    worst = float(np.max(np.abs(np.diff(D, axis=0))) / np.max(np.abs(D)))
    assert worst <= 1e-2, (worst, "a branch switch at tiny Gamma, or a missing " + NARROW)


def _curvature_agrees(column, E0):
    d = 1e-3

    def curvature(g):
        return (column(E0 + d, g) - 2.0 * column(E0, g) + column(E0 - d, g)) / d ** 2

    zero = curvature(0.0)
    return [float(np.max(np.abs(curvature(g) - zero)) / np.max(np.abs(zero))) for g in (1e-8, 1e-4)]


@pytest.mark.parametrize("E0", [5.013, 0.317, 5.0])
def test_step_7b_the_lineshape_curvature_does_not_depend_on_which_tiny_gamma(E0):
    assert max(_curvature_agrees(lambda e, g: _model(e, g, 300.0, XS), E0)) <= 1e-2, NARROW


@pytest.mark.parametrize("E0", [
    5.013, 0.317,
    5.0])
def test_step_7b_the_unit_column_curvature_does_not_depend_on_which_tiny_gamma(E0):
    assert max(_curvature_agrees(_column, E0)) <= 1e-2, NARROW


# --- the height coordinate (C-dho): worked examples 9-10 and validation step 6 -----------------
def _ctx(x, center=0.0, T=300.0):
    return ScanContext(x=x, counts=np.zeros_like(x), mask=np.ones_like(x, dtype=bool), T=T,
                       center=center, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="BL43LXU",
                       energy_status="needs_zero", resolution_curve=CURVE)


DENSE = np.arange(0.0, 60.0001, 0.01)
#: A column is the node row linearly interpolated at the data energies, so a dense sample of it
#: sits below the parabolic-vertex height by up to (R''/R)(dx/2)^2/2 = 2.7e-3 on a06 (R''/R = -8.7)
#: and never above it. Plan row 9 says 1e-4, which holds only for a Gaussian-sharp curve.
COLUMN_PEAK_SHORTFALL = 3e-3


def _peaks_at_one(col_max):
    return 1.0 - COLUMN_PEAK_SHORTFALL <= col_max <= 1.0 + 1e-9


@pytest.mark.parametrize("E0, G", [(6.0, 0.8), (0.4, 3.0), (11.0, 1e-3), (2.0, 5.0)])
def test_example_9_column_height_one_and_area_matches_the_trapezoid(E0, G):
    ctx = _ctx(DENSE)
    comp = PhononDHO()
    col = comp.unit_columns(DENSE, {"omega0": E0, "Gamma": G}, ctx)["h"]
    assert _peaks_at_one(col.max()), col.max()               # parabolic vertex vs dense sampling
    up = comp.unit_peak({"omega0": E0, "Gamma": G}, ctx)
    evaluated = comp.evaluate(DENSE, {"omega0": E0, "Gamma": G, "h": 1.0}, ctx)
    assert evaluated is not col and np.array_equal(evaluated, col)
    assert 1.0 * up.area_stokes / up.height == pytest.approx(np.trapezoid(evaluated, DENSE), rel=1e-3)
    assert up.area_both > up.area_stokes > 0.0


def test_example_10_the_elastic_column_peaks_at_one_at_the_centre():
    center = 0.2
    x = np.arange(-3.0, 3.0001, 0.001)
    col = Elastic().unit_columns(x, {}, _ctx(x, center=center))["h"]
    assert col.max() == pytest.approx(1.0, rel=1e-12)
    assert abs(x[int(np.argmax(col))] - center) < 0.1


def test_step_6_every_column_peaks_at_one_and_h_round_trips_through_the_intrinsic_amplitude():
    rng = np.random.default_rng(6)
    comp = PhononDHO()
    ctx = _ctx(DENSE)
    for _ in range(200):
        E0, G = float(rng.uniform(0.01, 15.0)), float(10 ** rng.uniform(-3, math.log10(8.0)))
        local = {"omega0": E0, "Gamma": G, "h": float(rng.uniform(0.5, 500.0))}
        col = comp.unit_columns(DENSE, local, ctx)["h"]
        assert _peaks_at_one(col.max()), (E0, G, col.max())
        a = comp.intrinsic_amplitude(local, ctx)
        assert a * comp.unit_peak(local, ctx).height == pytest.approx(local["h"], rel=1e-12)


def test_the_lineshape_at_the_intrinsic_amplitude_peaks_at_the_height():
    """h is the maximum of the convolved profile with the lineshape's A = h / UnitPeak.height."""
    ctx = _ctx(DENSE)
    comp = PhononDHO()
    local = {"omega0": 5.0, "Gamma": 0.4, "h": 123.0}
    a = comp.intrinsic_amplitude(local, ctx)
    y = L.convolute_voigt_dho(DENSE, 5.0, 0.0, 0.0, 0.0, 0.4, a, 0.0, 0.0, 300.0,
                              resolution_curve=CURVE)
    assert y.max() == pytest.approx(123.0, rel=1e-3)


def test_one_batched_convolution_serves_every_mode_and_a_stokes_only_mode_has_its_own_height():
    ctx = _ctx(DENSE)
    cols, ups = phonon_unit_columns(DENSE, ctx, [(3.0, 0.3), (7.0, 0.6), (0.0 + 0.2, 0.2)],
                                    ["stokes+astokes", "stokes", "stokes+astokes"])
    assert cols.shape == (3, DENSE.size) and len(ups) == 3
    assert all(_peaks_at_one(m) for m in cols.max(axis=1)), cols.max(axis=1)
    for i, (mode, peak) in enumerate([((3.0, 0.3), "stokes+astokes"), ((7.0, 0.6), "stokes")]):
        alone, _ = phonon_unit_columns(DENSE, ctx, [mode], [peak])
        np.testing.assert_allclose(cols[i], alone[0], rtol=0, atol=1e-12)
    # near-elastic mode: the whole profile's maximum is read over E >= 0, never a NaN column
    assert np.all(np.isfinite(cols[2])) and ups[2].height > 0.0
    empty, none = phonon_unit_columns(DENSE, ctx, [], [])
    assert empty.shape == (0, DENSE.size) and none == ()


def test_a_mode_with_no_positive_height_is_refused_by_name():
    conv = L.resolution_convolver(DENSE, 0.0, 0.0, CURVE)
    with pytest.raises(ValueError, match="E0=3.0.*Gamma=0.5"):
        L.unit_peak_of_nodes(conv, np.zeros(conv.x_out.size), 3.0, 0.5)


def test_an_optimizer_nan_step_reaches_the_cost_as_nan_not_an_exception():
    """MIGRAD can step to NaN; the profiled fit then fails over to the free fit on a NaN cost. A NaN
    E0 or Gamma is that step, not a lineshape failure, so its column is NaN (a finite mode's
    missing height still raises, above)."""
    ctx = _ctx(DENSE)
    for mode in ((float("nan"), 0.3), (3.0, float("nan"))):
        cols, ups = phonon_unit_columns(DENSE, ctx, [mode, (3.0, 0.3)], ["stokes+astokes"] * 2)
        assert np.all(np.isnan(cols[0])) and np.isnan(ups[0].height)
        assert np.all(np.isfinite(cols[1]))
