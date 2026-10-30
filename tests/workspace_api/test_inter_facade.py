"""The read facade over inter-spectra fitting (C-gui-reads, C-preview, C-bundle), on S1U1's synthetic BL43-like
workspace generated at test time. Batches are saved through the real writer; nothing here reads ``real_data/``.
"""
from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import isar
from inter_fixtures import build_inter_workspace, spectrum_keys
from isar.analyze.coupled.settings import SETTING_DEFS
from isar.intake import inputs, inter_values, scans as intake_scans, store
from isar.schema.card import load_state
from isar.session import writer
from isar.session.state import SessionState
from isar.session.inter_spectra import records
from isar.workspace_api import inter_spectra as facade
from isar.workspace_api import SpectraCache
from isar.workspace_api.inter_spectra import BatchRequest, inter_history, inter_overview
from isar.workspace_api.workspace import Workspace

REPO = Path(__file__).resolve().parents[2]
B1, B2 = "b000000000001", "b000000000002"
F1, F2, F3 = (f"f{x:012x}" for x in (0xa001, 0xa002, 0xa003))


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


@pytest.fixture
def root(tmp_path):
    return build_inter_workspace(tmp_path, temperatures=(100, 200), setups=("0p5_0p5_3",), n_analyzers=3)


def _hashes(root, units):
    read = store.read_all(root, store.ENTRIES)
    return records.inputs_now_from(intake_scans.read_scans(root), inputs.index_entries(read.entries, lines=read.lines),
                                   load_state(Path(root) / "card.state.json").tabulated_calibration, units)


def _payload(batch, event, body):
    return {"schema": records.SCHEMA_SAVE, "unit": f"inter:{batch}:batch", "pass": "inter", "batch": batch,
            "event": event, event: body}


def _refit(unit, fit, h, *, status="accepted", flag=None, why=None, cond=(), overlay=True, stage_idx=1, came_out=None):
    return {"fit": fit, "unit": unit, "status": status, "reason": None, "seed": "fast", "inputs": h,
            "conditioned_on": [{"unit": u, "fit": f, "inputs": hh} for u, f, hh in cond], "flag": flag,
            "why_changed": why,
            "payload": {"came_in": {}, "came_out": came_out or {"errors": "conditional"},
                        "overlay": {"n_modes": 2, "omega0": 3.5, "best_label": "2 modes", "interval": [3.4, 3.6],
                                    "modes": [{"omega0": 3.5, "omega0_err": 0.1, "gamma": 0.4, "amplitude": 50.0},
                                              {"omega0": 7.5, "omega0_err": 0.2, "gamma": 0.6, "amplitude": 20.0}]}
                        if overlay else None}}


def save(root, batch, refits, *, stage="ring", index=1, seams=(), end=None, start=True, drift=None):
    """Start (optionally), one stage and (optionally) the end of a batch, through the real writer."""
    body = {"batch_kind": "anchored", "scope": {"direction": "q", "members": sorted({r["unit"] for r in refits})},
            "anchors": [], "rings": {}, "seams": [], "settings": {"version": 1, "values": {}}, "preview": "t",
            "fixed": {}, "unreached": {}, "terms": [], "metric": {}, "core": "t", "inputs_at": 0,
            "intake_seq": inter_seq(root), "epochs": {}}
    stage_body = {"stage": stage, "index": index, "round": None, "refits": refits, "seams": list(seams),
                  "progress": {"done": len(refits), "failed": 0, "not_reached": 0, "fixed": 0, "total": 9},
                  "not_reached": {}}
    payloads = ([_payload(batch, "start", body)] if start else []) + [_payload(batch, "stage", stage_body)]
    if end:
        payloads.append(_payload(batch, "end", {"state": end, "reason": "sweep cap" if end == "did_not_settle" else None,
                                                "done": len(refits), "failed": 0, "not_reached": [], "rings_done": 1,
                                                "sweeps": None, "rounds": None, "timing": {}, "drift": drift}))
    out = writer.save_inter(root, payloads)
    assert all(isinstance(v, dict) for v in out), out
    return out


