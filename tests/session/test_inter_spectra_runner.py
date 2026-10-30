"""The inter-spectra batch runner (C-queue, S1U6): anchored rings, simultaneous rounds and sweeps, the pool, the
fingerprint the writer checks, and the timing block against a target nothing enforces.

Most tests run ``run_batch`` in process with the fake core of ``_inter_fakes`` (injected ``core=``) on S1U1's
synthetic BL43-like workspace (``tests/inter_fixtures.py``, with a generated resolution library so every
member's context prepares), and save through the real ``writer.save_inter``; the harness plays the service's
``emit``/``ack``. One test drives the real core end to end on tiny spectra generated here, and one runs the
batch in the real analysis service to show a stop kills the pool. Nothing reads ``real_data/``; no measured
value is used.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import Future
from pathlib import Path

import pytest

import intake_fixtures as fx
import isar
from inter_fixtures import build_inter_workspace
from inter_q_fixtures import epoch, set_epochs
from isar.intake import inputs, inter_values, scans as intake_scans, store
from isar.schema.card import load_state
from isar.session import writer
from isar.session.inter_spectra import records, runner
from isar.session.inter_spectra.graph import anchored_scope
from isar.session.inter_spectra.nodes import read_nodes
from isar.session.inter_spectra.q_provider import INVALID_CELL
from isar.session.workspace import Workspace

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))       # the fake core is imported by name, here and in spawned pool workers
import _inter_fakes as fakes  # noqa: E402


def test_import_root_is_the_worktree():
    root = Path(isar.__file__).resolve().parents[1]
    print("import root:", root)
    assert root == REPO
    assert Path(fakes.__file__).resolve().parent == HERE


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _workspace(tmp_path, temperatures, n_analyzers) -> Path:
    """The S1U1 fixture with a resolution library saved first, so every member prepares a fit context."""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    dets = tuple(f"a{n:02d}" for n in fx_analyzers(n_analyzers))
    fx.write_library(ws / "rf", analyzers=dets)
    assert fx.save_resolution_folder(ws, ws / "rf").ok
    return build_inter_workspace(tmp_path, temperatures=temperatures, setups=("0p5_0p5_3",), n_analyzers=n_analyzers)


def fx_analyzers(n):
    from bl43_fixture import ANALYZERS
    return ANALYZERS[:n]


def _keys(ws) -> dict:
    """``(detector, T) -> spectrum key``."""
    return {(n.detector, n.T): k for k, n in read_nodes(ws).nodes.items()}


def _set_fast(ws, values: dict) -> None:
    """Give spectra a stored fast solo with one mode at ``omega0`` and the fake core's behaviour ``cfg``."""
    w = Workspace.open(ws)
    card, session = w.load()
    for key, (omega0, cfg) in values.items():
        det = key.rsplit(":", 1)[1]
        fp = session.scans[key].detectors[det]["fast_pass"]
        fp["peaks"] = [{"index": 0, "omega0": omega0, "Gamma": 0.5, "height": 1.0}]
        fp["fake"] = dict(cfg)
    w.save(card, session)


class Bench:
    """The service's side for an in-process run: every handed-over payload is saved by the real writer at once."""

    def __init__(self, root, on_progress=None):
        self.root, self.on_progress = Path(root), on_progress
        self.results, self.progress, self.verdicts = [], [], {}

    def emit(self, r):
        self.results.append(r)
        if r.payload is not None and r.ack is not None:
            v = writer.save_inter(self.root, [r.payload])[0]
            self.verdicts[r.ack] = v if isinstance(v, dict) else {"saved": False, "error": v}

    def ack(self, token):
        return self.verdicts.pop(token)

    def prog(self, p):
        self.progress.append(p)
        if self.on_progress is not None:
            self.on_progress(p)

    def run(self, spec, *, cores=1, core=fakes, **kw):
        item = runner.BatchItem("q1", f"inter:{spec.batch_id}:batch", "inter", cores, spec.to_json())
        runner.run_batch(self.root, item, progress=self.prog, emit=self.emit, ack=self.ack, core=core, **kw)
        return records.read_log(self.root)

    def end(self):
        (r,) = [r for r in self.results if r.payload is not None and r.payload["event"] == "end"]
        return r


def _anchored(ws, anchors, direction="T"):
    scope, _ = anchored_scope(anchors, direction, read_nodes(ws))
    return runner.BatchSpec(records.new_batch_id(), "anchored", direction, tuple(anchors), tuple(sorted(scope)))


def _simultaneous(ws, keys):
    return runner.BatchSpec(records.new_batch_id(), "simultaneous", None, (), tuple(sorted(keys)))


def _lines(ws, batch):
    log = records.read_log(ws)
    return [(r, records.read_payload(ws, r)) for r in log.refits if r.batch == batch]


def _now(ws, units):
    read = store.read_all(ws, store.ENTRIES)
    return records.inputs_now_from(intake_scans.read_scans(ws), inputs.index_entries(read.entries, lines=read.lines),
                                   load_state(Path(ws) / "card.state.json").tabulated_calibration, units)


def _calibrate(root, scan, det, zero):
    """Change a scan's stored tabulated calibration, as a recalibration saved by another writer does."""
    path = Path(root) / "card.state.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    d.setdefault("tabulated_calibration", {})[scan] = {"setting": "s1", "setting_basis": "declared",
                                                        "detectors": {det: {"zero": zero, "zero_source": "test"}}}
    path.write_text(json.dumps(d), encoding="utf-8", newline="\n")


def _when(stage, index, phase, action):
    """A progress hook that runs ``action`` once, the first time the batch reports that stage, index and phase."""
    done = []

    def hook(p):
        if not done and (p["stage"], p["index"], p["phase"]) == (stage, index, phase):
            done.append(True)
            action()
    return hook


