"""PROOF (real data, run on purpose): BL43LXU per-analyzer parsing on the real CsV3Sb5 set.

The real folder goes through ``ingest_batch`` into a workspace scaffolded as ``isar init`` scaffolds
one (the shipped card template, empty state and session), so the real file sets pass through
discovery and grouping. Checks: one scan per stem with 28 analyzers; each analyzer's ``.var`` facts
and trailing ``#Q=``; each scan's filename facts; no spectrum takes a Q (rung 4); and the raw counts
recovered from the printed numbers are integers to within print rounding. Only structure and bounds
are asserted: no measured value is written here, so no expectation file is needed.
"""
import shutil

import numpy as np
import pytest

from isar.parsers import metadata, sp8_bl43
from isar.session import ingest
from isar.session.state import SessionState
from isar.session.workspace import Workspace

FOLDER = "leTacon_SP8_CsV3Sb5"
N_ANALYZERS = 28
VAR_FACTS = ("efficiency", "polarisation_factor", "elastic_centroid_meV", "elastic_area")
# Count bounds. Correct `_i` recovery gives near-integers, a few percent of bins pushed past 0.25 by
# print rounding; a wrong non-integer column spreads deviations over [0, 0.5], so about half the bins
# exceed 0.25. A monitor-scale column gives N near 1e22, where floats are all integers, so only the
# magnitude bound catches it (a 10 s bin of one analyzer holds far fewer counts than MAX_COUNT).
OFF_INTEGER = 0.25
MAX_OFF_SHARE = 0.10
MAX_INVALID_SHARE = 0.10
MAX_COUNT = 1e9


@pytest.fixture
def parsed(real_file, tmp_path):
    """(ingest report, ledger, scan records) for the real folder, in a fresh workspace."""
    folder = real_file(FOLDER)
    ws = Workspace.open(tmp_path / "ws")
    ws.root.mkdir()
    shutil.copyfile(sp8_bl43._CARD_PATH, ws.policy_path)
    ws.state_path.write_text("{}", encoding="utf-8")
    SessionState().save(ws.session_path)
    card, session = ws.load()
    report = ingest.ingest_batch(card, session, [folder])
    records = ingest.parse_all(ingest.discover([folder], card), card)
    return report, session, records


def test_each_stem_is_one_scan_with_28_analyzers(parsed):
    report, session, records = parsed
    assert not [f for f in report.files if f.get("error")], "a real measurement file failed to parse"
    assert not report.refused
    assert len(report.added) == 2
    assert len(session.scans) == 2 * N_ANALYZERS          # one ledger entry per (scan, analyzer)
    assert len(records) == 2 and len({r.scan_number for r in records}) == 2
    for rec in records:
        assert len(rec.spectra) == N_ANALYZERS
        assert not [f for f in rec.scan_flags if f.type == "missing_analyzer"]


def test_analyzers_carry_sidecar_and_trailer_facts(parsed):
    _, _, records = parsed
    for rec in records:
        for det, sp in rec.spectra.items():
            for name in VAR_FACTS:
                assert sp.facts[name]["source"] == "sidecar", f"{det}: {name}"
                assert isinstance(sp.facts[name]["value"], float), f"{det}: {name}"
            assert sp.facts["Q_nm"]["source"] == "header", det     # the file's trailing #Q=
            assert isinstance(sp.facts["Q_nm"]["value"], float), det


def test_scan_facts_carry_filename_and_header_sources(parsed):
    _, _, records = parsed
    for rec in records:
        assert rec.facts["scan_label"]["source"] == "filename"
        assert rec.facts["hkl_nominal"]["source"] == "filename"
        assert len(rec.facts["hkl_nominal"]["value"]) == 3
        assert rec.facts["T_K"]["source"] == "header"                # the trailing #T= beats the name
        assert rec.facts["bin_meV"]["source"] == "sidecar"
        assert rec.facts["scan_numbers"]["source"] == "missing"      # the names carry no scan number


def test_no_spectrum_takes_the_nominal_hkl_as_its_q(parsed):
    _, _, records = parsed
    for rec in records:
        for det, sp in rec.spectra.items():
            assert sp.q is None and sp.q_source == "none", det
            assert sp.facts["hkl"]["source"] == "missing", det


def test_recovered_counts_are_integers(parsed, real_file):
    _, _, records = parsed
    folder = real_file(FOLDER)
    for rec in records:
        family = rec.raw_header["intensity_family"]
        # `_lr` is near-integer on real data too, so integrality cannot tell it from `_i`: pin the family.
        assert family == "_i", rec.scan_number
        for det, sp in rec.spectra.items():
            m = sp8_bl43.read_member(folder / rec.raw_header["files"][det], family)
            # unrounded N per bin, NaN where invalid
            n = metadata.recover_counts(m.y_text, m.e_text, integer=False).counts
            valid = ~np.isnan(n)
            assert valid.mean() >= 1 - MAX_INVALID_SHARE, det
            assert (n[valid] > 0).any(), det
            assert n[valid].max() < MAX_COUNT, det
            dev = np.abs(n[valid] - np.round(n[valid]))
            assert np.mean(dev > OFF_INTEGER) < MAX_OFF_SHARE, det
            assert sp.facts["count_recovery_max_deviation"]["value"] == pytest.approx(dev.max()), det