def inter_seq(root):
    from isar.intake.inter_spectra import read_user
    return read_user(root).max_seq


def recalibrate(root, scan, det, zero):
    path = Path(root) / "card.state.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    d.setdefault("tabulated_calibration", {})[scan] = {"setting": "s1", "setting_basis": "declared",
                                                        "detectors": {det: {"zero": zero, "zero_source": "test"}}}
    path.write_text(json.dumps(d), encoding="utf-8", newline="\n")


# ---------------------------------------------------------------------------
# the overview
# ---------------------------------------------------------------------------
def test_an_empty_workspace_has_the_empty_overview(root):
    o = inter_overview(root)
    assert (o.revision, dict(o.by_key), o.anchors, o.batches) == ("0", {}, (), ())
    assert dict(o.settings) == {d.name: d.default for d in SETTING_DEFS}        # every named setting, defaults filled
    assert inter_history(root, spectrum_keys(root)[0])[0].fit_id == "fast"      # the solo alone


def test_a_refit_in_use_shows_its_batch_label_flag_and_seam(root):
    k1, k2 = spectrum_keys(root)[:2]
    h = _hashes(root, [k1, k2])
    flag = {"score": 61.0, "flagged": True, "reasons": [{"code": "branch_lost_mid_window", "severity": "warn",
                                                         "text": "branch B, 3.2 meV"}]}
    seam = {"a": k1, "b": k2, "link": "q", "mismatch": 1.2,
            "flag": {"code": "seam_disagreement", "severity": "serious", "text": "1.2 meV apart"}}
    save(root, B1, [_refit(k1, F1, h[k1], flag=flag), _refit(k2, F2, h[k2])], seams=[seam])
    o = inter_overview(root)
    a, b = o.by_key[k1], o.by_key[k2]
    assert (a.fit_id, a.in_use, a.source, a.label) == (F1, True, "batch", "Batch 1 · ring 1")
    assert [r.code for r in a.flag.reasons] == ["branch_lost_mid_window", "seam_disagreement"]
    assert a.flag.severity == "serious" and a.flag.score == 61.0
    assert [r.code for r in b.flag.reasons] == ["seam_disagreement"]       # both ends of the seam, not just the flagged
    assert not a.locked and not a.stale and a.stale_text is None
    (summary,) = o.batches
    assert (summary.label, summary.status, summary.n_total, summary.n_done, summary.kind, summary.direction) == (
        "Batch 1", "running", 2, 2, "anchored", "q")


def test_an_unflagged_refit_has_no_flag_and_the_solo_is_never_flagged(root):
    k1 = spectrum_keys(root)[0]
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    assert inter_overview(root).by_key[k1].flag is None
    assert inter_values.pick_fit(root, k1, "fast").ok
    s = inter_overview(root).by_key[k1]
    assert (s.fit_id, s.in_use, s.source, s.flag, s.label) == ("fast", False, "pick", None, "Your pick: Fast solo")


def test_a_batch_that_did_not_settle_flags_its_refits_serious_and_ends_the_summary(root):
    k1 = spectrum_keys(root)[0]
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])], stage="sweep", index=3, end="did_not_settle")
    o = inter_overview(root)
    assert [(r.code, r.severity) for r in o.by_key[k1].flag.reasons] == [("did_not_settle", "serious")]
    assert o.by_key[k1].label == "Batch 1 · sweep 3" and o.batches[0].status == "did_not_settle"


