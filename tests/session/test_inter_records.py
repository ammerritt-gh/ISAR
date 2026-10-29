"""The inter-spectra machine store (C-records) and its one writer, ``writer.save_inter`` (C-save-inter).

Every workspace is S1U1's synthetic BL43-like one (``tests/inter_fixtures.py``), generated at test time; the
payloads are built by hand in the ``isar.inter-result/1`` shape, with no fit. Nothing reads ``real_data/``.
"""
from __future__ import annotations

import json
from dataclasses import astuple
import re
from pathlib import Path

import pytest

import isar
from inter_fixtures import build_inter_workspace, spectrum_keys
from isar.intake import inputs, inter_values, scans as intake_scans, store
from isar.intake.inter_spectra import read_user
from isar.schema.card import load_state
from isar.session import file_fit, writer
from isar.session.inter_spectra import records

REPO = Path(__file__).resolve().parents[2]


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
@pytest.fixture
def ws(tmp_path):
    """A small fixture workspace: one q setting at 100 K and 200 K, three analyzers, each with a fast solo."""
    return build_inter_workspace(tmp_path, temperatures=(100, 200), setups=("0p5_0p5_3",), n_analyzers=3)


def _now(root, units):
    """The inputs hashes the writer computes now (the same reads it makes under the lock)."""
    read = store.read_all(root, store.ENTRIES)
    return records.inputs_now_from(intake_scans.read_scans(root), inputs.index_entries(read.entries, lines=read.lines),
                                   load_state(Path(root) / "card.state.json").tabulated_calibration, units)


def _start(batch, intake_seq=0):
    return {"schema": records.SCHEMA_SAVE, "unit": f"inter:{batch}:batch", "pass": "inter", "batch": batch,
            "event": "start",
            "start": {"batch_kind": "anchored", "scope": {"direction": "q", "members": []}, "anchors": [],
                      "rings": {}, "seams": [], "settings": {"version": 1, "values": {}}, "preview": "test",
                      "fixed": {}, "unreached": {}, "terms": [], "metric": {}, "core": "test", "inputs_at": 0,
                      "intake_seq": intake_seq, "epochs": {}}}


def _refit(unit, fit, inputs_hash, *, status="accepted", cond=(), overlay=True):
    return {"fit": fit, "unit": unit, "status": status, "reason": None, "seed": "fast", "inputs": inputs_hash,
            "conditioned_on": [{"unit": u, "fit": f, "inputs": h} for u, f, h in cond],
            "flag": {"score": 0.5, "flagged": False, "reasons": []}, "why_changed": None,
            "payload": {"came_in": {"unit": unit, "x": float("nan")}, "came_out": {"unit": unit},
                        "overlay": {"n_modes": 1, "unit": unit} if overlay else None}}


def _stage(batch, refits, index=1):
    return {"schema": records.SCHEMA_SAVE, "unit": f"inter:{batch}:batch", "pass": "inter", "batch": batch,
            "event": "stage",
            "stage": {"stage": "ring", "index": index, "round": None, "refits": refits, "seams": [],
                      "progress": {"done": len(refits), "failed": 0, "not_reached": 0, "fixed": 0, "total": 9},
                      "not_reached": {}}}


def _end(batch, state="completed"):
    return {"schema": records.SCHEMA_SAVE, "unit": f"inter:{batch}:batch", "pass": "inter", "batch": batch,
            "event": "end", "end": {"state": state, "reason": None, "done": 1, "failed": 0, "not_reached": [],
                                    "rings_done": 1, "sweeps": None, "rounds": None, "timing": {}}}


def _files(root):
    """The bytes of the index and every payload file."""
    root = Path(root)
    out = {records.LOG: (root / records.LOG).read_bytes() if (root / records.LOG).exists() else b""}
    for p in sorted((root / records.PAYLOAD_DIR).glob("*.jsonl")):
        out[p.name] = p.read_bytes()
    return out


def _calibrate(root, scan, det, zero):
    """Change a scan's stored tabulated calibration, as a recalibration saved by another writer does."""
    path = Path(root) / "card.state.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    d.setdefault("tabulated_calibration", {})[scan] = {"setting": "s1", "setting_basis": "declared",
                                                        "detectors": {det: {"zero": zero, "zero_source": "test"}}}
    path.write_text(json.dumps(d), encoding="utf-8", newline="\n")