# ---------------------------------------------------------------------------
# the boundary
# ---------------------------------------------------------------------------
def test_a_batch_spec_round_trips_and_a_bad_field_is_named():
    spec = runner.BatchSpec("b0123456789ab", "anchored", "qT", ("e:1:a03",), ("e:1:a03", "e:1:a04"))
    assert runner.BatchSpec.from_json(json.loads(json.dumps(spec.to_json()))) == spec
    for bad, field in (({"batch_id": "x"}, "batch_id"), ({"kind": "both"}, "kind"), ({"direction": None}, "direction"),
                       ({"members": "e:1:a03"}, "members"), ({"anchors": []}, "anchors")):
        with pytest.raises(ValueError, match=field):
            runner.BatchSpec.from_json({**spec.to_json(), **bad})


# ---------------------------------------------------------------------------
# check 1: anchored
# ---------------------------------------------------------------------------
def test_an_anchored_batch_grows_ring_by_ring_on_frozen_state(tmp_path):
    """Rings from the plan; every refit of a ring sees only the state frozen at its start; a seam pair never sees
    each other and is reported with its ring; a failed ring-1 member cuts off the ring-2 member that had only
    it and leaves another fitted against its other neighbour only; a lock set mid-ring is refused at save and
    is a fixed neighbour from the next ring on."""
    ws = _workspace(tmp_path, (100, 150, 200, 250, 300), 4)
    k = _keys(ws)
    fast = {key: (5.0, {}) for key in k.values()}
    fast[k["a03", 250.0]] = (15.0, {})                   # two fronts that disagree: the seam
    fast[k["a04", 100.0]] = (7.0, {})                    # a04@150 would refit to 7.1; its fast value is 5.0
    fast[k["a05", 150.0]] = (5.0, {"fail": True})
    fast[k["a06", 150.0]] = (5.0, {"fail": True})
    _set_fast(ws, fast)
    anchors = [k["a03", 100.0], k["a03", 250.0], k["a04", 100.0], k["a05", 100.0], k["a05", 300.0], k["a06", 100.0]]
    spec = _anchored(ws, anchors)
    locked = k["a04", 150.0]
    bench = Bench(ws, _when("ring", 1, "refine", lambda: inter_values.set_lock(ws, locked, locked=True, fit_id="fast")))
    log = bench.run(spec)
    info = log.batches[spec.batch_id]
    ring_of = info.rings
    lines = _lines(ws, spec.batch_id)
    by_unit = {}
    for r, p in lines:
        by_unit.setdefault(r.unit, []).append((r, p))

    # rings in the plan's order, each refit conditioned only on fixed fits or fits of an earlier ring
    assert [r.index for r, _ in lines] == sorted(r.index for r, _ in lines)
    fit_ring = {r.fit: r.index for r, _ in lines}
    for r, p in lines:
        assert r.index == ring_of[r.unit]
        for c in r.conditioned_on:
            assert c.fit == "fast" or fit_ring[c.fit] < r.index, (r.unit, c)
        mates = {u for u, ring in ring_of.items() if ring == r.index}
        held = [h["unit"] for t in p["came_in"]["terms"] for h in t["held"]]
        assert not mates & set(held)                    # a ring-mate's new value never enters its ring
    # the seam pair a03@150 / a03@200 sees only its own front, and the disagreement is reported with ring 1
    left, right = k["a03", 150.0], k["a03", 200.0]
    assert [c.unit for c in by_unit[left][0][0].conditioned_on] == [k["a03", 100.0]]
    assert [c.unit for c in by_unit[right][0][0].conditioned_on] == [k["a03", 250.0]]
    (ring1,) = [s for s in log.stages if s.batch == spec.batch_id and s.stage == "ring" and s.index == 1]
    assert {(s["a"], s["b"]) for s in ring1.seams} == {(left, right)}
    assert ring1.seams[0]["flag"]["code"] == "seam_disagreement"
    # a failed neighbour: a05@200 is fitted against a05@250 only; a06@200 and beyond are cut off
    (r200, _), = by_unit[k["a05", 200.0]]
    assert [c.unit for c in r200.conditioned_on] == [k["a05", 250.0]]
    assert by_unit[k["a05", 150.0]][0][0].status == "failed"
    end = info.end
    missed = {d["unit"]: d["reason"] for d in end["not_reached"]}
    assert missed == {k["a06", t]: runner.CUT_OFF for t in (200.0, 250.0, 300.0)}
    # the lock: refused at save in ring 1, a fixed neighbour (its locked fast fit) for ring 2
    (lr, _), = by_unit[locked]
    assert (lr.accept.decision, lr.accept.reason) == ("declined", "locked")
    (n200, p200), = by_unit[k["a04", 200.0]]
    assert [(c.unit, c.fit) for c in n200.conditioned_on] == [(locked, "fast")]
    assert [m["omega0"] for t in p200["came_in"]["terms"] for h in t["held"] for m in h["modes"]] == [5.0]
    # counts: done, failed, not reached and fixed are separate and add up to the members
    assert (end["state"], end["done"], end["failed"], len(end["not_reached"])) == ("completed", 8, 2, 3)
    assert bench.end().status == "done_warning"
    last = [p for p in bench.progress if p["stage"] == "ring"][-1]
    assert (last["done"], last["failed"], last["not_reached"], last["fixed"], last["total"]) == (8, 2, 3, 7, 20)


