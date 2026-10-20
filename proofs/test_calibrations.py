"""PROOF (real data, run on purpose): the calibration layer reproduces the gold-check recipe.

Moved from the two real-data tests in `tests/session/test_calibrations.py`. `resolve()` reproduces
the gold check's per-scan (center, resolution) for the S30 scans and the IN8 Cooper-Nathans width.
The number of S30 scans matched against the gold table is a measured value: it is read from
`real_data/APS S30/test_calibrations.expect.json`.
"""
import pytest

from isar.session import calibrations
from isar.session.state import SessionState

S30 = "APS S30"
IN8 = "ILL IN8"


def test_s30_resolve_matches_gold_check_recipe(real_file, expect):
    from isar.parsers import spec_s30
    from isar.analyze import gold
    from isar.calibrate import bragg
    from isar.calibrate.energy_zero import build_energy_calibration

    s30_supp = real_file(S30, "FeSe_1-xS_x_1.spec_supplementary.dat")
    s30_spec = real_file(S30, "FeSe_1-xS_x_1.spec")
    s30_gold = real_file(S30, "FeSe_1_fit_results.csv")

    card = spec_s30.card_template()
    by_scan = {r.scan_number: r for r in spec_s30.parse_file(s30_supp, card)}
    sess = SessionState()
    calibrations.refresh(card, sess, companion_files=[s30_spec])
    rc = calibrations.calibration_view(card)

    ref = build_energy_calibration(bragg.read_elastic_scans(s30_spec))
    rows = gold.load_gold(s30_gold)
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
    assert n == expect(S30)["n_scans_matched"]


def test_in8_cooper_nathans_center_zero(real_file):
    from isar.parsers import ill_in8
    from isar.calibrate import tas

    card = ill_in8.card_template()
    rec = ill_in8.parse_file(real_file(IN8, "055182"), card)[0]
    calibrations.refresh(card, SessionState())
    rc = calibrations.calibration_view(card)
    cr = calibrations.resolve(rec, "det0", card, rc)
    assert cr.center == 0.0
    assert cr.gauss_fwhm == pytest.approx(tas.cn_energy_fwhm(rec, w=0.0))
    assert cr.resolution_source == "cooper_nathans"
