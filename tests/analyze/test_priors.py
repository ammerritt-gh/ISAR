"""The catalogue prior machinery (SCENE-1 PR-b): proper, truncated, applied exactly once.

Every candidate parameter resolves to exactly one prior factor — a transported soft constraint
IS the prior for its parameter (never doubled with an entry declaration), entry declarations
match by suffix, and everything else is normalized flat over the optimizer bounds so lnZ carries
an honest Occam factor.
"""
import math

import numpy as np
import pytest
from scipy import stats
from scipy.integrate import quad

from isar.analyze import catalogue
from isar.analyze.components import CompositeModel, ConstantBackground, PhononDHO
from isar.analyze.context import ScanContext
from isar.schema.steering import ParamConstraints


def _ctx(**kw):
    x = np.linspace(-10.0, 10.0, 81)
    defaults = dict(x=x, counts=np.full(81, 10.0), mask=np.ones(81, dtype=bool), T=100.0,
                    center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                    energy_status="needs_zero")
    defaults.update(kw)
    return ScanContext(**defaults)


def _composite():
    return CompositeModel([PhononDHO(), ConstantBackground()])


_BOUNDS = [(1e-3, 10.0), (1e-3, 5.0), (0.0, 1e4), (0.0, 100.0)]


def test_default_is_normalized_flat_over_bounds():
    bundle = catalogue.resolve_priors(_composite(), _ctx(), _BOUNDS)
    assert [p.name for p in bundle.params] == ["phonon.omega0", "phonon.Gamma",
                                               "phonon.h", "bg.BG"]
    # re-pin (recipe /5): the width default is the softened log-uniform, not flat; (recipe /6) the
    # energy default is the penalised energy density at the context's resolution FWHM
    got = {q.name: (q.kind, q.origin) for q in bundle.params}
    assert got["phonon.Gamma"] == ("softlog", "default_softlog")
    assert got["phonon.omega0"] == ("energy_ramp", "default_energy")
    assert all(v == ("flat", "default_flat") for n, v in got.items()
               if n not in ("phonon.Gamma", "phonon.omega0"))
    p = bundle.by_name()["phonon.omega0"]
    from isar.analyze import prior
    assert p.energy == prior.energy_prior(1e-3, 10.0, 1.0, prior.PriorSettings())
    total, _ = quad(lambda v: math.exp(p.logpdf(v)), p.lo, p.hi, points=[0.2, 0.3, 0.95, 1.05])
    assert abs(total - 1.0) < 1e-9
    assert p.logpdf(p.lo - 1.0) == float("-inf")
    assert p.logpdf(0.1) == pytest.approx(p.logpdf(5.0) - 7.5)     # the ceiling below 1/4 FWHM
    assert p.ppf(0.0) == p.lo and p.ppf(1.0) == p.hi
    assert bundle.nontrivial == ()
    p = bundle.by_name()["bg.BG"]
    total, _ = quad(lambda v: math.exp(p.logpdf(v)), p.lo, p.hi)
    assert abs(total - 1.0) < 1e-9


def test_gaussian_prior_matches_truncated_scipy():
    p = catalogue.ResolvedPrior("phonon.omega0", "gaussian", 4.0, 1.0, 1e-3, 10.0, "identity")
    dist = stats.norm(loc=4.0, scale=1.0)
    z = dist.cdf(10.0) - dist.cdf(1e-3)
    for v in (0.5, 2.0, 4.0, 9.0):
        assert abs(p.logpdf(v) - (dist.logpdf(v) - math.log(z))) < 1e-12
    total, _ = quad(lambda v: math.exp(p.logpdf(v)), p.lo, p.hi)
    assert abs(total - 1.0) < 1e-8


def test_lognormal_prior_matches_truncated_scipy_and_median():
    p = catalogue.ResolvedPrior("phonon.h", "lognormal", 50.0, 0.5, 0.0, 1e4, "identity")
    dist = stats.lognorm(s=0.5, scale=50.0)
    z = dist.cdf(1e4) - dist.cdf(0.0)
    assert abs(p.logpdf(50.0) - (dist.logpdf(50.0) - math.log(z))) < 1e-12
    # mu is the linear-space median
    assert abs(p.ppf(0.5 * z / z) - dist.ppf(dist.cdf(0.0) + 0.5 * z)) < 1e-9


