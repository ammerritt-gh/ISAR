"""Shared SPEC reader (§6): the instrument-agnostic block parser the IXS facilities share.

This is a *structural* test against the real S30 file — it asserts the reader carves the SPEC
blocks faithfully (header groups, scan blocks, the #V↔#H mapping, the full column table) without
yet building any ScanRecord. ID28 and BL43 later reuse this same reader as more cards over it.
"""
from pathlib import Path

import numpy as np

from isar.parsers import spec_base

REPO = Path(__file__).resolve().parents[1]
S30_FILE = REPO / "test_data" / "APS S30" / "FeSe_1-xS_x_1.spec_supplementary.dat"


def test_reads_global_header():
    header, _ = spec_base.read_spec(S30_FILE)
    assert header.F.endswith("FeSe_1-xS_x_1.spec")
    assert isinstance(header.E, int)
    # #H4..#H12 name the nine analyzers' H/K/L.
    assert header.H_groups[4] == ["Analyzer1_H", "Analyzer1_K", "Analyzer1_L"]
    assert header.H_groups[8] == ["Analyzer5_H", "Analyzer5_K", "Analyzer5_L"]
    assert header.H_groups[12] == ["Analyzer9_H", "Analyzer9_K", "Analyzer9_L"]
    assert header.H_groups[2] == ["T_Sample", "T_Control"]
    assert header.O_groups[0][0] == "HerixTTH"


def test_reads_all_ninety_scan_blocks():
    _, blocks = spec_base.read_spec(S30_FILE)
    assert len(blocks) == 90


def test_first_block_structure():
    _, blocks = spec_base.read_spec(S30_FILE)
    b = blocks[0]
    assert b.scan_number == 53
    assert b.command.startswith("fscan")
    assert b.T[0] == 2.0 and b.T[1] == "Seconds"
    # #Q 2 0.050001 -5.65969e-06
    assert np.allclose(b.Q, [2.0, 0.050001, -5.65969e-06])
    assert b.n_columns == 30
    assert len(b.column_labels) == 30
    assert b.column_labels[0] == "HerixE"
    assert b.column_labels[-2] == "ICO-C"
    assert b.column_labels[-1] == "Seconds"
    assert b.data.shape[1] == 30
    assert b.data.shape[0] > 10            # a real energy scan has tens of points
    # #G is sparse (no #G2 in the real file) — store it gap-tolerantly.
    assert 2 not in b.G and 0 in b.G and 1 in b.G


def test_v_h_mapping_and_central_analyzer():
    header, blocks = spec_base.read_spec(S30_FILE)
    b = blocks[0]
    # #V2 = T_Sample T_Control
    assert np.allclose(b.V_groups[2], [300.016, 290.002])
    # The #V↔#H map by index: Analyzer3 ↔ #V6.
    assert np.allclose(spec_base.v_group(b, header, "Analyzer3"), [1.76439, 0.0432437, -0.0423059])
    # ana5 ↔ #V8 is the central analyzer == #Q (the D2 mapping, confirmed on the real file).
    assert np.allclose(b.V_groups[8], b.Q)


def test_column_index_resolves_by_name():
    _, blocks = spec_base.read_spec(S30_FILE)
    b = blocks[0]
    assert spec_base.column_index(b, "HerixE") == 0
    assert spec_base.column_index(b, "ICO-C") == 28
    assert spec_base.column_index(b, "Ana5_3std") == 14


# ---------------------------------------------------------------------------
# Multi-word #L columns (raw S30 has "Heat Stage T1" etc.) — §step 2.
# ---------------------------------------------------------------------------
def test_join_multiword_collapses_multi_word_column_names():
    line = "HerixE Foo Heat Stage T1 Heat Stage T2 ICO-C Seconds"
    labels = spec_base._join_multiword(line, ["Heat Stage T1", "Heat Stage T2"])
    assert labels == ["HerixE", "Foo", "Heat_Stage_T1", "Heat_Stage_T2", "ICO-C", "Seconds"]