def test_a_branch_missing_in_the_middle_of_an_anchored_chain_is_carried_to_where_it_returns(tmp_path):
    """The anchor x (100 K) holds branches p and r, y (150 K) only r, z (200 K) two modes again. y's accepted refit
    records p as not observed here, so z's ring conditions on a neighbour that still names p (its return keeps the
    id, with no unmatched-mode price), the record is saved with y's refit, and a later batch reloads it."""
    from isar.analyze.coupled.types import Dormant
    ws = _workspace(tmp_path, (100, 150, 200), 1)
    k = _keys(ws)
    x, y, z = k["a03", 100.0], k["a03", 150.0], k["a03", 200.0]
    w = Workspace.open(ws)
    card, session = w.load()
    for key, peaks in ((x, [(4.0, "p"), (8.0, "r")]), (y, [(8.0, "r")]), (z, [(4.0, "z0"), (8.0, "z1")])):
        fp = session.scans[key].detectors[key.rsplit(":", 1)[1]]["fast_pass"]
        fp["peaks"] = [{"index": i, "omega0": e, "Gamma": 0.5, "height": 1.0, "branch": b}
                       for i, (e, b) in enumerate(peaks)]
        fp["fake"] = {"toy": "keep", "slots": ["p", "r"]} if key == z else {"toy": "keep"}
    w.save(card, session)
    spec = _anchored(ws, [x])
    Bench(ws).run(spec)
    lines = {r.unit: (r, p) for r, p in _lines(ws, spec.batch_id)}
    gone = {"branch": "p", "last_seen_fit": "fast", "last_seen_unit": x}
    assert lines[y][1]["came_out"]["dormant"] == [gone]                            # saved with y's refit
    held_y = [h for t in lines[z][1]["came_in"]["terms"] for h in t["held"] if h["unit"] == y]
    assert held_y and held_y[0]["dormant"] == [gone]                               # carried to z's ring
    assert lines[z][0].accept.decision == "authoritative"
    # a later batch reads y's stored refit back with its record
    again = _anchored(ws, [z])
    run = runner._Run(Path(ws), runner.BatchItem("q2", f"inter:{again.batch_id}:batch", "inter", 1, again.to_json()),
                      lambda p: None, lambda r: None, lambda t: {}, fakes, runner.TARGET_S)
    run.records_mod, run.ctx, run.inputs, run.nodes = records, {y: None}, {}, read_nodes(ws).nodes
    assert run.held_of(y, lines[y][0].fit).dormant == (Dormant("p", "fast", x),)


def _merging(orig, extra):
    """``canonical_factors`` that also merges ``extra`` (alias -> name), as the identity does when two ids meet."""
    def patched(A, ids, refs, outside):
        out, rename = orig(A, ids, refs, outside)
        return out, {**rename, **extra}
    return patched


def test_a_locked_fit_dormant_branch_a_later_batch_merged_is_read_under_the_merged_id(tmp_path, monkeypatch):
    """z's refit (batch 1) records q as dormant. The user locks z; batch 2 merges q into p and its refits record the
    alias q -> p. Batch 3 reads z's stored fit: its held state names p where it named q, so p and q never meet."""
    from isar.analyze.coupled.types import Dormant
    ws = _workspace(tmp_path, (100, 150, 200), 1)
    k = _keys(ws)
    x, y, z = k["a03", 100.0], k["a03", 150.0], k["a03", 200.0]
    w = Workspace.open(ws)
    card, session = w.load()
    for key, peaks in ((x, [(4.0, "q"), (8.0, "r")]), (y, [(8.0, "r")]), (z, [(4.0, "z0"), (8.0, "z1")])):
        fp = session.scans[key].detectors[key.rsplit(":", 1)[1]]["fast_pass"]
        fp["peaks"] = [{"index": i, "omega0": e, "Gamma": 0.5, "height": 1.0, "branch": b}
                       for i, (e, b) in enumerate(peaks)]
        fp["fake"] = {"toy": "keep"}
    w.save(card, session)
    first = _anchored(ws, [x])
    Bench(ws).run(first)
    z_fit = {r.unit: r for r, _ in _lines(ws, first.batch_id)}[z].fit
    assert inter_values.set_lock(ws, z, locked=True, fit_id=z_fit).ok
    monkeypatch.setattr(runner, "canonical_factors", _merging(runner.canonical_factors, {"q": "p"}))
    Bench(ws).run(_simultaneous(ws, (x, y, z)))
    later = _simultaneous(ws, (x, y, z))
    run = runner._Run(Path(ws), runner.BatchItem("q3", f"inter:{later.batch_id}:batch", "inter", 1, later.to_json()),
                      lambda p: None, lambda r: None, lambda t: {}, fakes, runner.TARGET_S)
    run.records_mod, run.ctx, run.inputs, run.nodes = records, {z: None}, {}, read_nodes(ws).nodes
    held = run.held_of(z, z_fit)
    assert sorted(d.branch for d in held.dormant) == ["p", "r"]                    # q is p now, and r is y's
    assert held.dormant[0] == Dormant("p", "fast", x)


def test_a_locked_refit_is_drawn_with_the_calibration_resolution_and_temperature_it_was_made_with(tmp_path):
    """Through the real writer and facade: y's refit is locked, then y's calibration, resolution and temperature
    change and its fast solo is rerun. The locked refit stays in use (stale) and is still drawn in its own context,
    never rebuilt with the new solo's."""
    from isar.workspace_api import Workspace as Facade
    from isar.workspace_api.inter_spectra import fit_view_and_label
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"},) * 3)

    def solo(change):
        w = Workspace.open(ws)
        card, session = w.load()
        change(session.scans[y].detectors[y.rsplit(":", 1)[1]])
        w.save(card, session)
    solo(lambda out: out.update(calib={"center": 0.0, "gauss_fwhm": 1.0, "lorentz_fwhm": 0.0}, T=100.0))
    spec = _simultaneous(ws, (x, y, z))
    Bench(ws).run(spec)
    fit = [r for r, _ in _lines(ws, spec.batch_id) if r.unit == y and r.accept.decision == "authoritative"][-1].fit
    assert inter_values.set_lock(ws, y, locked=True, fit_id=fit).ok

    def drawn():
        view, label = fit_view_and_label(Facade.open(ws), y, "authoritative")
        assert label.startswith("Batch 1")
        return view.resolution_context()
    before = drawn()
    assert before == {"center": 0.0, "gauss_fwhm": 1.0, "lorentz_fwhm": 0.0, "T": 100.0}
    solo(lambda out: out.update(calib={"center": 2.5, "gauss_fwhm": 3.0, "lorentz_fwhm": 0.0}, T=333.0))
    assert drawn() == before

def test_spread_in_q_with_no_accepted_hkl_leaves_every_other_spectrum_unreached(tmp_path):
    ws = _workspace(tmp_path, (100, 200), 3)
    k = _keys(ws)
    spec = _anchored(ws, [k["a03", 100.0]], "q")
    bench = Bench(ws)
    log = bench.run(spec)
    info = log.batches[spec.batch_id]
    assert info.unreached == {k["a04", 100.0]: "no accepted hkl", k["a05", 100.0]: "no accepted hkl"}
    assert not [r for r in log.refits if r.batch == spec.batch_id]
    assert {d["reason"] for d in info.end["not_reached"]} == {"no accepted hkl"}