def test_a_cumulative_drift_flags_the_drifting_spectra_only(root):
    """The batch end's drift report (the runner's drift_report over the final fits) reaches the flag of each
    spectrum on a flagged segment, flagged on its own; a spectrum on an unflagged segment gets nothing."""
    k1, k2 = spectrum_keys(root)[:2]
    h = _hashes(root, [k1, k2])
    drift = {"segments": [{"analyzer": "s|a03", "branch": "p", "units": [k1, "other"], "flagged": True,
                           "dE": 4.0, "dT": 100.0, "U": 96.0},
                          {"analyzer": "s|a04", "branch": "q", "units": [k2, "other"], "flagged": False,
                           "dE": 0.1, "dT": 100.0, "U": 0.1}],
             "flags": {"t_drift_cumulative": {"severity": "warn", "units": [k1, "other"], "ids": [], "values": []}}}
    save(root, B1, [_refit(k1, F1, h[k1]), _refit(k2, F2, h[k2])], stage="sweep", end="settled", drift=drift)
    o = inter_overview(root)
    (reason,) = o.by_key[k1].flag.reasons
    assert (reason.code, reason.severity) == ("t_drift_cumulative", "warn") and "+4.00 meV over 100 K" in reason.detail
    assert o.by_key[k1].flag.flagged and o.by_key[k2].flag is None


def test_a_flag_shows_the_two_parts_of_its_score(root):
    """F = R + max U (C-refit): the flag shows its release regret and its largest link mismatch, seam or not."""
    k1 = spectrum_keys(root)[0]
    came_out = {"errors": "conditional", "R": 40.0, "U": {"q": 21.0, "T": 3.0}}
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1], flag={"score": 61.0, "flagged": True, "reasons": []},
                           came_out=came_out)])
    f = inter_overview(root).by_key[k1].flag
    assert (f.score, f.cost_of_coupling, f.link_mismatch) == (61.0, 40.0, 21.0)


def test_stale_inputs_fall_back_to_the_solo_fit_and_say_why(root):
    k1 = spectrum_keys(root)[0]
    scan, det = k1.rsplit(":", 1)
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    before = inter_overview(root)
    recalibrate(root, scan, det, 0.25)
    s = inter_overview(root).by_key[k1]
    assert (s.fit_id, s.in_use, s.source, s.stale, s.flag) == ("fast", False, "fast", True, None)
    assert s.stale_text == "its own inputs changed since the refit"
    assert s.label == "Fast solo (Batch 1 · ring 1 is out of date: its own inputs changed since the refit)"
    assert inter_overview(root).revision != before.revision


def test_a_stale_neighbour_is_named(root):
    keys = spectrum_keys(root)
    k1, k2 = keys[0], keys[-1]                    # another scan: recalibrating it leaves k1's own inputs alone
    assert k1.rsplit(":", 1)[0] != k2.rsplit(":", 1)[0]
    h = _hashes(root, [k1, k2])
    save(root, B1, [_refit(k1, F1, h[k1], cond=[(k2, "fast", h[k2])])])
    recalibrate(root, *k2.rsplit(":", 1), 0.5)
    assert _hashes(root, [k1, k2])[k1] == h[k1] and _hashes(root, [k1, k2])[k2] != h[k2]
    s = inter_overview(root).by_key[k1]
    assert s.stale and s.stale_text == f"inputs of {k2} changed since the refit"


def test_a_locked_stale_fit_stays_in_use_and_is_flagged_serious(root):
    k1 = spectrum_keys(root)[0]
    scan, det = k1.rsplit(":", 1)
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    assert inter_values.set_lock(root, k1, locked=True, fit_id=F1).ok
    recalibrate(root, scan, det, 0.25)
    s = inter_overview(root).by_key[k1]
    assert (s.fit_id, s.source, s.locked, s.locked_fit_id, s.lock_stale, s.stale) == (F1, "lock", True, F1, True, False)
    assert s.label == "Locked: Batch 1 · ring 1" and s.stale_text == "its own inputs changed since the refit"
    assert ("stale_locked_fit", "serious") in [(r.code, r.severity) for r in s.flag.reasons]


def test_anchors_and_settings_are_read_and_change_the_revision(root):
    k1 = spectrum_keys(root)[0]
    r0 = inter_overview(root).revision
    assert inter_values.set_anchors(root, [k1], anchored=True).ok
    o = inter_overview(root)
    assert o.anchors == (k1,) and o.revision != r0 and dict(o.by_key) == {}
    assert inter_values.save_settings(root, {"flag_threshold": 40.0}).ok
    o2 = inter_overview(root)
    assert o2.settings["flag_threshold"] == 40.0 and o2.revision != o.revision
    assert inter_overview(root).revision == o2.revision          # a pure function of what is on disk
    assert inter_values.set_anchors(root, [spectrum_keys(root)[1]], anchored=True).ok
    o3 = inter_overview(root)
    assert o3.anchors == (k1, spectrum_keys(root)[1]) and o3.revision != o2.revision    # anchors alone move it


