"""IN8 parser robustness — a `bs`-driver motor scan is NOT a constant-Q measurement.

Numor 055189 of the DIR-397 campaign carries ``COMND: bs a3p 15 da3p 1 np 31 ti 1`` — the `bs`
batch-scan driver rocking the a3p motor, not a `bs qh` Q-E scan. It shares the ``bs`` measurement
pattern token but scans an angle, so it must classify as an alignment/context scan and never reach
``_bs_q_and_deltas`` (which would ``float("da3p")`` and crash). This fixture reproduces that block in
the shipped numor format and asserts the parser yields a clean alignment record, no crash.
"""
from __future__ import annotations

import pytest

from isar.parsers import ill_in8

# One numor block in the ILL ASCII format numor_base reads: RRRR banner, keyword header, DATA_ table.
_BS_A3P_NUMOR = "\n".join([
    "R" * 80,
    "  55189       2       0",
    "ILL TAS data in the new ASCII format follow after the line VV..V",
    "A" * 80,
    "      80",
    "IN8   fixture   27-Jul-25 02:18:14",
    "V" * 80,
    "INSTR: IN8",
    "EXPNO: DIR-397",
    "FILE_: 055189",
    "DATE_: 27-Jul-25 02:18:14",
    "TITLE: a3p rocking curve driven by bs",
    "TYPE_: tas",
    "COMND: bs a3p 15 da3p 1 np 31 ti 1",
    "POSQE: QH= 4.0000, QK= 0.0000, QL= 1.0000, EN= 0.0000, UN=meV",
    "PARAM: DM=   3.35500, DA=   3.35500, KFIX=   2.66202",
    "PARAM: SM=  1, SS= -1, SA=  1, FX=  2",
    "PARAM: TT= 200.00, RT= 200.00, MAG=      0.00",
    "DATA_:",
    "  PNT       A3P        M1        M2      TIME      CNTS",
    "   1      15.0000     2430        1     1.00       330",
    "   2      16.0000     2459        4     1.00       232",
    "   3      17.0000     2501        2     1.00       410",
])


def test_bs_motor_scan_classifies_as_alignment_not_measurement(tmp_path):
    p = tmp_path / "055189"
    p.write_text(_BS_A3P_NUMOR, encoding="utf-8")
    card = ill_in8.card_template()

    records = ill_in8.parse_file(p, card)

    assert len(records) == 1                              # no crash, one record produced
    rec = records[0]
    assert rec.scan_type == "alignment"                  # routed to context, not a Q-E measurement
    assert rec.scan_number == 55189
    # the scanned axis is the a3p motor, not energy
    assert rec.scan_axes[0].name == "A3P"
    assert rec.scan_axes[0].kind == "angle"


def test_unrecognized_only_block_is_visible_file_failure(tmp_path, caplog):
    """A wholly unparseable recognized file must not look like a successful zero-scan ingest."""
    text = _BS_A3P_NUMOR.replace("COMND: bs a3p 15 da3p 1 np 31 ti 1",
                                 "COMND: bs qh X 0 1 dqh 0 0 0 0.5 np 3 ti 1")
    p = tmp_path / "055999"
    p.write_text(text, encoding="utf-8")
    card = ill_in8.card_template()

    with caplog.at_level("WARNING"), pytest.raises(ValueError, match="parsed 0 of 1"):
        ill_in8.parse_file(p, card)

    assert "055189" in caplog.text or "unrecognized COMND" in caplog.text
