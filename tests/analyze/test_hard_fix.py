"""Phase 3b — hard-fix escalation (branch_conditional).

At true low-count degeneracy the branch model freezes Γ to its branch value, dropping it from
the free set (k reduced). Freezing is applied only at the optimiser boundary (loop._minimize_fixed):
the composite stays full-vector, so the positional per-phonon machinery is untouched, and an
unconstrained fit is bit-identical. The result is tagged ``branch_conditional``.
"""
import numpy as np

from isar.analyze import loop
from isar.analyze.context import ScanContext
from isar.analyze.structures import enumerate_structures
from isar.schema.steering import ParamConstraints
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