def test_the_overview_never_writes(root):
    k1 = spectrum_keys(root)[0]
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    snap = {p: p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}
    inter_overview(root)
    inter_history(root, k1)
    assert snap == {p: p.read_bytes() for p in Path(root).rglob("*") if p.is_file()}


# ---------------------------------------------------------------------------
# the history
# ---------------------------------------------------------------------------
def test_history_lists_the_solo_then_refits_by_batch_with_unchanged_ones_marked(root):
    k1 = spectrum_keys(root)[0]
    h = _hashes(root, [k1])[k1]
    save(root, B1, [_refit(k1, F1, h)], index=2)
    save(root, B2, [_refit(k1, F2, h, status="unchanged", why="nothing moved"),
                    _refit(k1, F3, h, status="rejected", overlay=False)], stage="sweep", index=3)
    hist = inter_history(root, k1)
    assert [e.fit_id for e in hist] == ["fast", F1, F2]                 # the rejected attempt is not offered
    assert [e.label for e in hist] == ["Fast solo", "Batch 1 · ring 2", "Batch 2 · sweep 3"]
    assert [(e.batch_label, e.stage_label) for e in hist[1:]] == [("Batch 1", "ring 2"), ("Batch 2", "sweep 3")]
    assert [e.changed for e in hist] == [True, True, False]
    assert [e.authoritative for e in hist] == [False, False, True]
    assert [e.conditional_errors for e in hist] == [False, True, True]
    assert all(e.errors_available for e in hist)
    assert hist[2].why_changed[0].detail == "nothing moved"
    assert not any(e.picked or e.locked or e.stale for e in hist)


def test_history_marks_the_pick_the_lock_and_staleness(root):
    k1 = spectrum_keys(root)[0]
    scan, det = k1.rsplit(":", 1)
    h = _hashes(root, [k1])[k1]
    save(root, B1, [_refit(k1, F1, h)])
    save(root, B2, [_refit(k1, F2, h)])
    assert inter_values.pick_fit(root, k1, F1).ok
    hist = {e.fit_id: e for e in inter_history(root, k1)}
    assert hist[F1].picked and hist[F1].authoritative and not hist[F2].authoritative
    assert inter_values.set_lock(root, k1, locked=True, fit_id=F1).ok
    recalibrate(root, scan, det, 0.25)
    hist = {e.fit_id: e for e in inter_history(root, k1)}
    assert hist[F1].locked and hist[F1].stale and hist[F2].stale and not hist["fast"].stale
    assert hist[F1].stale_text == "its own inputs changed since the refit"
    assert hist[F1].authoritative and ("stale_locked_fit", "serious") in [(r.code, r.severity) for r in hist[F1].flag.reasons]


def test_errors_available_follows_the_recorded_errors_not_the_interval(root):
    """A refit's error bars are as its record says ("conditional" | "unavailable"), whatever interval it carries."""
    k1 = spectrum_keys(root)[0]
    h = _hashes(root, [k1])[k1]
    save(root, B1, [_refit(k1, F1, h, came_out={"errors": "unavailable"})])            # covariance failed, interval kept
    r2 = _refit(k1, F2, h, came_out={"errors": "conditional"})
    r2["payload"]["overlay"]["interval"] = None                                       # good errors, no interval
    save(root, B2, [r2], stage="sweep", index=3)
    assert [e.errors_available for e in inter_history(root, k1)] == [True, False, True]


def _hold(root, unit):
    """Held for calibration: the ledger entry keeps its detectors, but no fast solo fit stands for the spectrum."""
    from bl43_fixture import _load, _save
    ws, card, session = _load(root)
    session.scans[unit].status = "held_for_calibration"
    _save(ws, card, session)


