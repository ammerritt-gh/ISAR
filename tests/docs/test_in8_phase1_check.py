"""Producer-side checks for the historical IN8 evidence report."""
from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "docs" / "in8_phase1_check.py"
SPEC = importlib.util.spec_from_file_location("in8_phase1_check", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
in8_check = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(in8_check)


def test_failed_anchor_reports_candidate_without_nan_uncertainty():
    text = in8_check._fit_estimate_text(
        {
            "success": 0,
            "omega0": 5.4321,
            "omega0_err_hesse": float("nan"),
        }
    )
    assert text == (
        "candidate ω₀ = 5.4321 meV; uncertainty unavailable because the fit failed"
    )
    assert "±" not in text
    assert "nan" not in text


def test_successful_anchor_keeps_finite_uncertainty():
    text = in8_check._fit_estimate_text(
        {"success": 1, "omega0": 4.2, "omega0_err_hesse": 0.3}
    )
    assert text == "ω₀ = 4.2 ± 0.3 meV"


def test_boolean_true_success_keeps_finite_uncertainty():
    text = in8_check._fit_estimate_text(
        {"success": True, "omega0": 4.2, "omega0_err_hesse": 0.3}
    )
    assert text == "ω₀ = 4.2 ± 0.3 meV"


def test_nonfinite_uncertainty_is_not_presented_even_if_success_flag_is_set():
    text = in8_check._fit_estimate_text(
        {"success": 1, "omega0": 4.2, "omega0_err_hesse": float("nan")}
    )
    assert text == (
        "candidate ω₀ = 4.2 meV; uncertainty unavailable because the fit did not produce "
        "a finite uncertainty"
    )
    assert "±" not in text
    assert "nan" not in text


def test_failed_clean_anchor_claims_are_explicitly_diagnostic_only():
    failed = {"success": 0, "omega0": 4.4, "omega0_err_hesse": float("nan")}
    for headline in (False, True):
        text = in8_check._clean_anchor_claim(failed, headline=headline)
        assert "candidate ω₀ = 4.4 meV" in text
        assert "fit failed" in text.lower() or "did not yield a successful fit" in text
        assert "matching the hand-vetted" not in text
        assert "first evidence" not in text
        assert "**Recovers" not in text


def test_successful_clean_anchor_claims_retain_recovery_language():
    successful = {"success": True, "omega0": 4.2, "omega0_err_hesse": 0.3}
    summary = in8_check._clean_anchor_claim(successful, headline=False)
    headline = in8_check._clean_anchor_claim(successful, headline=True)
    assert "**Recovers ω₀" in summary
    assert "matching the hand-vetted ~4.2 meV phonon" in summary
    assert "first evidence" in headline
