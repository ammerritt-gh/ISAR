"""Phase 3a — the Stokes-only (Bose-dead) high-q rescue.

When the prior predicts a phonon whose *predicted anti-Stokes counts* cannot be told from the
background noise, the anti-Stokes only adds a degenerate gain-side peak that steals amplitude
from the weak Stokes line (the ``no_signal`` failure). The phonon is then fit Stokes-only, and
the result is tagged ``stokes_only`` so it never reads as an ordinary phonon. The drop is a
detectability test — temperature × amplitude × background — not temperature alone.
"""
import numpy as np

from isar.analyze import loop
from isar.analyze import phonon as S
from isar.analyze.components import CompositeModel, ConstantBackground, PhononDHO
from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import convolute_voigt_dho


def _ctx(prior_omega=None, T=100.0, bg=10.0, stokes_amp=30.0):
    """S30-like context with a planted Stokes peak of height ``stokes_amp`` on a flat
    background ``bg`` (so the anti-Stokes detectability predicate has real counts to read)."""
    x = np.linspace(-10.0, 40.0, 101)
    counts = np.full_like(x, bg)
    if prior_omega is not None and stokes_amp > 0:
        counts = counts + stokes_amp * np.exp(-0.5 * ((x - prior_omega) / 1.2) ** 2)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool),
                       T=T, center=0.0, gauss_fwhm=1.2, lorentz_fwhm=0.0,
                       instrument="S30", energy_status="needs_zero", prior_omega=prior_omega)


# --------------------------------------------------------------------------- component knob
def test_default_phonon_keeps_bose_tied_antistokes():
    assert PhononDHO().peak == "stokes+astokes"            # default unchanged (regression)


def test_stokes_only_drops_the_gain_side_peak():
    """A Stokes-only phonon has no anti-Stokes mirror, so at finite T it integrates to less
    total intensity than the Bose-tied pair (the dropped gain-side counts)."""
    ctx = _ctx(prior_omega=6.0, T=300.0)                  # warm ⇒ anti-Stokes non-negligible
    x = ctx.x
    local = {"Omega_d": 6.0, "Gamma": 0.5, "A": 100.0}
    tied = PhononDHO(peak="stokes+astokes").evaluate(x, local, ctx)
    stokes = PhononDHO(peak="stokes").evaluate(x, local, ctx)
    assert stokes.sum() > 0
    assert tied.sum() > stokes.sum()                       # anti-Stokes adds counts
    gain = x < ctx.center                                   # extra intensity is gain-side
    assert tied[gain].sum() > stokes[gain].sum() + 1e-9


# --------------------------------------------------------------------------- detectability predicate
def test_tied_without_prior():
    assert S.antistokes_mode(_ctx(prior_omega=None)) == "tied"


def test_tied_when_no_measurable_stokes_peak():
    # flat counts ⇒ no Stokes excursion ⇒ can't justify dropping a side ⇒ safe default
    assert S.antistokes_mode(_ctx(prior_omega=30.0, T=100.0, stokes_amp=0.0)) == "tied"


def test_stokes_only_when_antistokes_buried():
    # E=30, T=100, amp=30, bg=10 → predicted anti-Stokes ≈ 0.9 counts ≪ 3·√10 → drop
    assert S.antistokes_mode(_ctx(prior_omega=30.0, T=100.0, bg=10.0, stokes_amp=30.0)) == "stokes_only"


def test_tied_when_antistokes_resolvable():
    # E=5, T=100, amp=30, bg=10 → predicted anti-Stokes ≈ 17 counts > 3·√10 → keep
    assert S.antistokes_mode(_ctx(prior_omega=5.0, T=100.0, bg=10.0, stokes_amp=30.0)) == "tied"


def test_strong_stokes_keeps_weight_suppressed_antistokes():
    """Owner's point: a temperature-suppressed anti-Stokes on a *strong* Stokes peak is still
    resolvable — amplitude matters, not just kT. Same E/T/bg as the buried case, big Stokes."""
    assert S.antistokes_mode(_ctx(prior_omega=30.0, T=100.0, bg=10.0, stokes_amp=2000.0)) == "tied"


def test_high_background_buries_an_otherwise_alive_antistokes():
    """Same phonon and temperature, only the background differs: a low-background scan resolves
    the anti-Stokes (tied); a high-background / short-count scan does not (dropped)."""
    assert S.antistokes_mode(_ctx(prior_omega=15.0, T=200.0, bg=10.0, stokes_amp=30.0)) == "tied"
    assert S.antistokes_mode(_ctx(prior_omega=15.0, T=200.0, bg=400.0, stokes_amp=30.0)) == "stokes_only"


# --------------------------------------------------------------------------- enumeration wiring
def test_enumeration_emits_stokes_only_when_buried():
    hyps = [h for h in S.enumerate_phonon_structures(_ctx(prior_omega=30.0, T=100.0))
            if "stokes" in h.label]                  # the phonon-less nulls carry no lineshape
    assert hyps and all(h.build().is_stokes_only() for h in hyps)
    # param set unchanged — Stokes-only is a lineshape choice, not fewer params
    assert hyps[0].build().param_names() == ["phonon.Omega_d", "phonon.Gamma", "phonon.A", "bg.BG"]


def test_enumeration_stays_tied_when_resolvable():
    hyps = S.enumerate_phonon_structures(_ctx(prior_omega=5.0, T=100.0))
    assert hyps and not any(h.build().is_stokes_only() for h in hyps)


def test_multimode_honors_stokes_only():
    hyps = S.enumerate_phonon_multimode(_ctx(prior_omega=30.0, T=100.0))
    assert hyps and all(h.build().is_stokes_only() for h in hyps)
    assert "phonon.Omega_d" in hyps[0].build().param_names()  # mode-1 nesting preserved


# --------------------------------------------------------------------------- composite property
def test_is_stokes_only_requires_a_phonon_and_all_one_sided():
    assert CompositeModel([PhononDHO(peak="stokes"), ConstantBackground()]).is_stokes_only()
    assert not CompositeModel([PhononDHO(), ConstantBackground()]).is_stokes_only()
    assert not CompositeModel([ConstantBackground()]).is_stokes_only()        # no phonon
    mixed = CompositeModel([PhononDHO(peak="stokes"), PhononDHO(prefix="phonon2"),
                            ConstantBackground()])
    assert not mixed.is_stokes_only()                                          # one tied ⇒ not


# --------------------------------------------------------------------------- end-to-end fit_kind
def test_fit_kind_tagged_stokes_only_and_recovers_omega0():
    """A high-q Bose-dead Stokes-only spectrum: enumeration goes Stokes-only, the fit recovers
    ω₀, and the result is tagged ``stokes_only`` (never reads as an ordinary resolved phonon)."""
    x = np.linspace(-8.0, 45.0, 121)
    counts = convolute_voigt_dho(x, 25.0, 0.0, 1.1, 0.0, 0.4, 20.0, 8.0, 0.0, 70.0,
                                 peak="stokes")             # cold, high-q, one-sided truth
    ctx = ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=70.0,
                      center=0.0, gauss_fwhm=1.1, lorentz_fwhm=0.0, instrument="S30",
                      energy_status="needs_zero", prior_omega=25.0)
    assert S.antistokes_mode(ctx) == "stokes_only"          # exp(-25/(KB·70)) ≈ 0.016 → buried
    hyp = {h.label: h for h in S.enumerate_phonon_structures(ctx)}["stokes+bg"]
    res = loop.fit_hypothesis(hyp, ctx, minos=False)
    assert res.fit_kind == "stokes_only"
    assert res.optimizer_converged and abs(res.omega0 - 25.0) < 0.5
