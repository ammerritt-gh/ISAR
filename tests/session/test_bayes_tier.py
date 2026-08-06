"""Stage 2 gates: the ``--errors bayes`` tier in ``fit_one`` / the driver.

The bayes tier is a strict superset of ``full`` (bootstrap + MINOS + profile) plus a nested-sampling
posterior attached to the prelim-best label only. The posterior is diagnostics-only — the bootstrap
``interval`` stays authoritative and its semantics are unchanged. These tests monkeypatch the
(60 s) real sampler with a canned :class:`BayesResult`, mirroring tests/analyze/test_errors_attach.py.
"""
import numpy as np
import pytest

from isar.analyze.context import ScanContext
from isar.analyze.errors.bayes import BayesResult
from isar.schema import card as card_mod
from isar.session import driver
from isar.analyze.lineshapes import convolute_voigt_dho


# --------------------------------------------------------------------------- fixtures
def _card():
    pol = card_mod.CardPolicy(
        facility="APS", instrument="S30", format_version="2025", plugin="spec_s30",
        detectors=[card_mod.DetectorSpec("ana7", 10, "Ana7_3std", True)],
        energy_column="HerixE", monitor_column="ICO-C", counting_mode="time",
        normalize_by="monitor", raw_abscissa_kind="energy", energy_status="needs_zero",
        measurement_pattern="fscan", alignment_pattern="ascan",
        temperature_rule={}, collation={}, fitting={})
    return card_mod.Card(pol, card_mod.CardState())


def _single_peak_ctx():
    """A well-resolved single Stokes/anti-Stokes peak at ω₀≈5 — converges cleanly so the prelim-best
    label is a real phonon fit the posterior attaches to (same spectrum as the fast-tier gate)."""
    x = np.linspace(-8.0, 16.0, 81)
    counts = convolute_voigt_dho(x, 5.0, 0.0, 1.0, 0.0, 0.4, 500.0, 0.0, 0.0, 50.0,
                                 peak="stokes+astokes") + 8.0
    return ScanContext(x=x, counts=counts, mask=np.ones_like(x, dtype=bool), T=50.0,
                       center=0.0, gauss_fwhm=1.0, lorentz_fwhm=0.0, instrument="S30",
                       energy_status="needs_zero")


def _canned_bayes(converged=True, omega0_interval=(4.9, 5.0, 5.1), fail_reason=None):
    """A stand-in BayesResult so the tier WIRING is tested without a real sampler run."""
    return BayesResult(omega0_interval=omega0_interval, gamma_interval=(0.30, 0.40, 0.50),
                       omega0_samples=np.array([4.9, 5.0, 5.1]), gamma_samples=np.array([0.4]),
                       log_evidence=-85.0, log_evidence_err=0.5, ess=500.0, bimodal=False,
                       converged=converged, n_live=250, seed=777, prior_used=False,
                       fail_reason=fail_reason)


def _patch_bayes(monkeypatch, canned=None):
    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior",
                        lambda *a, **k: canned if canned is not None else _canned_bayes())


# --------------------------------------------------------------------------- tier behaviour
@pytest.mark.slow
def test_bayes_tier_populates_posterior_and_keeps_bootstrap(monkeypatch):
    _patch_bayes(monkeypatch)
    # bayes_opts is what run_batch resolves (defaults) and threads through to fit_one; the driver
    # never calls fit_one without it, so pass it here to exercise the provenance (bayes_n_live) path.
    fit = driver.fit_one(_single_peak_ctx(), _card(), prior=None, M=20, seed=1,
                         errors_tier="bayes",
                         bayes_opts={"n_live": 250, "dlogz": 0.5, "max_iter": 30_000})

    # Posterior diagnostics landed.
    assert fit["bayes_interval"] == (4.9, 5.0, 5.1)
    assert fit["bayes_ok"] is True
    assert fit["bayes_ess"] == 500.0
    assert fit["bayes_fail_reason"] is None
    assert np.isfinite(fit["log_evidence"]) and np.isfinite(fit["log_evidence_err"])
    assert fit["bayes_freq_ratio"] is not None and np.isfinite(fit["bayes_freq_ratio"])
    assert fit["hesse_freq_ratio"] is not None and np.isfinite(fit["hesse_freq_ratio"])
    assert fit["bayes_seconds"] >= 0.0
    # Provenance-only sampler config.
    assert fit["bayes_n_live"] == 250
    assert fit["bayes_seed"] == 777
    assert fit["dynesty_version"] is not None

    # Bootstrap interval is still the authoritative one, unchanged in semantics.
    assert fit["interval"] is not None
    lo, med, hi = fit["interval"]
    assert lo <= fit["omega0"] <= hi
    assert fit["interval_reliable"] is True