def _accept(root, fit):
    return next(r.accept for r in records.read_log(root).refits if r.fit == fit)


# ---------------------------------------------------------------------------
# ids
# ---------------------------------------------------------------------------
def test_new_ids_are_b_or_f_and_twelve_hex_digits():
    assert re.fullmatch(r"b[0-9a-f]{12}", records.new_batch_id())
    assert re.fullmatch(r"f[0-9a-f]{12}", records.new_fit_id())
    assert records.new_fit_id() != records.new_fit_id()


# ---------------------------------------------------------------------------
# worked examples of C-save-inter
# ---------------------------------------------------------------------------
def test_an_unlocked_refit_with_equal_inputs_is_authoritative_and_reads_back(ws):
    unit, nb = spectrum_keys(ws)[:2]
    now = _now(ws, [unit, nb])
    inter_values.set_anchors(ws, [nb], anchored=True)          # one intake entry: max seq 1
    batch, fit = records.new_batch_id(), records.new_fit_id()
    v = writer.save_inter(ws, [_start(batch, intake_seq=1),
                               _stage(batch, [_refit(unit, fit, now[unit], cond=[(nb, "fast", now[nb])])]),
                               _end(batch)])
    assert v[0] == {"saved": True, "accepted": {}, "refused": {}}
    assert v[1] == {"saved": True, "accepted": {unit: fit}, "refused": {}}
    log = records.read_log(ws)
    assert log.damaged == ()
    info = log.batches[batch]
    assert (info.number, info.batch_kind, info.intake_seq, info.end["state"]) == (1, "anchored", 1, "completed")
    (line,) = log.refits
    assert line.accept == records.Accept("authoritative", None, None, 1)
    assert line.conditioned_on == (records.Cond(nb, "fast", now[nb]),)
    assert line.flag == records.Flag(0.5, False, ())
    assert (line.stage, line.index, line.status, line.inputs) == ("ring", 1, "accepted", now[unit])
    assert len(log.stages) == 1 and log.stages[0].progress["done"] == 1
    payload = records.read_payload(ws, line)
    assert payload["overlay"] == {"n_modes": 1, "unit": unit}
    assert payload["came_in"]["x"] is None                    # strict JSON: a non-finite float is written as null
    # a second batch is numbered after the first
    b2 = records.new_batch_id()
    writer.save_inter(ws, [_start(b2)])
    assert records.read_log(ws).batches[b2].number == 2


def test_a_refit_of_a_spectrum_locked_by_the_user_is_declined_but_kept(ws):
    unit = spectrum_keys(ws)[0]
    batch, fit, old = records.new_batch_id(), records.new_fit_id(), records.new_fit_id()
    writer.save_inter(ws, [_start(batch)])
    assert inter_values.set_lock(ws, unit, locked=True, fit_id=old).ok
    (v,) = writer.save_inter(ws, [_stage(batch, [_refit(unit, fit, _now(ws, [unit])[unit])])])
    assert v == {"saved": True, "accepted": {}, "refused": {unit: "locked since the batch started"}}
    assert _accept(ws, fit) == records.Accept("declined", "locked", old, read_user(ws).max_seq)
    assert records.read_payload(ws, records.read_log(ws).refits[0])["overlay"] is not None


def test_changed_own_or_neighbour_inputs_decline_the_refit(ws):
    keys = spectrum_keys(ws)
    a, c, b = keys[0], keys[1], keys[3]                       # b is of the other scan
    scan, det_b = b.rsplit(":", 1)
    now = _now(ws, [a, b, c])
    batch = records.new_batch_id()
    writer.save_inter(ws, [_start(batch)])
    _calibrate(ws, scan, det_b, 0.25)                         # b's scan is recalibrated during the batch
    assert _now(ws, [b])[b] != now[b] and _now(ws, [a])[a] == now[a]
    own, nbr = records.new_fit_id(), records.new_fit_id()
    (v,) = writer.save_inter(ws, [_stage(batch, [_refit(b, own, now[b]),
                                                 _refit(a, nbr, now[a], cond=[(c, "fast", now[c]),
                                                                              (b, "fast", now[b])])])])
    assert v["accepted"] == {}
    assert v["refused"] == {b: "its inputs changed during the batch",
                            a: f"the inputs of {b} changed during the batch"}
    assert astuple(_accept(ws, own))[:3] == ("declined", "inputs_changed", None)
    assert astuple(_accept(ws, nbr))[:3] == ("declined", "neighbour_inputs_changed", b)


