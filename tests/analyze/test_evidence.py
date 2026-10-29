"""Evidence ladder (SCENE-1 PR-b commit 3): Laplace exactness, arena guard, rung selection,
exactly-once prior counting, and the generalized prior-bundle dynesty lane.

Mirrors ``test_bayes.py`` idioms (small nlive + ``sample="unif"`` for the sampler lanes; one
fitted spectrum reused across assertions). Nothing consumes evidence yet — these tests pin the
engine's contract before select.py rebuilds on it."""
import math
import types
from dataclasses import replace

import numpy as np
import pytest

from isar.analyze import catalogue as C
from isar.analyze import evidence as E
from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.errors import bayes as BZ
from isar.analyze.lineshapes import KB, convolute_voigt_dho
from isar.analyze.phonon import enumerate_phonon_structures
from isar.schema.statistics import EVIDENCE_METHODS
from isar.schema.steering import ParamConstraints

pytestmark = pytest.mark.filterwarnings("ignore:The enlargement factor")

FAST = dict(n_live=80, dlogz=1.5, sample="unif", seed=3)


def _spectrum_ctx(seed=0, omega0=5.0, n=81, constraints=None):
    x = np.linspace(-10.0, 10.0, n)
    # 300 is the pre-Eq.-2 Lorentzian amplitude (Stokes area pi 300 / 0.4); converted to the
    # intrinsic A so the spectrum keeps the counts these tests were written for.
    a = 300.0 * np.pi / 0.4 * -np.expm1(-omega0 / (KB * 100.0))
    lam = convolute_voigt_dho(x, omega0, 0.0, 1.0, 0.0, 0.4, a, 80.0, 15.0, 100.0, peak="all")
    counts = np.random.default_rng(seed).poisson(np.clip(lam, 0.0, None)).astype(float)
    kw = {} if constraints is None else {"constraints": constraints}
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero", **kw)


def _fitted(ctx, label="elastic+stokes+bg"):
    hyp = {h.label: h for h in enumerate_phonon_structures(ctx)}[label]
    return hyp, loop.fit_hypothesis(hyp, ctx, minos=False)


def _bundle(hyp, ctx, *, entry=None):
    prob = loop.build_problem(hyp, ctx)
    exclude = [prob.free_names[i] for i in prob.fixed_pos]
    return C.resolve_priors(prob.composite, ctx, prob.bounds, entry=entry, exclude=exclude)


@pytest.fixture(scope="module")
def clean():
    ctx = _spectrum_ctx()
    hyp, fit = _fitted(ctx)
    return ctx, hyp, fit


# ------------------------------------------------------------------ Laplace exactness
def test_laplace_matches_analytic_gaussian_product():
    """Gaussian likelihood × Gaussian prior: Laplace at the MAP is EXACT, and the analytic
    marginal is ∫L·p = √(2πs²)·N(m; μ₀, √(s²+τ²)). Formula check with no fitter involved."""
    m, s = 2.0, 0.5          # likelihood: L(θ) = exp(−(θ−m)²/2s²), so cost = (θ−m)²/s²
    mu0, tau = 1.0, 0.8      # prior N(μ₀, τ), truncation negligible on wide bounds
    prior = C.ResolvedPrior("p.x", "gaussian", mu0, tau, -100.0, 100.0, "identity")
    var_post = 1.0 / (1.0 / s**2 + 1.0 / tau**2)
    theta_map = var_post * (m / s**2 + mu0 / tau**2)
    cost = (theta_map - m) ** 2 / s**2
    ln_z = E.laplace_ln_z(cost, prior.logpdf(theta_map), np.array([[var_post]]))
    expected = (0.5 * math.log(2.0 * math.pi * s**2)
                - 0.5 * (m - mu0) ** 2 / (s**2 + tau**2)
                - 0.5 * math.log(2.0 * math.pi * (s**2 + tau**2)))
    assert ln_z == pytest.approx(expected, abs=1e-9)


def test_laplace_refuses_non_pd_covariance():
    with pytest.raises(np.linalg.LinAlgError):
        E.laplace_ln_z(0.0, 0.0, np.array([[1.0, 2.0], [2.0, 1.0]]))  # indefinite


# ------------------------------------------------------------------ arena guard
def test_arena_mismatch_raises(clean):
    ctx, hyp, fit = clean
    rec = E.structure_evidence(hyp, ctx, fit, _bundle(hyp, ctx))
    es = E.EvidenceSet(arena_fingerprint=rec.arena_fingerprint)
    es.add(rec)                                           # same arena: fine
    assert es.by_label()[fit.label] is rec
    other = E.EvidenceSet(arena_fingerprint="isar-fp:something-else")
    with pytest.raises(E.ArenaMismatchError):
        other.add(rec)


def test_bundle_name_mismatch_is_a_construction_error(clean):
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    short = C.PriorBundle(bundle.params[:-1])             # dropped a free param
    with pytest.raises(ValueError, match="do not match"):
        E.structure_evidence(hyp, ctx, fit, short)


# ------------------------------------------------------------------ rung 1: Laplace at θ̂
def test_flat_bundle_stays_on_rung_one(clean):
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    rec = E.structure_evidence(hyp, ctx, fit, bundle)
    assert rec.evidence_method == "laplace"
    assert np.isfinite(rec.log_evidence)
    assert rec.log_evidence_err is None
    assert rec.k_free == fit.k
    assert rec.validity["unseen_informative"] == []
    assert rec.validity["boundary_pinned"] == []
    # exactly-once, numerically: lnZ is data cost + one bundle.logpdf, nothing else
    names = list(fit.params)
    free = [p.name for p in bundle.params]
    sigma = np.asarray(fit.parameter_covariance)[np.ix_(
        [names.index(n) for n in free], [names.index(n) for n in free])]
    assert rec.log_evidence == pytest.approx(
        E.laplace_ln_z(fit.cost, bundle.logpdf(fit.params), sigma))


def test_transport_prior_counts_exactly_once():
    """A soft-constrained fit: the transported Gaussian IS the prior — already in the fit
    objective (θ̂ its MAP, HESSE its curvature), so no refit fires, and lnZ adds its normalized
    logpdf once on top of the DATA-only cost (never posterior_cost)."""
    constraints = ParamConstraints(soft={"phonon.Gamma": (0.5, 0.2)},
                                   origins={"phonon.Gamma": "transport_test"})
    ctx = _spectrum_ctx(constraints=constraints)
    hyp, fit = _fitted(ctx)
    assert fit.fit_kind == "soft_prior" and fit.prior_penalty is not None
    bundle = _bundle(hyp, ctx)
    (gamma_prior,) = [p for p in bundle.params if p.name == "phonon.Gamma"]
    assert gamma_prior.origin == "transport"
    rec = E.structure_evidence(hyp, ctx, fit, bundle)
    assert rec.evidence_method == "laplace"               # transport never triggers the refit
    names = list(fit.params)
    free = [p.name for p in bundle.params]
    sigma = np.asarray(fit.parameter_covariance)[np.ix_(
        [names.index(n) for n in free], [names.index(n) for n in free])]
    expected = E.laplace_ln_z(fit.cost, bundle.logpdf(fit.params), sigma)
    assert rec.log_evidence == pytest.approx(expected)
    wrong_double_count = E.laplace_ln_z(fit.posterior_cost, bundle.logpdf(fit.params), sigma)
    assert rec.log_evidence != pytest.approx(wrong_double_count)


def test_transport_prior_carries_user_declared_origin_detail():
    """SCENE-1 PR-c: a ``user_declared`` soft constraint still resolves via the ``transport``
    path (catalogue.py:465-468) with the origin string preserved in ``origin_detail``."""
    constraints = ParamConstraints(soft={"phonon.Gamma": (0.5, 0.2)},
                                   origins={"phonon.Gamma": "user_declared"})
    ctx = _spectrum_ctx(constraints=constraints)
    hyp, fit = _fitted(ctx)
    bundle = _bundle(hyp, ctx)
    (gamma_prior,) = [p for p in bundle.params if p.name == "phonon.Gamma"]
    assert gamma_prior.origin == "transport"
    assert gamma_prior.origin_detail == "user_declared"


