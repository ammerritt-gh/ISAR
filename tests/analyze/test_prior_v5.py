"""Prior recipe v5 (S1U3): every density is normalised, the k! normalisation of the ordered
energies is right in the Laplace path, and the box-rule Laplace evidence prices a Gaussian that
spreads beyond the prior's support (C-prior worked examples 1-8)."""
import math

import numpy as np
import pytest
from scipy.integrate import quad, trapezoid
from scipy.special import erf, gammaln, log_ndtr, logsumexp

from isar.analyze import prior
from isar.analyze.context import ScanContext
from isar.analyze.phonon import EPS_OMEGA0, GAMMA_FLOOR, GAMMA_MAX, GAMMA_MAX_V4
from isar.analyze.prior import PriorSettings, SENSITIVITY_ARMS, laplace_log_evidence, resolve_prior


# --------------------------------------------------------------------------- the densities
def test_every_arm_density_is_normalised_on_its_support():
    for name, s in SENSITIVITY_ARMS.items():
        if s.legacy_v4:
            continue
        sp = resolve_prior([0, 3, 250, 7], np.linspace(-16, 20, 50), s)
        zw = quad(lambda g: math.exp(sp.ln_width(g)), 0, s.width_cap, limit=200)[0]
        zh = quad(lambda h: math.exp(sp.ln_height(h)), 0, sp.height_hi)[0]
        zb = quad(lambda b: math.exp(sp.ln_background(b)), 0, sp.background_hi)[0]
        for z in (zw, zh, zb):
            assert z == pytest.approx(1.0, abs=1e-8), name


