"""Delivery-layer contract for a phonon-less winner (SCENE-1 PR-a).

Two rules the fitter's NaN convention must not leak past: (1) the delivered record's "no number"
is None — NaN passes every ``is not None`` guard in the branch layer and would flow into
results.csv / report.json / exposure targeting; (2) no ω₀ interval may be published — the
fold-pooled fallback belongs to the *rejected* phonon-bearing survivors (a phonon-less winner
carries no omega0_samples), and publishing it reliable would hand the driver a certifiable
interval for a scan that measured no phonon.
"""
import numpy as np

from isar.analyze.context import ScanContext
from isar.analyze.lineshapes import Voigt
from isar.schema import card as card_mod
from isar.session import scan_delivery


def _elastic_ctx(seed=11):
    x = np.linspace(-10.0, 10.0, 81)
    lam = 20.0 + Voigt(x, 400.0, 1.0, 0.0, 0.0)
    counts = np.random.default_rng(seed).poisson(lam).astype(float)
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _card(dispersion=None):
    policy = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan", temperature_rule={},
        collation={}, fitting={}, dispersion=dispersion or {})
    return card_mod.Card(policy, card_mod.CardState())


def test_elastic_only_row_delivers_no_number_and_no_interval():
    fit = scan_delivery.fit_one(_elastic_ctx(), _card(), prior=None, M=20, seed=1)
    assert fit["verdict"] == "elastic_only"
    assert fit["best_label"] == "elastic+bg"
    assert 0.0 < fit["confidence"] <= 0.5 + 1e-12
    # contract (1): None, never NaN
    assert fit["omega0"] is None and fit["omega0_err"] is None and fit["gamma"] is None
    # contract (2): no interval of any kind, and never flagged reliable
    assert fit["interval"] is None and fit["omega0_profile"] is None
    assert fit["hesse_interval"] is None and fit["hesse_trusted"] is False
    assert fit["interval_reliable"] is False
    assert fit["n_modes"] == 0


def test_null_winner_row_passes_branch_triage_as_no_measurement():
    """omega0=None (not NaN) must take the branch layer's no-measurement path: no fabricated
    feature, no delivery, no NaN selected_energy (the NaN leak the verifier reproduced)."""
    ctx = _elastic_ctx()
    card = _card(dispersion={"zone_period": 1.0})
    fit = scan_delivery.fit_one(ctx, card, prior=None, M=20, seed=1)
    out = scan_delivery._assign_and_triage(fit, None, ctx, card=card, sample="S",
                                           T=50.0, q=0.3, t_round=1.0, near_gamma=False)
    assert out is not None
    assert out["deliver"] is False
    assert out["selected_energy"] is None
