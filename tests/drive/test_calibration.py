"""σ-calibration application (LLM_HARNESS_DESIGN §5) — the math and the prior-sigma guard."""
import json

import pytest

from isar.drive.acquisition import calibration as cal_mod
from isar.drive.acquisition.calibration import Calibration, load_calibration


# The `isar score --truth --json` output shape (isar.analyze.truth.report_to_dict).
_CALIB_JSON = {
    "truth_source": "fn:phonon_dft",
    "bucket_by": "verdict",
    "n_scored": 40,
    "coverage68": 0.20,
    "inflation": 2.5,
    "pull_mean": 0.1,
    "pull_std": 1.8,
    "center_pull_mean": 0.02,
    "median_abs_delta": 0.16,
    "buckets": [
        {"label": "resolved", "n": 30, "coverage68": 0.25, "inflation": 2.2, "center_pull_mean": 0.01},
        {"label": "flagged", "n": 10, "coverage68": 0.10, "inflation": 4.0, "center_pull_mean": 0.2},
    ],
}


def _calib():
    return Calibration.from_dict(_CALIB_JSON)


def test_calibrated_halfwidth_is_inflation_times_raw_halfwidth():
    c = _calib()
    # interval [4.8, 5.2] -> raw halfwidth 0.2; inflation 2.5 -> 0.5.
    assert c.calibrated_halfwidth(4.8, 5.2) == pytest.approx(0.5)


def test_bucket_inflation_overrides_global():
    c = _calib()
    # flagged bucket k=4.0; raw halfwidth 0.2 -> 0.8.
    assert c.calibrated_halfwidth(4.8, 5.2, bucket="flagged") == pytest.approx(0.8)
    # resolved bucket k=2.2 -> 0.44.
    assert c.calibrated_halfwidth(4.8, 5.2, bucket="resolved") == pytest.approx(0.44)


def test_unknown_bucket_falls_back_to_global():
    c = _calib()
    assert c.calibrated_halfwidth(4.8, 5.2, bucket="does_not_exist") == pytest.approx(0.5)


def test_rejects_prior_sigma_keyword():
    """The guard: prior_sigma (floors at 0.3 meV) must never enter the convergence test."""
    c = _calib()
    with pytest.raises(TypeError) as exc:
        c.calibrated_halfwidth(4.8, 5.2, prior_sigma=0.3)
    assert "prior_sigma" in str(exc.value)


def test_rejects_sigma_keyword():
    c = _calib()
    with pytest.raises(TypeError):
        c.calibrated_halfwidth(4.8, 5.2, sigma=0.3)


def test_requires_both_interval_bounds():
    c = _calib()
    with pytest.raises(ValueError):
        c.calibrated_halfwidth(None, 5.2)
    with pytest.raises(ValueError):
        c.calibrated_halfwidth(4.8, None)


def test_rejects_inverted_interval():
    c = _calib()
    with pytest.raises(ValueError):
        c.calibrated_halfwidth(5.2, 4.8)


def test_missing_inflation_raises():
    c = Calibration.from_dict({"inflation": None, "buckets": []})
    with pytest.raises(ValueError):
        c.calibrated_halfwidth(4.8, 5.2)


def test_nonpositive_inflation_raises():
    c = Calibration.from_dict({"inflation": 0.0, "buckets": []})
    with pytest.raises(ValueError):
        c.calibrated_halfwidth(4.8, 5.2)


def test_load_calibration_from_file(tmp_path):
    p = tmp_path / "calib.json"
    p.write_text(json.dumps(_CALIB_JSON), encoding="utf-8")
    c = load_calibration(p)
    assert c.inflation == pytest.approx(2.5)
    assert c.bucket_by == "verdict"
    assert set(c.buckets) == {"resolved", "flagged"}
    assert c.calibrated_halfwidth(4.8, 5.2) == pytest.approx(0.5)


def test_zero_halfwidth_gives_zero():
    c = _calib()
    assert c.calibrated_halfwidth(5.0, 5.0) == pytest.approx(0.0)