# ------------------------------------------------- rung 1b: Γ railed at the floor (H8)
@pytest.fixture(scope="module")
def railed():
    """A resolution-limited Stokes peak whose Γ̂ lands ON ``GAMMA_FLOOR`` with a converged fit
    and a PD covariance, so the ONLY at_limit pin is ``phonon.Gamma`` — the precondition of the
    H8 carve-out. Counts are deliberately modest: at high statistics σ_Γ shrinks until the
    truncated mass is numerically 1 and the correction stops testing anything. The truth is a
    mode at the floor (the narrow rendering keeps its area, so a 0.01 meV truth is now resolved
    from its Lorentzian tails and no longer rails); its area matches the earlier fixture's."""
    x = np.linspace(-10.0, 10.0, 81)
    lam = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.001, 1.5, 20.0, 40.0, 100.0, peak="all")
    counts = np.random.default_rng(10).poisson(np.clip(lam, 0.0, None)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    hyp, fit = _fitted(ctx)
    assert fit.optimizer_converged and fit.covariance_ok
    assert [n for n, v in fit.at_limit.items() if v] == ["phonon.Gamma"]
    return ctx, hyp, fit


def test_railed_gamma_takes_the_truncated_laplace_rung(railed):
    """GAMMA_FLOOR is a modelling artifact, not physics, so a Γ jammed against it is an
    integrable boundary: rung 1 stands and sheds the Gaussian mass outside the support instead
    of paying for dynesty. The correction must be MATERIAL here (Γ̂ is at the floor, so ~half the
    Gaussian is outside), otherwise the rung would be indistinguishable from plain Laplace."""
    ctx, hyp, fit = railed
    bundle = _bundle(hyp, ctx)
    rec = E.structure_evidence(hyp, ctx, fit, bundle)

    assert rec.evidence_method == "laplace_truncated"
    assert rec.evidence_method in EVIDENCE_METHODS          # persistable vocabulary
    assert rec.validity["gamma_boundary_truncated"] == ["phonon.Gamma"]
    assert rec.validity["boundary_pinned"] == ["phonon.Gamma"]
    assert "escalation" not in rec.validity                 # no sampler was reached
    assert rec.log_evidence_err is None and np.isfinite(rec.log_evidence)

    names = list(fit.params)
    free = [p.name for p in bundle.params]
    sigma = np.asarray(fit.parameter_covariance)[np.ix_(
        [names.index(n) for n in free], [names.index(n) for n in free])]
    uncorrected = E.laplace_ln_z(fit.cost, bundle.logpdf(fit.params), sigma)
    # Measured 2026-10-23 (the area-preserving narrow rendering): g = 1.28e4, σ_Γ = 8.93e-5 ⇒
    # gσ = 1.141, and Γ̂ sits a = 1.06e-7 above the floor (a/σ = 1.2e-3, well inside
    # GAMMA_BOUND_TOL = 5e-3) ⇒ correction = −1.411 nats.
    # Direction: the gradient term sheds MORE than ln½ (= −0.693) and the tiny offset gives a
    # little back, so a railed Γ still loses appreciably more than half its Gaussian mass.
    assert rec.log_evidence < uncorrected - 0.4
    assert rec.validity["boundary_gradient"]["phonon.Gamma"] > 0.0


def test_unavailable_gradient_escalates_and_persists_no_nan(railed, monkeypatch):
    """A non-finite boundary gradient must escalate AND leave a JSON-safe validity payload:
    evidence_validity is persisted through candidate rows, and canonical JSON refuses NaN.
    Finite gradients go in boundary_gradient; unmeasurable axes are named separately."""
    ctx, hyp, fit = railed
    monkeypatch.setattr(E, "_inward_cost_gradients",
                        lambda *a, **k: {"phonon.Gamma": float("nan")})
    rec = E.structure_evidence(hyp, ctx, fit, _bundle(hyp, ctx), escalation="defer")
    assert rec.evidence_method == "deferred_escalation"
    assert rec.validity["escalation"] == "laplace_invalid"
    assert rec.validity["boundary_gradient"] == {}
    assert rec.validity["boundary_gradient_unavailable"] == ["phonon.Gamma"]
    assert not any(isinstance(v, float) and math.isnan(v)
                   for v in rec.validity["boundary_gradient"].values())


def test_ceiling_railed_gamma_escalates_not_truncates():
    """Γ has TWO bounds and only the floor is the H8 modelling artifact. A Γ railed at
    GAMMA_MAX is a mode broader than the ceiling allows — the likelihood gradient at the
    bound is genuinely nonzero and an overdamped-vs-two-mode contest is selection-material —
    so it must pay for the sampler, never take the cheap rung. Found by a fresh-context
    verifier 2026-08-09: the original gate tested the NAME suffix only, and this exact fit
    (Γ̂ = 4.9999… at GAMMA_MAX = 5.0) came back laplace_truncated. The planted mode is
    overdamped (Γ = 15 > E0 = 5): the full DHO form expresses any Γ below the ceiling, so only a
    width beyond it rails there. Recipe /5 lifted the ceiling from 5 to 20 meV (re-pin: planted Γ
    8 -> 15, which still rails at 20 for this seed; a planted Γ above 20 pins the elastic line)."""
    x = np.linspace(-15.0, 15.0, 101)
    lam = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 15.0, 200.0, 0.0, 20.0, 300.0, peak="all")
    counts = np.random.default_rng(3).poisson(np.clip(lam, 0.0, None)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=300.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    hyp, fit = _fitted(ctx)
    assert fit.optimizer_converged and fit.covariance_ok
    pins = [n for n, v in fit.at_limit.items() if v]
    assert pins == ["phonon.Gamma"]
    assert fit.params["phonon.Gamma"] > 1.0                  # the CEILING, not the floor

    bundle = _bundle(hyp, ctx)
    rec = E.structure_evidence(hyp, ctx, fit, bundle, escalation="defer")
    assert rec.evidence_method == "deferred_escalation"
    assert rec.validity["escalation"] == "laplace_invalid"
    assert "gamma_boundary_truncated" not in rec.validity


def test_truncated_laplace_agrees_with_dynesty(railed):
    """The load-bearing one: the analytic shortcut must answer the same integral the sampler
    does. Same candidate, same bundle — the pin is widened to a non-Γ parameter only to force
    the escalation lane (coarse budget ⇒ coarse tolerance, as in the laplace_map test)."""
    ctx, hyp, fit = railed
    bundle = _bundle(hyp, ctx)
    lap = E.structure_evidence(hyp, ctx, fit, bundle)
    assert lap.evidence_method == "laplace_truncated"

    # ``sample="unif"`` (the FAST default elsewhere) is hopeless on a boundary-sharp posterior —
    # rejection sampling inside the bounding ellipsoid stalls for minutes. rwalk finishes in ~15 s.
    forced = replace(fit, at_limit=dict(fit.at_limit, **{"phonon.h": True}))
    dyn = E.structure_evidence(hyp, ctx, forced, bundle, **dict(FAST, sample="rwalk"))
    assert dyn.evidence_method == "dynesty"
    # Re-pin (recipe /5): 2.0 -> 7.0 floor. The Laplace-at-theta-hat rung sits ~5 nats below the
    # sampler on this fixture whatever the prior (measured: -78.3 vs -73.6 on the v4 bounds with a
    # flat 5 meV width, where the sampler's own error of 1.4 hid it; the v5 priors shrink that
    # error to 0.6). It is the unmodelled non-Gaussian tail of the weak peak, not the truncation
    # correction (the test above pins that); the fast pass prices the mode with the box rule instead.
    assert abs(lap.log_evidence - dyn.log_evidence) < max(7.0, 5.0 * dyn.log_evidence_err)


def test_a_non_gamma_pin_never_takes_the_truncated_rung(railed):
    """The carve-out is Γ-only. Ω_d at the window edge is real physics (H8) — a boundary the
    Gaussian picture genuinely cannot describe — so it keeps escalating even alongside a Γ pin."""
    ctx, hyp, fit = railed
    forced = replace(fit, at_limit=dict(fit.at_limit, **{"phonon.omega0": True}))
    rec = E.structure_evidence(hyp, ctx, forced, _bundle(hyp, ctx), escalation="defer")

    assert rec.evidence_method == "deferred_escalation"     # i.e. NOT laplace_truncated
    assert rec.validity["escalation"] == "laplace_invalid"
    assert "gamma_boundary_truncated" not in rec.validity


def test_two_railed_gammas_escalate_instead_of_multiplying_per_axis_masses():
    """The truncated rung is SINGLE-pin only. Two Γ pins would need the pinned block's joint
    orthant mass; the per-axis product ignores their correlation and is selection-material
    (bivariate orthant at |ρ| = 0.9 is 0.428 vs the product's 0.25, ~0.5-1.3 nats). The gate
    must therefore escalate on two pins exactly as on any other invalid-Gaussian case.

    Construction: a converged, PD, UNPINNED two-mode fit, with the pins forced on by hand —
    a genuine double rail is fit-fragile, and the guard under test is the pin COUNT, so the
    same fit with only one of the two pins is the control that must still take the cheap rung.
    """
    from isar.analyze.phonon import (GAMMA_FLOOR, enumerate_phonon_multimode,
                                     enumerate_phonon_structures, greedy_multimode_ic)

    x = np.linspace(-12.0, 12.0, 121)
    lam = (convolute_voigt_dho(x, 3.0, 0.0, 1.0, 0.0, 0.01, 40.0, 0.0, 25.0, 100.0, peak="all")
           + convolute_voigt_dho(x, 8.0, 0.0, 1.0, 0.0, 0.01, 30.0, 0.0, 0.0, 100.0, peak="all"))
    # Near-floor widths make most draws crawl into MIGRAD's call limit (under the old count-scaled
    # bounds as under the prior recipe); this draw, seeded as production seeds a second mode,
    # converges with a PD covariance and nothing at a limit.
    counts = np.random.default_rng(5).poisson(np.clip(lam, 0.0, None)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    hyp = {h.label: h for h in enumerate_phonon_multimode(ctx)}["stokes2+bg"]
    one = loop.fit_hypothesis(
        {h.label: h for h in enumerate_phonon_structures(ctx)}["stokes+bg"], ctx)
    fit = loop.fit_hypothesis(hyp, ctx, minos=False,
                              x0=greedy_multimode_ic(hyp.build(), ctx, one))
    assert fit.optimizer_converged and fit.covariance_ok
    gammas = ["phonon.Gamma", "phonon2.Gamma"]
    bundle = _bundle(hyp, ctx)
    assert set(gammas) <= {p.name for p in bundle.params}     # both Γ are genuinely free

    # Both Γ at the floor and flagged: the ONLY thing the gate can object to is the count.
    at_floor = replace(fit, params=dict(fit.params, **{n: GAMMA_FLOOR for n in gammas}))
    both = replace(at_floor, at_limit=dict(fit.at_limit, **{n: True for n in gammas}))
    rec = E.structure_evidence(hyp, ctx, both, bundle, escalation="defer")
    assert rec.evidence_method == "deferred_escalation"       # i.e. NOT laplace_truncated
    assert rec.validity["escalation"] == "laplace_invalid"
    assert rec.validity["boundary_pinned"] == gammas
    assert "gamma_boundary_truncated" not in rec.validity

    one = replace(at_floor, at_limit=dict(fit.at_limit, **{gammas[0]: True}))
    ctrl = E.structure_evidence(hyp, ctx, one, bundle, escalation="defer")
    assert ctrl.evidence_method == "laplace_truncated"
    assert ctrl.validity["gamma_boundary_truncated"] == [gammas[0]]


def test_boundary_mass_correction_sheds_half_a_gaussian_at_a_flat_bound():
    """The g → 0 limit of the gradient-aware factor is the plain half-Gaussian. A parameter on
    its lower bound whose likelihood is locally FLAT along that axis keeps exactly the mass
    inside the support: ½·erfcx(0) = ½. Backward-compatible with the pre-gradient factor."""
    prior = C.ResolvedPrior("phonon.Gamma", "flat", None, None, 1e-3, 5.0, "default_flat")
    bundle = C.PriorBundle((prior,))
    corr = E._boundary_mass_correction(["phonon.Gamma"], {"phonon.Gamma": 1e-3},
                                       np.array([0.1]), ["phonon.Gamma"], bundle,
                                       {"phonon.Gamma": 0.0})
    assert corr == pytest.approx(math.log(0.5), abs=1e-12)


def test_boundary_mass_correction_is_gradient_aware():
    """With θ̂ ON the bound and a nonzero inward gradient the local likelihood is
    exp(−g·t − t²/2σ²) on t ≥ 0, whose mass relative to the full Gaussian is
    exp(g²σ²/2)·Φ(−gσ) = ½·erfcx(gσ/√2). The a = 0 case of the offset-aware form must reproduce
    that EXACTLY, so the tolerance is float-tight.

    Note the DIRECTION: erfcx is monotonically decreasing on [0, ∞), so this factor is ≤ ½ for
    every g ≥ 0 and sheds MORE than the half-Gaussian, never less — a likelihood falling away
    from θ̂ into the support leaves less than half the Gaussian's mass there, not more."""
    from scipy.special import erfcx

    prior = C.ResolvedPrior("phonon.Gamma", "flat", None, None, 1e-3, 5.0, "default_flat")
    bundle = C.PriorBundle((prior,))
    sigma = 0.1
    g = 2.0 / sigma                                     # gσ = 2
    corr = E._boundary_mass_correction(["phonon.Gamma"], {"phonon.Gamma": 1e-3},
                                       np.array([sigma]), ["phonon.Gamma"], bundle,
                                       {"phonon.Gamma": g})
    assert corr == pytest.approx(math.log(0.5 * float(erfcx(math.sqrt(2.0)))), abs=1e-12)
    assert corr < math.log(0.5)


def test_boundary_mass_correction_is_offset_aware():
    """θ̂ need not sit exactly ON the floor: the gate admits it up to GAMMA_BOUND_TOL inside,
    where far less than half the mass is outside. With a = θ̂ − lo the exact mass fraction is
    exp(g²σ²/2)·Φ(a/σ − gσ); assuming a = 0 there would over-shed by up to ~0.68 nats.

    Both references are ``scipy.integrate.quad`` of the raw integrand exp(−g·t − t²/2σ²) over
    t ∈ [−a, ∞), divided by σ√2π (agreement ≤ 3e-13 nats):
      * a = 3σ, g = 0   → ln Φ(3)  = −0.00135080996474798
      * a = 2σ, gσ = 1  → +0.32724622097655068  (the tilt pulls in mass from below θ̂)"""
    prior = C.ResolvedPrior("phonon.Gamma", "flat", None, None, 1e-3, 5.0, "default_flat")
    bundle = C.PriorBundle((prior,))
    sigma = 1e-3                                        # so 3σ still fits inside GAMMA_BOUND_TOL

    flat = E._boundary_mass_correction(["phonon.Gamma"], {"phonon.Gamma": 1e-3 + 3 * sigma},
                                       np.array([sigma]), ["phonon.Gamma"], bundle,
                                       {"phonon.Gamma": 0.0})
    assert flat == pytest.approx(-0.00135080996474798, abs=1e-12)
    assert flat == pytest.approx(math.log(0.5 * math.erfc(-3.0 / math.sqrt(2.0))), abs=1e-12)

    tilted = E._boundary_mass_correction(["phonon.Gamma"], {"phonon.Gamma": 1e-3 + 2 * sigma},
                                         np.array([sigma]), ["phonon.Gamma"], bundle,
                                         {"phonon.Gamma": 1.0 / sigma})
    assert tilted == pytest.approx(0.32724622097655068, abs=1e-12)


def test_degenerate_boundary_mass_fails_safe():
    """A mass the correction cannot compute must return None so the caller escalates — a
    silently-zero correction would publish an uncorrected lnZ under a truncated label."""
    prior = C.ResolvedPrior("phonon.Gamma", "flat", None, None, 1e-3, 5.0, "default_flat")
    bundle = C.PriorBundle((prior,))
    params, names = {"phonon.Gamma": 1e-3}, ["phonon.Gamma"]
    flat = {"phonon.Gamma": 0.0}
    for sigma in (0.0, float("nan"), float("inf"), -1.0):
        assert E._boundary_mass_correction(names, params, np.array([sigma]), names,
                                           bundle, flat) is None
    # An unavailable gradient (non-finite cost at either probe point) is the same refusal.
    for grad in ({}, {"phonon.Gamma": float("nan")}, {"phonon.Gamma": float("inf")}):
        assert E._boundary_mass_correction(names, params, np.array([0.1]), names,
                                           bundle, grad) is None
    # θ̂ off the floor by more than the caller's gate allows: refuse rather than extrapolate the
    # sits-ON-lo formula.
    assert E._boundary_mass_correction(names, {"phonon.Gamma": 0.5}, np.array([0.1]), names,
                                       bundle, flat) is None
    unrelated = C.PriorBundle((C.ResolvedPrior("bg.BG", "flat", None, None, 0.0, 1e3,
                                               "default_flat"),))
    assert E._boundary_mass_correction(names, params, np.array([0.1]), names,
                                       unrelated, flat) is None


# ------------------------------------------------------------------ rung 2: MAP refit
def _narrow_entry(fit, param_suffix="Gamma", sigma=1e-3):
    """A catalogue entry declaring a narrow identity prior the fit never saw, centred at the
    fitted value so the warm MAP refit converges immediately."""
    name = next(n for n in fit.params if n.endswith("." + param_suffix))
    return types.SimpleNamespace(
        name="test-entry",
        parameter_priors=(C.ParamPrior(param=param_suffix, kind="gaussian",
                                       mu=max(float(fit.params[name]), 1e-3), sigma=sigma),))


def test_unseen_informative_prior_triggers_map_refit(clean):
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx, entry=_narrow_entry(fit))
    rec = E.structure_evidence(hyp, ctx, fit, bundle)
    assert rec.evidence_method == "laplace_map"
    assert np.isfinite(rec.log_evidence)
    assert rec.validity["unseen_informative"] == ["phonon.Gamma"]
    assert rec.validity["map_refit"]["success"] is True


def _widened(fit, name, sd):
    """``fit`` with ``name``'s posterior sd set to ``sd``, its correlations kept."""
    names = list(fit.params)
    i = names.index(name)
    cov = np.array(fit.parameter_covariance, dtype=float)
    scale = np.ones(len(names))
    scale[i] = sd / math.sqrt(cov[i, i])
    return replace(fit, parameter_covariance=cov * np.outer(scale, scale))


def test_an_energy_posterior_reaching_the_ramp_takes_the_map_refit(clean):
    # The fit never saw the near-zero energy penalty (recipe /6). With FWHM 1 the ramp's top corner
    # ends at 1.05 meV: a posterior wholly above it (5 - 3 x 1.25 = 1.25) stays at rung 1; one
    # reaching it (5 - 3 x 1.4 = 0.8) or a fitted energy below it goes to the MAP refit.
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    ramp = bundle.by_name()["phonon.omega0"]
    assert ramp.kind == "energy_ramp" and ramp.energy.start + ramp.energy.corner == \
        pytest.approx(1.05)
    above = E.structure_evidence(hyp, ctx, _widened(fit, "phonon.omega0", 1.25), bundle)
    assert above.evidence_method == "laplace" and above.validity["unseen_informative"] == []
    reaching = E.structure_evidence(hyp, ctx, _widened(fit, "phonon.omega0", 1.4), bundle)
    assert reaching.validity["unseen_informative"] == ["phonon.omega0"]
    assert reaching.evidence_method == "laplace_map" and "map_refit" in reaching.validity
    low = replace(fit, params=dict(fit.params, **{"phonon.omega0": 0.6}))
    below = E.structure_evidence(hyp, ctx, low, bundle, escalation="defer")
    assert below.validity["unseen_informative"] == ["phonon.omega0"]
    assert "map_refit" in below.validity                     # rung 2 ran; it never sampled
    off = _bundle(hyp, replace(ctx, gauss_fwhm=1e-3))       # a ramp far below the posterior
    assert E.structure_evidence(hyp, ctx, low, off).validity["unseen_informative"] == []


def test_map_refit_agrees_with_dynesty(clean):
    """The escalated lane and the analytic lane answer the same integral: on a well-behaved
    scan laplace_map ≈ dynesty within sampler error (coarse budget ⇒ coarse tolerance)."""
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx, entry=_narrow_entry(fit, sigma=3e-2))
    lap = E.structure_evidence(hyp, ctx, fit, bundle)
    assert lap.evidence_method == "laplace_map"
    pinned = replace(fit, at_limit=dict(fit.at_limit, **{"phonon.h": True}))
    dyn = E.structure_evidence(hyp, ctx, pinned, bundle, **FAST)
    assert dyn.evidence_method == "dynesty"
    assert abs(lap.log_evidence - dyn.log_evidence) < max(2.0, 5.0 * dyn.log_evidence_err)


