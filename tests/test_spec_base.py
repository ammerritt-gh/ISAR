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
    # Without the hint, the 3-word name over-tokenizes to 6 labels but there are only 4 data columns.
    _, naive = spec_base.read_spec(p)
    assert len(naive[0].column_labels) == 6
    # With the hint, the labels collapse to the true 4 columns aligned to the data.
    _, blocks = spec_base.read_spec(p, multiword_columns=["Heat Stage T1"])
    b = blocks[0]
    assert b.column_labels == ["HerixE", "Heat_Stage_T1", "ICO-C", "Seconds"]
    assert spec_base.column_index(b, "ICO-C") == 2
    assert b.data.shape == (2, 4)
