"""The Qt-free scan-list read (``isar.workspace_api.scan_list``) against the BL43-shaped fixture.

The worked examples of the read's contract, run on a generated workspace (hard rule 10: no real
data). ``sample_snapshot`` is the answer written out by hand; the read must equal it.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

from bl43_fixture import KEY_A, KEY_B, pooled_workspace, sample_snapshot, stamp_fit
from isar.workspace_api import Workspace
from isar.workspace_api.scan_list import Fact, ScanListSnapshot, Status, scan_list
from isar.workspace_api.views import CardView, FileView, ScanView


def test_read_equals_the_worked_examples(bl43_ws_readonly):
    assert scan_list(Workspace.open(bl43_ws_readonly)) == sample_snapshot(bl43_ws_readonly)


def test_fitted_spectra_carry_their_fit_and_others_do_not(bl43_ws_readonly):
    snap = scan_list(Workspace.open(bl43_ws_readonly))
    assert snap.spectrum(f"{KEY_A}:a03").fit is not None
    assert snap.spectrum(f"{KEY_B}:a03").fit is None          # held: no fit view


def test_same_temperature_sets_stay_distinct(bl43_ws_readonly):
    snap = scan_list(Workspace.open(bl43_ws_readonly))
    a, b = snap.spectrum(f"{KEY_A}:a04"), snap.spectrum(f"{KEY_B}:a04")
    assert (a.set_key, b.set_key) == (KEY_A, KEY_B)
    assert a.fast.word == "close_call" and b.fast.word == "not fitted"


def test_hashed_placeholder_numbers_never_reach_a_label_or_name(bl43_ws_readonly):
    snap = scan_list(Workspace.open(bl43_ws_readonly))
    shown = ([s.label for s in snap.sets] + [u.name + u.reason for u in snap.files_not_read]
             + [sp.file_name or "" for s in snap.sets for sp in s.spectra])
    for hashed in ("1842762476", "1126149381"):
        assert not any(hashed in label_or_name for label_or_name in shown)
        assert any(hashed in s.key for s in snap.sets)   # the identity keeps it


def test_pooled_master_is_a_pooled_row_not_a_set(tmp_path):
    snap = scan_list(Workspace.open(pooled_workspace(tmp_path)))
    assert [p.key for p in snap.pooled] == ["tavi:900:det"]
    assert snap.pooled[0].label == "pooled master (2 members)"
    assert snap.pooled[0].temperature == Fact(200.0, "K", "fit", "fit")
    assert snap.pooled[0].q == Fact((2.0, 0.0, 0.0), "rlu", "fit", "fit")
    assert snap.pooled[0].fast == Status("not recorded", False, "no fast-pass record")
    assert snap.scan_set("tavi:900") is None


def test_files_received_counts_files_not_spectra():
    manifest = FileView(path="/data/set_a.dat", size=None, mtime=None,
                        classification="measurement", n_scans=1, scan_ids=("exp:1",))
    entries = [ScanView(scan_id=f"exp:1:a{n:02d}", scan_number=1, scan_type="measurement",
                        status="ingested", provenance={}, detectors={}, hkl=None)
               for n in (3, 4)]
    snap = scan_list(SimpleNamespace(scans=entries, files=[manifest], card=CardView(None, None)))
    assert [(s.files_received, len(s.spectra)) for s in snap.sets] == [(1, 2)]


def test_no_fast_record_reads_not_recorded_and_deep_refusal_reads_refused(bl43_ws):
    stamp_fit(bl43_ws, f"{KEY_A}:a03", "resolved",
              deep={"schema": "isar.deep-pass/1", "state": "refused", "reason": "refused: test"})
    session_path = bl43_ws / "session.json"
    data = json.loads(session_path.read_text(encoding="utf-8"))
    del data["scans"][f"{KEY_A}:a03"]["detectors"]["a03"]["fast_pass"]
    session_path.write_text(json.dumps(data), encoding="utf-8", newline="\n")
    spectrum = scan_list(Workspace.open(bl43_ws)).spectrum(f"{KEY_A}:a03")
    assert spectrum.fast == Status("not recorded", False, "no fast-pass record")
    assert spectrum.deep == Status("refused", True, "refused: test")


def test_empty_workspace_reads_as_an_empty_snapshot():
    assert scan_list(SimpleNamespace(scans=[], files=[], card=CardView(None, None))) == ScanListSnapshot()


def test_read_leaves_the_workspace_bytes_unchanged(bl43_ws):
    names = ("session.json", "report/results.csv")
    before = {n: (bl43_ws / n).read_bytes() for n in names}
    scan_list(Workspace.open(bl43_ws))
    assert {n: (bl43_ws / n).read_bytes() for n in names} == before
