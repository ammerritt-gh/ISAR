"""Phase 2c — the LOW-q rescue at the fit level: a branch soft-constraint pins Γ and recovers ω₀
where the acoustic peak has merged with the elastic line. (Campaign-level validation is separate.)"""
from dataclasses import replace

import numpy as np
import pytest

from isar.analyze import loop
from isar.analyze.components import ConstantBackground, PhononDHO
from isar.analyze.context import ScanContext
from isar.analyze.structures import Hypothesis
from isar.schema.steering import ParamConstraints
from isar.session import branch_model as BM

_HYP = Hypothesis("stokes+bg", (PhononDHO, ConstantBackground))


def _low_q_ctx(omega0=1.3, gamma=0.5, amp=400.0, bg=20.0, fwhm=1.5, seed=0):
    """A near-zone-centre spectrum: ω₀ within ~1 FWHM of the elastic line (merging regime)."""
    x = np.linspace(-6.0, 6.0, 81)
    base = ScanContext(x=x, counts=np.zeros_like(x), mask=np.ones_like(x, bool), T=100.0,
                       center=0.0, gauss_fwhm=fwhm, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="absolute")
    od = np.sqrt(max(omega0 ** 2 - gamma ** 2, 1e-6))
    model = _HYP.build().evaluate(x, np.array([od, gamma, amp, bg]), base)
    counts = np.random.default_rng(seed).poisson(np.clip(model, 0, None)).astype(float)
    return replace(base, counts=counts)


def _branch(gamma=0.5):
    """A branch built from well-separated mid-q anchors with Γ≈``gamma`` and a 1/ω intensity."""
    pts = [{"scan_id": f"S:{i}", "sample": "S", "T": 100.0, "t_bin": 100.0, "q": [3, 0.2, 0],
            "omega0": w, "modality": "fold", "verdict": "resolved", "gamma": gamma,
            "intensity": {"value": 300.0 * 4.0 / w, "gauss_fwhm": 1.5}}
           for i, w in enumerate((3.0, 4.0, 5.0, 6.0))]
    return BM.build_branch(pts, sample="S", t_bin=100.0)


def test_low_q_branch_constraint_pins_gamma_and_tags():
    ctx = _low_q_ctx(omega0=1.3, gamma=0.5)
    pred = BM.predict(_branch(gamma=0.5), 1.3)
    # Γ-only soft constraint (A validated separately; avoids exposure/amplitude scale coupling here).
    cons = ParamConstraints(soft={"phonon.Gamma": (pred.gamma, pred.gamma_sigma)})

    r0 = loop.fit_hypothesis(_HYP, ctx)
    rc = loop.fit_hypothesis(_HYP, replace(ctx, constraints=cons))

    assert rc.fit_kind == "soft_prior" and r0.fit_kind == "unconstrained"
    # Γ is pinned near the branch value (within a few branch σ) ...
    assert abs(rc.params["phonon.Gamma"] - pred.gamma) <= 3.0 * pred.gamma_sigma
    # ... and ω₀ is recovered (near truth) under the constraint.
    assert abs(rc.omega0 - 1.3) <= 0.6
    # data cost stays the reported GOF (contract 1): posterior_cost carries the penalty, not cost.
    assert rc.posterior_cost == pytest.approx(rc.cost + rc.prior_penalty)


def test_driver_trigger_threshold_is_low_q_only():
    """The driver applies the branch only when the predicted ω₀ is within LOW_Q_FWHM_FACTOR·FWHM."""
    from isar.session import driver
    fwhm = 1.5
    assert 1.3 < driver.LOW_Q_FWHM_FACTOR * fwhm        # low-q → would trigger
    assert not (5.0 < driver.LOW_Q_FWHM_FACTOR * fwhm)  # mid-q → left unconstrained