# ------------------------------------------------------------------ rung 3: dynesty escalation
def test_boundary_pin_escalates_to_dynesty(clean):
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    pinned = replace(fit, at_limit=dict(fit.at_limit, **{"phonon.omega0": True}))
    rec = E.structure_evidence(hyp, ctx, pinned, bundle, **FAST)
    assert rec.validity["escalation"] == "laplace_invalid"
    assert rec.validity["boundary_pinned"] == ["phonon.omega0"]
    assert rec.evidence_method == "dynesty"
    assert np.isfinite(rec.log_evidence)
    assert rec.log_evidence_err is not None and rec.log_evidence_err > 0.0


def test_missing_at_limit_is_fail_safe(clean):
    """A fit persisted before the at_limit field exists is UNKNOWN, not clean: it escalates."""
    ctx, hyp, fit = clean
    old = replace(fit, at_limit=None)
    rec = E.structure_evidence(hyp, ctx, old, _bundle(hyp, ctx), **FAST)
    assert rec.evidence_method == "dynesty"


def test_nonconverged_fit_is_still_sampled_and_judged(clean):
    """A model whose optimizer did not converge is judged by the sampler, never refused; deferred,
    it carries no ceiling (its θ̂ is not the maximum) and is exempt from pruning."""
    ctx, hyp, fit = clean
    bad = replace(fit, optimizer_converged=False)
    rec = E.structure_evidence(hyp, ctx, bad, _bundle(hyp, ctx), **FAST)
    assert rec.evidence_method == "dynesty"
    assert np.isfinite(rec.log_evidence)
    assert rec.validity["reason"] == "fit did not converge"
    assert rec.validity["escalation"] == "optimizer_not_converged"
    deferred = E.structure_evidence(hyp, ctx, bad, _bundle(hyp, ctx), escalation="defer")
    assert deferred.evidence_method == "deferred_escalation"
    assert deferred.validity["prune_exempt"] is True
    assert "log_evidence_upper_bound" not in deferred.validity


