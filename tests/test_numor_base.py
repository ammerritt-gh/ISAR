"""The shared ILL-numor reader (the spec_base analogue): keyword header + DATA_ column table.

Columns vary by scan type (a `bs qh` Q-E scan has QH/QK/QL/EN; a `sc a3` motor scan has A3), so the
reader resolves columns BY NAME — never positionally (the legacy parser's fragile EN+1 monitor). Real
numor files carry one block; the synthetic emitter may concatenate several, so read_numor returns a
list. Run on the committed artificial copies (`artificial_data/`: the facility's layout, every
data-row number replaced); table shape, labels and values are asserted against each copy's truth file.
"""
import numpy as np

from artificial import IN8_ALIGN as ALIGN, IN8_MEAS as MEAS, truth
from isar.parsers import numor_base


def _assert_table_matches_truth(path):
    """Every column of the parsed table equals the tokens the copy tool wrote, by position."""
    b = numor_base.read_numor(path)[0]
    s = truth(path)["scans"][0]
    assert b.column_labels == s["column_names"]
    assert b.data.shape == (s["points"], len(s["column_names"]))
    for j, c in enumerate(s["data_columns"]):
        assert np.allclose(b.data[:, j], [float(t) for t in c["tokens"]], rtol=1e-12, atol=0), c["name"]


def test_reads_one_block_with_keyword_header():
    blocks = numor_base.read_numor(MEAS)
    assert len(blocks) == 1
    b = blocks[0]
    assert b.keywords["INSTR"] == "IN8"
    assert b.keywords["EXPNO"] == "DIR-397"
    assert b.keywords["FILE_"] == "055181"
    assert b.keywords["TYPE_"] == "tas"
    assert b.keywords["COMND"].startswith("bs qh")


def test_params_parse_geometry_and_posqe():
    b = numor_base.read_numor(MEAS)[0]
    assert np.isclose(numor_base.param(b, "KFIX"), 2.66202)
    assert np.isclose(numor_base.param(b, "DM"), 3.355)
    assert np.isclose(numor_base.param(b, "DA"), 3.355)
    assert numor_base.param(b, "FX") == 2.0
    assert np.isclose(numor_base.param(b, "QH"), 4.0003, atol=1e-3)   # from POSQE


def test_data_table_resolves_columns_by_name():
    b = numor_base.read_numor(MEAS)[0]
    assert b.column_labels[0] == "PNT"
    for name in ("QH", "QK", "QL", "EN", "M1", "M2", "TIME", "CNTS", "TT", "TRT"):
        assert name in b.column_labels
    assert b.data.shape == (41, len(b.column_labels))
    en = b.data[:, numor_base.column_index(b.column_labels, "EN")]
    assert en[0] < en[-1]                              # energy ascends
    cnts = b.data[:, numor_base.column_index(b.column_labels, "CNTS")]
    assert np.all(cnts >= 0)


def test_data_table_matches_the_truth_column_by_column():
    """A shifted column or a mis-split table puts another column's numbers under a label."""
    _assert_table_matches_truth(MEAS)
    _assert_table_matches_truth(ALIGN)


def test_column_by_name_returns_that_columns_values():
    b = numor_base.read_numor(MEAS)[0]
    s = truth(MEAS)["scans"][0]
    for name in ("EN", "CNTS", "TT"):
        tokens = next(c["tokens"] for c in s["data_columns"] if c["name"] == name)
        got = b.data[:, numor_base.column_index(b.column_labels, name)]
        assert np.allclose(got, [float(t) for t in tokens], rtol=1e-12, atol=0), name


def test_alignment_numor_has_motor_columns_not_energy():
    b = numor_base.read_numor(ALIGN)[0]
    assert b.keywords["COMND"].startswith("sc a3")
    assert "A3" in b.column_labels and "EN" not in b.column_labels
    assert numor_base.column_index(b.column_labels, "A3") == 1      # first match = the scanned motor
    assert b.data.shape[0] == 31


def test_column_index_missing_returns_negative():
    b = numor_base.read_numor(ALIGN)[0]
    assert numor_base.column_index(b.column_labels, "NOPE") == -1
