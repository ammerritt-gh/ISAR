"""`isar export` carries the inter-spectra fit as a third labelled pass and marks the authoritative one (S1U11).

Qt-free, on the S1U1 fixture workspace; refits are hand-written log lines and payloads (no fitting, hard rule 10),
saved through the real writer. Authority is the real resolver; nothing here re-derives it.
"""
from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

import isar
from inter_fixtures import build_inter_workspace, spectrum_keys
from isar.intake import inter_values, values
from isar.intake.inter_spectra import read_user
from isar.report import export as export_mod
from isar.schema.card import load_state
from isar.session import writer
from isar.session.inter_spectra import records

REPO = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 10, 29, 12, 0, 0)
F1, F2 = "f0000000000a1", "f0000000000a2"
OVERLAY = {"n_modes": 1, "omega0": 3.5, "best_label": "1 mode",
           "modes": [{"omega0": 3.5, "omega0_err": 0.2, "gamma": 0.4, "gamma_err": 0.1}],
           "fast_pass": {"state": "resolved", "peaks": []}}
FLAG = {"score": 2.0, "flagged": True, "reasons": [{"code": "moved", "severity": "warn", "text": "moved"}]}
NO_MODES = {"n_modes": 0, "omega0": None, "best_label": "no modes", "modes": [],
            "fast_pass": {"state": "unresolved", "peaks": []}}
REPAIR = {"score": 0.5, "flagged": False,
          "reasons": [{"code": "neighbour_supported_split", "severity": "info", "text": "split, repaired"}]}


def test_the_worktree_is_the_import_root():
    print("isar imported from", isar.__file__)
    assert Path(isar.__file__).resolve().parents[1] == REPO


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build_inter_workspace(tmp_path_factory.mktemp("exi"), temperatures=(100, 200), setups=("0p5_0p5_3",),
                                 n_analyzers=3)


@pytest.fixture
def ws_root(built, tmp_path):
    copy = tmp_path / "exi"
    shutil.copytree(built.parent, copy)
    return copy / built.name


def _hash(root, unit):
    from isar.intake import inputs, scans as intake_scans, store
    read = store.read_all(root, store.ENTRIES)
    return records.inputs_now_from(intake_scans.read_scans(root), inputs.index_entries(read.entries, lines=read.lines),
                                   load_state(root / "card.state.json").tabulated_calibration, [unit])[unit]


def _save_ring(root, batch, refits, number_flag=None, overlay=OVERLAY):
    """One anchored batch with one ring; ``refits``: (fit id, unit) pairs, each stamped with the unit's hash now."""
    start = {"schema": records.SCHEMA_SAVE, "unit": f"inter:{batch}:batch", "pass": "inter", "batch": batch,
             "event": "start",
             "start": {"batch_kind": "anchored", "scope": {"direction": "q", "members": []}, "anchors": [],
                       "rings": {}, "seams": [], "settings": {"version": 1, "values": {}}, "preview": "t",
                       "fixed": {}, "unreached": {}, "terms": [], "metric": {}, "core": "t", "inputs_at": 0,
                       "intake_seq": read_user(root).max_seq, "epochs": {}}}
    stage = {"schema": records.SCHEMA_SAVE, "unit": f"inter:{batch}:batch", "pass": "inter", "batch": batch,
             "event": "stage",
             "stage": {"stage": "ring", "index": 1, "round": None, "seams": [], "not_reached": {},
                       "progress": {"done": len(refits), "failed": 0, "not_reached": 0, "fixed": 0, "total": 9},
                       "refits": [{"fit": f, "unit": u, "status": "accepted", "reason": None, "seed": "fast",
                                   "inputs": _hash(root, u), "conditioned_on": [], "flag": number_flag,
                                   "why_changed": "a shoulder split off",
                                   "payload": {"came_in": {}, "came_out": {"errors": "conditional"},
                                               "overlay": overlay}} for f, u in refits]}}
    writer.save_inter(root, [start, stage])