def test_a_held_spectrum_offers_no_fast_solo_even_while_its_detectors_remain(root):
    k1 = spectrum_keys(root)[0]
    _hold(root, k1)
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    assert [e.fit_id for e in inter_history(root, k1)] == [F1]
    assert inter_overview(root).by_key[k1].fit_id == F1


def test_a_spectrum_with_no_fast_solo_and_a_stale_refit_reports_no_fit(root):
    """Nothing to use: no fit id, no source, and a plain label, never the fast solo's (C-gui-reads)."""
    k1 = spectrum_keys(root)[0]
    _hold(root, k1)
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    recalibrate(root, *k1.rsplit(":", 1), 0.25)                 # the refit is now out of date; nothing stands in
    s = inter_overview(root).by_key[k1]
    assert (s.fit_id, s.in_use, s.source, s.stale) == (None, False, None, True)
    assert s.label == "No fit in use (Batch 1 · ring 1 is out of date: its own inputs changed since the refit)"


# ---------------------------------------------------------------------------
# the drawable view and the bundle
# ---------------------------------------------------------------------------
def test_fit_view_of_serves_the_solo_the_refit_and_nothing_for_unknown_ids(root):
    ws = Workspace.open(root)
    k1 = spectrum_keys(root)[0]
    solo = facade.fit_view_of(ws, k1)
    assert solo is not None and solo.unit_id == k1 and facade.fit_view_of(ws, k1, "fast") is solo
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    ws.refresh()
    fv = facade.fit_view_of(ws, k1)                                      # authoritative is now the refit
    assert fv.n_modes == 2 and fv.best_label == "2 modes" and fv.verdict is None and fv.unit_id == k1
    assert facade.fit_view_of(ws, k1, "fast").n_modes == solo.n_modes    # the solo is unchanged
    assert facade.fit_view_of(ws, k1, "f0000000000ff") is None
    assert facade.fit_view_of(ws, spectrum_keys(root)[1], F1) is None    # another spectrum's refit is not this one's


def _same(a, b):
    """Two bundles are the same draw (the arrays make ``==`` on the dataclass unusable)."""
    return (a.revision, a.fit_id, a.fit_label, a.summaries, a.not_drawn) == (
        b.revision, b.fit_id, b.fit_label, b.summaries, b.not_drawn) and all(
        np.array_equal(getattr(a, f), getattr(b, f), equal_nan=True) for f in ("energy", "counts", "variance"))


def test_spectrum_bundles_fit_choice(root):
    from isar.workspace_api import spectrum_bundles
    from isar.workspace_api.scan_list import scan_list
    ws = Workspace.open(root)
    k1 = spectrum_keys(root)[0]
    row = scan_list(ws).spectrum(k1)
    plain = spectrum_bundles(ws, [row])[0]
    assert (plain.fit_id, plain.fit_label) == ("fast", "Fast solo")
    assert _same(spectrum_bundles(ws, [row], fit_choice=None)[0], plain)
    assert _same(spectrum_bundles(ws, [row], fit_choice={"other:1:a01": F1})[0], plain)      # a missing key: the solo
    assert _same(spectrum_bundles(ws, [row], fit_choice={k1: "fast"})[0], plain)
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    ws.refresh()
    row = scan_list(ws).spectrum(k1)
    cache = SpectraCache()
    chosen = spectrum_bundles(ws, [row], cache=cache, fit_choice={k1: F1})[0]
    solo = spectrum_bundles(ws, [row], cache=cache)[0]
    assert (chosen.fit_id, chosen.fit_label) == (F1, "Batch 1 · ring 1") and solo.fit_id == "fast"
    assert chosen.revision != solo.revision                                  # the fit id is in the revision
    assert chosen.summaries["fast"].label == "2 modes" and solo.summaries["fast"].label == "1 DHO"
    assert [m.energy.value for m in chosen.summaries["fast"].modes] == [3.5, 7.5]
    assert np.array_equal(chosen.counts, solo.counts)                         # the data are the spectrum's own
    again = spectrum_bundles(ws, [row], cache=cache, fit_choice={k1: F1})[0]
    assert (again.fit_id, again.fit_label) == (F1, "Batch 1 · ring 1")        # a cache hit keeps the fit it drew
    save(root, B2, [_refit(k1, F2, _hashes(root, [k1])[k1])])             # same content, another fit id
    ws.refresh()
    row = scan_list(ws).spectrum(k1)
    other = spectrum_bundles(ws, [row], cache=cache, fit_choice={k1: F2})[0]
    assert (other.fit_id, other.fit_label) == (F2, "Batch 2 · ring 1") and other.revision != chosen.revision
    gone = spectrum_bundles(ws, [row], fit_choice={k1: "f0000000000ff"})[0]     # an unknown fit: the solo, said so
    assert gone.fit_id == "fast" and gone.summaries == solo.summaries