def test_spread_in_q_on_an_invalid_epoch_names_the_epoch_not_the_hkl(tmp_path):
    ws = _workspace(tmp_path, (100, 200), 3)
    set_epochs(ws, epoch(1, a=0.0))
    k = _keys(ws)
    spec = _anchored(ws, [k["a03", 100.0]], "q")
    log = Bench(ws).run(spec)
    assert log.batches[spec.batch_id].unreached == {k["a04", 100.0]: INVALID_CELL, k["a05", 100.0]: INVALID_CELL}


def test_the_fingerprint_the_runner_stamps_is_the_one_the_writer_checks(tmp_path):
    """Joint worked example (S1 lead), parts 1 and 2, through the runner: an unchanged spectrum's stamped inputs
    equal what save_inter recomputes and the stored fast solo's, and it is accepted; a spectrum whose scan is
    recalibrated during the ring is declined as inputs_changed."""
    ws = _workspace(tmp_path, (100, 150, 200, 250), 2)
    k = _keys(ws)
    stay, moved = k["a04", 200.0], k["a03", 150.0]
    stored = Workspace.open(ws).load()[1].scans[stay].provenance["fit_inputs"]["hash"]
    scan, det = moved.rsplit(":", 1)
    # ring 1: a03@150 (against a03@100) and a04@200 (against a04@250); only the 150 K scan is recalibrated
    spec = _anchored(ws, [k["a03", 100.0], k["a04", 250.0]])
    bench = Bench(ws, _when("ring", 1, "refine", lambda: _calibrate(ws, scan, det, 0.5)))
    bench.run(spec)
    lines = {r.unit: r for r, _ in _lines(ws, spec.batch_id)}
    assert lines[stay].inputs == _now(ws, [stay])[stay] == stored is not None
    assert lines[stay].accept.decision == "authoritative"
    assert (lines[moved].accept.decision, lines[moved].accept.reason) == ("declined", "inputs_changed")
    # the batch says why: one reason, the runner's own sentence, in the last event and the end record
    why = ("fitted but not saved (the inputs it was fitted from do not match the experiment's record); start the "
           "batch again, and if it fails the same way, add the data again (the same files or their folder) so ISAR "
           "reads them afresh")
    last = [p for p in bench.progress if p["stage"] == "ring"][-1]
    assert last["failed_reasons"] == [[why, last["failed"]]] and last["failed"] >= 1
    assert sum(n for _, n in last["not_reached_reasons"]) == last["not_reached"]
    failed_units = bench.end().payload["end"]["failed_units"]
    assert {"unit": moved, "reason": why} in failed_units and len(failed_units) == last["failed"]


def test_a_scan_with_no_record_of_its_files_fails_its_members_with_that_reason_and_never_reads_the_folder(tmp_path):
    """S1U6: neither the import row nor the ledger lists the scan's files; the batch says so, it does not walk
    the folder."""
    ws = _workspace(tmp_path, (100, 150, 200, 250), 2)
    k = _keys(ws)
    moved = k["a03", 150.0]
    scan = moved.rsplit(":", 1)[0]
    path = Path(ws) / intake_scans.INTAKE_SCANS
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["scans"][scan].pop("import")
    path.write_text(json.dumps(doc), encoding="utf-8", newline="\n")
    w = Workspace.open(ws)
    card, session = w.load()
    for row in session.files.values():
        if scan in (row.get("scan_ids") or ()):
            row["scan_ids"] = [s for s in row["scan_ids"] if s != scan]
    w.save(card, session)
    spec = _anchored(ws, [k["a03", 100.0], k["a04", 250.0]])
    bench = Bench(ws)
    bench.run(spec)
    failed = {f["unit"]: f["reason"] for f in bench.end().payload["end"]["failed_units"]}
    assert "no record of which files" in failed[moved] and "no record of which files" in failed[k["a04", 150.0]]


# ---------------------------------------------------------------------------
# check 2: simultaneous
# ---------------------------------------------------------------------------
def _chain(tmp_path, cfgs, fasts=(4.0, 6.0, 9.0)):
    """One analyzer at 100, 150, 200 K: x - y - z by T-links; the plan's groups are (y) then (x, z)."""
    ws = _workspace(tmp_path, (100, 150, 200), 1)
    k = _keys(ws)
    x, y, z = k["a03", 100.0], k["a03", 150.0], k["a03", 200.0]
    _set_fast(ws, {u: (f, c) for u, f, c in zip((x, y, z), fasts, cfgs)})
    return ws, x, y, z


def test_a_simultaneous_batch_settles_gauss_seidel_on_one_frozen_stage_1(tmp_path):
    """Stage 1 on one frozen snapshot, the sweeps in group order (each group sees the earlier groups' new values),
    two quiet sweeps, then a reopened stage 1 that changes nothing: settled in 3 sweeps and 2 rounds."""
    ws, x, y, z = _chain(tmp_path, ({"toy": "average"}, {"toy": "keep"}, {"toy": "average"}))
    bench = Bench(ws)
    spec = _simultaneous(ws, (x, y, z))
    log = bench.run(spec)
    end = log.batches[spec.batch_id].end
    assert (end["state"], end["sweeps"], end["rounds"]) == ("settled", 3, 2)
    lines = _lines(ws, spec.batch_id)
    sweep1 = {r.unit: (r, p) for r, p in lines if r.index == 1}
    for r, p in sweep1.values():
        assert p["came_out"]["assignment"]["snapshot_id"] == f"{spec.batch_id}:round1"
        assert p["came_in"]["stage_id"] == f"{spec.batch_id}:round1"
        assert "stage1" in p["came_out"]                 # the round's stage 1 rides on its first sweep (E12)
    assert all("stage1" not in p["came_out"] for r, p in lines if r.index > 1)
    # Gauss-Seidel: x and z (group 2) are conditioned on y's sweep-1 refit, y (group 1) on their incoming fits
    y_fit = sweep1[y][0].fit
    assert [c.fit for c in sweep1[x][0].conditioned_on] == [y_fit]
    assert {c.fit for c in sweep1[y][0].conditioned_on} == {"fast"}
    final = {r.unit: p["came_out"]["energies"] for r, p in lines if r.index == 3}
    assert final[x] == {"B": pytest.approx(5.0)} and final[z] == {"B": pytest.approx(7.5)}
    assert bench.end().status == "done"