def test_a_neighbour_declined_earlier_in_the_same_call_declines_its_dependants(ws):
    a, b = spectrum_keys(ws)[:2]
    now = _now(ws, [a, b])
    batch, fb, fa = records.new_batch_id(), records.new_fit_id(), records.new_fit_id()
    inter_values.set_lock(ws, b, locked=True, fit_id="fast")   # before the start: only the decline in this call counts
    writer.save_inter(ws, [_start(batch, intake_seq=read_user(ws).max_seq)])
    v = writer.save_inter(ws, [_stage(batch, [_refit(b, fb, now[b])], index=1),
                               _stage(batch, [_refit(a, fa, now[a], cond=[(b, fb, now[b])])], index=2)])
    assert v[1]["refused"] == {a: f"the fit of {b} it was conditioned on is no longer the one in use"}
    assert astuple(_accept(ws, fa))[:3] == ("declined", "neighbour_fit_changed", b)


def test_a_pick_after_the_start_naming_another_fit_declines_one_before_it_does_not(ws):
    a, b, c = spectrum_keys(ws)[:3]
    now = _now(ws, [a, b, c])
    other = records.new_fit_id()
    inter_values.pick_fit(ws, c, other)                       # before the start: seq 1
    batch = records.new_batch_id()
    writer.save_inter(ws, [_start(batch, intake_seq=read_user(ws).max_seq)])
    inter_values.pick_fit(ws, b, other)                       # after the start, naming a fit other than "fast"
    fa, fa2 = records.new_fit_id(), records.new_fit_id()
    (v,) = writer.save_inter(ws, [_stage(batch, [_refit(a, fa, now[a], cond=[(c, "fast", now[c])])])])
    assert v["accepted"] == {a: fa}                           # c's pick predates the batch: it saw it
    (v,) = writer.save_inter(ws, [_stage(batch, [_refit(a, fa2, now[a], cond=[(b, "fast", now[b])])], index=2)])
    assert v["refused"] == {a: f"the fit of {b} it was conditioned on is no longer the one in use"}
    assert astuple(_accept(ws, fa2))[:3] == ("declined", "neighbour_fit_changed", b)


def test_a_failed_refit_is_logged_with_no_acceptance_and_its_came_in(ws):
    unit = spectrum_keys(ws)[0]
    batch, fit = records.new_batch_id(), records.new_fit_id()
    bad = _refit(unit, fit, None, status="failed", overlay=False)
    bad["payload"] = {"came_in": {"unit": unit}}
    v = writer.save_inter(ws, [_start(batch), _stage(batch, [bad])])
    assert v[1] == {"saved": True, "accepted": {}, "refused": {}}
    (line,) = records.read_log(ws).refits
    assert (line.status, line.accept) == ("failed", None)
    assert records.read_payload(ws, line) == {"came_in": {"unit": unit}, "came_out": None, "overlay": None}


def test_saving_the_same_payloads_twice_returns_the_same_decisions_and_writes_nothing(ws):
    a, b = spectrum_keys(ws)[:2]
    now = _now(ws, [a, b])
    batch = records.new_batch_id()
    inter_values.set_lock(ws, b, locked=True, fit_id="fast")
    payloads = [_start(batch), _stage(batch, [_refit(a, records.new_fit_id(), now[a]),
                                              _refit(b, records.new_fit_id(), now[b])]), _end(batch)]
    first = writer.save_inter(ws, payloads)
    before = _files(ws)
    assert writer.save_inter(ws, payloads) == first
    assert _files(ws) == before
    assert first[1]["refused"] == {b: "locked since the batch started"} and list(first[1]["accepted"]) == [a]


def test_a_stage_of_an_unknown_batch_is_refused_and_writes_nothing(ws):
    unit = spectrum_keys(ws)[0]
    batch = records.new_batch_id()
    v = writer.save_inter(ws, [_stage(batch, [_refit(unit, records.new_fit_id(), None)]), _end(batch)])
    assert v == [f"unknown batch {batch}", f"unknown batch {batch}"]
    assert _files(ws) == {records.LOG: b""}


