"""The writer beside the analysis queue (C-save) and the workspace write lock (C-lock).

Every workspace is a generated BL43 one (``tests/intake_fixtures.py``), read in by the real ``isar ingest``;
the payloads are built by hand in the C-payload shape, with no fit. Nothing reads ``real_data/``.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

import intake_fixtures as fx
import isar
from isar.cli import ingest_cmd
from isar.intake import fit_inputs, inputs, scans, store, values
from isar.schema import _json
from isar.session import atomic, ingest, outcome, writer
from isar.session.atomic import WorkspaceBusy, workspace_write_lock
from isar.session.state import SessionState, canonical_hash

REPO = Path(__file__).resolve().parents[2]


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _ingest(ws, data) -> None:
    assert ingest_cmd.run(argparse.Namespace(data=[str(data)], workspace=str(ws.root), json=False)) == 0


@pytest.fixture
def bl43(tmp_path):
    """``(ws, data, units)``: a read-in BL43 workspace of two scans of three analyzers, ``units`` sorted."""
    ws, data = fx.bl43_workspace(tmp_path)
    _ingest(ws, data)
    return ws, data, sorted(ws.load()[1].scans)


def _units_of(ws, stem: str) -> list[str]:
    """The ledger units of the scan read from ``<STEM>_<stem>``."""
    scan = next(k for k, r in scans.read_scans(ws.root).items() if r["set"] == f"{fx.STEM}_{stem}")
    return sorted(u for u in ws.load()[1].scans if u.rsplit(":", 1)[0] == scan)


def _inputs_hash(root, unit: str) -> str:
    """The unit's inputs hash from the saved intake entries now (as a start point computes it)."""
    scan, det = unit.rsplit(":", 1)[0], unit.rsplit(":", 1)[1]
    row = scans.read_scans(root)[scan]
    read = store.read_all(root, store.ENTRIES)
    si = inputs.scan_inputs(row, inputs.index_entries(read.entries, lines=read.lines))
    return inputs.inputs_hash(inputs.unit_inputs(row, si, det, None))


def _fast(ws, unit: str, *, h="i0", fast_pass=None, base_hash=None) -> dict:
    """A fast payload for ``unit`` as fit_file builds it, from the stored entry at item start."""
    cur = ws.load()[1].get(unit)
    det = unit.rsplit(":", 1)[1]
    scan = unit.rsplit(":", 1)[0]
    entry = _json.to_jsonable(cur)
    entry["status"] = "fitted"
    entry["detectors"] = {det: {"verdict": "clean", "disposition": "accepted", "omega0": 4.0,
                                "fast_pass": fast_pass or {"pass": "fast", "unit": unit}}}
    entry["provenance"] = {"fit_inputs": {"run": "", "hash": h, "fallbacks": []}, "fit_seconds": 1.0}
    entry["measurement_record"] = {"scan_ref": unit, "count_trust": "trusted"}
    return {"schema": writer.SCHEMA, "unit": unit, "pass": "fast",
            "base": {"content_hash": cur.content_hash if base_hash is None else base_hash, "fast_id": None},
            "entry": entry, "deep": None,
            "fit_inputs": {"hash": h, "fallbacks": [], "condition": False,
                           "started": [{"unit": unit, "scan": scan, "pass": "fast", "inputs": {}, "hash": h,
                                        "fallbacks": []}],
                           "finished": [{"unit": unit, "scan": scan, "pass": "fast", "status": "fitted",
                                         "fallbacks": [], "result": {"verdict": "clean"}}]},
            "calibration": None, "resolution_version": None}


def _deep(unit: str, fast_payload: dict, *, h="i0", state="close_call") -> dict:
    det, scan = unit.rsplit(":", 1)[1], unit.rsplit(":", 1)[0]
    fp = fast_payload["entry"]["detectors"][det]["fast_pass"]
    return {"schema": writer.SCHEMA, "unit": unit, "pass": "deep",
            "base": {"content_hash": fast_payload["entry"]["content_hash"], "fast_id": canonical_hash(fp)},
            "entry": None, "deep": {"record": {"schema": "isar.deep-pass/1", "state": state},
                                    "timing": {"seconds": 2.0}},
            "fit_inputs": {"hash": h, "fallbacks": [], "condition": False,
                           "started": [{"unit": unit, "scan": scan, "pass": "deep", "inputs": {}, "hash": h,
                                        "fallbacks": []}],
                           "finished": [{"unit": unit, "scan": scan, "pass": "deep", "status": "fitted",
                                         "fallbacks": [], "result": {"state": state}}]},
            "calibration": None, "resolution_version": None}


