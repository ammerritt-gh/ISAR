"""The visual audit overlays the expert's (provisional) fit on the data next to ISAR's. To draw the
expert curve we reconstruct it from the expert's reported ω₀/Γ/center/resolution by solving only the
linear amplitudes (NNLS) at those fixed nonlinear values — the same component model and amplitude
solve ISAR uses. This tests that pure reconstruction recovers a model it was built from."""
from types import SimpleNamespace

import numpy as np
import pytest

from isar.analyze.components import CompositeModel, ConstantBackground, Elastic, PhononDHO
from isar.report import audit


def _ctx(center=0.2, gfw=1.0, lfw=0.5, T=120.0):
    return SimpleNamespace(center=center, gauss_fwhm=gfw, lorentz_fwhm=lfw, T=T)


def _truth(x, ctx, omega_d, gamma, A, E, BG):
    comp = CompositeModel([PhononDHO(), Elastic(), ConstantBackground()])
    return comp.evaluate(x, [omega_d, gamma, A, E, BG], ctx)


def test_reconstruct_curve_recovers_a_known_model():
    x = np.linspace(-8.0, 12.0, 90)
    ctx = _ctx()
    omega_d, gamma, A, E, BG = 5.0, 1.0, 60.0, 200.0, 4.0
    y = _truth(x, ctx, omega_d, gamma, A, E, BG)
    omega0 = float(np.hypot(omega_d, gamma))

    rc = audit.reconstruct_curve(x, y, omega0=omega0, gamma=gamma, center=ctx.center,
                                 gauss_fwhm=ctx.gauss_fwhm, lorentz_fwhm=ctx.lorentz_fwhm, T=ctx.T)

    # NNLS recovers the amplitudes -> the reconstructed total matches the data it was built from
    assert np.allclose(rc.total, y, atol=1e-3 * float(y.max()))
    # the per-component pieces sum to the total
    summed = np.sum([v for _, v in rc.components], axis=0)
    assert np.allclose(summed, rc.total, atol=1e-9)
    # amplitudes are non-negative (physical)
    assert all(t >= -1e-9 for t in rc.theta[2:])


def test_reconstruct_curve_can_evaluate_on_a_finer_grid():
    x = np.linspace(-6.0, 10.0, 40)
    ctx = _ctx()
    y = _truth(x, ctx, 4.0, 0.8, 50.0, 150.0, 2.0)
    xf = np.linspace(-6.0, 10.0, 400)
    rc = audit.reconstruct_curve(x, y, omega0=float(np.hypot(4.0, 0.8)), gamma=0.8,
                                 center=ctx.center, gauss_fwhm=ctx.gauss_fwhm,
                                 lorentz_fwhm=ctx.lorentz_fwhm, T=ctx.T, x_eval=xf)
    assert rc.total.shape == xf.shape and np.all(np.isfinite(rc.total))


def test_reconstruct_curve_handles_overdamped_omega0_below_gamma():
    x = np.linspace(-8.0, 8.0, 50)
    rc = audit.reconstruct_curve(x, np.full_like(x, 5.0), omega0=1.0, gamma=3.0,
                                 center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.5, T=100.0)
    assert np.all(np.isfinite(rc.total))


def test_assemble_curve_rejects_non_mapping_amp():
    # A malformed persisted amplitudes value (a raw string, not a mapping) must raise a clean
    # ValueError, not an AttributeError from `amp.get` — downstream GUI callers catch ValueError.
    comp = CompositeModel([PhononDHO(), Elastic(), ConstantBackground()])
    x = np.linspace(-4.0, 6.0, 20)
    with pytest.raises(ValueError, match="amp must be a mapping"):
        audit.assemble_curve(comp, x, omega0=5.0, gamma=1.0, amp="junk", ctx=_ctx())