def test_a_cumulative_drift_reaches_the_drifting_spectra_flags(tmp_path):
    """x, y, z hold 4, 6 and 9 meV on one branch from 100 to 200 K (the keep toy never moves them): the end's
    drift report flags the segment, and the read facade shows t_drift_cumulative on each of the three."""
    from isar.workspace_api.inter_spectra import inter_overview
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"},) * 3)
    spec = _simultaneous(ws, (x, y, z))
    end = Bench(ws).run(spec).batches[spec.batch_id].end
    assert end["drift"]["flags"]["t_drift_cumulative"]["units"] == sorted((x, y, z))
    o = inter_overview(ws)
    for u in (x, y, z):
        assert "t_drift_cumulative" in [r.code for r in o.by_key[u].flag.reasons] and o.by_key[u].flag.flagged


def test_a_batch_that_cannot_settle_ends_at_the_cap_and_keeps_its_results(tmp_path):
    ws, x, y, z = _chain(tmp_path, ({"toy": "oscillate"}, {"toy": "keep"}, {"toy": "keep"}))
    assert inter_values.save_settings(ws, {"sweep_cap": 4}).ok
    bench = Bench(ws)
    spec = _simultaneous(ws, (x, y, z))
    log = bench.run(spec)
    end = log.batches[spec.batch_id].end
    assert (end["state"], end["sweeps"]) == ("did_not_settle", 4)
    assert bench.end().status == "done_warning" and "did not settle" in bench.end().reason
    assert sorted(s.index for s in log.stages if s.batch == spec.batch_id) == [1, 2, 3, 4]
    assert {r.accept.decision for r in log.refits if r.batch == spec.batch_id} == {"authoritative"}


def test_a_member_that_fails_every_sweep_keeps_the_batch_from_settling(tmp_path):
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep", "fail": True}, {"toy": "keep"}, {"toy": "keep"}))
    assert inter_values.save_settings(ws, {"sweep_cap": 3}).ok
    spec = _simultaneous(ws, (x, y, z))
    end = Bench(ws).run(spec).batches[spec.batch_id].end
    assert (end["state"], end["sweeps"], end["failed"]) == ("did_not_settle", 3, 1)


def test_a_count_change_in_a_later_round_reopens_the_sweeps(tmp_path):
    ws, x, y, z = _chain(tmp_path, ({"toy": "average", "change_rounds": [2]}, {"toy": "keep"}, {"toy": "average"}))
    bench = Bench(ws)
    spec = _simultaneous(ws, (x, y, z))
    end = bench.run(spec).batches[spec.batch_id].end
    assert (end["state"], end["sweeps"], end["rounds"]) == ("settled", 5, 3)


def test_refused_members_of_one_group_go_back_and_the_sweep_is_not_quiet(tmp_path):
    """The user picks y's fast solo during sweep 1: y's own refit is refused (``picked``) and y is refitted no
    more; x and z (one group, both conditioned on y's batch refit) are refused at save too. Their held values go
    back and that sweep is not quiet; from sweep 2 they are conditioned on y's pick, which the writer accepts,
    and the batch settles instead of refusing them sweep after sweep to the cap."""
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"}, {"toy": "keep"}, {"toy": "keep"}))
    assert inter_values.save_settings(ws, {"sweep_cap": 6}).ok
    bench = Bench(ws, _when("sweep", 1, "refine", lambda: inter_values.pick_fit(ws, y, "fast")))
    spec = _simultaneous(ws, (x, y, z))
    log = bench.run(spec)
    lines = _lines(ws, spec.batch_id)
    refused = {r.unit for r, _ in lines if r.accept.decision == "declined"}
    assert refused == {x, y, z}
    assert {r.unit for r, _ in lines if r.index > 1} == {x, z}
    second = [r for r, _ in lines if r.index == 2]
    assert {(c.unit, c.fit) for r in second for c in r.conditioned_on} == {(y, "fast")}
    assert all(r.accept.decision == "authoritative" for r in second)
    end = log.batches[spec.batch_id].end
    assert end["state"] == "settled" and end["sweeps"] < 6


def test_a_neighbour_whose_inputs_changed_conditions_no_later_sweep(tmp_path):
    """y's scan is recalibrated during sweep 1: y is refused (its inputs changed), x and z for conditioning on
    it. y's held value was made from the old inputs, so nothing conditions on it again and the batch stops
    instead of refusing x and z every sweep to the cap."""
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"}, {"toy": "keep"}, {"toy": "keep"}))
    scan, det = y.rsplit(":", 1)
    bench = Bench(ws, _when("sweep", 1, "refine", lambda: _calibrate(ws, scan, det, 0.5)))
    spec = _simultaneous(ws, (x, y, z))
    log = bench.run(spec)
    lines = _lines(ws, spec.batch_id)
    assert {(r.unit, r.accept.reason) for r, _ in lines} == {
        (y, "inputs_changed"), (x, "neighbour_inputs_changed"), (z, "neighbour_inputs_changed")}
    assert log.batches[spec.batch_id].end["sweeps"] == 1


def test_the_stopping_cost_is_summed_on_the_state_the_writer_accepted(tmp_path):
    """x's fast solo is picked during sweep 1, so x's refit is refused: the shared objective of that sweep carries
    only the accepted refits of y and z, not x's data deviance beside its reverted energies."""
    import types
    seen = []
    spy = types.ModuleType("spy_core")
    spy.__dict__.update({k: v for k, v in vars(fakes).items() if not k.startswith("__")})

    def total_objective(factors, energies, data, unary):
        seen.append(sorted(data))
        return fakes.total_objective(factors, energies, data, unary)
    spy.total_objective = total_objective
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"}, {"toy": "keep"}, {"toy": "keep"}))
    assert inter_values.save_settings(ws, {"sweep_cap": 2}).ok
    bench = Bench(ws, _when("sweep", 1, "refine", lambda: inter_values.pick_fit(ws, x, "fast")))
    bench.run(_simultaneous(ws, (x, y, z)), core=spy)
    assert seen[0] == sorted([y, z])


