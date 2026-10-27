"""`isar export` carries each pass's currentness (S2U4): which inputs the fit used and whether they still hold.

Qt-free, on the S2 fixture (hard rule 10: all data generated here); the changed input is saved through the real
intake writer, and the export reads it back from the backend without refitting anything.
"""
from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

import bl43_fixture
import bl43_intake_fixture
from bl43_fixture import KEY_A, KEY_B
from isar.intake import values
from isar.report import export as export_mod

NOW = datetime(2026, 10, 27, 12, 0, 0)
REPO = Path(__file__).resolve().parents[2]
A03, A04, A05 = (f"{KEY_A}:a{n:02d}" for n in (3, 4, 5))


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("exc"))


@pytest.fixture
def ws_root(built, tmp_path):
    copy = tmp_path / "exc"
    shutil.copytree(built.parent, copy)
    return copy / built.name


def _export(root):
    folder = export_mod.export_workspace(root, now=NOW).folder
    doc = json.loads((folder / "fits.json").read_text(encoding="utf-8"))
    with open(folder / "modes.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return folder, doc, {r["spectrum_id"]: r for r in doc["spectra"]}, rows


def _readme(folder) -> str:
    return (folder / "README.txt").read_text(encoding="utf-8")


def test_the_worktree_is_the_import_root():
    import isar
    print("isar imported from", isar.__file__)
    assert Path(isar.__file__).resolve().is_relative_to(REPO)


def test_a_fresh_fit_is_current_and_says_what_it_used(ws_root):
    folder, doc, rec, rows = _export(ws_root)
    assert doc["currentness"]["assessed"] is True
    fast = rec[A03]["inputs"]["fast"]
    assert fast["current"] is True and fast["changed"] == []
    assert fast["used"]["temperature"]["value"] == 97.0 and fast["used"]["temperature"]["source"] == "file"
    a03_rows = [r for r in rows if r["scan_id"] == A03]
    assert a03_rows and {r["inputs_current"] for r in a03_rows} == {"true"}


def test_a_changed_input_marks_the_fast_rows_and_names_the_input(ws_root):
    assert values.save_value(ws_root, "temperature", 100, [KEY_A]).ok
    folder, doc, rec, rows = _export(ws_root)
    for unit in (A03, A04):
        fast = rec[unit]["inputs"]["fast"]
        assert fast["current"] is False
        assert fast["used"]["temperature"]["value"] == 97.0 and fast["used"]["temperature"]["source"] == "file"
        assert fast["changed"] == [{"input": "temperature", "from": 97.0, "to": 100.0, "to_source": "user"}]
    marked = [r for r in rows if r["scan_id"] in (A03, A04, f"{KEY_A}:a06")]
    assert marked and {(r["pass"], r["inputs_current"]) for r in marked} == {("fast", "false")}
    assert rec[A04]["inputs"]["deep"] == {"current": None, "used": None, "changed": []}   # no deep pass recorded
    other = rec[f"{KEY_B}:a03"]["inputs"]                      # held: no fit, no pass
    assert other == {"fast": None, "deep": None}


def test_saving_the_files_own_value_marks_nothing(ws_root):
    assert values.save_value(ws_root, "temperature", 97, [KEY_A]).ok
    _folder, _doc, rec, rows = _export(ws_root)
    assert rec[A03]["inputs"]["fast"]["current"] is True
    assert {r["inputs_current"] for r in rows if r["scan_id"] == A03} == {"true"}


def test_a_never_fitted_spectrum_has_no_pass_to_judge(ws_root):
    _folder, _doc, rec, _rows = _export(ws_root)
    assert rec[A05]["inputs"] == {"fast": None, "deep": None}


def test_an_older_fit_without_a_fast_pass_record_is_not_judged_but_still_has_its_rows(ws_root):
    ws, card, session = bl43_fixture._load(ws_root)           # a fit stored before the fast-pass record
    del session.scans[A03].detectors["a03"]["fast_pass"]
    bl43_fixture._save(ws, card, session)
    (ws_root / "report" / "fit_inputs.jsonl").unlink()        # and no fit-input record of it
    _folder, _doc, rec, rows = _export(ws_root)
    assert rec[A03]["fast"]["record"] == "fit_without_fast_pass"
    assert rec[A03]["inputs"]["fast"] == {"current": None, "used": None, "changed": []}
    a03_fast = [r for r in rows if r["scan_id"] == A03 and r["pass"] == "fast"]
    assert a03_fast and {r["inputs_current"] for r in a03_fast} == {""}


def test_the_readme_names_the_inputs_key_and_the_column_for_a_bl43_experiment(ws_root):
    folder = _export(ws_root)[0]
    text = _readme(folder)
    assert "Currentness is not assessed" not in text
    assert "`inputs`" in text and "inputs_current" in text and "counts_accepted" in text and "calibration_zero" in text


def test_a_workspace_without_fit_input_records_does_not_claim_to_be_assessed(tmp_path):
    root = bl43_intake_fixture.build_intake_workspace(tmp_path, fits=True)
    (root / "report" / "fit_inputs.jsonl").unlink()
    _folder, doc, rec, rows = _export(root)
    assert doc["currentness"]["assessed"] is False and "no recorded fit inputs" in doc["currentness"]["note"]
    assert rec[A03]["inputs"]["fast"] == {"current": None, "used": None, "changed": []}
    assert {r["inputs_current"] for r in rows} == {""}


def test_another_instrument_keeps_not_assessed(tmp_path):
    root = bl43_fixture.pooled_workspace(tmp_path)           # a TAVI workspace: no intake records exist for it
    folder, doc, rec, _rows = _export(root)
    assert doc["currentness"]["assessed"] is False
    assert "Currentness is not assessed" in _readme(folder)
    for r in doc["spectra"]:
        assert all(p is None or p["current"] is None for p in r["inputs"].values())
