"""The inter-spectra resolver (C-authority): the table of the plan's section 2, row by row, with no files, plus
``overlay_outcome``, ``fit_label`` and ``effective`` on the S1U1 fixture workspace.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

import isar
from inter_fixtures import build_inter_workspace, spectrum_keys
from isar.intake import inter_values
from isar.intake.inter_spectra import InterUser, LockState, Pick, read_user
from isar.session import outcome as outcome_mod, writer
from isar.session.inter_spectra import authority, records
from isar.session.inter_spectra.authority import Stale, resolve, resolve_all
from isar.session.inter_spectra.records import Accept, BatchInfo, Cond, InterLog, RefitLine

REPO = Path(__file__).resolve().parents[2]
U = "spec1:42:a06"
N = "spec1:42:a07"


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


def line(fit, *, unit=U, status="accepted", seen=0, decision="authoritative", reason=None, detail=None,
         inputs="h", cond=(), batch="b000000000001", stage="ring", index=1):
    accept = None if decision is None else Accept(decision, reason, detail, seen)
    return RefitLine(batch, fit, unit, "t", stage, index, status, None, "fast", inputs,
                     tuple(Cond(*c) for c in cond), None, None, accept, None)


def res(refits=(), *, pick=None, lock=None, has_fast=True, now=None):
    return resolve(U, refits=list(refits), pick=pick, lock=lock, has_fast=has_fast,
                   inputs_now={U: "h"} if now is None else now)


def where(r):
    return r.fit, r.source


# ---------------------------------------------------------------------------
# the table (section 2)
# ---------------------------------------------------------------------------
def test_none_is_the_fast_solo():
    r = res()
    assert where(r) == ("fast", "fast_solo") and r.conditions and r.stale is None and r.notes == ()


def test_a_batch_acceptance_is_in_use():
    assert where(res([line("f1", seen=3)])) == ("f1", "batch")


def test_a_pick_after_the_acceptance_wins():
    assert where(res([line("f1", seen=3)], pick=Pick("fast", 5))) == ("fast", "pick")


def test_a_batch_that_saw_the_pick_replaces_it():
    assert where(res([line("f2", seen=5)], pick=Pick("fast", 5))) == ("f2", "batch")


def test_the_race_the_pick_came_after_the_check():
    assert where(res([line("f2", seen=4)], pick=Pick("fast", 5))) == ("fast", "pick")


def test_an_older_pick_of_f1_beats_f2_and_f2_is_not_stale_because_of_it():
    refits = [line("f1", seen=0), line("f2", seen=1)]
    r = res(refits, pick=Pick("f1", 2))
    assert where(r) == ("f1", "pick") and r.stale is None
    assert where(res(refits)) == ("f2", "batch")                 # without the pick the newest acceptance wins


def test_the_newest_acceptance_wins_by_log_order_at_equal_seq():
    assert res([line("f1", seen=2), line("f2", seen=2)]).fit == "f2"


def test_a_declined_refit_is_pickable_but_not_authoritative():
    refits = [line("f1", seen=0), line("f2", seen=1, decision="declined", reason="locked", detail="f1")]
    assert where(res(refits)) == ("f1", "batch")
    assert where(res(refits, pick=Pick("f2", 4))) == ("f2", "pick")


def test_a_lock_pins_its_fit_against_a_later_acceptance():
    r = res([line("f1", seen=0), line("f2", seen=9)], lock=LockState(True, "f1", 3))
    assert where(r) == ("f1", "lock") and r.locked and r.lock_fit == "f1" and r.conditions


def test_a_lock_on_the_solo_fit():
    r = res([line("f1", seen=0)], lock=LockState(True, "fast", 3))
    assert where(r) == ("fast", "lock")


def test_an_unlocked_state_does_not_lock():
    r = res([line("f1", seen=0)], lock=LockState(False, None, 7))
    assert where(r) == ("f1", "batch") and not r.locked


def test_stale_own_inputs_fall_back_to_the_solo_fit():
    r = res([line("f1")], now={U: "other"})
    assert where(r) == ("fast", "fallback") and r.stale == Stale("inputs_changed", U)
    assert r.chosen == "f1" and r.conditions


def test_stale_neighbour_inputs_fall_back_to_the_solo_fit():
    r = res([line("f1", cond=[(N, "fast", "n1")])], now={U: "h", N: "n2"})
    assert where(r) == ("fast", "fallback") and r.stale == Stale("neighbour_inputs_changed", N)


def test_own_staleness_is_named_before_a_neighbours():
    r = res([line("f1", cond=[(N, "fast", "n1")])], now={U: "x", N: "n2"})
    assert r.stale == Stale("inputs_changed", U)


def test_a_locked_stale_fit_stays_chosen_and_stops_conditioning():
    r = res([line("f1")], lock=LockState(True, "f1", 3), now={U: "other"})
    assert where(r) == ("f1", "lock") and r.stale == Stale("inputs_changed", U) and not r.conditions


def test_an_unjudged_unit_is_never_stale():
    r = res([line("f1", inputs=None)], now={U: None})
    assert where(r) == ("f1", "batch") and r.stale is None and "its inputs are not judged" in r.notes
    assert res([line("f1")], now={}).stale is None            # no hash known for the unit at all


def test_a_dangling_pick_is_ignored_with_a_note():
    r = res([line("f1", seen=0)], pick=Pick("f0123456789ab", 4))
    assert where(r) == ("f1", "batch") and any("pick" in n for n in r.notes)


def test_a_lock_naming_a_missing_fit_falls_through_with_a_note():
    r = res([line("f1")], lock=LockState(True, "f0123456789ab", 4), now={U: "other"})
    assert where(r) == ("fast", "fallback") and any("lock" in n for n in r.notes)    # not a locked-stale fit


def test_rejected_and_failed_refits_are_never_candidates():
    refits = [line("f1", status="rejected", decision=None), line("f2", status="failed", decision=None)]
    assert where(res(refits)) == ("fast", "fast_solo")
    assert where(res(refits, pick=Pick("f1", 3))) == ("fast", "fast_solo")      # a pick of one is dangling


def test_other_spectra_refits_are_ignored():
    assert where(res([line("f1", unit=N)])) == ("fast", "fast_solo")


def test_no_solo_fit_and_nothing_chosen():
    r = res(has_fast=False)
    assert r.fit is None and r.chosen is None
    assert resolve(U, refits=[line("f1")], pick=None, lock=None, has_fast=False, inputs_now={U: "x"}).fit is None


def test_resolve_all_covers_refitted_picked_and_locked_units_only():
    log = InterLog({}, (line("f1", unit=U), line("f2", unit=N, inputs="n")), (), ())
    user = InterUser((), {"spec1:42:a08": Pick("fast", 1)}, {"spec1:42:a09": LockState(True, "fast", 2),
                                                             "spec1:42:a10": LockState(False, None, 3)}, {}, 3, ())
    out = resolve_all(log, user, {U: "h", N: "n"}, {U, N, "spec1:42:a08", "spec1:42:a09", "spec1:42:a10"})
    assert sorted(out) == [U, N, "spec1:42:a08", "spec1:42:a09"] and out[U].fit == "f1"
    assert out["spec1:42:a09"].source == "lock"


# ---------------------------------------------------------------------------
# labels, overlay, the solo-verdict keys
# ---------------------------------------------------------------------------
def test_fit_label_words():
    batches = {"b000000000001": dataclasses.replace(_batch(), number=2)}
    assert authority.fit_label(None, batches) == "Fast solo"
    assert authority.fit_label(line("f1"), batches) == "Batch 2 · ring 1"
    assert authority.fit_label(line("f1", stage="sweep", index=4), batches) == "Batch 2 · sweep 4"


def _batch():
    return BatchInfo("b000000000001", 1, "t", "anchored", {}, (), {}, (), {}, "", {}, {}, (), {}, "", 0, {}, None)


def test_solo_verdict_keys_are_real_fitoutcome_fields_and_none_leaks():
    fields = {f.name for f in dataclasses.fields(outcome_mod.FitOutcome)}
    assert set(authority.SOLO_VERDICT_KEYS) <= fields and set(authority.OVERLAY_KEYS) <= fields
    assert not set(authority.SOLO_VERDICT_KEYS) & set(authority.OVERLAY_KEYS)
    solo = {k: f"solo-{k}" for k in fields}
    overlay = {k: f"refit-{k}" for k in authority.OVERLAY_KEYS}
    out = authority.overlay_outcome(solo, overlay)
    assert all(out[k] is None for k in authority.SOLO_VERDICT_KEYS)          # no solo verdict leaks
    assert all(out[k] == f"refit-{k}" for k in authority.OVERLAY_KEYS)
    assert out["detector_id"] == "solo-detector_id" and solo["verdict"] == "solo-verdict"   # the rest kept; input intact


# ---------------------------------------------------------------------------
# effective(), on the fixture workspace
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def ws(tmp_path_factory):
    return build_inter_workspace(tmp_path_factory.mktemp("auth"), temperatures=(100, 200), setups=("0p5_0p5_3",),
                                 n_analyzers=3)


def _save_ring(root, batch, refits, overlay=True):
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
                                   "inputs": h, "conditioned_on": [], "flag": None, "why_changed": None,
                                   "payload": {"came_in": {}, "came_out": {},
                                               "overlay": {"n_modes": 2, "omega0": 3.5, "best_label": "2 modes"}
                                               if overlay else None}} for f, u, h in refits]}}
    return writer.save_inter(root, [start, stage])


def _hashes(root, units):
    from isar.intake import inputs, scans as intake_scans, store
    from isar.schema.card import load_state
    read = store.read_all(root, store.ENTRIES)
    return records.inputs_now_from(intake_scans.read_scans(root),
                                   inputs.index_entries(read.entries, lines=read.lines),
                                   load_state(Path(root) / "card.state.json").tabulated_calibration, units)


def _outcomes(root, keys):
    from isar.session.workspace import Workspace
    _, session = Workspace.open(root).load()
    return {k: dict(next(iter(session.scans[k].detectors.values()))) for k in keys}


def test_effective_overlays_the_refit_and_leaves_other_units_out(tmp_path):
    root = build_inter_workspace(tmp_path, temperatures=(100, 200), setups=("0p5_0p5_3",), n_analyzers=3)
    k1, k2 = spectrum_keys(root)[:2]
    now = _hashes(root, [k1, k2])
    _save_ring(root, "b000000000001", [("f0000000000a1", k1, now[k1])])
    outcomes = _outcomes(root, [k1, k2])
    from isar.schema.card import load_state
    out = authority.effective(root, outcomes, load_state(root / "card.state.json").tabulated_calibration)
    assert list(out) == [k1]
    e = out[k1]
    assert e.resolution.fit == "f0000000000a1" and e.label == "Batch 1 · ring 1" and e.line.fit == e.resolution.fit
    assert e.outcome["n_modes"] == 2 and e.outcome["best_label"] == "2 modes" and e.outcome["verdict"] is None
    assert outcomes[k1]["verdict"] is not None                       # the stored solo outcome is untouched


def test_effective_a_pick_of_the_solo_fit_returns_the_solo_outcome(tmp_path):
    root = build_inter_workspace(tmp_path, temperatures=(100, 200), setups=("0p5_0p5_3",), n_analyzers=3)
    k1 = spectrum_keys(root)[0]
    _save_ring(root, "b000000000001", [("f0000000000a1", k1, _hashes(root, [k1])[k1])])
    assert inter_values.pick_fit(root, k1, "fast").ok
    outcomes = _outcomes(root, [k1])
    from isar.schema.card import load_state
    e = authority.effective(root, outcomes, load_state(root / "card.state.json").tabulated_calibration)[k1]
    assert (e.resolution.fit, e.resolution.source, e.label, e.line) == ("fast", "pick", "Fast solo", None)
    assert e.outcome == outcomes[k1]


def test_effective_falls_back_when_the_overlay_is_gone(tmp_path):
    root = build_inter_workspace(tmp_path, temperatures=(100, 200), setups=("0p5_0p5_3",), n_analyzers=3)
    k1 = spectrum_keys(root)[0]
    _save_ring(root, "b000000000001", [("f0000000000a1", k1, _hashes(root, [k1])[k1])])
    (root / records.PAYLOAD_DIR / "b000000000001.jsonl").unlink()
    from isar.schema.card import load_state
    e = authority.effective(root, _outcomes(root, [k1]), load_state(root / "card.state.json").tabulated_calibration)[k1]
    assert e.resolution.fit == "fast" and e.resolution.source == "fallback" and e.line is None
    assert any("cannot be drawn" in n for n in e.resolution.notes)


def test_effective_with_no_refits_is_empty(tmp_path):
    assert authority.effective(tmp_path, {}, {}) == {}
