"""MINOS↔bootstrap agreement (§5.5, frequentist half): where the fast profile error and the
bootstrap disagree, the scan is out of the asymptotic regime. Deterministic — fabricated
boot/minos inputs exercise the boundary logic."""
from types import SimpleNamespace

import numpy as np

from isar.analyze.errors import agree as A


def _boot(gamma_interval, omega0_interval=(4.9, 5.0, 5.1)):
    return SimpleNamespace(gamma_interval=gamma_interval, omega0_interval=omega0_interval)


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