# ------------------------------------------------------------------ proper-prior guard
def test_resolve_priors_refuses_non_finite_bounds(clean):
    ctx, hyp, _fit = clean
    prob = loop.build_problem(hyp, ctx)
    bad = list(prob.bounds)
    bad[-1] = (float("-inf"), float("inf"))
    with pytest.raises(ValueError, match="non-finite optimizer bounds"):
        C.resolve_priors(prob.composite, ctx, bad)


def test_qe_candidate_gets_finite_laplace_evidence():
    """Regression for the unbounded quasielastic amplitude: a qe-bearing candidate must get
    finite lnZ on rung 1, not a silent −inf that drops it from the posterior (found on the
    first real IN8 scan: the best-cost model lost with no flag)."""
    x = np.linspace(-10.0, 10.0, 81)
    lorentz = 40.0 / (1.0 + (x / 1.5) ** 2)
    counts = np.random.default_rng(5).poisson(lorentz + 30.0).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero", quasielastic_fwhm=3.0)
    hyps = {h.label: h for h in enumerate_phonon_structures(ctx)}
    label = next(l for l in hyps if "qe" in l)
    prob = loop.build_problem(hyps[label], ctx)
    qe_lo, qe_hi = next(b for n, b in zip(prob.free_names, prob.bounds)
                        if n.endswith(".QE"))
    assert np.isfinite(qe_lo) and np.isfinite(qe_hi)      # the root cause, pinned directly
    fit = loop.fit_hypothesis(hyps[label], ctx, minos=False)
    assert fit.optimizer_converged
    rec = E.structure_evidence(hyps[label], ctx, fit, _bundle(hyps[label], ctx))
    assert np.isfinite(rec.log_evidence), rec.validity
    assert rec.evidence_method in ("laplace", "laplace_map", "dynesty")


def _escalated(*args, **kwargs):
    """The pre-ladder escalation lane (dominance pruning, budget, parallel batch), opt-in since rung
    1 of the count ladder is Laplace only."""
    return E.candidate_set_evidence(*args, escalate=True, **kwargs)


# ------------------------------------------------------------------ dominance pruning
def _set_triples(ctx, results):
    from isar.analyze.phonon import enumerate_phonon_structures
    hyps = {h.label: h for h in enumerate_phonon_structures(ctx)}
    triples = []
    for r in results:
        if not r.optimizer_converged:
            continue
        prob = loop.build_problem(hyps[r.label], ctx)
        bundle = C.resolve_priors(prob.composite, ctx, prob.bounds,
                                  exclude=[prob.free_names[i] for i in prob.fixed_pos])
        triples.append((hyps[r.label], r, bundle))
    return triples


def test_dominated_candidate_never_pays_for_dynesty(clean, monkeypatch):
    """On a decisive phonon scan the railed elastic+bg null's ceiling (−cost/2) sits
    thousands of nats under the leader: it must be recorded dominated WITHOUT sampling."""
    ctx, _hyp, _fit = clean
    results = loop.fit_admissible_structures(ctx, errors=False)

    def _no_sampling(*a, **k):
        raise AssertionError("dynesty must not run for a dominated candidate")

    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior", _no_sampling)
    es = _escalated(_set_triples(ctx, results), ctx)
    recs = es.by_label()
    dom = recs["elastic+bg"]
    assert dom.evidence_method == "dominated"
    assert math.isnan(dom.log_evidence)
    bound = dom.validity["log_evidence_upper_bound"]
    leader = dom.validity["dominated_by_leader_log_evidence"]
    assert bound < leader - E.PRUNE_MARGIN                # the certificate is quantitative
    assert recs["elastic+stokes+bg"].evidence_method == "laplace"


def test_failed_sampler_gate_yields_no_usable_evidence(clean, monkeypatch):
    """A dynesty run that fails its own §5.4 gate returns a finite logz — and must NOT be
    usable evidence. Finiteness cannot separate the failures (max_iter, low ESS, logzerr blow-up,
    a railed omega0 all leave logz finite), so the gate is convergence, not finiteness.
    Otherwise a sampler the module already distrusts for pruning decides the production
    posterior, since select_models admits every finite lnZ."""
    ctx, hyp, fit = clean
    failed = types.SimpleNamespace(converged=False, fail_reason="ess=12 below 100",
                                   ess=12.0, log_evidence=-123.5, log_evidence_err=7.0)
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: failed)
    rec = E.structure_evidence(hyp, ctx, replace(fit, at_limit=None), _bundle(hyp, ctx), **FAST)

    assert rec.evidence_method == "invalid"
    assert math.isnan(rec.log_evidence)
    # The number is kept for diagnosis, with the sampler's own reason — refused, not discarded.
    assert rec.validity["dynesty"]["converged"] is False
    assert rec.validity["dynesty"]["fail_reason"] == "ess=12 below 100"
    assert rec.validity["dynesty"]["log_evidence_untrusted"] == -123.5


