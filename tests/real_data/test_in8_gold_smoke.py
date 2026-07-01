"""Smoke test for the IN8 anchor gold table (Step 8): it loads and its scans are committed numors.

The scoring *logic* is unit-tested in tests/analyze/test_gold.py; this only guards that the anchor
CSV stays in sync with the committed numor files and keeps the two vetted anchors the gate relies on.
"""
from pathlib import Path

from isar.analyze import gold
from isar.parsers import ill_in8

REPO = Path(__file__).resolve().parents[2]
IN8 = REPO / "test_data" / "ILL IN8"
GOLD = IN8 / "IN8_fit_results.csv"


def test_gold_table_rows_map_to_committed_numors():
    rows = gold.load_gold(GOLD)
    assert rows, "anchor gold table is empty"
    for r in rows:
        numor = IN8 / f"{r.scan:06d}"
        assert numor.exists(), f"gold row {r.scan} has no committed numor {numor.name}"


def test_gold_table_keeps_the_two_vetted_anchors():
    by_scan = {r.scan: r for r in gold.load_gold(GOLD)}
    # 055182: the hand-vetted recovery anchor (~4.2 meV, OK).
    assert 55182 in by_scan
    assert by_scan[55182].quality.upper() == "OK"
    assert abs(by_scan[55182].omega0 - 4.2) < 0.5
    # 055183: the aborted safety anchor (REJECT, no phonon energy).
    assert 55183 in by_scan
    assert by_scan[55183].quality.upper() == "REJECT"


def test_gold_anchor_numor_parses_as_a_single_detector_measurement():
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(IN8 / "055182", card)[0]
    assert rec.scan_number == 55182
    assert set(rec.spectra) == {"det0"}
    assert rec.energy_calibration.status == "absolute"