@pytest.mark.slow
def test_full_tier_leaves_all_bayes_fields_default():
    fit = driver.fit_one(_single_peak_ctx(), _card(), prior=None, M=20, seed=1, errors_tier="full")
    for key in ("bayes_interval", "bayes_ok", "bayes_ess", "bayes_fail_reason",
                "log_evidence", "log_evidence_err", "bayes_freq_ratio", "hesse_freq_ratio"):
        assert fit[key] is None, key
    for key in ("bayes_prior_driven", "bayes_fork_suspected", "bayes_unconstrained"):
        assert fit[key] is False, key
    assert fit["bayes_seconds"] == 0.0
    # Behaviour otherwise identical: the bootstrap interval is present as before.
    assert fit["interval"] is not None
    lo, _med, hi = fit["interval"]
    assert lo <= fit["omega0"] <= hi


@pytest.mark.slow
def test_posterior_sampled_once_on_prelim_best_only(monkeypatch):
    calls = []

    def _capture(hyp, *a, **k):
        calls.append(getattr(hyp, "label", None))
        return _canned_bayes()

    monkeypatch.setattr("isar.analyze.errors.bayes.bayesian_posterior", _capture)
    fit = driver.fit_one(_single_peak_ctx(), _card(), prior=None, M=20, seed=1, errors_tier="bayes")

    assert len(calls) == 1                     # bounded cost: posterior on one label only
    assert calls[0] == fit["best_label"]       # and that label is the (prelim==final) best


@pytest.mark.slow
def test_failed_posterior_falls_back_to_frequentist(monkeypatch):
    _patch_bayes(monkeypatch, _canned_bayes(converged=False, omega0_interval=(float("nan"),) * 3,
                                            fail_reason="ess=12 below 100"))
    fit = driver.fit_one(_single_peak_ctx(), _card(), prior=None, M=20, seed=1, errors_tier="bayes")

    assert fit["bayes_ok"] is False
    assert fit["bayes_fail_reason"] == "ess=12 below 100"
    # The authoritative frequentist interval is untouched and still reliable.
    assert fit["interval"] is not None
    lo, _med, hi = fit["interval"]
    assert lo <= fit["omega0"] <= hi
    assert fit["interval_reliable"] is True


# --------------------------------------------------------------------------- FitOutcome back-compat
def test_fitoutcome_back_compat_and_to_dict_roundtrip():
    # An old ledger dict lacking every new bayes key → all defaults, no error.
    old = {
        "detector_id": "ana7", "verdict": "resolved", "disposition": "resolved",
        "confidence": 0.8, "omega0": 5.0, "omega0_err": 0.05, "interval": [4.9, 5.0, 5.1],
        "modality": "fold", "gamma": 0.4, "gamma_provisional": False, "best_label": "stokes+bg",
        "cost_per_dof": 1.1, "q": [3.0, 0.2, 0.0], "T": 50.0, "sample": "S", "prior_used": None,
        "calib": {}, "reasons": [], "n_modes": 1}
    oc = driver.FitOutcome(**old)
    assert oc.bayes_interval is None and oc.bayes_ok is None
    assert oc.bayes_prior_driven is False and oc.bayes_fork_suspected is False

    # A new outcome list-ifies bayes_interval in to_dict and round-trips through FitOutcome(**d).
    oc2 = driver.FitOutcome(
        detector_id="ana7", verdict="resolved", disposition="resolved", confidence=0.8,
        omega0=5.0, omega0_err=0.05, interval=(4.9, 5.0, 5.1), modality="fold", gamma=0.4,
        gamma_provisional=False, best_label="stokes+bg", cost_per_dof=1.1, q=[3.0, 0.2, 0.0],
        T=50.0, sample="S", prior_used=None, calib={}, bayes_interval=(4.9, 5.0, 5.1),
        bayes_ok=True, bayes_ess=500.0)
    d = oc2.to_dict()
    assert d["bayes_interval"] == [4.9, 5.0, 5.1]      # list-ified like `interval`
    oc3 = driver.FitOutcome(**d)
    assert oc3.bayes_interval == [4.9, 5.0, 5.1] and oc3.bayes_ok is True


# --------------------------------------------------------------------------- _resolve_bayes_opts
def test_resolve_bayes_opts_defaults():
    assert driver._resolve_bayes_opts() == {"n_live": 250, "dlogz": 0.5, "max_iter": 30_000}
    assert driver._resolve_bayes_opts({}) == {"n_live": 250, "dlogz": 0.5, "max_iter": 30_000}


def test_resolve_bayes_opts_env_override(monkeypatch):
    monkeypatch.setenv("ISAR_BAYES_NLIVE", "400")
    monkeypatch.setenv("ISAR_BAYES_DLOGZ", "0.1")
    monkeypatch.setenv("ISAR_BAYES_MAXITER", "5000")
    assert driver._resolve_bayes_opts() == {"n_live": 400, "dlogz": 0.1, "max_iter": 5000}


def test_resolve_bayes_opts_cli_beats_env(monkeypatch):
    monkeypatch.setenv("ISAR_BAYES_NLIVE", "400")
    out = driver._resolve_bayes_opts({"n_live": 123})
    assert out["n_live"] == 123                        # CLI wins over env
    assert out["dlogz"] == 0.5 and out["max_iter"] == 30_000   # untouched → defaults
