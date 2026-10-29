"""PROOF (real data, run on purpose): the le Tacon CsV3Sb5 `_sum` sets read their gpi files and pass the check.

Every scan set whose name ends `_sum` must have `gpi_files/gpi.<set>` beside the data: a missing file is an error
naming it, and finding no `_sum` set at all is an error too. Each set's rows come from the shipped card and the
reduction the import makes; its gpi file is read and judged against the data's own `#Q=` values. Checks: the
check accepts the file, every analyzer has an HKL, the slit is known, and the named SARF set has its curve file
in `rf/` for every analyzer. Only structure is asserted; no measured number is written here, so no expectation
file is needed.
"""
import pytest

from isar.intake import scans
from isar.intake.gpi import judge, read_gpi
from isar.parsers import sp8_bl43
from isar.reduce import combine, qc
from isar.session import ingest

FOLDER = "leTacon_SP8_CsV3Sb5"


def _sum_rows(real_file) -> dict[str, list[dict]]:
    """scan set -> its import rows, for the sets whose name ends `_sum`."""
    card = sp8_bl43.card_template()
    records = ingest.parse_all(ingest.discover([real_file(FOLDER)], card), card)
    for rec in records:
        combine.reduce_record(rec, card)
        qc.qc_record(rec, card)
    sets: dict[str, list[dict]] = {}
    for rec in records:
        row = scans.scan_row(rec, "proof")
        sets.setdefault(row["set"], []).append(row)
    summed = {s: rows for s, rows in sets.items() if s.endswith("_sum")}
    if not summed:
        pytest.fail(f"no *_sum scan set was read from {real_file(FOLDER)}", pytrace=False)
    return summed


def test_every_sum_set_reads_its_gpi_file_and_passes_the_check(real_file):
    for set_name, rows in sorted(_sum_rows(real_file).items()):
        gpi = read_gpi(real_file(FOLDER, "gpi_files", f"gpi.{set_name}"))    # a missing file fails naming it
        for row in rows:
            analyzers = sorted(row["analyzers"])
            data_q = {det: (row["analyzers"][det] or {}).get("Q_nm") for det in analyzers}
            problem = judge(gpi, data_q)
            assert problem is None, f"{set_name}: the check rejects its gpi file: {problem}"
            assert [d for d in analyzers if gpi.hkl.get(d) is None] == [], f"{set_name}: analyzers with no HKL"
            assert gpi.slit is not None, f"{set_name}: the gpi file gives no slit (agaph x agapv)"
            assert gpi.sarf_name, f"{set_name}: the gpi file names no curve set"
            for det in analyzers:    # real_file fails naming the first curve file that is absent from rf/
                real_file(FOLDER, "rf", f"{gpi.sarf_name}_{det}")