def test_penalised_fit_is_never_dominance_pruned(monkeypatch):
    """−cost/2 is a ceiling only when θ̂ is the data MLE. For a soft-constrained fit θ̂ is the
    MAP, so the bound UNDERstates lnZ and pruning on it could drop a competitive candidate
    unsampled. A penalised fit is still DEFERRED (so it can join the same escalation batch as
    everything else — 2026-09-04 amendment, separating the pruning rule from execution order)
    but is marked prune-exempt and carries no ceiling to be pruned by."""
    constraints = ParamConstraints(soft={"phonon.Gamma": (0.5, 0.2)},
                                   origins={"phonon.Gamma": "transport_test"})
    ctx = _spectrum_ctx(constraints=constraints)
    hyp, fit = _fitted(ctx)
    assert fit.prior_penalty is not None                  # the precondition the guard reads

    rec = E.structure_evidence(hyp, ctx, replace(fit, at_limit=None), _bundle(hyp, ctx),
                               escalation="defer", **FAST)

    assert rec.evidence_method == "deferred_escalation"   # deferred, not run inline
    assert rec.validity["prune_exempt"] is True
    assert "log_evidence_upper_bound" not in rec.validity

    # candidate_set_evidence must route it straight to escalation regardless of the leader: never
    # "dominated", and it pays for the sampler even against a leader no cost-based ceiling of its
    # own could ever beat.
    sampled = types.SimpleNamespace(converged=True, fail_reason=None, ess=500.0,
                                    log_evidence=-42.0, log_evidence_err=0.3)
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: sampled)
    es = _escalated([(hyp, replace(fit, at_limit=None), _bundle(hyp, ctx))], ctx,
                                  **FAST)
    only = es.records[0]
    assert only.evidence_method == "dynesty"
    assert "log_evidence_upper_bound" not in only.validity


def test_genuine_contest_still_pays_for_dynesty():
    """On a blank (bg-only) scan a railed phonon candidate fits AS WELL as the winner —
    its ceiling is near the leader, so pruning must NOT fire and dynesty must run: the
    ambiguous scans are exactly where the sampler earns its cost."""
    x = np.linspace(-10.0, 10.0, 81)
    counts = np.random.default_rng(11).poisson(np.full_like(x, 60.0)).astype(float)
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                      center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero")
    results = loop.fit_admissible_structures(ctx, errors=False)
    es = _escalated(_set_triples(ctx, results), ctx, **FAST)
    methods = {label: r.evidence_method for label, r in es.by_label().items()}
    escalated = [m for m in methods.values() if m == "dynesty"]
    assert escalated, f"expected at least one close-contest escalation, got {methods}"
    assert "dominated" not in set(methods.values()) or all(
        r.validity["log_evidence_upper_bound"]
        < r.validity["dominated_by_leader_log_evidence"] - E.PRUNE_MARGIN
        for r in es.records if r.evidence_method == "dominated")


def test_escalation_n_live_reads_the_environment_and_refuses_nonsense(monkeypatch):
    """The cost knob exists so a SPAWNED pool child samples where the parent does — a module
    constant cannot cross that boundary, an environment variable can. An explicit ``n_live``
    still wins, and a malformed value raises instead of silently reverting: a run whose sampling
    resolution is unknowable after the fact is worse than a run that refused to start."""
    monkeypatch.delenv(E.ESCALATION_N_LIVE_ENV, raising=False)
    assert E.escalation_n_live() == E.ESCALATION_N_LIVE

    monkeypatch.setenv(E.ESCALATION_N_LIVE_ENV, "32")
    assert E.escalation_n_live() == 32

    monkeypatch.setenv(E.ESCALATION_N_LIVE_ENV, "not-a-number")
    with pytest.raises(ValueError):
        E.escalation_n_live()
    monkeypatch.setenv(E.ESCALATION_N_LIVE_ENV, "0")
    with pytest.raises(ValueError):
        E.escalation_n_live()