def test_a_pick_made_during_the_batch_survives_and_its_member_is_refitted_no_more(tmp_path):
    """The user picks x's fast solo during sweep 1: x's refit of that sweep is declined (``picked``, kept in the
    history), x is not refitted again, the pick stays in use, and the batch still settles. (A pick older than the
    batch start is the one the batch replaces: test_inter_authority, a batch that saw the pick replaces it.)"""
    from isar.intake.inter_spectra import read_user
    from isar.session.inter_spectra import authority
    ws, x, y, z = _chain(tmp_path, ({"toy": "average"}, {"toy": "keep"}, {"toy": "average"}))
    bench = Bench(ws, _when("sweep", 1, "refine", lambda: inter_values.pick_fit(ws, x, "fast")))
    spec = _simultaneous(ws, (x, y, z))
    log = bench.run(spec)
    xs = [r for r, _ in _lines(ws, spec.batch_id) if r.unit == x]
    assert [(r.index, r.accept.decision, r.accept.reason) for r in xs] == [(1, "declined", "picked")]
    assert log.batches[spec.batch_id].end["state"] == "settled"
    got = authority.resolve_all(log, read_user(ws), _now(ws, (x, y, z)), {x, y, z})
    assert (got[x].fit, got[x].source) == ("fast", "pick")


def test_a_pick_in_a_later_sweep_is_what_the_neighbours_condition_on(tmp_path):
    """y's refit of sweep 1 is accepted; the user picks y's fast solo during sweep 2. y's sweep-2 refit is refused
    (``picked``) and y's held value becomes the pick, not the sweep-1 refit: x and z, refused for conditioning on
    y's old refit, are accepted from sweep 3 conditioned on (y, fast), and the batch settles before the cap."""
    ws, x, y, z = _chain(tmp_path, ({"toy": "average"}, {"toy": "average"}, {"toy": "average"}))
    assert inter_values.save_settings(ws, {"sweep_cap": 8}).ok
    bench = Bench(ws, _when("sweep", 2, "refine", lambda: inter_values.pick_fit(ws, y, "fast")))
    spec = _simultaneous(ws, (x, y, z))
    log = bench.run(spec)
    lines = _lines(ws, spec.batch_id)
    later = [r for r, _ in lines if r.index > 2 and r.unit in (x, z)]
    assert later and all(r.accept.decision == "authoritative" for r in later)
    assert {(c.unit, c.fit) for r in later for c in r.conditioned_on if c.unit == y} == {(y, "fast")}
    end = log.batches[spec.batch_id].end
    assert end["state"] == "settled" and end["sweeps"] < 8


def test_a_lock_mid_sweep_is_refused_and_the_member_leaves_the_sweeps(tmp_path):
    ws, x, y, z = _chain(tmp_path, ({"toy": "average"}, {"toy": "keep"}, {"toy": "average"}))
    bench = Bench(ws, _when("sweep", 1, "refine", lambda: inter_values.set_lock(ws, x, locked=True, fit_id="fast")))
    spec = _simultaneous(ws, (x, y, z))
    log = bench.run(spec)
    lines = _lines(ws, spec.batch_id)
    (xr,) = [r for r, _ in lines if r.unit == x]
    assert (xr.index, xr.accept.reason) == (1, "locked")
    later_y = [r for r, _ in lines if r.unit == y and r.index > 1]
    assert later_y and all(c.fit == "fast" for r in later_y for c in r.conditioned_on if c.unit == x)
    assert log.batches[spec.batch_id].end["state"] == "settled"


@pytest.mark.parametrize("when", ["before", "during", "current"])
def test_a_stale_locked_fit_never_conditions_a_refit(tmp_path, when):
    """y's refit from a first batch goes stale when z is recalibrated (y's own fast solo stays current). Locked
    before the batch or during it, it is neither a fixed neighbour nor conditioned on; a current locked refit is
    a fixed neighbour stamped with the inputs it was made from."""
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"},) * 3)
    first = _simultaneous(ws, (x, y, z))
    Bench(ws).run(first)
    (y_line,) = [r for r, _ in _lines(ws, first.batch_id) if r.unit == y and r.accept.decision == "authoritative"][-1:]
    if when != "current":
        scan, det = z.rsplit(":", 1)
        _calibrate(ws, scan, det, 0.5)

    def lock():
        assert inter_values.set_lock(ws, y, locked=True, fit_id=y_line.fit).ok
    if when != "during":
        lock()
    bench = Bench(ws, _when("sweep", 1, "refine", lock) if when == "during" else None)
    spec = _simultaneous(ws, (x, y))
    bench.run(spec)
    used = [c for r, _ in _lines(ws, spec.batch_id) for c in r.conditioned_on if c.unit == y]
    if when == "current":
        assert used and {(c.fit, c.inputs) for c in used} == {(y_line.fit, y_line.inputs)}
    else:
        assert all(c.fit != y_line.fit for c in used)


def test_a_stale_lock_counts_as_fixed_and_is_recorded_not_called_unreached(tmp_path):
    """Operator ruling 2026-10-30: the to-do counts leave out whatever will not be fit. A stale lock is neither
    refitted nor used, so it is fixed (not 'not reached'), is left out of the end record's not-reached list, and is
    recorded in its own list; the accounting still adds up to the batch."""
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"},) * 3)
    first = _simultaneous(ws, (x, y, z))
    Bench(ws).run(first)
    (y_line,) = [r for r, _ in _lines(ws, first.batch_id) if r.unit == y and r.accept.decision == "authoritative"][-1:]
    scan, det = z.rsplit(":", 1)
    _calibrate(ws, scan, det, 0.5)
    assert inter_values.set_lock(ws, y, locked=True, fit_id=y_line.fit).ok
    bench = Bench(ws)
    spec = _simultaneous(ws, (x, y))
    bench.run(spec)
    body = bench.end().payload["end"]
    assert body["stale_locks"] == [{"unit": y, "reason": runner.STALE_LOCK}]
    assert all(d["unit"] != y for d in body["not_reached"])
    last = bench.progress[-1]
    assert (last["fixed"], last["total"]) == (1, 2)
    assert last["done"] + last["failed"] + last["not_reached"] + last["fixed"] == last["total"]
    assert all(r != runner.STALE_LOCK for r, _ in last["not_reached_reasons"])
    assert bench.progress[0]["phase"] == "prepare" and bench.progress[0]["fixed"] == 0


