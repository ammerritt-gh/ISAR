"""Physics-gated enumeration of admissible structure hypotheses (§2).

Step 1 emits {stokes+bg} and {elastic+stokes+bg}; the elastic gate encodes the IN8
off-window-elastic degeneracy fix, and anti-Stokes is handled by the tie inside PhononDHO.
"""
import numpy as np

from isar.analyze import structures as S
from isar.analyze.context import ScanContext


def _ixs_ctx(center=-0.5, status="needs_zero"):
    """S30-like: elastic line in-window."""
    x = np.linspace(-10.0, 10.0, 41)
    return ScanContext(x=x, counts=np.ones_like(x), mask=np.ones_like(x, dtype=bool),
                       T=100.0, center=center, gauss_fwhm=1.2, lorentz_fwhm=0.0,
                       instrument="S30", energy_status=status)


def _tas_offwindow_ctx():
    """IN8-like FX=2: EN starts at 1.0, elastic at 0 is off-window."""
    x = np.arange(1.0, 21.5, 0.5)
    return ScanContext(x=x, counts=np.ones_like(x), mask=np.ones_like(x, dtype=bool),
                       T=290.0, center=0.0, gauss_fwhm=0.6, lorentz_fwhm=0.0,
                       instrument="IN8", energy_status="absolute")


def test_in_window_ixs_emits_both_hypotheses():
    hyps = S.enumerate_structures(_ixs_ctx())
    by_label = {h.label: h for h in hyps}
    assert set(by_label) == {"stokes+bg", "elastic+stokes+bg"}
    assert by_label["stokes+bg"].k == 4
    assert by_label["elastic+stokes+bg"].k == 5


def test_off_window_elastic_excluded():
    hyps = S.enumerate_structures(_tas_offwindow_ctx())
    assert [h.label for h in hyps] == ["stokes+bg"]


def test_needs_conversion_refuses_elastic():
    hyps = S.enumerate_structures(_ixs_ctx(status="needs_conversion_and_zero"))
    assert [h.label for h in hyps] == ["stokes+bg"]


def test_antistokes_gate_is_tied_at_step1():
    assert S.antistokes_mode(_ixs_ctx()) == "tied"


def test_hypothesis_build_param_order():
    by_label = {h.label: h for h in S.enumerate_structures(_ixs_ctx())}
    full = by_label["elastic+stokes+bg"].build()
    assert full.param_names() == ["phonon.Omega_d", "phonon.Gamma", "phonon.A",
                                  "elastic.E", "bg.BG"]
    stokes_only = by_label["stokes+bg"].build()
    assert stokes_only.param_names() == ["phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG"]
