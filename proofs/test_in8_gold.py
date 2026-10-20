"""PROOF (real data, run on purpose): the IN8 anchor gold table (Step 8) loads and its scans are real numors.

The scoring *logic* is unit-tested in tests/analyze/test_gold.py; this only guards that the anchor
CSV stays in sync with the real numor files and keeps the two vetted anchors the gate relies on.
"""
from isar.analyze import gold
from isar.parsers import ill_in8

FOLDER = "ILL IN8"


def test_gold_table_rows_map_to_real_numors(real_file):
    rows = gold.load_gold(real_file(FOLDER, "IN8_fit_results.csv"))
    assert rows, "anchor gold table is empty"
    for r in rows:
        real_file(FOLDER, f"{r.scan:06d}")   # fails naming the numor if the gold row has none


def test_gold_table_keeps_the_two_vetted_anchors(real_file, expect):
    exp = expect(FOLDER)
    by_scan = {r.scan: r for r in gold.load_gold(real_file(FOLDER, "IN8_fit_results.csv"))}
    # 055182: the hand-vetted recovery anchor.
    assert 55182 in by_scan
    assert by_scan[55182].quality.upper() == exp["anchor_055182_quality"]
    assert abs(by_scan[55182].omega0 - exp["anchor_055182_omega0_meV"]) < 0.5
    # 055183: the aborted safety anchor (no phonon energy).
    assert 55183 in by_scan
    assert by_scan[55183].quality.upper() == exp["anchor_055183_quality"]


def test_gold_anchor_numor_parses_as_a_single_detector_measurement(real_file):
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(real_file(FOLDER, "055182"), card)[0]
    assert rec.scan_number == 55182
    assert set(rec.spectra) == {"det0"}
    assert rec.energy_calibration.status == "absolute"