def test_only_a_units_latest_attempt_gives_a_failure_reason():
    """judge() keeps a unit's latest attempt: two analysis errors and a refusal group by sentence, and a unit that
    then succeeds leaves the list."""
    import types
    r = runner._Run.__new__(runner._Run)
    r.members, r.fixed, r.stale_locks = ("a", "b", "c"), {}, set()
    r.latest, r.failed, r.not_reached = {}, {}, {}
    r.records_mod, r.refused = records, set()
    nothing = {"refused": {}, "accepted": {}}
    r.judge("a", ValueError("x"), nothing, "f1")
    r.judge("b", ValueError("x"), nothing, "f2")
    r.judge("c", types.SimpleNamespace(status="failed", reason="r"),
            {"refused": {"c": records.REFUSED["locked"]}, "accepted": {}}, "f3")
    assert r.failed_reasons() == [["analysis error (ValueError: x)", 2],
                                  ["fitted but not saved (locked since the batch started)", 1]]
    r.latest["a"] = "done"
    r.failed.pop("a")
    assert r.failed_reasons()[0] == ["analysis error (ValueError: x)", 1]
    assert r.counts()["not_reached"] == 0


def test_a_batch_whose_members_all_leave_is_not_called_settled(tmp_path):
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"}, {"toy": "keep"}, {"toy": "keep"}))

    def lock_all():
        for u in (x, y, z):
            assert inter_values.set_lock(ws, u, locked=True, fit_id="fast").ok
    bench = Bench(ws, _when("sweep", 1, "refine", lock_all))
    spec = _simultaneous(ws, (x, y, z))
    end = bench.run(spec).batches[spec.batch_id].end
    assert (end["state"], bench.end().status) == ("nothing_left", "done_warning")
    assert end["reason"] == runner.NOTHING_LEFT


def test_assignments_that_disagree_on_a_branch_carry_no_factor_for_it(tmp_path):
    """E18: x assigns its mode to branch B, y (changed, to C) does not carry B; after the stage boundary neither
    carries a factor for B, and the shared objective over the stage sums without raising."""
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"}, {"toy": "keep", "slots": ["C"], "change_rounds": [1]},
                                    {"toy": "keep"}))
    spec = _simultaneous(ws, (x, y))
    bench = Bench(ws)
    log = bench.run(spec)
    sweep1 = {r.unit: p for r, p in _lines(ws, spec.batch_id) if r.index == 1}
    assert sweep1[x]["came_out"]["assignment"]["slots"] == ["B"]
    assert sweep1[x]["came_out"]["assignment"]["factors"] == []
    assert sweep1[y]["came_out"]["assignment"]["factors"] == []
    assert log.batches[spec.batch_id].end["state"] == "settled"


# ---------------------------------------------------------------------------
# check 3: the pool
# ---------------------------------------------------------------------------
class _SpyPool:
    """Stands in for ProcessPoolExecutor: records its size and the jobs submitted, runs each inline."""
    made: list = []

    def __init__(self, max_workers):
        self.max_workers, self.submitted = max_workers, 0
        _SpyPool.made.append(self)

    def submit(self, fn, *args):
        self.submitted += 1
        f = Future()
        f.set_result(fn(*args))
        return f

    def shutdown(self, *a, **k):
        pass


def test_the_pool_is_capped_by_isar_workers_and_one_job_runs_inline(tmp_path, monkeypatch):
    monkeypatch.setenv("ISAR_WORKERS", "2")                         # the conftest's nested-pool guard value
    assert runner.pool_size(8, 10) == 2 and runner.pool_size(1, 10) == 1 and runner.pool_size(8, 1) == 1
    ws = _workspace(tmp_path, (100, 150, 200), 2)
    k = _keys(ws)
    monkeypatch.setattr(runner, "ProcessPoolExecutor", _SpyPool)
    _SpyPool.made = []
    bench = Bench(ws)
    spec = _anchored(ws, [k["a03", 100.0], k["a04", 150.0]])        # ring 1: three refits, ring 2: one
    bench.run(spec, cores=8)
    (pool,) = _SpyPool.made
    assert pool.max_workers == 2
    assert pool.submitted == 3                                      # ring 2's single refit ran inline
    assert [r.cores for r in bench.results if r.status == "cores"] == [2]
    overlays = [p["overlay"]["fast_pass"] for _, p in _lines(ws, spec.batch_id)]
    assert len(overlays) == 4 and {o["workers"] for o in overlays} == {1}    # the core is always told 1
    # one core: no pool at all, no cores notice
    _SpyPool.made = []
    bench = Bench(ws)
    bench.run(_anchored(ws, [k["a03", 100.0], k["a04", 150.0]]), cores=1)
    assert _SpyPool.made == [] and not [r for r in bench.results if r.status == "cores"]


def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    import ctypes
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
    finally:
        k32.CloseHandle(handle)