def _mini_spec(rows: list[str]) -> list[str]:
    return ["#F raw.spec", "#E 1", "#S 7  fscan herixE", "#N 3",
            "#L HerixE ICO-C Seconds", *rows, ""]


def test_rectangular_block_unchanged(tmp_path):
    p = tmp_path / "rect.spec"
    p.write_text("\n".join(_mini_spec(["0.0 1.0 2.0", "0.1 1.1 2.1", "0.2 1.2 2.2"])),
                 encoding="utf-8")
    _, blocks = spec_base.read_spec(p)
    assert len(blocks) == 1
    assert blocks[0].data.shape == (3, 3)


def test_truncated_final_row_dropped_with_warning(tmp_path, caplog):
    """A scan aborted mid-write leaves one SHORT final row (the FeSeS scan-173 artifact): drop just
    that row, keep the scan, and warn naming the scan + line — never crash, never pad."""
    p = tmp_path / "trunc.spec"
    p.write_text("\n".join(_mini_spec(["0.0 1.0 2.0", "0.1 1.1 2.1", "0.2 1.2"])),  # last row short
                 encoding="utf-8")
    with caplog.at_level("WARNING"):
        _, blocks = spec_base.read_spec(p)
    assert len(blocks) == 1
    assert blocks[0].scan_number == 7
    assert blocks[0].data.shape == (2, 3)          # the good rows kept, the truncated row dropped
    msg = caplog.text
    assert "scan 7" in msg and "truncated final row" in msg
    assert "2 of 3 columns" in msg


def test_midblock_ragged_skips_whole_scan_with_warning(tmp_path, caplog):
    """A short/over-wide row that is NOT the final one is corruption: skip the whole scan (warned),
    do not append a partial block."""
    text = "\n".join([
        "#F raw.spec", "#E 1",
        "#S 5  fscan herixE", "#N 3", "#L HerixE ICO-C Seconds",
        "0.0 1.0 2.0", "0.1 1.1", "0.2 1.2 2.2",           # bad row in the MIDDLE
        "#S 6  fscan herixE", "#N 3", "#L HerixE ICO-C Seconds",
        "0.0 1.0 2.0", "0.1 1.1 2.1",                       # clean neighbor
        "",
    ])
    p = tmp_path / "ragged.spec"
    p.write_text(text, encoding="utf-8")
    with caplog.at_level("WARNING"):
        _, blocks = spec_base.read_spec(p)
    nums = [b.scan_number for b in blocks]
    assert nums == [6]                                     # scan 5 skipped, scan 6 survives
    assert "scan 5" in caplog.text and "ragged data block" in caplog.text


def test_over_wide_final_row_skips_scan(tmp_path, caplog):
    """An over-wide final row is not the truncation artifact — it is corruption, so skip the scan."""
    p = tmp_path / "wide.spec"
    p.write_text("\n".join(_mini_spec(["0.0 1.0 2.0", "0.1 1.1 2.1 9.9"])),  # last row too wide
                 encoding="utf-8")
    with caplog.at_level("WARNING"):
        _, blocks = spec_base.read_spec(p)
    assert blocks == []
    assert "scan 7" in caplog.text and "ragged data block" in caplog.text


def test_read_spec_multiword_columns_resolves_true_column_count(tmp_path):
    # A minimal SPEC block whose #L carries a 3-word column name: a naive split over-counts.
    text = "\n".join([
        "#F raw.spec",
        "#E 1",
        "#S 1  fscan herixE",
        "#N 4",
        "#L HerixE Heat Stage T1 ICO-C Seconds",
        "0.0 295.0 100.0 1.0",
        "0.1 295.1 101.0 1.0",
        "",
    ])
    p = tmp_path / "mini.spec"
    p.write_text(text, encoding="utf-8")
    # Without the hint, the 3-word name over-tokenizes to 6 labels against #N 4 — a structural
    # inconsistency (every by-name lookup would misalign), so the block is skipped, not exposed.
    _, naive = spec_base.read_spec(p)
    assert naive == []
    # With the hint, the labels collapse to the true 4 columns aligned to the data.
    _, blocks = spec_base.read_spec(p, multiword_columns=["Heat Stage T1"])
    b = blocks[0]
    assert b.column_labels == ["HerixE", "Heat_Stage_T1", "ICO-C", "Seconds"]
    assert spec_base.column_index(b, "ICO-C") == 2
    assert b.data.shape == (2, 4)