def test_a_malformed_payload_is_refused_alone_and_a_non_sequence_raises(ws):
    unit = spectrum_keys(ws)[0]
    batch = records.new_batch_id()
    no_overlay = _stage(batch, [_refit(unit, records.new_fit_id(), None, overlay=False)])
    bad_fit = _stage(batch, [_refit(unit, "fast", None)])
    v = writer.save_inter(ws, [_start(batch), no_overlay, bad_fit])
    assert v[0]["saved"] is True and isinstance(v[1], str) and isinstance(v[2], str)
    assert records.read_log(ws).refits == ()
    with pytest.raises(writer.PayloadError):
        writer.save_inter(ws, _start(batch))


def test_a_half_written_last_index_line_is_skipped_and_listed_as_damaged(ws):
    unit = spectrum_keys(ws)[0]
    batch, fit = records.new_batch_id(), records.new_fit_id()
    writer.save_inter(ws, [_start(batch), _stage(batch, [_refit(unit, fit, None)])])
    log_path = Path(ws) / records.LOG
    whole = log_path.read_bytes()
    with open(log_path, "ab") as f:
        f.write(b'{"schema":"isar.inter-log/1","kind":"refit","batch":"' + batch.encode() + b'","fi')
    log = records.read_log(ws)
    assert [r.fit for r in log.refits] == [fit]
    assert [d.line for d in log.damaged] == [len(whole.splitlines()) + 1]
    # the next save ends the torn line first, so its own lines stay whole
    writer.save_inter(ws, [_end(batch)])
    assert records.read_log(ws).batches[batch].end is not None


# ---------------------------------------------------------------------------
# joint worked example: the fingerprint agrees at save time (shared with S1U6's runner test)
# ---------------------------------------------------------------------------
def test_the_fingerprint_file_fit_stamps_is_the_one_save_inter_recomputes(ws):
    unit, other = spectrum_keys(ws)[0], spectrum_keys(ws)[-1]
    from isar.session import currentness
    seen = []
    judge = currentness.judge

    def spy(prev, det, now, request):
        seen.append(now)
        return judge(prev, det, now, request)

    results = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(currentness, "judge", spy)
        file_fit.fit_file(ws, file_fit.FileItem("i1", unit, "fast", 1), progress=None, emit=results.append)
    # the stamped fast solo is current, so file_fit computes the same fingerprint and refits nothing
    assert [r.status for r in results] == ["already_current"]
    from isar.session.workspace import Workspace
    _, session = Workspace.open(ws).load()
    (stamped,) = {now.inputs_hash for now in seen}
    # (1) what file_fit stamps == what save_inter recomputes == the stored fast solo's fingerprint
    assert stamped is not None
    assert _now(ws, [unit])[unit] == stamped == session.scans[unit].provenance["fit_inputs"]["hash"]
    batch, fit = records.new_batch_id(), records.new_fit_id()
    (_, v) = writer.save_inter(ws, [_start(batch), _stage(batch, [_refit(unit, fit, stamped)])])
    assert v["accepted"] == {unit: fit}
    # (2) a calibration change between refit start and save: declined as inputs_changed
    scan, det = unit.rsplit(":", 1)
    _calibrate(ws, scan, det, 0.5)
    assert _now(ws, [unit])[unit] != stamped
    fit2 = records.new_fit_id()
    (v,) = writer.save_inter(ws, [_stage(batch, [_refit(unit, fit2, stamped)], index=2)])
    assert v["refused"] == {unit: "its inputs changed during the batch"}
    assert astuple(_accept(ws, fit2))[:2] == ("declined", "inputs_changed")
    # (3) outside BL43 (no import rows) nothing is judged: None, and the refit is accepted
    (Path(ws) / intake_scans.INTAKE_SCANS).unlink()
    assert _now(ws, [unit, other]) == {unit: None, other: None}
    fit3 = records.new_fit_id()
    (v,) = writer.save_inter(ws, [_stage(batch, [_refit(other, fit3, None)], index=3)])
    assert v["accepted"] == {other: fit3}
