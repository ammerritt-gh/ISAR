"""Physics-gated enumeration of admissible structure hypotheses (§2).

Step 1 emits the phonon-bearing structures ({stokes+bg} plus the gated elastic/QE variants)
and, since SCENE-1 PR-a, the phonon-less nulls (`elastic+bg` when the elastic gate fires, `bg`
always) — the `elastic_only` / `no_signal` selection candidates. The elastic gate encodes the IN8
off-window-elastic degeneracy fix, and anti-Stokes is handled by the tie inside PhononDHO.
"""
import numpy as np

from isar.analyze import phonon as S
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
    hyps = S.enumerate_phonon_structures(_ixs_ctx())
    by_label = {h.label: h for h in hyps}
    assert set(by_label) == {"stokes+bg", "elastic+stokes+bg", "elastic+bg", "bg"}
    assert by_label["stokes+bg"].k == 4
    assert by_label["elastic+stokes+bg"].k == 5
    assert by_label["elastic+bg"].k == 2 and by_label["bg"].k == 1


def test_off_window_elastic_excluded():
    hyps = S.enumerate_phonon_structures(_tas_offwindow_ctx())
    assert [h.label for h in hyps] == ["stokes+bg", "bg"]


def test_needs_conversion_refuses_elastic():
    hyps = S.enumerate_phonon_structures(_ixs_ctx(status="needs_conversion_and_zero"))
    assert [h.label for h in hyps] == ["stokes+bg", "bg"]


def test_tas_quasielastic_admitted_when_card_enables_it():
    """IN8 (off-window elastic) + a card-declared QE width: the broad quasi-elastic
    hypothesis is added even though the sharp elastic is excluded (§8)."""
    hyps = S.enumerate_phonon_structures(_tas_offwindow_ctx(quasielastic_fwhm=1.5))
    assert [h.label for h in hyps] == ["stokes+bg", "stokes+qe+bg", "bg"]
    qe = {h.label: h for h in hyps}["stokes+qe+bg"]
    assert qe.k == 5  # phonon (omega0, Gamma, h) + qe.QE + bg.BG
    assert qe.build().param_names() == ["phonon.omega0", "phonon.Gamma", "phonon.h",
                                        "qe.QE", "bg.BG"]


def test_quasielastic_not_admitted_when_card_silent():
    """A TAS scan whose card does not declare a QE width stays single-structure — the
    quasi-elastic is opt-in per card, never enumerated by default."""
    hyps = S.enumerate_phonon_structures(_tas_offwindow_ctx())
    assert [h.label for h in hyps] == ["stokes+bg", "bg"]


def test_in_window_elastic_and_quasielastic_emit_four_hypotheses():
    """When both gates fire (in-window elastic AND a declared QE width), the elastic-only,
    QE-only, and elastic+QE structures are all admissible; model selection decides."""
    hyps = S.enumerate_phonon_structures(_ixs_ctx(quasielastic_fwhm=1.5))
    assert [h.label for h in hyps] == [
        "stokes+bg", "elastic+stokes+bg", "stokes+qe+bg", "elastic+stokes+qe+bg",
        "elastic+bg", "bg"]
    full = {h.label: h for h in hyps}["elastic+stokes+qe+bg"].build()
    assert full.param_names() == ["phonon.omega0", "phonon.Gamma", "phonon.h",
                                  "elastic.h", "qe.QE", "bg.BG"]


def test_antistokes_gate_is_tied_at_step1():
    assert S.antistokes_mode(_ixs_ctx()) == "tied"


def test_enumerate_multimode_two_phonon_build_and_k():
    """The 2-mode escalation set: mode 1 keeps the ``phonon`` prefix (so the 1-mode hypothesis
    is strictly nested), mode 2 is ``phonon2``; k counts both phonons + bg."""
    hyps = S.enumerate_phonon_multimode(_ixs_ctx(), n_max=2)
    by_label = {h.label: h for h in hyps}
    assert set(by_label) == {"stokes2+bg", "elastic+stokes2+bg"}
    two = by_label["stokes2+bg"].build()
    assert two.param_names() == ["phonon.omega0", "phonon.Gamma", "phonon.h",
                                 "phonon2.omega0", "phonon2.Gamma", "phonon2.h", "bg.BG"]
    assert by_label["stokes2+bg"].k == 7
    assert by_label["elastic+stokes2+bg"].k == 8
    # 1-mode params are a strict subset of the 2-mode params (nesting the LRT keys on)
    one = {h.label: h for h in S.enumerate_phonon_structures(_ixs_ctx())}
    assert set(one["stokes+bg"].build().param_names()) < set(two.param_names())


def test_enumerate_multimode_honors_elastic_and_qe_gates():
    # off-window elastic, no QE → only stokes2+bg
    assert [h.label for h in S.enumerate_phonon_multimode(_tas_offwindow_ctx(), 2)] == ["stokes2+bg"]
    # off-window elastic + QE declared → stokes2+bg + stokes2+qe+bg
    assert [h.label for h in S.enumerate_phonon_multimode(_tas_offwindow_ctx(quasielastic_fwhm=1.5), 2)] == \
        ["stokes2+bg", "stokes2+qe+bg"]
    # in-window elastic + QE declared → all four
    assert [h.label for h in S.enumerate_phonon_multimode(_ixs_ctx(quasielastic_fwhm=1.5), 2)] == \
        ["stokes2+bg", "elastic+stokes2+bg", "stokes2+qe+bg", "elastic+stokes2+qe+bg"]


def test_enumerate_multimode_runs_every_count_to_the_ceiling():
    """The two-mode cap is gone: every count 2..n_max, count by count, mode i in ``phonon<i>``
    (each count strictly nested in the next); ``None`` is the default ceiling (the suite's
    ``ISAR_MODE_CEILING``)."""
    assert S.enumerate_phonon_multimode(_ixs_ctx(), n_max=1) == []
    hyps = S.enumerate_phonon_multimode(_ixs_ctx(), n_max=4)
    assert [h.label for h in hyps] == ["stokes2+bg", "elastic+stokes2+bg", "stokes3+bg",
                                       "elastic+stokes3+bg", "stokes4+bg", "elastic+stokes4+bg"]
    four = {h.label: h for h in hyps}["stokes4+bg"]
    assert four.k == 13
    assert four.build().param_names()[-4:] == ["phonon4.omega0", "phonon4.Gamma", "phonon4.h",
                                               "bg.BG"]
    three = {h.label: h for h in hyps}["stokes3+bg"].build()
    assert set(three.param_names()) < set(four.build().param_names())
    assert [h.label for h in S.enumerate_phonon_multimode(_ixs_ctx())] == [
        h.label for h in S.enumerate_phonon_multimode(_ixs_ctx(), n_max=S.default_mode_ceiling())]


def test_hypothesis_build_param_order():
    by_label = {h.label: h for h in S.enumerate_phonon_structures(_ixs_ctx())}
    full = by_label["elastic+stokes+bg"].build()
    assert full.param_names() == ["phonon.omega0", "phonon.Gamma", "phonon.h",
                                  "elastic.h", "bg.BG"]
    stokes_only = by_label["stokes+bg"].build()
    assert stokes_only.param_names() == ["phonon.omega0", "phonon.Gamma", "phonon.h", "bg.BG"]