def _blank_scan_ctx():
    """The blank (bg-only) scan of :func:`test_genuine_contest_still_pays_for_dynesty`: a railed
    phonon candidate fits as well as the winner, so its ceiling sits near the leader and the
    escalation is genuinely contested — the only case where the budget can bite."""
    x = np.linspace(-10.0, 10.0, 81)
    counts = np.random.default_rng(11).poisson(np.full_like(x, 60.0)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def test_the_suite_cap_still_produces_sampling_that_counts():
    """The cap must make escalations CHEAPER, not fake. A dynesty run that fails its own §5.4
    convergence gate is fast and worthless — it yields no usable evidence, so a cap set too low
    would quietly convert every escalation into invalid evidence and push selection onto the
    recorded-AIC lane while the suite still went green. This runs the genuine-contest scan at
    whatever n_live the environment actually imposes (the repo-root conftest's cap in a suite
    run, ESCALATION_N_LIVE otherwise) and demands a converged sampler."""
    ctx = _blank_scan_ctx()
    results = loop.fit_admissible_structures(ctx, errors=False)
    es = _escalated(_set_triples(ctx, results), ctx)   # no explicit n_live

    sampled = [r for r in es.records if r.evidence_method == "dynesty"]
    assert sampled, "expected a contested escalation to reach the sampler"
    for r in sampled:
        assert r.validity["dynesty"]["converged"], (
            f"{r.label} sampled at n_live={r.validity['dynesty'].get('n_live')} but failed its "
            f"convergence gate ({r.validity['dynesty'].get('fail_reason')}) — the cap is too low "
            f"and is buying speed by discarding evidence")
        assert np.isfinite(r.log_evidence)


def test_exhausted_budget_records_unresolved_and_never_samples(monkeypatch):
    """Budget-exhausted honesty: a contested candidate the budget skipped is RECORDED with its
    ceiling, not dropped and not given a number. NaN lnZ keeps it out of the posterior, and
    unresolved_contenders() reports it so selection can refuse to rank on an incomplete set."""
    ctx = _blank_scan_ctx()
    results = loop.fit_admissible_structures(ctx, errors=False)

    def _no_sampling(*a, **k):
        raise AssertionError("dynesty must not run once the escalation budget is exhausted")

    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior", _no_sampling)
    es = _escalated(_set_triples(ctx, results), ctx, budget_s=0.0, **FAST)

    unresolved = [r for r in es.records if r.evidence_method == "unresolved"]
    assert unresolved, f"expected a budget-skipped candidate, got {[r.evidence_method for r in es.records]}"
    for r in unresolved:
        assert math.isnan(r.log_evidence)                     # never a number it did not earn
        assert math.isfinite(r.validity["log_evidence_upper_bound"])   # ceiling kept
        assert r.validity["escalation_budget_s"] == 0.0
    assert set(es.unresolved_contenders()) == {r.label for r in unresolved}


def test_budget_never_costs_a_certain_dominance_ruling(clean, monkeypatch):
    """The budget must not convert a FREE certain ruling into an unresolved one. Dominance is
    decided from an already-computed ceiling at no sampler cost, so it is checked first: on a
    decisive scan the railed null stays "dominated" (ruled out) under a zero budget, and the
    set still names its winner on evidence."""
    ctx, _hyp, _fit = clean
    results = loop.fit_admissible_structures(ctx, errors=False)
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no sampling")))
    es = _escalated(_set_triples(ctx, results), ctx, budget_s=0.0)

    assert es.by_label()["elastic+bg"].evidence_method == "dominated"
    assert es.unresolved_contenders() == []                   # nothing left in doubt


def test_a_ruled_out_budget_skip_is_not_a_contender():
    """Symmetry check on the predicate itself: an unresolved candidate whose own ceiling sits
    BELOW the best trusted evidence is already ruled out by that bound, so the winner stands
    and the set stays resolvable. Only a ceiling that could still overtake the leader degrades
    selection — otherwise the backstop would punish every set it ever touched."""
    def _rec(label, lnz, method, bound=None):
        validity = {} if bound is None else {"log_evidence_upper_bound": bound}
        return E.EvidenceRecord(label=label, log_evidence=lnz, log_evidence_err=None,
                                evidence_method=method, k_free=3, arena_fingerprint="arena",
                                prior_spec=(), validity=validity)

    es = E.EvidenceSet(arena_fingerprint="arena")
    es.add(_rec("stokes+bg", -10.0, "laplace"))
    es.add(_rec("ruled_out", float("nan"), "unresolved", bound=-40.0))
    assert es.unresolved_contenders() == []

    es.add(_rec("could_have_won", float("nan"), "unresolved", bound=-5.0))
    assert es.unresolved_contenders() == ["could_have_won"]

    # Fail safe on an unreadable ceiling. A bound we cannot read cannot rule the candidate out, so
    # it must COUNT as a contender: `nan >= best` is False, which would take the other direction
    # and let a winner stand on a set that was never completed.
    es_nan = E.EvidenceSet(arena_fingerprint="arena")
    es_nan.add(_rec("stokes+bg", -10.0, "laplace"))
    es_nan.add(_rec("unreadable", float("nan"), "unresolved", bound=float("nan")))
    assert es_nan.unresolved_contenders() == ["unreadable"]
    es_missing = E.EvidenceSet(arena_fingerprint="arena")
    es_missing.add(_rec("stokes+bg", -10.0, "laplace"))
    es_missing.add(_rec("no_bound", float("nan"), "unresolved"))       # no ceiling recorded at all
    assert es_missing.unresolved_contenders() == ["no_bound"]


def test_the_budget_default_is_resolved_at_call_time(monkeypatch):
    """``budget_s`` must not bind ESCALATION_BUDGET_S in the signature: a default argument is
    captured at import, so patching the constant would be a silent no-op and any test that tried
    it would pass vacuously. Same trap ``escalation_n_live()`` exists to avoid."""
    blank = _blank_scan_ctx()
    results = loop.fit_admissible_structures(blank, errors=False)
    monkeypatch.setattr(E, "ESCALATION_BUDGET_S", 0.0)

    def _no_sampling(*a, **k):
        raise AssertionError("a zero budget patched onto the constant must stop the sampler")

    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior", _no_sampling)
    es = _escalated(_set_triples(blank, results), blank)    # no explicit budget_s
    assert any(r.evidence_method == "unresolved" for r in es.records)


def test_unresolved_contender_degrades_selection_to_recorded_aic(clean):
    """The consumer half: an incomplete comparison must not be ranked as if it were complete.
    select_models records ``evidence_budget_exhausted`` and falls back to the AIC winner —
    the lane EIG eligibility already fails closed on — instead of publishing a posterior
    computed over whichever candidates happened to get sampled."""
    from isar.analyze.select import select_models

    ctx, _hyp, _fit = clean
    results = loop.fit_admissible_structures(ctx, errors=False)
    triples = _set_triples(ctx, results)
    es = _escalated(triples, ctx)
    resolved = select_models(results, ctx=None, evidence=es, stamp=False)
    assert resolved.selection_fallback is None                # precondition: the set was fine

    best_lnz = max(r.log_evidence for r in es.records if math.isfinite(r.log_evidence))
    es.records.append(E.EvidenceRecord(
        label="stokes2+bg", log_evidence=float("nan"), log_evidence_err=None,
        evidence_method="unresolved", k_free=7, arena_fingerprint=es.arena_fingerprint,
        prior_spec=(), validity={"log_evidence_upper_bound": best_lnz + 1.0,
                                 "escalation_budget_s": 600.0}))
    degraded = select_models(results, ctx=None, evidence=es, stamp=False)

    assert degraded.selection_fallback == "evidence_budget_exhausted"
    assert degraded.best == min(degraded.scores, key=lambda s: s.aic).label


def test_all_railed_set_escalates_highest_ceiling_first(clean, monkeypatch):
    """No trusted leader exists when every candidate railed: the highest-ceiling candidate
    is sampled first, becomes the leader, and prunes the rest. Pinned with stubbed records
    so only the ordering/leader policy is under test."""
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    strong = replace(fit, label="strong", cost=100.0)
    weak = replace(fit, label="weak", cost=700.0)          # ceiling 300 nats below strong's
    ran = []

    def _stub(h, c, f, b, *, escalation="run", **k):
        if escalation == "defer":
            return E.EvidenceRecord(
                label=f.label, log_evidence=float("nan"), log_evidence_err=None,
                evidence_method="deferred_escalation", k_free=f.k,
                arena_fingerprint=loop.data_likelihood_fingerprint(c), prior_spec=(),
                validity={"log_evidence_upper_bound": -0.5 * f.cost})
        ran.append(f.label)
        return E.EvidenceRecord(
            label=f.label, log_evidence=-0.5 * f.cost - 10.0, log_evidence_err=0.3,
            evidence_method="dynesty", k_free=f.k,
            arena_fingerprint=loop.data_likelihood_fingerprint(c), prior_spec=(),
            validity={"dynesty": {"converged": True}})

    monkeypatch.setattr(E, "structure_evidence", _stub)
    es = _escalated([(hyp, strong, bundle), (hyp, weak, bundle)], ctx)
    assert ran == ["strong"]                               # exactly one sampler run
    assert es.by_label()["strong"].evidence_method == "dynesty"
    assert es.by_label()["weak"].evidence_method == "dominated"


def test_candidate_set_escalations_use_the_scoped_n_live_cut(clean, monkeypatch):
    """Owner ruling 2026-08-08, amended 2026-09-03: post-pruning escalations sample at
    ESCALATION_N_LIVE=32 (128 before the amendment; the ~1.2 nat evidence error at 32 is still
    irrelevant against the 10–40 nat gaps the rung separates, and 128 cost five minutes a fit);
    the direct-call default and the bayes diagnostics tier (D4) stay at 400. An explicit
    caller n_live wins over the cut (setdefault, not override).

    The environment is cleared first, so this pins the PRODUCTION lane: the repo-root conftest
    caps the suite at ``ISAR_ESCALATION_N_LIVE=32`` for cost, and without the delenv this test
    would silently be asserting the test-suite cap rather than what ships."""
    import inspect

    ctx, hyp, fit = clean
    monkeypatch.delenv(E.ESCALATION_N_LIVE_ENV, raising=False)
    bundle = _bundle(hyp, ctx)
    original = E.structure_evidence
    seen = []

    def _stub(h, c, f, b, *, escalation="run", **k):
        if escalation == "defer":
            return E.EvidenceRecord(
                label=f.label, log_evidence=float("nan"), log_evidence_err=None,
                evidence_method="deferred_escalation", k_free=f.k,
                arena_fingerprint=loop.data_likelihood_fingerprint(c), prior_spec=(),
                validity={"log_evidence_upper_bound": -0.5 * f.cost})
        seen.append(k.get("n_live"))
        return E.EvidenceRecord(
            label=f.label, log_evidence=-0.5 * f.cost - 10.0, log_evidence_err=0.3,
            evidence_method="dynesty", k_free=f.k,
            arena_fingerprint=loop.data_likelihood_fingerprint(c), prior_spec=(),
            validity={"dynesty": {"converged": True, "n_live": k.get("n_live")}})

    monkeypatch.setattr(E, "structure_evidence", _stub)
    es = _escalated([(hyp, fit, bundle)], ctx)
    assert seen == [E.ESCALATION_N_LIVE] and E.ESCALATION_N_LIVE == 32
    # the record carries the cut honestly (rides validity into the /2 semantics record)
    assert es.by_label()[fit.label].validity["dynesty"]["n_live"] == 32

    seen.clear()
    _escalated([(hyp, fit, bundle)], ctx, n_live=80)
    assert seen == [80]                                   # explicit caller n_live wins

    # The cap must actually REACH the sampler — that is the whole point of routing it through the
    # environment rather than a patched constant, since a spawned pool child re-imports the module.
    seen.clear()
    monkeypatch.setenv(E.ESCALATION_N_LIVE_ENV, "32")
    es_capped = _escalated([(hyp, fit, bundle)], ctx)
    assert seen == [32]
    assert es_capped.by_label()[fit.label].validity["dynesty"]["n_live"] == 32

    # the two-lane split: direct calls and the bayes diagnostics tier keep D4's 400
    assert inspect.signature(original).parameters["n_live"].default == 400
    assert BZ.DEFAULT_NLIVE == 400


# ------------------------------------------------------------------ parallel escalation (executor=)
def _stub_evidence(ceilings, ran, *, leader_label=None, leader_lnz=-50.0):
    """A ``structure_evidence`` stand-in: ``leader_label`` (if given) answers ``escalation="defer"``
    with a trusted Gaussian-rung record (so the pre-batch leader is not -inf); every other label
    defers with its ``ceilings[label]``, and a ``"run"`` call is deterministic in ``cost`` alone —
    same seed-independent story ``test_all_railed_set_escalates_highest_ceiling_first`` uses — so
    serial and parallel calls of the SAME candidate must land on bit-identical records."""
    def _stub(h, c, f, b, *, escalation="run", **k):
        arena = loop.data_likelihood_fingerprint(c)
        if escalation == "defer":
            if f.label == leader_label:
                return E.EvidenceRecord(
                    label=f.label, log_evidence=leader_lnz, log_evidence_err=None,
                    evidence_method="laplace", k_free=f.k, arena_fingerprint=arena,
                    prior_spec=(), validity={})
            return E.EvidenceRecord(
                label=f.label, log_evidence=float("nan"), log_evidence_err=None,
                evidence_method="deferred_escalation", k_free=f.k, arena_fingerprint=arena,
                prior_spec=(), validity={"log_evidence_upper_bound": ceilings[f.label]})
        ran.append(f.label)
        return E.EvidenceRecord(
            label=f.label, log_evidence=-0.5 * f.cost - 10.0, log_evidence_err=0.3,
            evidence_method="dynesty", k_free=f.k, arena_fingerprint=arena,
            prior_spec=(), validity={"dynesty": {"converged": True}})
    return _stub


def _two_survivor_triples(clean):
    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    a = replace(fit, label="a", cost=100.0)   # ceiling -50.0
    b = replace(fit, label="b", cost=140.0)   # ceiling -70.0, within PRUNE_MARGIN of -inf leader
    return ctx, hyp, bundle, [(hyp, a, bundle), (hyp, b, bundle)]


def test_parallel_escalation_matches_serial(clean, monkeypatch):
    """Same candidates, same seeds: ``executor=`` must yield records equal to the serial call —
    same labels, ``log_evidence``/``log_evidence_err`` exactly equal, same method and validity."""
    from concurrent.futures import ThreadPoolExecutor

    ctx, hyp, bundle, triples = _two_survivor_triples(clean)
    ran_serial, ran_parallel = [], []

    monkeypatch.setattr(E, "structure_evidence", _stub_evidence({"a": -50.0, "b": -70.0}, ran_serial))
    es_serial = _escalated(triples, ctx)

    monkeypatch.setattr(E, "structure_evidence",
                        _stub_evidence({"a": -50.0, "b": -70.0}, ran_parallel))
    with ThreadPoolExecutor(2) as pool:
        es_parallel = _escalated(triples, ctx, executor=pool)

    assert ran_serial == ran_parallel == ["a", "b"]            # both ran under both paths
    rs, rp = es_serial.by_label(), es_parallel.by_label()
    assert set(rs) == set(rp) == {"a", "b"}
    for label in rs:
        assert rs[label].log_evidence == rp[label].log_evidence
        assert rs[label].log_evidence_err == rp[label].log_evidence_err
        assert rs[label].evidence_method == rp[label].evidence_method
        assert rs[label].validity == rp[label].validity


def test_parallel_escalation_still_prunes_before_the_batch(clean, monkeypatch):
    """A candidate whose ceiling sits PRUNE_MARGIN below the leader known before the batch is
    ``dominated`` and never submitted — the pre-batch prune survives parallelization."""
    from concurrent.futures import ThreadPoolExecutor

    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    survivor = replace(fit, label="survivor", cost=100.0)       # ceiling -50.0, near leader -50.0
    survivor2 = replace(fit, label="survivor2", cost=104.0)     # ceiling -52.0, also near leader
    dominated = replace(fit, label="doomed", cost=200.0)        # ceiling -100.0 < -50-30=-80
    triples = [(hyp, replace(fit, label="leader"), bundle),
              (hyp, survivor, bundle), (hyp, survivor2, bundle), (hyp, dominated, bundle)]
    ran = []
    monkeypatch.setattr(E, "structure_evidence", _stub_evidence(
        {"survivor": -50.0, "survivor2": -52.0, "doomed": -100.0}, ran, leader_label="leader"))

    with ThreadPoolExecutor(2) as pool:
        es = _escalated(triples, ctx, executor=pool)

    assert "doomed" not in ran                                 # never submitted to the executor
    assert es.by_label()["doomed"].evidence_method == "dominated"
    assert es.by_label()["survivor"].evidence_method == "dynesty"
    assert es.by_label()["survivor2"].evidence_method == "dynesty"


def test_parallel_escalation_falls_back_to_serial_on_submit_failure(clean, monkeypatch):
    """A broken executor (unpicklable candidate, dead pool) logs a warning and the set still
    completes on the serial path, with the same records the serial call alone would produce."""
    ctx, hyp, bundle, triples = _two_survivor_triples(clean)

    class _BrokenExecutor:
        def submit(self, *a, **k):
            raise RuntimeError("pool is broken")

    ran_serial, ran_fallback = [], []
    monkeypatch.setattr(E, "structure_evidence", _stub_evidence({"a": -50.0, "b": -70.0}, ran_serial))
    es_serial = _escalated(triples, ctx)

    monkeypatch.setattr(E, "structure_evidence",
                        _stub_evidence({"a": -50.0, "b": -70.0}, ran_fallback))
    es_fallback = _escalated(triples, ctx, executor=_BrokenExecutor())

    assert ran_fallback == ["a", "b"]
    rs, rf = es_serial.by_label(), es_fallback.by_label()
    for label in rs:
        assert rs[label].log_evidence == rf[label].log_evidence
        assert rs[label].evidence_method == rf[label].evidence_method


def test_parallel_batch_includes_prune_exempt_penalised_candidate(monkeypatch):
    """A penalised candidate whose Laplace is forced invalid (railed-fixture idiom: force
    ``sigma_pd`` False rather than stub ``structure_evidence`` outright, so the real prune-exempt
    branch runs) must still escalate, never be ``dominated``, and land in the SAME parallel batch
    as a plain candidate — with a record equal to what the serial path alone produces."""
    from concurrent.futures import ThreadPoolExecutor

    constraints = ParamConstraints(soft={"phonon.Gamma": (0.5, 0.2)},
                                   origins={"phonon.Gamma": "transport_test"})
    ctx = _spectrum_ctx(seed=1, constraints=constraints)
    hyp, fit = _fitted(ctx)
    assert fit.prior_penalty is not None                   # the precondition the guard reads
    bundle = _bundle(hyp, ctx)
    penalised = replace(fit, label="penalised", at_limit=None)
    plain = replace(fit, label="plain", at_limit=None, prior_penalty=None)
    triples = [(hyp, penalised, bundle), (hyp, plain, bundle)]

    monkeypatch.setattr(E, "_is_pd", lambda sigma: False)   # every candidate's Laplace is invalid
    sampled = types.SimpleNamespace(converged=True, fail_reason=None, ess=500.0,
                                    log_evidence=-42.0, log_evidence_err=0.3)
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior", lambda *a, **k: sampled)

    es_serial = _escalated(triples, ctx, **FAST)
    with ThreadPoolExecutor(2) as pool:
        es_parallel = _escalated(triples, ctx, executor=pool, **FAST)

    for es in (es_serial, es_parallel):
        rec = es.by_label()["penalised"]
        assert rec.evidence_method == "dynesty"             # escalated, never inline-run at defer
        assert rec.evidence_method != "dominated"
        assert "log_evidence_upper_bound" not in rec.validity

    rs, rp = es_serial.by_label(), es_parallel.by_label()
    assert set(rs) == set(rp) == {"penalised", "plain"}
    for label in rs:
        assert rs[label].log_evidence == rp[label].log_evidence
        assert rs[label].evidence_method == rp[label].evidence_method
        assert rs[label].validity == rp[label].validity


def _stub_exempt_and_ceiling(ran):
    """One prune-exempt deferred marker (label ``"exempt"``, no ceiling) and one ordinary
    ceiling-bearing deferred marker (label ``"ceiling"``); both defer, since neither is Gaussian-
    trusted, so a tiny budget can exhaust before either escalation runs."""
    def _stub(h, c, f, b, *, escalation="run", **k):
        arena = loop.data_likelihood_fingerprint(c)
        if escalation == "defer":
            validity = ({"prune_exempt": True} if f.label == "exempt"
                       else {"log_evidence_upper_bound": -50.0})
            return E.EvidenceRecord(label=f.label, log_evidence=float("nan"),
                                    log_evidence_err=None, evidence_method="deferred_escalation",
                                    k_free=f.k, arena_fingerprint=arena, prior_spec=(),
                                    validity=validity)
        ran.append(f.label)
        return E.EvidenceRecord(
            label=f.label, log_evidence=-0.5 * f.cost - 10.0, log_evidence_err=0.3,
            evidence_method="dynesty", k_free=f.k, arena_fingerprint=arena,
            prior_spec=(), validity={"dynesty": {"converged": True}})
    return _stub


def test_prune_exempt_candidate_ignores_the_budget(clean, monkeypatch):
    """A prune-exempt candidate must always be sampled: with a tiny (exhausted) budget it is
    still "dynesty", never "unresolved" — an unresolved contender can force select_models onto
    the AIC fallback, and the whole point of deferring a penalised fit is that its own posterior
    IS the number selection needs. A same-batch ceiling-ranked candidate still budgets out.
    Serial and parallel must agree."""
    from concurrent.futures import ThreadPoolExecutor

    ctx, hyp, fit = clean
    bundle = _bundle(hyp, ctx)
    triples = [(hyp, replace(fit, label="exempt", cost=90.0), bundle),
              (hyp, replace(fit, label="ceiling", cost=100.0), bundle)]

    ran_serial, ran_parallel = [], []
    monkeypatch.setattr(E, "structure_evidence", _stub_exempt_and_ceiling(ran_serial))
    es_serial = _escalated(triples, ctx, budget_s=0.0)

    monkeypatch.setattr(E, "structure_evidence", _stub_exempt_and_ceiling(ran_parallel))
    with ThreadPoolExecutor(2) as pool:
        es_parallel = _escalated(triples, ctx, executor=pool, budget_s=0.0)

    for es, ran in ((es_serial, ran_serial), (es_parallel, ran_parallel)):
        recs = es.by_label()
        assert recs["exempt"].evidence_method == "dynesty"
        assert recs["ceiling"].evidence_method == "unresolved"
        assert ran == ["exempt"]                          # only the exempt candidate was sampled

    # "Agree" on the outcome (method, and lnZ when either has one) — not on every validity field:
    # the "ceiling" candidate's ``leader_log_evidence`` legitimately differs, since the parallel
    # path's budget check runs pre-batch (finding 2) while the serial path's per-item check can
    # see a leader the exempt candidate already advanced by running first.
    rs, rp = es_serial.by_label(), es_parallel.by_label()
    for label in rs:
        a, b = rs[label].log_evidence, rp[label].log_evidence
        assert (math.isnan(a) and math.isnan(b)) or a == b
        assert rs[label].evidence_method == rp[label].evidence_method


def test_parallel_escalation_cancels_pending_futures_on_partial_submit_failure(clean, monkeypatch):
    """submit() succeeding once and then raising must cancel the already-submitted (still
    pending) future before falling back to serial, so no orphaned duplicate is left
    unaccounted for. Records still equal the serial path."""
    from concurrent.futures import Future

    ctx, hyp, bundle, triples = _two_survivor_triples(clean)

    class _TrackingFuture(Future):
        def cancel(self):
            self.cancel_called = True
            return super().cancel()

    class _PartialExecutor:
        def __init__(self):
            self.futures = []

        def submit(self, fn, args):
            if not self.futures:
                fut = _TrackingFuture()
                fut.cancel_called = False
                self.futures.append(fut)
                return fut
            raise RuntimeError("submit failed on the second candidate")

    ran_serial, ran_fallback = [], []
    monkeypatch.setattr(E, "structure_evidence", _stub_evidence({"a": -50.0, "b": -70.0}, ran_serial))
    es_serial = _escalated(triples, ctx)

    monkeypatch.setattr(E, "structure_evidence",
                        _stub_evidence({"a": -50.0, "b": -70.0}, ran_fallback))
    partial = _PartialExecutor()
    es_partial = _escalated(triples, ctx, executor=partial)

    assert len(partial.futures) == 1
    assert partial.futures[0].cancel_called and partial.futures[0].cancelled()

    assert ran_fallback == ["a", "b"]                      # serial fallback still ran both
    rs, rp = es_serial.by_label(), es_partial.by_label()
    for label in rs:
        assert rs[label].log_evidence == rp[label].log_evidence
        assert rs[label].evidence_method == rp[label].evidence_method


# ------------------------------------------------------------------ generalized bayes lane
def test_prior_bundle_ppf_drives_the_sampler(clean):
    """The bundle enters via the prior transform: a near-delta prior on Ω_d pins the sampled
    column at its μ regardless of the likelihood's preference."""
    ctx, hyp, fit = clean
    prob = loop.build_problem(hyp, ctx)
    target = 3.0
    params = tuple(
        C.ResolvedPrior(p.name, "gaussian", target, 1e-4, p.lo, p.hi, "identity")
        if p.name == "phonon.omega0" else p
        for p in _bundle(hyp, ctx).params)
    bz = BZ.bayesian_posterior(hyp, ctx, fit, prior_bundle=C.PriorBundle(params), **FAST)
    od_i = bz.parameter_names.index("phonon.omega0")
    draws = bz.parameter_samples[:, od_i]
    assert np.all(np.abs(draws - target) < 1e-2)
    assert bz.prior_used is False                         # the ω₀ diagnostics tier is untouched
    del prob


def test_prior_and_prior_bundle_are_mutually_exclusive(clean):
    ctx, hyp, fit = clean
    from isar.analyze.prior import PhononPrior
    with pytest.raises(ValueError, match="mutually exclusive"):
        BZ.bayesian_posterior(hyp, ctx, fit, prior=PhononPrior(omega0=5.0, omega0_sigma=0.5),
                              prior_bundle=_bundle(hyp, ctx), **FAST)


def test_prior_bundle_holds_a_hard_fixed_coordinate_and_never_integrates_it():
    """A frozen coordinate must never become an integrated dimension: the sampler works in the
    free subspace with the frozen value held in every draw (so a hard-fixed catalogue variant is
    still judged in rung 2), and a bundle carrying a prior for the frozen name is refused."""
    ctx = _spectrum_ctx(constraints=ParamConstraints(hard={"phonon.Gamma": 0.4},
                                                     origins={"phonon.Gamma": "test"}))
    hyp, fit = _fitted(ctx)
    prob = loop.build_problem(hyp, ctx)
    full = C.resolve_priors(prob.composite, ctx, prob.bounds)   # covers the frozen param too
    with pytest.raises(ValueError, match="hard-fixed"):
        BZ.bayesian_posterior(hyp, ctx, fit, prior_bundle=full, **FAST)
    bz = BZ.bayesian_posterior(hyp, ctx, fit, prior_bundle=_bundle(hyp, ctx), **FAST)
    assert np.isfinite(bz.log_evidence)
    gamma = bz.parameter_samples[:, list(bz.parameter_names).index("phonon.Gamma")]
    assert np.all(gamma == 0.4)
    assert bz.max_likelihood_sample["phonon.Gamma"] == 0.4
    assert bz.prior_specification["hard_fixed"] == {"phonon.Gamma": 0.4}
    assert bz.prior_fingerprint is not None


def test_prior_bundle_must_cover_every_sampled_parameter(clean):
    ctx, hyp, fit = clean
    bundle = C.PriorBundle(_bundle(hyp, ctx).params[:-1])
    with pytest.raises(ValueError, match="no prior for sampled parameter"):
        BZ.bayesian_posterior(hyp, ctx, fit, prior_bundle=bundle, **FAST)


# ------------------------------------------------------------------ at_limit persistence
def test_fit_persists_per_param_at_limit(clean):
    _ctx, _hyp, fit = clean
    assert isinstance(fit.at_limit, dict)
    assert set(fit.at_limit) == set(fit.params)           # real dotted names, every param
    assert all(isinstance(v, bool) for v in fit.at_limit.values())


# ------------------------------------------------------------------ ordered mode energies
def _two_mode_ctx():
    x = np.linspace(-10.0, 12.0, 111)
    lam = (convolute_voigt_dho(x, 3.0, 0.0, 1.0, 0.0, 0.4, 300.0, 80.0, 0.0, 100.0, peak="all")
           + convolute_voigt_dho(x, 8.0, 0.0, 1.0, 0.0, 0.4, 200.0, 0.0, 15.0, 100.0, peak="all"))
    counts = np.random.default_rng(1).poisson(np.clip(lam, 0.0, None)).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=100.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


@pytest.fixture(scope="module")
def two_mode():
    from isar.analyze.phonon import placed_modes_ic

    ctx = _two_mode_ctx()
    hyp = {h.label: h for h in C.enumerate_count_structures(ctx, 2)}["elastic+stokes2+bg"]
    x0 = placed_modes_ic(hyp.build(), ctx, [3.0, 8.0])
    return ctx, hyp, loop.fit_hypothesis(hyp, ctx, x0=x0)


def test_laplace_lane_prices_two_modes_under_the_ordered_prior(two_mode):
    """Laplace covers one labelling of the modes, so its prior is the ordered one: ln 2! above the
    per-label flat product for two exchangeable modes, exactly once."""
    ctx, hyp, fit = two_mode
    assert fit.optimizer_converged and fit.covariance_ok
    bundle = _bundle(hyp, ctx)
    rec = E.structure_evidence(hyp, ctx, fit, bundle)
    assert rec.evidence_method == "laplace"
    sigma = E._free_submatrix(fit.parameter_covariance, list(fit.params),
                              [p.name for p in bundle.params])
    flat = E.laplace_ln_z(fit.cost, bundle.logpdf(fit.params), sigma)
    assert rec.log_evidence - flat == pytest.approx(math.log(2.0), abs=1e-9)


def test_nested_lane_samples_ordered_mode_energies(two_mode):
    """The sampler draws the mode energies as ordered statistics (every draw ascending) and the
    prior specification records which energies are ordered."""
    ctx, hyp, fit = two_mode
    bz = BZ.bayesian_posterior(hyp, ctx, fit, prior_bundle=_bundle(hyp, ctx), n_live=40,
                               dlogz=5.0, sample="unif", seed=3, max_iter=120)
    names = list(bz.parameter_names)
    first, second = names.index("phonon.omega0"), names.index("phonon2.omega0")
    draws = bz.parameter_samples
    assert draws.shape[0] > 0
    assert np.all(draws[:, first] <= draws[:, second])
    assert bz.prior_specification["mode_energy_order"] == [["phonon.omega0", "phonon2.omega0"]]


# ------------------------------------------------------------------ rung 1 of the count ladder
def test_rung_one_never_samples_or_prunes_a_laplace_invalid_candidate(clean, monkeypatch):
    """Rung 1 is Laplace only: the railed elastic+bg null keeps its deferred_escalation marker (no
    evidence), is never sampled, and is not pruned on its -cost/2 ceiling, which bounds lnZ only at
    a global optimum. Selection keeps it in its scores, with no weight, and names the winner."""
    from isar.analyze.select import select_models

    ctx, _hyp, _fit = clean
    results = loop.fit_admissible_structures(ctx, errors=False)

    def _no_sampling(*a, **k):
        raise AssertionError("rung 1 must not sample")

    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior", _no_sampling)
    triples = _set_triples(ctx, results)
    es = E.candidate_set_evidence(triples, ctx)
    rec = es.by_label()["elastic+bg"]
    assert rec.evidence_method == "deferred_escalation"
    assert math.isnan(rec.log_evidence)
    assert "dominated_by_leader_log_evidence" not in rec.validity
    assert {r.label for r in es.records} == {fit.label for _, fit, _ in triples}
    sel = select_models(results, {h.label: h for h, _, _ in triples}, ctx, stamp=False)
    score = {s.label: s for s in sel.scores}["elastic+bg"]
    assert score.evidence_method == "deferred_escalation"
    assert score.posterior_probability == 0.0
    assert sel.selection_fallback is None
    assert sel.best == "elastic+stokes+bg"