@pytest.mark.parametrize("kind,mu,sigma", [("gaussian", 4.0, 1.0), ("lognormal", 50.0, 0.5),
                                           ("flat", None, None), ("softlog", 0.12, None)])
def test_ppf_roundtrips_through_the_truncated_cdf(kind, mu, sigma):
    lo, hi = (1e-3, 10.0) if kind != "lognormal" else (1.0, 1e4)
    p = catalogue.ResolvedPrior("x.p", kind, mu, sigma, lo, hi, "identity")
    for u in (0.01, 0.25, 0.5, 0.75, 0.99):
        v = p.ppf(u)
        assert lo <= v <= hi
        if kind == "flat":
            u_back = (v - lo) / (hi - lo)
        elif kind == "softlog":       # closed-form CDF of 1/(x + mu), truncated to [lo, hi]
            u_back = math.log((v + mu) / (lo + mu)) / math.log((hi + mu) / (lo + mu))
        else:
            dist = stats.norm(loc=mu, scale=sigma) if kind == "gaussian" \
                else stats.lognorm(s=sigma, scale=mu)
            z = dist.cdf(hi) - dist.cdf(lo)
            u_back = (dist.cdf(v) - dist.cdf(lo)) / z
        assert abs(u_back - u) < 1e-9


def test_softlog_is_normalised_exactly_on_lo_to_hi():
    # recipe /7: the density is proper on the optimiser's own interval [lo, hi]; the mass below lo
    # (~1.6e-3, left out of the /6 normalisation) is no longer shed
    p = catalogue.ResolvedPrior("phonon.Gamma", "softlog", 0.12, None, 1e-3, 20.0, "default_softlog")
    pdf = lambda v: math.exp(p.logpdf(v))
    assert quad(pdf, 1e-3, 1.0)[0] + quad(pdf, 1.0, 20.0)[0] == pytest.approx(1.0, abs=1e-9)
    assert p.logpdf(0.5e-3) == float("-inf") and p.logpdf(20.5) == float("-inf")
    # one width density on both paths: the fast pass's SpectrumPrior is the same function
    from isar.analyze.prior import resolve_prior
    sp = resolve_prior([5.0, 9.0], [1.0, 9.0], fwhm=1.0)
    for v in (1e-3, 0.3, 4.0, 19.0):
        assert p.logpdf(v) == pytest.approx(sp.ln_width(v), abs=1e-12)


def test_ctx_attribute_resolution_and_fail_loud():
    entry = catalogue.PhenomenonDefinition(
        name="probe", identity="test entry",
        local_models=(catalogue.LocalModel("dho", PhononDHO),),
        parameter_priors=(catalogue.ParamPrior("omega0", "gaussian",
                                               mu="prior_omega", sigma="gauss_fwhm"),))
    bundle = catalogue.resolve_priors(_composite(), _ctx(prior_omega=6.0), _BOUNDS, entry=entry)
    p = bundle.by_name()["phonon.omega0"]
    assert p.kind == "gaussian" and p.origin == "identity"
    assert p.mu == 6.0 and p.sigma == 1.0                     # gauss_fwhm from ctx
    with pytest.raises(ValueError, match="prior_omega"):
        catalogue.resolve_priors(_composite(), _ctx(), _BOUNDS, entry=entry)  # attr is None


def test_transport_is_the_prior_never_doubled():
    """A transported soft constraint replaces any entry declaration for that parameter —
    exactly one prior factor per parameter (H4/D5)."""
    entry = catalogue.PhenomenonDefinition(
        name="probe", identity="test entry",
        local_models=(catalogue.LocalModel("dho", PhononDHO),),
        parameter_priors=(catalogue.ParamPrior("h", "lognormal", mu=99.0, sigma=1.0),))
    constraints = ParamConstraints(soft={"phonon.h": (40.0, 8.0)},
                                   origins={"phonon.h": "prior_predictive"})
    bundle = catalogue.resolve_priors(_composite(), _ctx(constraints=constraints), _BOUNDS,
                                      entry=entry)
    p = bundle.by_name()["phonon.h"]
    assert p.kind == "gaussian" and p.origin == "transport"   # the constraint won
    assert (p.mu, p.sigma) == (40.0, 8.0)
    assert p.origin_detail == "prior_predictive"
    assert sum(q.name == "phonon.h" for q in bundle.params) == 1