# ---------------------------------------------------------------------------
# Declared-width authority (#N, reconciled against #L): a truncated-but-uniform
# block must never be silently accepted — by-name column access past the data's
# edge raises IndexError downstream and the ingest layer then skips the FILE.
# ---------------------------------------------------------------------------
def test_uniformly_truncated_block_is_skipped_not_accepted(tmp_path, caplog):
    """The review reproducer: #N 3, three labels, uniform 2-wide rows -> skip the block (warned)."""
    p = tmp_path / "uniform_short.spec"
    p.write_text("\n".join(_mini_spec(["0.0 1.0", "0.1 1.1"])), encoding="utf-8")
    with caplog.at_level("WARNING"):
        _, blocks = spec_base.read_spec(p)
    assert blocks == []
    assert "scan 7" in caplog.text and "header declares 3" in caplog.text


def test_uniformly_wide_block_is_skipped(tmp_path, caplog):
    p = tmp_path / "uniform_wide.spec"
    p.write_text("\n".join(_mini_spec(["0.0 1.0 2.0 9.9", "0.1 1.1 2.1 9.9"])), encoding="utf-8")
    with caplog.at_level("WARNING"):
        _, blocks = spec_base.read_spec(p)
    assert blocks == []
    assert "scan 7" in caplog.text


def test_first_row_truncated_then_full_rows_skips_block(tmp_path, caplog):
    """A short FIRST row must not become the block's width (the old first-row anchor did that)."""
    p = tmp_path / "first_short.spec"
    p.write_text("\n".join(_mini_spec(["0.0 1.0", "0.1 1.1 2.1", "0.2 1.2 2.2"])), encoding="utf-8")
    with caplog.at_level("WARNING"):
        _, blocks = spec_base.read_spec(p)
    assert blocks == []
    assert "scan 7" in caplog.text and "first data row" in caplog.text


def test_missing_hash_n_falls_back_to_labels(tmp_path):
    """No #N line: the resolved #L labels are the remaining declaration; a clean table parses."""
    text = "\n".join(["#F raw.spec", "#E 1", "#S 9  fscan herixE",
                      "#L HerixE ICO-C Seconds", "0.0 1.0 2.0", "0.1 1.1 2.1", ""])
    p = tmp_path / "no_n.spec"
    p.write_text(text, encoding="utf-8")
    _, blocks = spec_base.read_spec(p)
    assert len(blocks) == 1 and blocks[0].data.shape == (2, 3)


def test_missing_hash_n_still_rejects_short_rows_against_labels(tmp_path, caplog):
    text = "\n".join(["#F raw.spec", "#E 1", "#S 9  fscan herixE",
                      "#L HerixE ICO-C Seconds", "0.0 1.0", "0.1 1.1", ""])
    p = tmp_path / "no_n_short.spec"
    p.write_text(text, encoding="utf-8")
    with caplog.at_level("WARNING"):
        _, blocks = spec_base.read_spec(p)
    assert blocks == []
    assert "scan 9" in caplog.text


def test_n_l_disagreement_skips_block_with_warning(tmp_path, caplog):
    """#N 3 but only two labels: a malformed header (or an unlisted multi-word name) — skip."""
    text = "\n".join(["#F raw.spec", "#E 1", "#S 4  fscan herixE", "#N 3",
                      "#L HerixE Seconds", "0.0 1.0 2.0", ""])
    p = tmp_path / "n_l_mismatch.spec"
    p.write_text(text, encoding="utf-8")
    with caplog.at_level("WARNING"):
        _, blocks = spec_base.read_spec(p)
    assert blocks == []
    assert "scan 4" in caplog.text and "#N declares 3" in caplog.text and "resolves 2" in caplog.text
