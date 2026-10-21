"""Shared metadata helpers: header reading, filename decoding, count recovery, fact precedence."""
import logging

import numpy as np
import pytest

from isar.parsers import metadata as md


def test_header_reads_known_keys_typed_and_stops_at_data():
    lines = ["# scan 58 59", "# T_K 302.0", "# hkl 0.5 0.5 3", "# analyzer a06", "# si_order 12",
             "#", "# Sample CsV3Sb5", "0.0 1.0", "# late 1"]
    h = md.read_header(lines)
    assert h == {"scan": [58, 59], "T_K": 302.0, "hkl": [0.5, 0.5, 3.0], "analyzer": "a06",
                 "si_order": 12, "Sample": "CsV3Sb5"}


def test_header_value_that_does_not_parse_is_kept_as_text_and_logged(caplog):
    with caplog.at_level(logging.WARNING, logger=md.log.name):
        h = md.read_header(["# T_K warm"])
    assert h == {"T_K": "warm"}
    assert any("T_K" in r.getMessage() for r in caplog.records)


def test_filename_decodes_p_decimal_m_minus_and_hkl_groups():
    pats = [r"(?P<sample>[A-Za-z0-9]+)_(?P<T>\d+)K_(?P<hkl>[\dp_]+)_sum"]
    got = md.match_filename("CsV3Sb5_302K_0p5_0p5_3_sum", pats)
    assert got == {"sample": "CsV3Sb5", "T": 302, "hkl": [0.5, 0.5, 3]}
    got = md.match_filename("scan_m0p0079_x", [r"scan_(?P<dq>m?\d+p\d+)_x"])
    assert got == {"dq": pytest.approx(-0.0079)}
    assert md.match_filename("nothing", [r"scan_(?P<n>\d+)"]) == {}


def test_filename_group_with_a_text_part_stays_one_string():
    # A file-set stem is one string key; a group of numbers alone still splits into its parts.
    got = md.match_filename("Sample_302K_0p5_0p5_3_sum", [r"(?P<stem>.+_sum)"])
    assert got == {"stem": "Sample_302K_0p5_0p5_3_sum"}
    assert md.match_filename("scan_58_59_x", [r"scan_(?P<scans>\d+_\d+)_x"]) == {"scans": [58, 59]}


def test_count_recovery_zero_and_zero_error_bins_and_invalid_bin():
    rec = md.recover_counts(["3", "0", "4", "0"], ["1.7320508", "0", "0", "2"], integer=True)
    assert rec.counts[0] == 3 and rec.counts[1] == 0.0 and rec.counts[3] == 0.0   # 0/0 is N=0, sigma 0
    assert np.isnan(rec.counts[2]) and list(rec.invalid) == [False, False, True, False]  # e=0, y!=0
    assert not rec.not_integer.any()


def test_count_recovery_negative_intensity_is_an_invalid_bin_not_a_positive_count():
    for integer in (True, False):
        rec = md.recover_counts(["-3", "3"], ["1.7320508", "1.7320508"], integer=integer)
        assert list(rec.invalid) == [True, False] and np.isnan(rec.counts[0])
        assert rec.counts[1] == pytest.approx(3, abs=1e-6)
        assert not rec.not_integer[0]                 # invalid bins are not also "reported"


def test_count_recovery_flags_non_integer_and_keeps_unrounded_when_not_integer():
    # N = 1.5492^2 = 2.40002: five printed digits leave a tolerance of 0.25, so it is reported.
    rec = md.recover_counts(["1.5492"], ["1.0000"], integer=False)
    assert rec.not_integer[0] and rec.counts[0] == pytest.approx(2.40002, abs=1e-5)
    assert rec.max_deviation == pytest.approx(0.40002, abs=1e-5)
    assert md.recover_counts(["1.5492"], ["1.0000"], integer=True).counts[0] == 2.0


def test_count_recovery_large_n_within_printed_precision_is_not_flagged():
    # N = 1000.5^2 = 1001000.25; five printed digits allow about 400 counts, so 0.25 is in tolerance.
    rec = md.recover_counts(["1000.5"], ["1.0000"], integer=True)
    assert not rec.not_integer[0] and rec.counts[0] == 1001000.0
    assert rec.max_deviation == 0.25


def test_count_recovery_unrounded_reports_every_non_integer_bin():
    # With integer=False the printed-digit tolerance must not hide a bin recovering to about 2.1.
    rec = md.recover_counts(["1.4491377", "4", "0"], ["1", "2", "0"], integer=False)
    assert list(rec.not_integer) == [True, False, False]
    assert rec.counts[0] == pytest.approx(2.1, abs=1e-6)


def test_count_recovery_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        md.recover_counts(["1", "2"], ["1"], integer=True)


def test_merge_facts_header_wins_conflict_is_flagged_missing_is_recorded():
    sources = {"header": {"slit": "60x60"}, "card": {"slit": "30x30"}, "filename": {"T_K": 302}}
    facts, flags = md.merge_facts(sources, expected=["T_K", "scan_label"], scope="a06: ")
    assert facts["slit"] == {"value": "60x60", "source": "header",
                             "conflicts": [{"source": "card", "value": "30x30"}]}
    assert facts["T_K"] == {"value": 302, "source": "filename"}
    assert facts["scan_label"] == {"value": None, "source": "missing"}
    assert [f.type for f in flags] == ["fact_conflict"] and flags[0].reason.startswith("a06: slit")


def test_merge_facts_agreeing_sources_raise_no_flag_and_unknown_source_is_refused():
    facts, flags = md.merge_facts({"header": {"T_K": 302.0}, "card": {"T_K": 302}})
    assert facts["T_K"] == {"value": 302.0, "source": "header"} and flags == []
    with pytest.raises(ValueError):
        md.merge_facts({"inferred": {"slit": "60x60"}})
