"""Step-3 gates (the retired unification build order §10.3, git history): the calibration layer reproduces the gold-check recipe.

Fast unit tests cover the version-dedup logic and the state round-trip; the real-data gate
(skipped when test_data/ is absent — it is gitignored) proves `resolve()` reproduces the gold
check's per-scan (center, resolution) for all 83 S30 scans and the IN8 Cooper-Nathans width.
"""
from pathlib import Path

import numpy as np
import pytest

from isar.schema import card as card_mod
from isar.session import calibrations
from isar.session.state import SessionState

REPO = Path(__file__).resolve().parents[2]
S30_SUPP = REPO / "test_data" / "APS S30" / "FeSe_1-xS_x_1.spec_supplementary.dat"
S30_SPEC = REPO / "test_data" / "APS S30" / "FeSe_1-xS_x_1.spec"
S30_GOLD = REPO / "test_data" / "APS S30" / "FeSe_1_fit_results.csv"
IN8_NUMOR = REPO / "test_data" / "ILL IN8" / "055182"

_have_s30 = S30_SUPP.exists() and S30_SPEC.exists() and S30_GOLD.exists()
_have_in8 = IN8_NUMOR.exists()


# --------------------------------------------------------------------------- fast unit tests
def _s30_card():
    from isar.parsers import spec_s30
    return spec_s30.card_template()


def test_version_appended_only_on_change():
    card = _s30_card()
    card.policy.calibration = {"method": "cooper_nathans"}
    sess = SessionState()
    calibrations.refresh(card, sess)
    calibrations.refresh(card, sess)          # identical -> no new version
    assert len(card.state.resolution_versions) == 1
    assert card.state.resolution_versions[0]["version"] == "v1"


def test_calibration_view_rebuilds_from_state():
    """State, not memory, is authoritative — a view rebuilt from card.state matches (restart-safe)."""
    card = _s30_card()
    card.state.energy_zero_series = [{"key": 100.0, "e0": -0.1}, {"key": 200.0, "e0": -0.2}]
    card.state.resolution_versions = [{
        "version": "v1", "method": "bragg_companion",
        "resolution_by_slit": {"18": [0.9, 0.5]}, "slit_by_scan": {"150": 18},
        "default_resolution": [0.96, 0.60],
    }]
    rc = calibrations.calibration_view(card)
    assert rc.zero(150) == pytest.approx(-0.15)      # interpolated between the two points
    assert rc.slit_for(150) == 18
    assert rc.resolution_for_slit(18) == (0.9, 0.5)
    assert rc.resolution_for_slit(999) == (0.96, 0.60)   # unseen slit -> default


def test_missing_zero_is_held_never_zero():
    card = _s30_card()
    card.policy.calibration = {"method": "bragg_companion"}
    # No energy_zero_series -> view has no zeros -> resolve holds (never center=0.0).
    rc = calibrations.calibration_view(card)
    from types import SimpleNamespace
    rec = SimpleNamespace(scan_number=999, spectra={}, raw_header={})
    cr = calibrations.resolve(rec, "ana7", card, rc)
    assert cr.held and cr.center is None


# --------------------------------------------------------------------------- real-data gate
@pytest.mark.skipif(not _have_s30, reason="S30 test_data not present (gitignored)")
def test_s30_resolve_matches_gold_check_recipe():
    from isar.parsers import spec_s30
    from isar.analyze import gold
    from isar.calibrate import bragg
    from isar.calibrate.energy_zero import build_energy_calibration

    card = spec_s30.card_template()
    by_scan = {r.scan_number: r for r in spec_s30.parse_file(S30_SUPP, card)}
    sess = SessionState()
    calibrations.refresh(card, sess, companion_files=[S30_SPEC])
    rc = calibrations.calibration_view(card)

    ref = build_energy_calibration(bragg.read_elastic_scans(S30_SPEC))
    rows = gold.load_gold(S30_GOLD)
    n = 0
    for row in rows:
        rec = by_scan.get(row.scan)
        if rec is None:
            continue
        n += 1
        cr = calibrations.resolve(rec, "ana7", card, rc)
        ref_zero = ref.zero(row.scan)
        if ref_zero is None:
            assert cr.held
            continue
        rgfw, rlfw = ref.resolution(row.slit)
        assert cr.center == pytest.approx(ref_zero)
        assert cr.gauss_fwhm == pytest.approx(rgfw)
        assert cr.lorentz_fwhm == pytest.approx(rlfw)
    assert n == 83


@pytest.mark.skipif(not _have_in8, reason="IN8 test_data not present (gitignored)")
def test_in8_cooper_nathans_center_zero():
    from isar.parsers import ill_in8
    from isar.calibrate import tas

    card = ill_in8.card_template()
    rec = ill_in8.parse_file(IN8_NUMOR, card)[0]
    calibrations.refresh(card, SessionState())
    rc = calibrations.calibration_view(card)
    cr = calibrations.resolve(rec, "det0", card, rc)
    assert cr.center == 0.0
    assert cr.gauss_fwhm == pytest.approx(tas.cn_energy_fwhm(rec, w=0.0))
    assert cr.resolution_source == "cooper_nathans"