def _export(root):
    s = export_mod.export_workspace(root, now=NOW)
    doc = json.loads((s.folder / "fits.json").read_text(encoding="utf-8"))
    with open(s.folder / "modes.csv", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return s, doc, {r["spectrum_id"]: r for r in doc["spectra"]}, rows


def _rows(rows, unit):
    return [r for r in rows if r["scan_id"] == unit]


def _by_pass(rows, unit):
    return {r["pass"]: r for r in _rows(rows, unit)}


def test_no_inter_files_marks_the_fast_rows_authoritative_and_adds_no_inter_rows(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    s, doc, rec, rows = _export(ws_root)
    assert s.n_modes_inter == 0 and s.n_modes == s.n_modes_fast + s.n_modes_deep
    assert doc["counts"]["modes_inter"] == 0 and rec[k1]["inter_spectra"] is None
    assert {r["pass"] for r in rows} == {"fast"}
    assert all((r["authoritative"], r["fit_source"], r["fit_id"], r["flagged"]) == ("true", "Fast solo", "fast", "")
               for r in rows)


def test_a_refit_is_exported_as_a_third_pass_and_the_fast_rows_stay(ws_root):
    k1, k2 = spectrum_keys(ws_root)[:2]
    _save_ring(ws_root, "b000000000001", [(F1, k1)], number_flag=FLAG)
    s, doc, rec, rows = _export(ws_root)
    p = _by_pass(rows, k1)
    assert set(p) == {"fast", "inter"} and s.n_modes_inter == 1 and doc["counts"]["modes_inter"] == 1
    assert (p["fast"]["authoritative"], p["fast"]["fit_id"]) == ("false", "fast")
    assert (p["inter"]["authoritative"], p["inter"]["fit_id"], p["inter"]["flagged"]) == ("true", F1, "true")
    assert p["inter"]["fit_source"] == p["fast"]["fit_source"] == "Batch 1 · ring 1"
    assert float(p["inter"]["omega0"]) == 3.5 and p["inter"]["trust"] == "not assessed"
    # (3) the refit's error is conditional, never labelled hesse
    assert p["inter"]["uncertainty_kind"] == "hesse_conditional" and float(p["inter"]["omega0_err"]) == 0.2
    assert p["inter"]["inputs_current"] == "true"
    # the neighbour without a refit is untouched
    assert {r["pass"] for r in _rows(rows, k2)} == {"fast"} and _rows(rows, k2)[0]["authoritative"] == "true"
    block = rec[k1]["inter_spectra"]
    assert block["authoritative"] == {"fit": F1, "source": "batch", "label": "Batch 1 · ring 1", "stale": False,
                                      "locked": False}
    assert block["flag"]["severity"] == "warn" and block["why_changed"] == "a shoulder split off"
    assert [h["fit"] for h in block["history"]] == ["fast", F1] and block["fit"]["n_modes"] == 1
    assert doc["passes"]["exported"] == ["fast", "deep", "inter"]


def test_a_repair_is_listed_as_a_reason_but_is_not_flagged_for_review(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    _save_ring(ws_root, "b000000000001", [(F1, k1)], number_flag=REPAIR)
    _, doc, rec, rows = _export(ws_root)
    assert _by_pass(rows, k1)["inter"]["flagged"] == "false"
    block = rec[k1]["inter_spectra"]
    assert block["flag"]["severity"] == "info"
    assert [r["code"] for r in block["flag"]["reasons"]] == ["neighbour_supported_split"]
    assert block["history"][-1]["flagged"] is False


def test_an_authoritative_refit_with_no_modes_leaves_the_solo_rows_not_authoritative(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    _save_ring(ws_root, "b000000000001", [(F1, k1)], overlay=NO_MODES)
    _, _, rec, rows = _export(ws_root)
    solo = _rows(rows, k1)
    assert solo and {r["pass"] for r in solo} == {"fast"}                   # the solo fit has modes; no inter row
    assert all((r["authoritative"], r["fit_id"], r["fit_source"]) == ("false", "fast", "Batch 1 · ring 1") for r in solo)
    assert rec[k1]["inter_spectra"]["authoritative"]["fit"] == F1


def test_picking_the_solo_fit_then_locking_a_refit_then_stale_follow_the_resolver(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    _save_ring(ws_root, "b000000000001", [(F1, k1)])
    # (1a) the user picks the solo fit: no inter row, the fast rows are authoritative, the label says whose choice
    assert inter_values.pick_fit(ws_root, k1, "fast").ok
    _, doc, rec, rows = _export(ws_root)
    p = _by_pass(rows, k1)
    assert set(p) == {"fast"} and p["fast"]["authoritative"] == "true"
    assert p["fast"]["fit_source"] == "Your pick: Fast solo"
    assert [h["fit"] for h in rec[k1]["inter_spectra"]["history"]] == ["fast", F1]
    # (1b) lock the refit: it is authoritative again and the label says locked
    assert inter_values.set_lock(ws_root, k1, locked=True, fit_id=F1).ok
    _, _, _, rows = _export(ws_root)
    p = _by_pass(rows, k1)
    assert p["inter"]["fit_source"] == "Locked: Batch 1 · ring 1" and p["fast"]["authoritative"] == "false"
    # (1c) its inputs change: a locked refit stays in use, now marked out of date
    assert values.save_value(ws_root, "temperature", 97, [k1.rsplit(":", 1)[0]]).ok
    _, doc, rec, rows = _export(ws_root)
    p = _by_pass(rows, k1)
    assert p["inter"]["inputs_current"] == "false" and p["inter"]["authoritative"] == "true"
    assert rec[k1]["inter_spectra"]["authoritative"]["stale"] is True


def test_an_unlocked_stale_refit_gives_way_to_the_solo_fit(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    _save_ring(ws_root, "b000000000001", [(F1, k1)])
    assert values.save_value(ws_root, "temperature", 97, [k1.rsplit(":", 1)[0]]).ok
    _, _, _, rows = _export(ws_root)
    p = _by_pass(rows, k1)
    assert set(p) == {"fast"} and p["fast"]["authoritative"] == "true"
    assert p["fast"]["fit_source"].startswith("Fast solo (Batch 1 · ring 1 is out of date")


def test_results_modes_and_report_json_do_not_change_when_inter_files_exist(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    report = ws_root / "report"
    names = ("results.csv", "modes.csv", "report.json")
    before = {n: (report / n).read_bytes() for n in names if (report / n).exists()}
    assert "results.csv" in before and "modes.csv" in before
    _save_ring(ws_root, "b000000000001", [(F1, k1)])
    _export(ws_root)
    assert {n: (report / n).read_bytes() for n in before} == before
