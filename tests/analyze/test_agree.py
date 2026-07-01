"""MINOS↔bootstrap agreement (§5.5, frequentist half): where the fast profile error and the
bootstrap disagree, the scan is out of the asymptotic regime. Deterministic — fabricated
boot/minos inputs exercise the boundary logic."""
from types import SimpleNamespace

import numpy as np

from isar.analyze.errors import agree as A


def _boot(gamma_interval=(0.30, 0.40, 0.50), omega0_interval=(4.9, 5.0, 5.1), bimodal=False):
    return SimpleNamespace(gamma_interval=gamma_interval, omega0_interval=omega0_interval,
                           bimodal=bimodal)


def _bayes(omega0_interval, bimodal=False, converged=True):
    return SimpleNamespace(omega0_interval=omega0_interval, bimodal=bimodal, converged=converged)


def test_agreement_asymptotic_when_minos_matches_bootstrap():
    boot = _boot((0.30, 0.40, 0.50))                  # Γ half-width 0.10
    minos = {"phonon.Gamma": (0.31, 0.49, True)}      # Γ half-width 0.09
    res = A.agreement(boot, minos)
    assert res.regime == "asymptotic"
    assert res.paradigm_divergence is False
    assert 0.5 < res.gamma_ratio < 2.0


def test_agreement_sparse_when_minos_much_tighter():
    boot = _boot((0.10, 0.40, 0.70))                  # Γ half-width 0.30
    minos = {"phonon.Gamma": (0.39, 0.41, True)}      # Γ half-width 0.01
    res = A.agreement(boot, minos)
    assert res.regime == "sparse"
    assert res.paradigm_divergence is True


def test_agreement_uses_profile_omega0_divergence():
    boot = _boot((0.30, 0.40, 0.50), omega0_interval=(4.8, 5.0, 5.2))  # ω₀ half-width 0.20
    minos = {"phonon.Gamma": (0.31, 0.49, True)}                        # Γ agrees
    res = A.agreement(boot, minos, profile_omega0=(4.99, 5.01))         # ω₀ half-width 0.01
    assert np.isfinite(res.omega0_ratio)
    assert res.regime == "sparse"                     # ω₀ disagreement trips it


# --- §5.5 three-method error-extraction cross-check (HESSE / bootstrap / posterior) ---

def test_agreement_new_fields_default_without_bayes_or_hesse():
    """Backward-compat: no bayes / no HESSE ⇒ the new fields sit at neutral defaults."""
    res = A.agreement(_boot(), {"phonon.Gamma": (0.31, 0.49, True)})
    assert res.bayes_available is False
    assert res.prior_driven is False and res.fork_suspected is False and res.unconstrained is False
    assert np.isnan(res.bayes_freq_ratio) and np.isnan(res.hesse_freq_ratio)


def test_agreement_carries_three_method_widths():
    boot = _boot(omega0_interval=(4.8, 5.0, 5.2))     # ω₀ half-width 0.20
    bayes = _bayes((4.9, 5.0, 5.1))                   # ω₀ half-width 0.10
    res = A.agreement(boot, None, bayes=bayes, hesse_omega0_sigma=0.05)
    assert abs(res.boot_half_width - 0.20) < 1e-9
    assert abs(res.hesse_half_width - 0.05) < 1e-9
    assert abs(res.bayes_half_width - 0.10) < 1e-9
    assert abs(res.hesse_freq_ratio - 0.25) < 1e-9    # 0.05 / 0.20
    assert abs(res.bayes_freq_ratio - 2.0) < 1e-9     # 0.20 / 0.10
    assert res.bayes_available is True


def test_agreement_prior_driven_when_freq_much_wider_than_bayes():
    boot = _boot(omega0_interval=(4.0, 5.0, 6.0))     # ω₀ half-width 1.0 (wide)
    bayes = _bayes((4.95, 5.0, 5.05))                 # ω₀ half-width 0.05 (tight) — ratio 20
    res = A.agreement(boot, None, bayes=bayes, prior=object())
    assert res.prior_driven is True


def test_agreement_not_prior_driven_without_a_prior():
    """The same freq≫bayes narrowing is NOT attributed to the prior when none was supplied."""
    boot = _boot(omega0_interval=(4.0, 5.0, 6.0))
    bayes = _bayes((4.95, 5.0, 5.05))
    res = A.agreement(boot, None, bayes=bayes, prior=None)
    assert res.prior_driven is False


def test_agreement_fork_suspected_when_boot_bimodal_bayes_unimodal():
    boot = _boot(omega0_interval=(4.0, 5.0, 8.0), bimodal=True)
    bayes = _bayes((4.9, 5.0, 5.1), bimodal=False)
    res = A.agreement(boot, None, bayes=bayes)
    assert res.fork_suspected is True


def test_agreement_unconstrained_when_both_wide_and_disagree():
    boot = _boot(omega0_interval=(-1.0, 5.0, 11.0))   # ω₀ half-width 6.0, /5 = 1.2 (wide)
    bayes = _bayes((2.4, 5.0, 7.6))                   # ω₀ half-width 2.6, /5 = 0.52 (wide); ratio 2.31
    res = A.agreement(boot, None, bayes=bayes)
    assert res.unconstrained is True


def test_agreement_bayes_ignored_when_not_converged():
    boot = _boot(omega0_interval=(4.0, 5.0, 6.0))
    bayes = _bayes((4.95, 5.0, 5.05), converged=False)
    res = A.agreement(boot, None, bayes=bayes, prior=object())
    assert res.bayes_available is False
    assert res.prior_driven is False and np.isnan(res.bayes_freq_ratio)