def test_a_chosen_refit_keeps_the_stored_deep_pass_drawn(root):
    """The refit replaces the fast slot only: the Deep slot is the solo fit's own, as with no choice (C-bundle)."""
    from bl43_fixture import _DEEP_RESOLVED, _load, _save
    from isar.workspace_api import spectrum_bundles
    from isar.workspace_api.scan_list import scan_list
    ws = Workspace.open(root)
    k1 = spectrum_keys(root)[0]
    sws, card, session = _load(root)
    session.scans[k1].detectors[k1.rsplit(":", 1)[1]]["deep_pass"] = {
        **_DEEP_RESOLVED, "trusted": True, "chosen": {"label": "2 DHO"}}
    _save(sws, card, session)
    ws.refresh()
    solo = spectrum_bundles(ws, [scan_list(ws).spectrum(k1)])[0]
    assert solo.summaries["deep"].label == "2 DHO" and solo.summaries["deep"].trusted is True
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    ws.refresh()
    chosen = spectrum_bundles(ws, [scan_list(ws).spectrum(k1)], fit_choice={k1: F1})[0]
    assert (chosen.fit_id, chosen.summaries["fast"].label) == (F1, "2 modes")
    assert chosen.summaries["deep"] == solo.summaries["deep"]
    assert type(chosen._passes["deep"]) is type(solo._passes["deep"])


def test_a_redraw_reads_the_log_once_for_all_its_rows(root, monkeypatch):
    """A Scan(s) redraw of N refitted rows reads the inter-spectra log once, not once per row (a counter on it)."""
    from isar.workspace_api import spectrum_bundles
    from isar.workspace_api.scan_list import scan_list
    ws = Workspace.open(root)
    keys, fits = spectrum_keys(root)[:3], (F1, F2, F3)
    h = _hashes(root, keys)
    save(root, B1, [_refit(k, f, h[k]) for k, f in zip(keys, fits)])
    ws.refresh()
    rows = [scan_list(ws).spectrum(k) for k in keys]
    reads = []
    read_log = records.read_log
    monkeypatch.setattr(records, "read_log", lambda root: reads.append(root) or read_log(root))
    bundles = spectrum_bundles(ws, rows, fit_choice=dict(zip(keys, fits)))
    assert len(reads) == 1
    assert [(b.key, b.fit_id) for b in bundles] == list(zip(keys, fits))


# ---------------------------------------------------------------------------
# the batch preview (S1U4's graph is faked: the wrapper is what is tested)
# ---------------------------------------------------------------------------
class Preview:
    def __init__(self, text, can_start, blocked, notes):
        self.text, self.can_start, self.blocked, self.notes = text, can_start, blocked, notes


