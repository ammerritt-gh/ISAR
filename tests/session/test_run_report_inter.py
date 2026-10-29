"""The run report shows each spectrum's authoritative fit (S1U11, D6): the triage row, its fit label and its flag.

Qt-free, on the S1U1 fixture workspace; refits are hand-written log lines and payloads, saved through the real writer
(no fitting, hard rule 10). report.json and the solo counts stay the fast pass's.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import isar
from inter_fixtures import build_inter_workspace, spectrum_keys
from isar.intake import inputs, inter_values, scans as intake_scans, store, values
from isar.intake.inter_spectra import read_user
from isar.report import run_report
from isar.schema.card import load_state
from isar.session import writer
from isar.session.inter_spectra import records
from isar.session.workspace import Workspace

REPO = Path(__file__).resolve().parents[2]
F1 = "f0000000000a1"
OVERLAY = {"n_modes": 1, "omega0": 3.5, "best_label": "1 mode",
           "modes": [{"omega0": 3.5, "omega0_err": 0.2, "gamma": 0.4, "gamma_err": 0.1}],
           "fast_pass": {"state": "resolved", "peaks": []}}
OVERLAY_IV = {**OVERLAY, "interval": [3.3, 3.5, 3.7]}
FLAG = {"score": 2.0, "flagged": True, "reasons": [{"code": "moved", "severity": "warn", "text": "moved"}]}


def test_the_worktree_is_the_import_root():
    print("isar imported from", isar.__file__)
    assert Path(isar.__file__).resolve().parents[1] == REPO


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return build_inter_workspace(tmp_path_factory.mktemp("rri"), temperatures=(100, 200), setups=("0p5_0p5_3",),
                                 n_analyzers=3)


@pytest.fixture
def ws_root(built, tmp_path):
    copy = tmp_path / "rri"
    shutil.copytree(built.parent, copy)
    return copy / built.name


def _hash(root, unit):
    read = store.read_all(root, store.ENTRIES)
    return records.inputs_now_from(intake_scans.read_scans(root), inputs.index_entries(read.entries, lines=read.lines),
                                   load_state(root / "card.state.json").tabulated_calibration, [unit])[unit]


def _save_ring(root, batch, refits, overlay=OVERLAY, number_flag=None):
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


def _write(root, edit=lambda session: None):
    """Write the run report of the workspace; return (report.md, report.json bytes, the session). ``edit`` may
    change the in-memory session first (the report reads only what it is handed)."""
    ws = Workspace.open(root)
    card, session = ws.load()
    edit(session)
    run_report.write_run_report(session, card, ws, formats=("markdown",))
    return ((ws.report_dir / "report.md").read_text(encoding="utf-8"),
            (ws.report_dir / "report.json").read_bytes(), session)


def _triage(md):
    """The triage table as one dict per row, keyed by its header cells."""
    lines = [ln for ln in md.split("## Triage table", 1)[1].splitlines() if ln.startswith("|")]
    head = [c.strip() for c in lines[0].strip("|").split("|")]
    return [dict(zip(head, [c.strip() for c in ln.strip("|").split("|")])) for ln in lines[2:]]


def _row(md, root, unit):
    scan = Workspace.open(root).load()[1].scans[unit].scan_number
    return next(r for r in _triage(md) if r["scan"] == str(scan))


def test_a_refit_in_use_is_its_triage_row_with_its_label_and_flag(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    solo = _row(_write(ws_root)[0], ws_root, k1)
    _save_ring(ws_root, "b000000000001", [(F1, k1)], overlay=OVERLAY_IV, number_flag=FLAG)
    md, _, _ = _write(ws_root)
    row = _row(md, ws_root, k1)
    assert (row["verdict"], row["disp"], row["fit"]) == ("inter-spectra", "flagged", "Batch 1 · ring 1")
    assert (row["ω₀"], row["68% interval"]) == ("3.500", "[3.30, 3.70]")          # the refit's numbers, not the solo's
    assert row["ω₀"] != solo["ω₀"]
    assert row["conf"] == "—" and "conditional" in md                              # the refit's error is conditional
    assert (ws_root / "report" / "figures" / "dispersion.png").exists()


def _solo_interval(unit):
    def edit(session):          # the fixture's solo fit has no interval; give it one so a leak would show
        next(iter(session.scans[unit].detectors.values()))["interval"] = [4.8, 5.0, 5.2]
    return edit


def test_a_refit_without_its_own_interval_shows_none_not_the_solo_one(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    assert _row(_write(ws_root, _solo_interval(k1))[0], ws_root, k1)["68% interval"] == "[4.80, 5.20]"
    _save_ring(ws_root, "b000000000001", [(F1, k1)], overlay=OVERLAY)
    row = _row(_write(ws_root, _solo_interval(k1))[0], ws_root, k1)
    assert row["fit"] == "Batch 1 · ring 1" and row["68% interval"] == "—"


def test_report_json_and_the_solo_head_do_not_change_and_unrefitted_rows_say_fast_solo(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    md0, json0, _ = _write(ws_root)
    assert {r["fit"] for r in _triage(md0)} == {"Fast solo"}
    _save_ring(ws_root, "b000000000001", [(F1, k1)], number_flag=FLAG)
    md1, json1, _ = _write(ws_root)
    assert json1 == json0                                                          # report.json is the solo fit's
    assert md1.split("## Triage table", 1)[0] == md0.split("## Triage table", 1)[0]  # head counts: the solo fit's
    others = [r for r in _triage(md1) if r["fit"] == "Fast solo"]
    assert len(others) == len(_triage(md1)) - 1


def test_a_pick_of_the_solo_fit_keeps_the_solo_row_bit_for_bit(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    solo = _row(_write(ws_root)[0], ws_root, k1)
    _save_ring(ws_root, "b000000000001", [(F1, k1)])
    assert inter_values.pick_fit(ws_root, k1, "fast").ok
    row = _row(_write(ws_root)[0], ws_root, k1)
    assert row["fit"] == "Your pick: Fast solo"
    assert {c: v for c, v in row.items() if c != "fit"} == {c: v for c, v in solo.items() if c != "fit"}


def test_a_stale_refit_gives_way_to_the_solo_row_and_says_why(ws_root):
    k1 = spectrum_keys(ws_root)[0]
    solo = _row(_write(ws_root)[0], ws_root, k1)
    _save_ring(ws_root, "b000000000001", [(F1, k1)])
    assert values.save_value(ws_root, "temperature", 97, [k1.rsplit(":", 1)[0]]).ok
    row = _row(_write(ws_root)[0], ws_root, k1)
    assert row["fit"].startswith("Fast solo (Batch 1 · ring 1 is out of date")
    assert row["verdict"] == solo["verdict"] and row["ω₀"] == solo["ω₀"]
