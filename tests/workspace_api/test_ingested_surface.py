"""Phase B additive workspace_api surfaces: ws.files, ScanView.T/source_file, and ingested-only
scans whose HKL falls back to provenance["q"] (no detector fit carries a Q yet).

authored-not-run (no `isar` env on the build machine).
"""
from __future__ import annotations

from isar.workspace_api import Workspace

from .conftest import scan_entry


def test_ws_files_exposes_manifest(make_ws):
    files = {
        "/data/run.dat": {"size": 1234, "mtime": 111.0, "format": "measurement",
                          "n_scans": 2, "scan_ids": ["SYN:1", "SYN:2"]},
        "/data/note.txt": {"size": 3, "mtime": 222.0, "format": "unrecognized"},
    }
    ws = Workspace.open(make_ws(files=files))
    by_path = {f.path: f for f in ws.files}
    assert len(by_path) == 2
    meas = by_path["/data/run.dat"]
    assert meas.classification == "measurement"
    assert meas.n_scans == 2
    assert meas.scan_ids == ("SYN:1", "SYN:2")
    unrec = by_path["/data/note.txt"]
    assert unrec.classification == "unrecognized"
    assert unrec.scan_ids == ()          # missing scan_ids degrades to empty tuple


def test_scanview_T_and_source_file(make_ws):
    prov = {"T": 205.0, "source_file": "/data/run.dat", "q": [2.0, 0.0, 0.0], "ingested_at": "z"}
    ws = Workspace.open(make_ws(
        scans={"SYN:7:ana1": scan_entry(7, status="ingested", detectors={}, provenance=prov)}))
    sv = ws.scans[0]
    assert sv.T == 205.0
    assert sv.source_file == "/data/run.dat"


def test_ingested_only_scan_resolves_hkl_from_provenance(make_ws):
    prov = {"T": 205.0, "source_file": "/data/run.dat", "q": [2.5, 0.0, 0.0], "ingested_at": "z"}
    ws = Workspace.open(make_ws(
        scans={"SYN:9:ana1": scan_entry(9, status="ingested", detectors={}, provenance=prov)}))
    sv = ws.scans[0]
    assert sv.status == "ingested"
    assert sv.detectors == {}                 # no fits yet
    assert sv.hkl == [2.5, 0.0, 0.0]          # fell back to provenance["q"]
    assert sv.T == 205.0


def test_ingested_scan_without_q_has_no_hkl(make_ws):
    prov = {"T": 205.0, "source_file": "/data/run.dat", "q": None, "ingested_at": "z"}
    ws = Workspace.open(make_ws(
        scans={"SYN:9:ana1": scan_entry(9, status="ingested", detectors={}, provenance=prov)}))
    assert ws.scans[0].hkl is None
