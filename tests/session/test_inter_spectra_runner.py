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
from isar.intake import inputs, inter_values, scans as intake_scans, store
from isar.schema.card import load_state
from isar.session import writer
from isar.session.inter_spectra import records, runner
from isar.session.inter_spectra.graph import anchored_scope
from isar.session.inter_spectra.nodes import read_nodes
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


def test_a_count_change_in_a_later_round_reopens_the_sweeps(tmp_path):
    ws, x, y, z = _chain(tmp_path, ({"toy": "average", "change_rounds": [2]}, {"toy": "keep"}, {"toy": "average"}))
    bench = Bench(ws)
    spec = _simultaneous(ws, (x, y, z))
    end = bench.run(spec).batches[spec.batch_id].end
    assert (end["state"], end["sweeps"], end["rounds"]) == ("settled", 5, 3)


def test_refused_members_of_one_group_go_back_and_the_sweep_is_not_quiet(tmp_path):
    """The user picks a fit for y during sweep 1: x and z (one group, both conditioned on y's batch refit) are
    refused at save. Both held values go back (sweep 2 starts each from its incoming fit), and no sweep with a
    refusal is quiet, so the batch never settles on them (the cap ends it) though no toy moves anything."""
    ws, x, y, z = _chain(tmp_path, ({"toy": "keep"}, {"toy": "keep"}, {"toy": "keep"}))
    assert inter_values.save_settings(ws, {"sweep_cap": 3}).ok
    bench = Bench(ws, _when("sweep", 1, "refine", lambda: inter_values.pick_fit(ws, y, "fast")))
    spec = _simultaneous(ws, (x, y, z))
    log = bench.run(spec)
    lines = _lines(ws, spec.batch_id)
    refused = {r.unit for r, _ in lines if r.index == 1 and r.accept.decision == "declined"}
    assert refused == {x, z}
    assert {r.unit: p["came_in"]["own"]["fit"] for r, p in lines if r.index == 2 and r.unit in (x, z)} == \
        {x: "fast", z: "fast"}
    assert log.batches[spec.batch_id].end["state"] == "did_not_settle"


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
