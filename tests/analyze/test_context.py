"""ScanContext — the per-fit context extracted from a ScanRecord + Card.

It carries the QC mask but never applies it (P5 / §0.4): the loop fits ``x_used`` only,
the dropped points stay in the record. The elastic-admissibility gate (structures.py)
reads ``energy_status`` and ``window`` from here, so the context is built from the
ScanRecord, not from raw x/counts/T (review F6).
"""
from types import SimpleNamespace

import numpy as np

from isar.analyze import context as CTX
from isar.schema import axes


def test_scancontext_honors_mask_without_applying_it():
    x = np.array([-2.0, -1.0, 0.0, 1.0, 2.0])
    counts = np.array([10.0, 20.0, 30.0, 40.0, 50.0])
    mask = np.array([True, True, False, True, True])
    ctx = CTX.ScanContext(x=x, counts=counts, mask=mask, T=100.0, center=0.0,
                          gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                          energy_status="needs_zero")
    # masked-in view used for fitting
    assert np.array_equal(ctx.x_used, np.array([-2.0, -1.0, 1.0, 2.0]))
    assert np.array_equal(ctx.counts_used, np.array([10.0, 20.0, 40.0, 50.0]))
    # original arrays are untouched (never applied)
    assert ctx.x.size == 5 and ctx.counts.size == 5


def test_scancontext_window_from_used_points():
    x = np.array([1.0, 2.0, 3.0, 21.0])
    mask = np.array([True, True, True, False])  # drop the far point
    ctx = CTX.ScanContext(x=x, counts=np.ones(4), mask=mask, T=100.0, center=0.0,
                          gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="IN8",
                          energy_status="absolute")
    assert ctx.window == (1.0, 3.0)


def _stub_record():
    egrid = np.linspace(-9.99, 9.99, 41)
    scan_axes = [
        axes.energy_axis(egrid),
        *axes.constant_q_axes(2.0, 0.05, 0.0, 41),
    ]
    sp = SimpleNamespace(intensity=np.linspace(5.0, 45.0, 41),
                         mask=np.ones(41, dtype=bool),
                         q=[2.0, 0.05, 0.0])
    return SimpleNamespace(
        scan_axes=scan_axes,
        spectra={"ana5": sp},
        temperature=SimpleNamespace(setpoint=np.full(41, 100.0)),
        energy_calibration=SimpleNamespace(status="needs_zero"),
    )


def test_from_scan_wires_fields():
    rec = _stub_record()
    card = SimpleNamespace(policy=SimpleNamespace(instrument="S30", quasielastic_fwhm=None))
    ctx = CTX.ScanContext.from_scan(rec, "ana5", card,
                                    center=-0.5, gauss_fwhm=1.3, lorentz_fwhm=0.2)
    assert np.allclose(ctx.x, axes.scanned_axes(rec)[0].values)
    assert np.allclose(ctx.counts, rec.spectra["ana5"].intensity)
    assert ctx.T == 100.0
    assert (ctx.center, ctx.gauss_fwhm, ctx.lorentz_fwhm) == (-0.5, 1.3, 0.2)
    assert ctx.instrument == "S30"
    assert ctx.energy_status == "needs_zero"
    assert ctx.prior_omega is None
    assert ctx.quasielastic_fwhm is None  # S30 card does not enable a quasi-elastic component
    assert ctx.x_used.size == 41  # all-true mask


def test_scancontext_quasielastic_fwhm_defaults_none():
    x = np.array([1.0, 2.0, 3.0])
    ctx = CTX.ScanContext(x=x, counts=np.ones(3), mask=np.ones(3, dtype=bool), T=100.0,
                          center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                          energy_status="needs_zero")
    assert ctx.quasielastic_fwhm is None


def test_from_scan_sources_quasielastic_fwhm_from_tas_card():
    rec = _stub_record()
    card = SimpleNamespace(policy=SimpleNamespace(instrument="IN8", quasielastic_fwhm=1.5))
    ctx = CTX.ScanContext.from_scan(rec, "ana5", card,
                                    center=0.0, gauss_fwhm=0.6, lorentz_fwhm=0.0)
    assert ctx.quasielastic_fwhm == 1.5