class Fakes:
    """Stand-ins for S1U4's reads and planners; ``calls`` records what the wrapper handed them."""

    def __init__(self, monkeypatch, keys, *, points=None, reasons=None, can_start=True, blocked=None):
        self.calls = {}
        ns = SimpleNamespace(nodes={k: object() for k in keys})

        def kinds(d):
            return frozenset({"q"} if d == "q" else {"q", "T"})

        monkeypatch.setattr(facade, "read_nodes", lambda root: ns)
        monkeypatch.setattr(facade, "grid_for", lambda plugin: ("grid", plugin))
        monkeypatch.setattr(facade, "anchored_scope", lambda a, d, n: (frozenset(keys), kinds(d)))
        monkeypatch.setattr(facade, "q_coordinates", lambda root, scope: SimpleNamespace(
            points=points or {}, reasons=reasons or {k: "no accepted hkl" for k in scope}))

        def build(nodes, ns_, **kw):
            self.calls["build"] = kw
            return "graph"

        def plan_anchored(g, anchors, locks, stale):
            self.calls["anch"] = (tuple(anchors), set(locks), set(stale))
            return "plan"

        def plan_simultaneous(g, locks):
            self.calls["sim"] = set(locks)
            return "plan"

        monkeypatch.setattr(facade, "build_graph", build)
        monkeypatch.setattr(facade, "plan_anchored", plan_anchored)
        monkeypatch.setattr(facade, "plan_simultaneous", plan_simultaneous)
        monkeypatch.setattr(facade, "preview_anchored",
                            lambda g, plan, d: Preview(f"anchored {d}", can_start, blocked, ("n1",)))
        monkeypatch.setattr(facade, "preview_simultaneous",
                            lambda g, plan: Preview("simultaneous", can_start, blocked, ("n1",)))


def test_preview_guards_ask_for_the_missing_choice_before_reading_anything(root):
    assert facade.batch_preview(root, BatchRequest("anchored", "q", (), ())).reason == facade.NO_ANCHOR
    assert facade.batch_preview(root, BatchRequest("anchored", None, ("a:1:a03",), ())).reason == facade.NO_DIRECTION
    p = facade.batch_preview(root, BatchRequest("simultaneous", None, (), ("a:1:a03",)))
    assert (p.can_start, p.reason, p.members, p.line) == (False, facade.NO_SELECTION, (), "")


def test_preview_anchored_wraps_the_graph_and_adds_provider_notes(root, monkeypatch):
    keys = ["e:1:a03", "e:1:a04", "e:1:a05"]
    f = Fakes(monkeypatch, keys, points={"e:1:a03": object()},
              reasons={"e:1:a04": "the recorded lattice epoch is not a valid cell",
                       "e:1:a05": "the recorded lattice epoch is not a valid cell", "e:9:a03": "unknown spectrum"})
    p = facade.batch_preview(root, BatchRequest("anchored", "qT", ("e:1:a03",), ()))
    assert p.can_start and p.reason is None and p.line == "anchored qT" and p.members == tuple(keys)
    assert p.notes == ("n1", "the recorded lattice epoch is not a valid cell: 2 spectra")
    assert f.calls["build"]["grid"] is not None and f.calls["build"]["kinds"] == frozenset({"q", "T"})
    assert f.calls["build"]["collinearity_tol"] == 0.01
    assert f.calls["build"]["q_reasons"]["e:1:a04"] == "the recorded lattice epoch is not a valid cell"


def test_preview_q_with_no_coordinates_says_why_whatever_the_graph_says(root, monkeypatch):
    Fakes(monkeypatch, ["e:1:a03", "e:1:a04"], points={}, can_start=True)
    p = facade.batch_preview(root, BatchRequest("anchored", "q", ("e:1:a03",), ()))
    assert not p.can_start and p.reason == "spreading in q needs each analyzer's accepted hkl; none is accepted for this scan"


def test_preview_passes_the_graph_blocked_reason_and_hides_it_when_startable(root, monkeypatch):
    Fakes(monkeypatch, ["e:1:a03"], points={"e:1:a03": object()}, can_start=False, blocked="nothing is linked")
    p = facade.batch_preview(root, BatchRequest("anchored", "T", ("e:1:a03",), ()))
    assert (p.can_start, p.reason) == (False, "nothing is linked")


def test_preview_simultaneous_drops_unknown_selected_keys_with_a_note_and_passes_locks(root, monkeypatch):
    k1, k2 = spectrum_keys(root)[:2]
    assert inter_values.set_lock(root, k1, locked=True, fit_id="fast").ok
    f = Fakes(monkeypatch, [k1, k2])
    p = facade.batch_preview(root, BatchRequest("simultaneous", None, (), (k2, k1, "zz:1:a03")))
    assert p.members == tuple(sorted([k1, k2])) and p.line == "simultaneous"
    assert "1 selected spectra are unknown to the import record" in p.notes
    assert f.calls["sim"] == {k1}
    assert f.calls["build"]["kinds"] == frozenset({"q", "T"})