def test_legacy_arm_is_the_v4_recipe_in_the_height_coordinate():
    x = np.linspace(-10.0, 10.0, 81)
    ctx = ScanContext(x=x, counts=np.full(81, 10.0), mask=np.ones(81, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="absolute", exposure_scale=np.ones(81),
                      exposure_reference=3.0e5, exposure_source="monitor")
    sp = prior.resolve_prior_for(ctx, PriorSettings(legacy_v4=True))
    assert sp.height_hi == sp.background_hi == prior.AMPLITUDE_SCALE_PER_MONITOR * 3.0e5
    assert sp.bounds("Gamma") == (GAMMA_FLOOR, GAMMA_MAX_V4)
    z = quad(lambda g: math.exp(sp.ln_width(g)), GAMMA_FLOOR, GAMMA_MAX_V4)[0]
    assert z == pytest.approx(1.0, abs=1e-9)
    assert sp.ln_width(GAMMA_MAX_V4 + 1.0) == -math.inf
    assert sp.bounds("omega0") == (EPS_OMEGA0, 10.0)            # v4 never capped the window
    with pytest.raises(ValueError, match="v4_height_hi"):             # counts alone cannot give it
        resolve_prior([5.0], [1.0], PriorSettings(legacy_v4=True)).height_hi


# --------------------------------------------------------------------------- worked examples 1-4
def test_ex1_resolved_prior_values():
    sp = resolve_prior([0, 3, 250, 7], np.linspace(-16, 20, 50))
    assert sp.max_count == 250 and sp.height_hi == pytest.approx(300.0) and sp.background_hi == 250
    assert sp.ln_height(100) == pytest.approx(-math.log(300.0))
    assert sp.ln_width(0) == pytest.approx(0.4867, abs=1e-4)
    assert sp.ln_energies([5, 9, 14]) == pytest.approx(-7.1953, abs=1e-4)


def test_ex2_max_count_is_at_the_reference_exposure():
    sp = resolve_prior([10, 200], [1, 5], exposure_scale=[1, 0.8])
    assert sp.max_count == pytest.approx(250.0)
    with pytest.raises(ValueError, match="no finite counts"):
        resolve_prior([np.nan, np.nan], [1, 5])
    with pytest.raises(ValueError, match="no positive energy"):
        resolve_prior([1, 2], [-5.0, -1.0])


def test_ex3_margin_arms_differ_by_the_stated_nats():
    e = np.linspace(-16, 20, 50)
    counts = [0, 3, 250, 7]
    r, m3, m105 = (resolve_prior(counts, e, SENSITIVITY_ARMS[a]) for a in ("recipe", "margin_3",
                                                                          "margin_1.05"))
    assert m3.ln_height(100) - r.ln_height(100) == pytest.approx(-math.log(3 / 1.2))
    assert m105.ln_height(100) - m3.ln_height(100) == pytest.approx(math.log(3 / 1.05))
    assert math.log(3 / 1.05) == pytest.approx(1.050, abs=1e-3)       # design 2.4's figure


def test_ex4_a_delta_mode_carries_no_width_factor():
    sp = resolve_prior([0, 3, 250, 7], np.linspace(-16, 20, 50))
    assert sp.ln_prior([(9.0, None, 100.0)], None, None) == pytest.approx(
        sp.ln_energies([9.0]) + sp.ln_height(100.0))
    assert sp.ln_prior([(9.0, 0.5, 100.0)], None, None) == pytest.approx(
        sp.ln_energies([9.0]) + sp.ln_height(100.0) + sp.ln_width(0.5))


def test_width_cap_is_the_phonon_ceiling():
    assert PriorSettings().width_cap == GAMMA_MAX == 20.0


# --------------------------------------------------------------------------- example 8: the k!
def test_ex8_ordered_energy_prior_carries_k_factorial():
    # Trap: two peaks of fixed, different heights with only positions free have ONE label copy, so
    # the grid agrees with Laplace without k!; a k! test must use equal heights or free all of each
    # peak's parameters, or it cannot tell right from wrong.
    x = np.linspace(0, 20, 120)
    w_lo, w_hi = 1e-3, 20.0
    h_true, b = np.array([50.0, 50.0]), 2.0

    def model(e1, e2):
        return b + h_true[0] * np.exp(-0.5 * (x - e1) ** 2) + h_true[1] * np.exp(-0.5 * (x - e2) ** 2)

    counts = np.round(model(6.0, 12.0)).astype(float)       # noiseless-rounded, deterministic

    def ln_l(e1, e2):
        mu = model(e1, e2)
        return float(np.sum(counts * np.log(mu) - mu - gammaln(counts + 1)))

    sp = resolve_prior(counts, x, PriorSettings(energy_floor=w_lo, energy_cap=w_hi))
    width = sp.e_hi - sp.e_lo
    # brute force over the unordered square with flat 1/W^2 (both label copies included)
    g = np.linspace(w_lo, w_hi, 1601)
    d = g[1] - g[0]
    g2 = h_true[1] * np.exp(-0.5 * (x[None, :] - g[:, None]) ** 2)
    ll = np.empty((g.size, g.size))
    for i, e1 in enumerate(g):                                # row by row: memory
        mu = b + h_true[0] * np.exp(-0.5 * (x - e1) ** 2) + g2
        ll[i] = np.sum(counts * np.log(mu) - mu, axis=1)
    ll -= np.sum(gammaln(counts + 1))
    ln_z_grid = float(logsumexp(ll) + 2 * math.log(d) - 2 * math.log(width))
    from scipy.optimize import minimize
    nlp = lambda th: -(ln_l(th[0], th[1]) + sp.ln_energies(th))
    th = minimize(nlp, [6.05, 11.95], method="Nelder-Mead",
                  options={"xatol": 1e-10, "fatol": 1e-12}).x
    lr = laplace_log_evidence(nlp, th, [w_lo, w_lo], [w_hi, w_hi])
    ln_z_no_kfact = lr.ln_z - math.log(2)
    assert lr.status == "ok"
    assert lr.ln_z == pytest.approx(ln_z_grid, abs=0.02)             # -237.3786 vs -237.3789
    assert abs(ln_z_no_kfact - ln_z_grid) > 0.6                      # -238.0720: k! is not optional


# --------------------------------------------------------------------------- the box rule
_F1 = lambda th: 0.5 * ((th[0] + 0.3) / 0.5) ** 2 + 0.5 * (th[1] / 0.2) ** 2


def _exact_1pin():
    return math.log(quad(lambda a: math.exp(-0.5 * ((a + 0.3) / 0.5) ** 2), 0, 5)[0]) \
        + 0.5 * math.log(2 * math.pi * 0.04)


def test_ex5_one_pin_at_either_bound_matches_the_exact_integral():
    r = laplace_log_evidence(_F1, [0.0, 0.0], [0.0, -5], [5, 5])
    assert r.status == "ok_boundary" and r.boundary_method == "box"
    assert r.ln_z == pytest.approx(_exact_1pin(), abs=1e-3)           # -1.7584
    mirror = lambda th: _F1([-th[0], th[1]])
    rm = laplace_log_evidence(mirror, [0.0, 0.0], [-5, -5], [0.0, 5])
    assert rm.status == "ok_boundary"
    assert rm.ln_z == pytest.approx(r.ln_z, abs=1e-6)


def test_one_pin_is_the_single_boundary_correction():
    # (g s)^2/2 + ln Phi(-g s): what evidence._boundary_mass_correction applies today.
    r = laplace_log_evidence(_F1, [0.0, 0.0], [0.0, -5], [5, 5])
    s = 1e-4 * max(0.0, 1e-2 * 5.0)                                   # the rule's step on that span
    g = (_F1([s, 0.0]) - _F1([0.0, 0.0])) / s
    sigma = math.sqrt(r.cov[0, 0])
    base = -_F1([0.0, 0.0]) + math.log(2 * math.pi) - 0.5 * r.ln_det_h
    assert r.ln_box_mass == pytest.approx(float(log_ndtr(-g * sigma)), abs=1e-12)
    assert r.ln_z == pytest.approx(base + 0.5 * (g * sigma) ** 2 + float(log_ndtr(-g * sigma)),
                                   abs=1e-12)


def test_ex6_two_pins_match_the_truncated_grid_integral():
    mu = np.array([-0.5, 0.3])
    sig = np.array([[1.0, 0.6], [0.6, 2.0]])
    prec = np.linalg.inv(sig)
    f = lambda th: 0.5 * float((np.asarray(th) - mu) @ prec @ (np.asarray(th) - mu))
    r = laplace_log_evidence(f, [0.0, 0.6], [0.0, 0.0], [12.0, 12.0])
    assert r.status == "ok_boundary" and r.boundary_method == "box"
    g = np.linspace(0.0, 12.0, 2401)
    a, b = np.meshgrid(g, g, indexing="ij")
    da, db = a - mu[0], b - mu[1]
    ex = np.exp(-0.5 * (prec[0, 0] * da * da + 2 * prec[0, 1] * da * db + prec[1, 1] * db * db))
    grid = float(trapezoid(trapezoid(ex, g, axis=1), g))
    assert r.ln_z == pytest.approx(math.log(grid), abs=1e-3)


def test_ex7_a_wide_coordinate_is_cut_by_the_box_without_being_railed():
    r = laplace_log_evidence(lambda th: th[0] ** 2 / 5000.0, [0.0], [-10.0], [10.0])
    exact = math.sqrt(5000 * math.pi) * float(erf(10 / math.sqrt(5000)))      # 19.87
    assert r.ln_z == pytest.approx(math.log(exact), abs=1e-3)
    assert r.ln_box_mass == pytest.approx(math.log(0.1585), abs=1e-3)
    assert r.status == "ok_boundary"


def test_a_non_positive_definite_hessian_has_no_evidence():
    r = laplace_log_evidence(lambda th: -th[0] ** 2 + th[1] ** 2, [0.0, 0.0], [-1.0, -1.0],
                             [1.0, 1.0])
    assert r.status == "no_evidence" and r.reason == "Hessian not positive definite"
    assert math.isnan(r.ln_z) and r.cov is None


def test_a_comfortable_interior_mode_is_plain_laplace():
    r = laplace_log_evidence(lambda th: 0.5 * np.sum(((np.asarray(th) - 2.0) / 0.1) ** 2),
                             [2.0, 2.0], [0.0, 0.0], [4.0, 4.0])
    assert r.status == "ok" and r.boundary_method == "" and r.ln_box_mass == 0.0
    assert r.ln_z == pytest.approx(math.log(2 * math.pi * 0.01), abs=1e-6)


# --------------------------------------------------------------------------- example 9: the bundle
def test_ex9_bundle_prices_what_the_spectrum_prior_prices_delta_twin_included():
    # What the deep pass samples (the campaign path's PriorBundle) is the same prior the fast pass
    # prices (SpectrumPrior), for the plain model and for its delta twin (mode 2's width pinned
    # and absent from the bundle). The ordered omega0 group spans every mode, delta or finite.
    from dataclasses import replace

    from isar.analyze import catalogue, evidence, loop
    x = np.linspace(-10.0, 10.0, 81)
    counts = np.round(5 + 200 * np.exp(-0.5 * ((x - 3) / 0.7) ** 2)
                      + 80 * np.exp(-0.5 * ((x - 7) / 0.7) ** 2) + 150 * np.exp(-0.5 * (x / 0.5) ** 2))
    ctx = ScanContext(x=x, counts=counts, mask=np.ones(81, dtype=bool), T=100.0, center=0.0,
                      gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30", energy_status="absolute")
    plain = next(h for h in catalogue.enumerate_count_structures(ctx, 2)
                 if h.label == "elastic+stokes2+bg")
    derived = loop._variant_context(ctx, {"phonon2.Gamma": GAMMA_FLOOR},
                                    {"phonon2.Gamma": "catalogue_variant"},
                                    variant=loop.DELTA_VARIANT, label=plain.label)
    twin = replace(plain, label=plain.label + "_delta2", context=derived)
    sp = prior.resolve_prior_for(ctx)
    point = {"phonon.omega0": 3.0, "phonon.Gamma": 0.4, "phonon.h": 190.0,
             "phonon2.omega0": 7.0, "phonon2.Gamma": 0.9, "phonon2.h": 70.0,
             "elastic.h": 140.0, "bg.BG": 5.0}
    for hyp, g2 in ((plain, 0.9), (twin, None)):
        bundle = evidence.candidate_bundle(hyp, ctx)
        names = [q.name for q in bundle.params]
        assert ("phonon2.Gamma" in names) == (g2 is not None)
        theta = {n: point[n] for n in names}
        want = sp.ln_prior([(3.0, 0.4, 190.0), (7.0, g2, 70.0)], 140.0, 5.0)
        assert bundle.logpdf_ordered(theta) == pytest.approx(want, abs=1e-9), hyp.label
        for q in bundle.params:
            kind = q.name.split(".", 1)[1]
            kind = {"BG": "bg"}.get(kind, "elastic" if q.name == "elastic.h" else kind)
            lo, hi = sp.bounds(kind)
            assert q.hi == hi and (q.lo == lo or kind == "Gamma"), q.name
            if kind == "Gamma":
                assert q.lo == GAMMA_FLOOR and q.kind == "softlog"       # the fit's floor, not 0