def test_hard_fixed_parameters_contribute_no_prior_factor():
    bundle = catalogue.resolve_priors(_composite(), _ctx(), _BOUNDS,
                                      exclude=("phonon.Gamma",))
    assert "phonon.Gamma" not in bundle.by_name()
    assert len(bundle.params) == 3


def test_bundle_logpdf_sums_and_spec_is_declarative():
    constraints = ParamConstraints(soft={"phonon.h": (40.0, 8.0)},
                                   origins={"phonon.h": "prior_predictive"})
    bundle = catalogue.resolve_priors(_composite(), _ctx(constraints=constraints), _BOUNDS)
    values = {"phonon.omega0": 4.0, "phonon.Gamma": 0.4, "phonon.h": 40.0, "bg.BG": 10.0}
    # recipe /7: a width's density is conditional on its mode's ceiling min(p.hi, omega0) = 4 here
    expected = sum(p.logpdf_given(values[p.name], values["phonon.omega0"]) if p.name.endswith(".Gamma")
                   else p.logpdf(values[p.name]) for p in bundle.params)
    assert abs(bundle.logpdf(values) - expected) < 1e-12
    assert bundle.logpdf(values) != sum(p.logpdf(values[p.name]) for p in bundle.params)
    spec = bundle.spec()
    assert [row["name"] for row in spec] == [p.name for p in bundle.params]
    assert all(set(row) == {"name", "kind", "mu", "sigma", "lo", "hi", "origin",
                            "origin_detail", "energy"} for row in spec)
    # the energy row carries the ramp, so the sampler's prior fingerprint moves with it
    assert spec[0]["energy"] == bundle.params[0].energy.spec() and spec[1]["energy"] is None
    assert bundle.nontrivial == (bundle.by_name()["phonon.h"],)


def test_invalid_hyperparameters_fail_at_resolution_time():
    """A broken prior must be refused when resolved, never fail later inside logpdf/dynesty."""
    bad_soft = ParamConstraints(soft={"phonon.h": (40.0, 0.0)},
                                origins={"phonon.h": "prior_predictive"})
    with pytest.raises(ValueError, match="sigma > 0"):
        catalogue.resolve_priors(_composite(), _ctx(constraints=bad_soft), _BOUNDS)
    nan_soft = ParamConstraints(soft={"phonon.h": (float("nan"), 5.0)},
                                origins={"phonon.h": "prior_predictive"})
    with pytest.raises(ValueError, match="non-finite"):
        catalogue.resolve_priors(_composite(), _ctx(constraints=nan_soft), _BOUNDS)
    entry = catalogue.PhenomenonDefinition(
        name="probe", identity="test entry",
        local_models=(catalogue.LocalModel("dho", PhononDHO),),
        parameter_priors=(catalogue.ParamPrior("h", "lognormal", mu=-1.0, sigma=0.5),))
    with pytest.raises(ValueError, match="lognormal.*mu > 0"):
        catalogue.resolve_priors(_composite(), _ctx(), _BOUNDS, entry=entry)


def test_stale_declaration_suffix_fails_loudly():
    """A declared suffix that matches no free parameter is a stale/misspelled declaration."""
    entry = catalogue.PhenomenonDefinition(
        name="probe", identity="test entry",
        local_models=(catalogue.LocalModel("dho", PhononDHO),),
        parameter_priors=(catalogue.ParamPrior("Amplitude", "gaussian", mu=1.0, sigma=1.0),))
    with pytest.raises(ValueError, match="Amplitude"):
        catalogue.resolve_priors(_composite(), _ctx(), _BOUNDS, entry=entry)
    # ...but a declaration overridden by transport (or excluded as hard-fixed) is consumed, not stale
    entry_a = catalogue.PhenomenonDefinition(
        name="probe", identity="test entry",
        local_models=(catalogue.LocalModel("dho", PhononDHO),),
        parameter_priors=(catalogue.ParamPrior("h", "gaussian", mu=40.0, sigma=8.0),))
    constraints = ParamConstraints(soft={"phonon.h": (40.0, 8.0)},
                                   origins={"phonon.h": "prior_predictive"})
    bundle = catalogue.resolve_priors(_composite(), _ctx(constraints=constraints), _BOUNDS,
                                      entry=entry_a)
    assert bundle.by_name()["phonon.h"].origin == "transport"