def test_preview_hands_a_stale_locked_fit_to_the_plan_as_not_conditioning(root, monkeypatch):
    k1, k2 = spectrum_keys(root)[:2]
    scan, det = k1.rsplit(":", 1)
    save(root, B1, [_refit(k1, F1, _hashes(root, [k1])[k1])])
    assert inter_values.set_lock(root, k1, locked=True, fit_id=F1).ok
    recalibrate(root, scan, det, 0.25)
    f = Fakes(monkeypatch, [k1, k2], points={k1: object()})
    facade.batch_preview(root, BatchRequest("anchored", "qT", (k2,), ()))
    anchors, locks, stale = f.calls["anch"]
    assert locks == {k1} and stale == {k1}


# ---------------------------------------------------------------------------
# the import contract
# ---------------------------------------------------------------------------
def test_the_facade_imports_neither_the_writers_nor_the_runner_nor_qt():
    code = ("import sys, isar.workspace_api.inter_spectra, isar.workspace_api as w\n"
            "bad = [m for m in sys.modules if m.startswith(('PySide', 'isar.intake.inter_values',"
            " 'isar.session.inter_spectra.runner', 'isar.session.writer'))]\n"
            "sys.exit(len(bad))")
    assert subprocess.run([sys.executable, "-c", code], cwd=REPO, check=False).returncode == 0
    tree = ast.parse((REPO / "isar/workspace_api/inter_spectra.py").read_text(encoding="utf-8"))
    names = [n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    assert not any(m.endswith(("inter_values", "runner", "writer")) for m in names)


def test_the_facade_names_are_exported_from_the_package():
    import isar.workspace_api as w
    for name in ("inter_overview", "inter_history", "batch_preview", "fit_view_of", "InterOverview", "SpectrumInter",
                 "FitEntry", "BatchRequest", "BatchPreview"):
        assert getattr(w, name) is getattr(facade, name)


def test_the_review_flag_is_the_stored_decision_or_a_facade_added_reason_never_severity(root):
    from isar.workspace_api.inter_spectra import is_flagged
    k1, k2, k3 = spectrum_keys(root)[:3]
    h = _hashes(root, [k1, k2, k3])
    note = {"score": 1.0, "flagged": False,
            "reasons": [{"code": "release_fit_failed", "severity": "warn", "text": "release fit failed"}]}
    stored = {"score": 60.0, "flagged": True, "reasons": []}
    save(root, B1, [_refit(k1, F1, h[k1], flag=note), _refit(k2, F2, h[k2], flag=stored)])
    by = inter_overview(root).by_key
    assert [r.code for r in by[k1].flag.reasons] == ["release_fit_failed"] and by[k1].flag.severity == "warn"
    assert not by[k1].flag.flagged and not is_flagged(by[k1].flag)       # a warn note is listed, not flagged
    assert by[k2].flag.flagged and is_flagged(by[k2].flag)               # the stored decision stands
    save(root, B2, [_refit(k3, F3, h[k3], flag=note)], stage="sweep", index=3, end="did_not_settle")
    assert inter_overview(root).by_key[k3].flag.flagged                  # did not settle: flagged whatever the score


def test_overview_preview_and_history_parse_the_ledger_once(root, monkeypatch):
    """One change of session.json costs one parse, however many facade reads follow it."""
    keys = spectrum_keys(root)
    loads = []
    load = SessionState.load.__func__
    monkeypatch.setattr(SessionState, "load", classmethod(lambda cls, path: loads.append(path) or load(cls, path)))
    inter_overview(root)
    facade.batch_preview(root, BatchRequest("simultaneous", None, (), tuple(keys[:2])))
    inter_history(root, keys[0])
    assert len(loads) == 1
