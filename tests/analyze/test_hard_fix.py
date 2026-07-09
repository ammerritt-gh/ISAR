"""Phase 3b — hard-fix escalation (branch_conditional).

At true low-count degeneracy the branch model freezes Γ to its branch value, dropping it from
the free set (k reduced). Freezing is applied only at the optimiser boundary (loop._minimize_fixed):
the composite stays full-vector, so the positional per-phonon machinery is untouched, and an
unconstrained fit is bit-identical. The result is tagged ``branch_conditional``.
"""
import types

import numpy as np

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.structures import enumerate_structures
from isar.schema.steering import ParamConstraints
from isar.session import driver
from isar.synth._ff import convolute_voigt_dho


def _ctx(constraints=None, center=-0.4, n=81):
    x = np.linspace(-10.0, 10.0, n)
    counts = convolute_voigt_dho(x, 5.0, center, 1.1, 0.0, 0.45, 400.0, 70.0, 12.0, 150.0,
                                 peak="all")
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=150.0,
                       center=center, gauss_fwhm=1.1, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero", prior_omega=5.0, constraints=constraints)


def _hyp(ctx, label="elastic+stokes+bg"):
    return {h.label: h for h in enumerate_structures(ctx)}[label]


# --------------------------------------------------------------------------- build_problem
def test_hard_fix_reduces_k_and_records_positions():
    ctx = _ctx(ParamConstraints(hard={"phonon.Gamma": 0.45}))
    prob = loop.build_problem(_hyp(ctx), ctx)
    full = prob.composite.param_names()
    assert prob.free_names == full                         # names stay full (positional layer intact)
    assert prob.k == len(full) - 1                         # …but k drops
    assert prob.ndof == prob.n_used - (len(full) - 1)
    assert prob.fixed_pos == [full.index("phonon.Gamma")]
    assert prob.fixed_vals == [0.45]
    assert prob.is_hard_fixed


def test_no_constraints_is_noop():
    ctx = _ctx()
    prob = loop.build_problem(_hyp(ctx), ctx)
    assert prob.k == len(prob.composite.param_names())
    assert prob.fixed_pos == [] and not prob.is_hard_fixed


def test_unknown_hard_name_ignored():
    ctx = _ctx(ParamConstraints(hard={"nope.X": 1.0}))
    prob = loop.build_problem(_hyp(ctx), ctx)
    assert prob.fixed_pos == [] and not prob.is_hard_fixed


# --------------------------------------------------------------------------- fit end-to-end
def test_fit_freezes_gamma_tags_branch_conditional_and_recovers_omega0():
    gfix = 0.45
    ctx = _ctx(ParamConstraints(hard={"phonon.Gamma": gfix}))
    res = loop.fit_hypothesis(_hyp(ctx), ctx, minos=False)
    assert res.fit_kind == "branch_conditional"
    assert res.optimizer_converged
    assert res.params["phonon.Gamma"] == gfix              # frozen exactly at the branch value
    assert res.param_errors["phonon.Gamma"] == 0.0         # zero variance for a fixed param
    assert res.param_errors["phonon.Omega_d"] and res.param_errors["phonon.Omega_d"] > 0
    assert res.k == 4                                       # elastic+stokes+bg is k=5 → 4 free
    assert abs(res.omega0 - 5.0) < 0.2


def test_hard_fixed_fit_matches_unconstrained_when_value_is_truth():
    """Freezing Γ at (near) its unconstrained best value leaves ω₀ essentially unchanged — the
    reduction is honest, not a bias injection."""
    base = _ctx()
    free = loop.fit_hypothesis(_hyp(base), base, minos=False)
    gfix = float(free.params["phonon.Gamma"])
    ctx = _ctx(ParamConstraints(hard={"phonon.Gamma": gfix}))
    fixed = loop.fit_hypothesis(_hyp(ctx), ctx, minos=False)
    assert abs(fixed.omega0 - free.omega0) < 1e-2
    assert fixed.k == free.k - 1


# --------------------------------------------------------------------------- driver escalation guard
def _escalation_env(monkeypatch):
    """A branch that would be a valid hard-fix target (in-range, Γ>0) and a fit_one spy — so any
    escalation attempt is observable as a fit_one call."""
    monkeypatch.setattr(driver.branch_model, "branch_for",
                        lambda *a, **k: types.SimpleNamespace(in_anchor_range=True, gamma=0.6))
    calls = []

    def _spy(*a, **k):
        calls.append((a, k))
        return {"omega0": 5.0, "gamma_at_bound": False}

    monkeypatch.setattr(driver, "fit_one", _spy)
    return calls


def test_escalation_skipped_when_gamma_already_hard_fixed(monkeypatch):
    """A near-Γ branch_conditional fit already hard-fixes phonon.Gamma (and phonon.A). gamma_at_bound
    is computed from the fitted Γ regardless of constraints, so it can still read True — but re-freezing
    an already-frozen Γ rescues nothing and would drop the existing hard A. The guard must skip it."""
    calls = _escalation_env(monkeypatch)
    ctx = _ctx(ParamConstraints(hard={"phonon.Gamma": 0.6, "phonon.A": 400.0}))
    fit = {"gamma_at_bound": True, "omega0": 5.0}
    prior = types.SimpleNamespace(omega0=5.0)
    region_reasons = []
    out = driver._hard_fix_escalation(
        fit, ctx, prior, card=None, sample="s", T=150.0, labels=None, peaks=None,
        descriptor=None, region_reasons=region_reasons, M=10, seed=0, errors_tier="fast",
        boot_workers=1, boot_executor=None, boot_adaptive=False)
    assert out is fit                                      # original fit unchanged
    assert calls == []                                     # no re-fit attempted
    assert region_reasons == []                            # no escalation provenance note


def test_escalation_fires_when_gamma_only_soft(monkeypatch):
    """Positive control: with Γ merely soft-constrained (not hard) and railed to bound, the escalation
    proceeds — proving the new guard keys specifically on an already-hard Γ, not on any constraint."""
    calls = _escalation_env(monkeypatch)
    ctx = _ctx(ParamConstraints(soft={"phonon.Gamma": (0.6, 0.2)}))
    fit = {"gamma_at_bound": True, "omega0": 5.0}
    prior = types.SimpleNamespace(omega0=5.0)
    card = types.SimpleNamespace(state=None)               # branch_for is monkeypatched; state unused
    region_reasons = []
    driver._hard_fix_escalation(
        fit, ctx, prior, card=card, sample="s", T=150.0, labels=None, peaks=None,
        descriptor=None, region_reasons=region_reasons, M=10, seed=0, errors_tier="fast",
        boot_workers=1, boot_executor=None, boot_adaptive=False)
    assert len(calls) == 1                                 # escalation attempted a re-fit
    assert region_reasons and "hard-fix escalation" in region_reasons[0]


# --------------------------------------------------------------------------- _minimize_fixed noop
def test_minimize_fixed_empty_delegates_unchanged():
    ctx = _ctx()
    prob = loop.build_problem(_hyp(ctx), ctx)
    be = [loop._sanitize(n) for n in prob.free_names]
    from isar.synth import backends
    ref = backends.minimize("iminuit", prob.objective, prob.x0, prob.bounds, be, minos=False)
    got = loop._minimize_fixed("iminuit", prob.objective, prob.x0, prob.bounds, be, [], [],
                               minos=False)
    assert np.allclose(got.x, ref.x)
    assert got.success == ref.success
