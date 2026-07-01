"""Physics-gated enumeration of admissible structure hypotheses (§2).

Step 1 emits {stokes+bg} and {elastic+stokes+bg}; the elastic gate encodes the IN8
off-window-elastic degeneracy fix, and anti-Stokes is handled by the tie inside PhononDHO.
"""
import numpy as np

from isar.analyze import structures as S
from isar.analyze.context import ScanContext


def _ixs_ctx(center=-0.5, status="needs_zero", quasielastic_fwhm=None):
    """S30-like: elastic line in-window."""
    x = np.linspace(-10.0, 10.0, 41)
    return ScanContext(x=x, counts=np.ones_like(x), mask=np.ones_like(x, dtype=bool),
                       T=100.0, center=center, gauss_fwhm=1.2, lorentz_fwhm=0.0,
                       instrument="S30", energy_status=status,
                       quasielastic_fwhm=quasielastic_fwhm)


def _tas_offwindow_ctx(quasielastic_fwhm=None):
    """IN8-like FX=2: EN starts at 1.0, elastic at 0 is off-window."""
    x = np.arange(1.0, 21.5, 0.5)
    return ScanContext(x=x, counts=np.ones_like(x), mask=np.ones_like(x, dtype=bool),
                       T=290.0, center=0.0, gauss_fwhm=0.6, lorentz_fwhm=0.0,
                       instrument="IN8", energy_status="absolute",
                       quasielastic_fwhm=quasielastic_fwhm)


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


def test_tas_quasielastic_admitted_when_card_enables_it():
    """IN8 (off-window elastic) + a card-declared QE width: the broad quasi-elastic
    hypothesis is added even though the sharp elastic is excluded (§8)."""
    hyps = S.enumerate_structures(_tas_offwindow_ctx(quasielastic_fwhm=1.5))
    assert [h.label for h in hyps] == ["stokes+bg", "stokes+qe+bg"]
    qe = {h.label: h for h in hyps}["stokes+qe+bg"]
    assert qe.k == 5  # phonon (Omega_d, Gamma, A) + qe.QE + bg.BG
    assert qe.build().param_names() == ["phonon.Omega_d", "phonon.Gamma", "phonon.A",
                                        "qe.QE", "bg.BG"]


def test_quasielastic_not_admitted_when_card_silent():
    """A TAS scan whose card does not declare a QE width stays single-structure — the
    quasi-elastic is opt-in per card, never enumerated by default."""
    hyps = S.enumerate_structures(_tas_offwindow_ctx())
    assert [h.label for h in hyps] == ["stokes+bg"]


def test_in_window_elastic_and_quasielastic_emit_four_hypotheses():
    """When both gates fire (in-window elastic AND a declared QE width), the elastic-only,
    QE-only, and elastic+QE structures are all admissible; model selection decides."""
    hyps = S.enumerate_structures(_ixs_ctx(quasielastic_fwhm=1.5))
    assert [h.label for h in hyps] == [
        "stokes+bg", "elastic+stokes+bg", "stokes+qe+bg", "elastic+stokes+qe+bg"]
    full = {h.label: h for h in hyps}["elastic+stokes+qe+bg"].build()
    assert full.param_names() == ["phonon.Omega_d", "phonon.Gamma", "phonon.A",
                                  "elastic.E", "qe.QE", "bg.BG"]


def test_antistokes_gate_is_tied_at_step1():
    assert S.antistokes_mode(_ixs_ctx()) == "tied"


def test_hypothesis_build_param_order():
    by_label = {h.label: h for h in S.enumerate_structures(_ixs_ctx())}
    full = by_label["elastic+stokes+bg"].build()
    assert full.param_names() == ["phonon.Omega_d", "phonon.Gamma", "phonon.A",
                                  "elastic.E", "bg.BG"]
    stokes_only = by_label["stokes+bg"].build()
    assert stokes_only.param_names() == ["phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG"]