def test_stopping_a_batch_in_the_service_kills_its_pool_workers(tmp_path):
    """The real service runs the real runner on the fake core (``_inter_fakes:run_batch``): ring 1's refits hang
    in two pool workers; a stop ends the item and every pool worker is gone by the time it is reported."""
    ws = _workspace(tmp_path, (100, 150, 200), 2)
    k = _keys(ws)
    pid_dir = tmp_path / "pids"
    pid_dir.mkdir()
    _set_fast(ws, {k[d, t]: (5.0, {"pid_dir": str(pid_dir), "hang": True}) for d in ("a03", "a04") for t in (100.0, 200.0)})
    spec = _anchored(ws, [k["a03", 150.0], k["a04", 150.0]])        # ring 1: four hanging refits
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.environ.get("PYTHONPATH")]))}
    proc = subprocess.Popen([sys.executable, "-m", "isar.session.analysis_service", "--workspace", str(ws),
                             "--inter-target", "_inter_fakes:run_batch"], cwd=REPO, env=env,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    events = []

    def send(**cmd):
        proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
        proc.stdin.flush()

    def wait(ev, timeout_s=60.0):
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            events.append(json.loads(line))
            if events[-1].get("ev") == ev:
                return events[-1]
        raise AssertionError(f"no {ev}; events: {events}")

    pids = []
    try:
        wait("ready")
        send(op="start", item_id="b1", unit_key=f"inter:{spec.batch_id}:batch", cores=2, batch=spec.to_json(),
             **{"pass": "inter"})
        root_pid = wait("started")["pid"]
        deadline = time.monotonic() + 60
        while len(list(pid_dir.iterdir())) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        pids = [int(f.name.split("-")[1]) for f in pid_dir.iterdir()]
        assert len(pids) == 2 and root_pid not in pids and all(_alive(p) for p in pids)
        send(op="stop", item_id="b1")
        assert wait("finished")["end"] == "stopped"
        deadline = time.monotonic() + 5
        while any(_alive(p) for p in [root_pid, *pids]) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not any(_alive(p) for p in [root_pid, *pids])
        send(op="shutdown")
        assert proc.wait(30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        for p in pids:
            if _alive(p):
                subprocess.run(["taskkill", "/PID", str(p), "/F"], capture_output=True, check=False) \
                    if os.name == "nt" else os.kill(p, 9)


# ---------------------------------------------------------------------------
# check 5: timing against a target nothing enforces
# ---------------------------------------------------------------------------
def test_a_batch_far_over_its_target_still_completes_and_records_its_timing(tmp_path):
    ws = _workspace(tmp_path, (100, 150, 200), 2)
    k = _keys(ws)
    _set_fast(ws, {key: (5.0, {"sleep": 0.1}) for key in k.values()})
    bench = Bench(ws)
    spec = _anchored(ws, [k["a03", 100.0], k["a04", 100.0]])
    log = bench.run(spec, target_s=0.2)
    end = log.batches[spec.batch_id].end
    t = end["timing"]
    assert end["state"] == "completed" and end["rings_done"] == 2 and end["done"] == 4
    assert len([s for s in log.stages if s.batch == spec.batch_id]) == 2          # every ring saved
    for name in ("wall_s", "prepare_s", "ship_s", "save_s", "continuous_s", "assign_s", "likelihood_s",
                 "uncertainty_s", "starts"):
        assert name in t
    assert [(s["kind"], s["index"], s["width"], s["n_refits"]) for s in t["stages"]] == [("ring", 1, 2, 2),
                                                                                        ("ring", 2, 2, 2)]
    assert t["wall_s"] >= sum(s["wall_s"] for s in t["stages"]) >= 0.4
    assert (t["target_s"], t["over_target"]) == (0.2, True)
    assert bench.end().status == "done"


# ---------------------------------------------------------------------------
# the real core, end to end (not slow: 2 analyzers x 2 temperatures, tiny generated spectra, one core)
# ---------------------------------------------------------------------------
def test_the_real_core_runs_both_batch_kinds_end_to_end(tmp_path):
    from isar.session.file_fit import FileItem, fit_file
    from isar.session import ingest
    ws = fx.workspace(tmp_path, sample="Smp")
    fx.write_library(ws.root / "rf", analyzers=("a03", "a04"))
    assert fx.save_resolution_folder(ws.root, ws.root / "rf").ok
    for i, temp in enumerate((100, 200)):
        fx.write_scan(tmp_path / "data", stem=f"Smp_{temp}K_0p5_0p5_3", analyzers=("a03", "a04"), seed=1 + i,
                      energy=fx.ENERGY_120)
    text = ws.policy_path.read_text(encoding="utf-8").replace("min_fit_points = 6", "min_fit_points = 6\nmax_modes = 1")
    ws.policy_path.write_text(text, encoding="utf-8", newline="\n")
    card, session = ws.load()
    ingest.ingest_batch(card, session, [tmp_path / "data"], workspace_root=ws.root)
    ws.save(card, session)
    for key in list(ws.load()[1].scans):
        out = []
        fit_file(ws.root, FileItem("q", key, "fast", 1), progress=None, emit=out.append)
        assert writer.save_results(ws.root, [r.payload for r in out if r.payload is not None]) == [None, None]
    assert inter_values.save_settings(ws.root, {"sweep_cap": 3}).ok
    k = _keys(ws.root)
    # simultaneous first, straight from the fast solos (no anchored batch before it), then anchored
    for spec in (_simultaneous(ws.root, list(k.values())),
                 _anchored(ws.root, [k["a03", 100.0], k["a04", 100.0]])):
        bench = Bench(ws.root)
        log = bench.run(spec, core=None)
        end = log.batches[spec.batch_id].end
        print(spec.kind, end["state"], json.dumps(end["timing"], indent=1))
        assert end["state"] in ("completed", "settled"), end
        lines = [r for r in log.refits if r.batch == spec.batch_id]
        assert lines and all(r.status in records.ELIGIBLE for r in lines)
        assert all(records.read_payload(ws.root, r)["overlay"]["fast_pass"]["source"] == "inter_refit" for r in lines)
        if spec.kind == "anchored":
            assert end["done"] == 2 and {r.accept.decision for r in lines} == {"authoritative"}
            continue
        # the T-linked fast solos share one branch id and keep their coupling factor after E18 (amendment 21)
        came_out = {r.unit: records.read_payload(ws.root, r)["came_out"] for r in lines if r.index == 1}
        for det in ("a03", "a04"):
            cold, hot = came_out[k[det, 100.0]], came_out[k[det, 200.0]]
            assert cold["assignment"]["slots"] == hot["assignment"]["slots"]
            assert cold["assignment"]["factors"] and hot["assignment"]["factors"]