def _bytes(path: Path) -> bytes:
    return path.read_bytes() if path.exists() else b""


def _csv_ids(ws) -> set[str]:
    import csv
    with open(ws.report_dir / "results.csv", encoding="utf-8", newline="") as f:
        return {row["scan_id"] for row in csv.DictReader(f)}


# ---------------------------------------------------------------------------
# C-lock
# ---------------------------------------------------------------------------
_HOLDER = """
import sys, time
from isar.session.atomic import workspace_write_lock
with workspace_write_lock(sys.argv[1]):
    print("held", flush=True)
    time.sleep(float(sys.argv[2]))
"""


def _holder(root, seconds: float) -> subprocess.Popen:
    p = subprocess.Popen([sys.executable, "-c", _HOLDER, str(root), str(seconds)], cwd=REPO,
                         stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def test_lock_waits_for_a_holder_in_another_process(tmp_path):
    p = _holder(tmp_path, 2.0)
    try:
        t0 = time.monotonic()
        with workspace_write_lock(tmp_path, timeout_s=5):
            waited = time.monotonic() - t0
        assert 1.0 < waited < 5
    finally:
        p.wait(10)


def test_lock_times_out_with_workspace_busy_and_writes_nothing(tmp_path):
    p = _holder(tmp_path, 3.0)
    try:
        before = sorted(x.name for x in tmp_path.iterdir())
        with pytest.raises(WorkspaceBusy, match=r"\.isar-write\.lock"):
            with workspace_write_lock(tmp_path, timeout_s=0.5):
                (tmp_path / "written").write_text("x", encoding="utf-8")
        assert sorted(x.name for x in tmp_path.iterdir()) == before
    finally:
        p.kill()
        p.wait(10)


def test_lock_is_released_when_its_holder_is_killed(tmp_path):
    p = _holder(tmp_path, 60.0)
    p.kill()
    p.wait(10)
    t0 = time.monotonic()
    with workspace_write_lock(tmp_path, timeout_s=5):
        assert time.monotonic() - t0 < 1.0


def test_lock_is_not_reentrant_and_a_body_store_busy_is_not_renamed(tmp_path):
    with workspace_write_lock(tmp_path):
        with pytest.raises(WorkspaceBusy):
            with workspace_write_lock(tmp_path, timeout_s=0.1):
                pass
        with pytest.raises(store.StoreBusy):          # raised by the body, not the take
            with workspace_write_lock(tmp_path / "other", timeout_s=0.1):
                raise store.StoreBusy("from the body")


# ---------------------------------------------------------------------------
# C-save examples
# ---------------------------------------------------------------------------
def test_1_a_save_keeps_a_unit_another_writer_added(bl43):
    ws, data, units = bl43
    u1 = units[0]
    p = _fast(ws, u1)
    fx.write_scan(data, stem=f"{fx.STEM}_C", seed=7, energy=fx.ENERGY_120)   # a later arrival
    _ingest(ws, data)
    added = sorted(set(ws.load()[1].scans) - set(units))
    assert added
    assert writer.save_results(ws.root, [p]) == [None]
    session = ws.load()[1]
    assert session.get(u1).status == "fitted" and session.get(u1).detectors
    assert all(session.get(u).status == "ingested" for u in added)
    assert {u1, *added} <= _csv_ids(ws)


def test_2_a_save_keeps_an_intake_value_stored_while_it_ran(bl43):
    ws, _data, units = bl43
    u1 = units[0]
    h = _inputs_hash(ws.root, u1)
    p = _fast(ws, u1, h=h)
    assert values.save_value(ws.root, "temperature", 120.0, [u1.rsplit(":", 1)[0]]).ok
    assert writer.save_results(ws.root, [p]) == [None]
    assert any(e.get("value") == 120.0 for e in store.read(ws.root, store.ENTRIES))
    stamp = ws.load()[1].get(u1).provenance["fit_inputs"]
    assert stamp["hash"] == h != _inputs_hash(ws.root, u1)       # the saved fit reads not current


def test_3_a_fast_save_on_reread_data_is_refused_and_writes_nothing(bl43):
    ws, _data, units = bl43
    u1 = units[0]
    p = _fast(ws, u1, base_hash="h0-at-start")       # the stored entry was re-registered since
    files = [ws.session_path, ws.root / fit_inputs.FIT_INPUTS, ws.root / fit_inputs.FIT_HISTORY,
             ws.report_dir / "measurements.jsonl", ws.state_path]
    before = [_bytes(f) for f in files]
    assert writer.save_results(ws.root, [p]) == [writer.FAST_STALE]
    assert [_bytes(f) for f in files] == before
    assert ws.load()[1].get(u1).status == "ingested"


def test_4_fast_then_deep_in_one_call_finish_in_one_run(bl43):
    ws, _data, units = bl43
    u1 = units[0]
    p = _fast(ws, u1)
    d = _deep(u1, p)
    assert writer.save_results(ws.root, [p, d]) == [None, None]
    entry = ws.load()[1].get(u1)
    det = u1.rsplit(":", 1)[1]
    assert entry.detectors[det]["deep_pass"]["state"] == "close_call"
    assert entry.provenance["deep_pass"] == {"seconds": 2.0}
    stamp = (entry.provenance["fit_inputs"]["run"], entry.provenance["fit_inputs"]["hash"])
    ledger = {u1: entry.provenance["fit_inputs"]}
    got = fit_inputs.latest_passes(fit_inputs.read_log(ws.root), ledger)[u1]
    assert set(got) == {"fast", "deep"} and {r.state for r in got.values()} == {"finished"}
    assert {r.run for r in got.values()} == {stamp[0]}
    assert fit_inputs.run_states(fit_inputs.read_log(ws.root))[stamp[0]].outcome == "complete"


def test_5_a_deep_save_on_a_replaced_fast_pass_is_refused(bl43):
    ws, _data, units = bl43
    u1 = units[0]
    p = _fast(ws, u1)
    first = _deep(u1, p, state="search_limited")
    assert writer.save_results(ws.root, [p, first]) == [None, None]
    stale = _deep(u1, _fast(ws, u1, fast_pass={"pass": "fast", "other": True}), state="close_call")
    assert writer.save_results(ws.root, [stale]) == [writer.DEEP_STALE]
    det = u1.rsplit(":", 1)[1]
    assert ws.load()[1].get(u1).detectors[det]["deep_pass"]["state"] == "search_limited"


def test_6_three_payloads_save_once(bl43, monkeypatch):
    ws, _data, units = bl43
    payloads = [_fast(ws, u) for u in units[:3]]
    calls = {"save": 0, "csv": 0}
    real_save, real_csv = SessionState.save, outcome._write_results_csv

    def save(self, path):
        calls["save"] += 1
        return real_save(self, path)

    def csv(w, s):
        calls["csv"] += 1
        return real_csv(w, s)
    monkeypatch.setattr(SessionState, "save", save)
    monkeypatch.setattr(writer, "_write_results_csv", csv)
    assert writer.save_results(ws.root, payloads) == [None] * 3
    assert calls == {"save": 1, "csv": 1}


def test_7_a_projection_failure_raises_projection_stale_and_the_next_save_rebuilds(bl43, monkeypatch):
    ws, _data, units = bl43
    u1, u2 = units[0], units[1]
    p = _fast(ws, u1)

    def held(_w, _s):
        raise PermissionError("results.csv is open in another program")
    monkeypatch.setattr(writer, "_write_results_csv", held)
    with pytest.raises(writer.ProjectionStale) as err:
        writer.save_results(ws.root, [p])
    assert err.value.verdicts == [None] and isinstance(err.value.cause, PermissionError)
    assert ws.load()[1].get(u1).status == "fitted"
    run = ws.load()[1].get(u1).provenance["fit_inputs"]["run"]
    assert fit_inputs.run_states(fit_inputs.read_log(ws.root))[run].outcome == "complete"
    monkeypatch.setattr(writer, "_write_results_csv", outcome._write_results_csv)
    assert writer.save_results(ws.root, [_fast(ws, u2)]) == [None]
    import csv
    with open(ws.report_dir / "results.csv", encoding="utf-8", newline="") as f:
        rows = {r["scan_id"]: r for r in csv.DictReader(f)}
    assert rows[u1]["status"] == "fitted" and rows[u2]["status"] == "fitted"


def test_8_an_attempt_alone_writes_only_its_measurement_line(bl43):
    ws, _data, units = bl43
    u1 = units[0]
    files = [ws.session_path, ws.root / fit_inputs.FIT_INPUTS, ws.state_path]
    before = [_bytes(f) for f in files]
    attempt = {"schema": writer.SCHEMA, "unit": u1, "pass": "attempt",
               "measurement_record": {"scan_ref": u1, "count_trust": "attempted"}}
    assert writer.save_results(ws.root, [attempt]) == [None]
    lines = (ws.report_dir / "measurements.jsonl").read_text(encoding="utf-8").splitlines()
    assert [json.loads(ln)["count_trust"] for ln in lines] == ["attempted"]
    assert [_bytes(f) for f in files] == before


def test_a_refit_over_a_fitted_entry_keeps_the_old_one_in_history(bl43):
    ws, _data, units = bl43
    u1 = units[0]
    assert writer.save_results(ws.root, [_fast(ws, u1)]) == [None]
    first = ws.load()[1].get(u1)
    assert writer.save_results(ws.root, [_fast(ws, u1, h="i1")]) == [None]
    hist = fit_inputs.read_history(ws.root)
    assert [(ln["unit"], ln["by"]) for ln in hist] == [(u1, "refit")]
    assert hist[0]["entry_hash"] == canonical_hash(_json.to_jsonable(first))
    assert hist[0]["run"] == ws.load()[1].get(u1).provenance["fit_inputs"]["run"]


def test_a_malformed_payload_raises_before_anything_is_written(bl43):
    ws, _data, units = bl43
    before = _bytes(ws.session_path)
    with pytest.raises(writer.PayloadError):
        writer.save_results(ws.root, [_fast(ws, units[0]), {"schema": "nope", "unit": units[1], "pass": "fast"}])
    assert _bytes(ws.session_path) == before


def test_calibration_state_is_stored_only_when_unchanged_since_start(bl43):
    ws, _data, units = bl43
    scan = units[0].rsplit(":", 1)[0]
    p = _fast(ws, units[0])
    p["calibration"] = {"scan": scan, "state": {"inputs_key": "k1"}, "replaces": None}
    assert writer.save_results(ws.root, [p]) == [None]
    assert ws.load()[0].state.tabulated_calibration[scan] == {"inputs_key": "k1"}
    late = _fast(ws, units[1])
    late["calibration"] = {"scan": scan, "state": {"inputs_key": "k2"}, "replaces": "k0"}  # started before k1
    assert writer.save_results(ws.root, [late]) == [None]
    assert ws.load()[0].state.tabulated_calibration[scan] == {"inputs_key": "k1"}


# ---------------------------------------------------------------------------
# the merge, both directions: isar ingest beside save_results
# ---------------------------------------------------------------------------
def _ingest_with_a_save_during_it(monkeypatch, ws, data, payloads) -> None:
    """``isar ingest`` with ``save_results`` landing between its load and its save."""
    real = ingest.ingest_batch

    def during(*a, **k):
        report = real(*a, **k)
        assert writer.save_results(ws.root, payloads) == [None] * len(payloads)
        return report
    monkeypatch.setattr(ingest, "ingest_batch", during)
    _ingest(ws, data)
    monkeypatch.setattr(ingest, "ingest_batch", real)


def test_merge_ingest_does_not_lose_a_saved_result(bl43, monkeypatch):
    ws, data, units = bl43
    u1 = units[0]
    p = _fast(ws, u1)
    fx.write_scan(data, stem=f"{fx.STEM}_C", seed=7, energy=fx.ENERGY_120)   # the new arrival
    _ingest_with_a_save_during_it(monkeypatch, ws, data, [p])
    session = ws.load()[1]
    assert session.get(u1).status == "fitted"
    assert [session.get(u).status for u in sorted(set(session.scans) - set(units))] == ["ingested"] * 3


def test_merge_a_saved_result_does_not_lose_ingests_reregistration(bl43, monkeypatch):
    ws, data, _units = bl43
    u1, u2 = _units_of(ws, "A")[0], _units_of(ws, "B")[0]
    old = ws.load()[1].get(u2).content_hash
    p = _fast(ws, u1)
    fx.write_scan(data, stem=f"{fx.STEM}_B", seed=42, energy=fx.ENERGY_120)   # B's data changed
    _ingest_with_a_save_during_it(monkeypatch, ws, data, [p])
    session = ws.load()[1]
    assert session.get(u1).status == "fitted"
    assert session.get(u2).status == "ingested" and session.get(u2).content_hash != old


def test_merge_a_fit_saved_during_a_reimport_enters_history(bl43, monkeypatch):
    ws, data, _units = bl43
    u2 = _units_of(ws, "B")[0]
    old = ws.load()[1].get(u2).content_hash
    p = _fast(ws, u2)
    fx.write_scan(data, stem=f"{fx.STEM}_B", seed=42, energy=fx.ENERGY_120)
    _ingest_with_a_save_during_it(monkeypatch, ws, data, [p])
    entry = ws.load()[1].get(u2)
    assert entry.status == "ingested" and entry.content_hash != old
    hist = [ln for ln in fit_inputs.read_history(ws.root) if ln["unit"] == u2]
    assert [(ln["by"], ln["entry"]["status"]) for ln in hist] == [("reimport", "fitted")]
