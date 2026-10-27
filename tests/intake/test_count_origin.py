"""Where the counts came from (design amendment 2): the reader's ``count_recovery`` fact and the import row's level."""
import numpy as np
import pytest

from intake_fixtures import ENERGY_120, bl43_workspace, rows_for, write_scan
from isar.intake import scans
from isar.parsers import sp8_bl43
from isar.session import ingest
from isar.session.state import content_hash


def _counts(rows, det):
    (row,) = rows
    return row["analyzers"][det]["counts"]


def test_off_integer_i_analyzer_is_a_warning_with_its_bin_counts(tmp_path):
    ws, data = bl43_workspace(tmp_path, stems=("A",), off_integer=("a05",))
    rows = rows_for(ws, data)
    c = _counts(rows, "a05")
    assert {k: c[k] for k in ("origin", "family", "bins_tested", "bins_off", "level")} == {
        "origin": "y_over_e_squared", "family": "_i", "bins_tested": 120, "bins_off": 40, "level": "warning"}
    assert 0.4 < c["max_deviation"] < 0.5                    # the largest deviation, about half a count
    clean = _counts(rows, "a03")                              # integer counts: tested, nothing off
    assert (clean["bins_tested"], clean["bins_off"], clean["level"]) == (120, 0, None)


def test_a_few_print_rounded_bins_are_a_notice(tmp_path):
    ws, data = bl43_workspace(tmp_path, stems=())
    write_scan(data, stem="Smp_100K_0p5_0p5_3_A", analyzers=("a03",), energy=ENERGY_120,
               off_integer=("a03",), off_step=60)
    c = _counts(rows_for(ws, data), "a03")
    assert (c["bins_off"], c["bins_tested"], c["level"]) == (2, 120, "notice")


def test_the_reader_records_the_fact_and_leaves_the_flag_rule_alone(tmp_path):
    ws, data = bl43_workspace(tmp_path, stems=("A",), off_integer=("a05",))
    (rec,) = [r for r in ingest.parse_all(ingest.discover([data], ws.load()[0]), ws.load()[0])]
    fact = rec.spectra["a05"].facts["count_recovery"]
    assert fact["source"] == "inferred" and fact["value"]["bins_off"] == 40
    assert fact["value"]["max_deviation"] == rec.spectra["a05"].facts["count_recovery_max_deviation"]["value"]
    (flag,) = [f for f in rec.spectra["a05"].spectrum_flags if f.type == "counts_not_integer"]
    assert flag.severity == "warning" and "40 of 120 bins" in flag.reason


def test_lr_set_is_family_lr_at_warning(tmp_path):
    e = np.round(np.arange(0, 20) * 0.2, 4)
    lr = np.linspace(10.3, 55.7, 20)                          # split counts: not integers
    rows = ["#      meV     lrn        norm_lr         a03_lr       error_lr"]
    rows += [f"{x:10.2f} {40:7d} {1e6:14.6E} {n / 1e6:14.6E} {n ** 0.5 / 1e6:14.6E}" for x, n in zip(e, lr)]
    (tmp_path / "Smp_100K_0p5_0p5_3_a03").write_text("\n".join(rows) + "\n", encoding="utf-8", newline="\n")
    card = sp8_bl43.card_template()
    card.policy.reader = {"intensity_family": "_lr"}
    (rec,) = ingest.parse_all(ingest.discover([tmp_path], card), card)
    c = scans.scan_row(rec, content_hash(rec))["analyzers"]["a03"]["counts"]
    assert (c["family"], c["level"], c["bins_tested"]) == ("_lr", "warning", 20)


@pytest.mark.parametrize("severity, level", [("warning", "warning"), ("info", "notice")])
def test_row_level_follows_the_flag_severity(tmp_path, severity, level):
    ws, data = bl43_workspace(tmp_path, stems=("A",), analyzers=("a03",))
    (rec,) = ingest.parse_all(ingest.discover([data], ws.load()[0]), ws.load()[0])
    from isar.schema.scan import Flag
    rec.spectra["a03"].spectrum_flags.append(Flag("counts_not_integer", severity, "a03: test"))
    assert scans.scan_row(rec, "h")["analyzers"]["a03"]["counts"]["level"] == level
