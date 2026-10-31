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


def test_a_start_records_the_q_slope_each_anchored_scan_used_and_a_q_link_needs_a_metric():
    # the start body built by the real _Run.start, only its transport stubbed: an anchored batch names the slope
    # and unit of each scan's metric, a simultaneous one names none (it uses no q slope)
    from types import SimpleNamespace
    from isar.analyze.coupled.settings import InterSettings
    sent = []
    graph = SimpleNamespace(metric={"E:1": "hkl", "E:2": "lattice"})
    for kind in ("anchored", "simultaneous"):
        run = SimpleNamespace(kind=kind, settings=InterSettings(), graph=graph, members=(), fixed={}, not_reached={},
                              frozen=SimpleNamespace(inputs_at=0), intake_seq=0, epochs={}, inputs={},
                              spec=SimpleNamespace(direction="q"), started=False,
                              payload=lambda event, body: {"event": event, "start": body},
                              send=lambda p: sent.append(p) or {"saved": True})
        runner._Run.start(run, anchors=(), rings={}, seams=(), preview="", terms=())
    anchored, simultaneous = (p["start"]["q_slope"] for p in sent)
    assert anchored == {"E:1": {"metric": "hkl", "slope": 10.6, "unit": "meV per r.l.u."},
                        "E:2": {"metric": "lattice", "slope": 8.0, "unit": "meV per inverse angstrom"}}
    assert simultaneous == {}
    link = SimpleNamespace(kind="q", a="E:1:a03", b="E:1:a04", step=0.1, metric=None)
    with pytest.raises(ValueError, match=r"E:1:a03\|E:1:a04 has no metric"):
        runner._term(link)
    assert runner._term(SimpleNamespace(**{**vars(link), "metric": "hkl"})).metric == "hkl"


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
# check 4: stage 1 is search, price, resume (S3U3)
# ---------------------------------------------------------------------------
def _ring1(tmp_path, cfgs: dict):
    """Two analyzers at 100 and 150 K, both 100 K scans the anchors: ring 1 is a03@150 and a04@150. ``cfgs``:
    (detector, T) -> fake knobs; every other scan gets none."""
    ws = _workspace(tmp_path, (100, 150), 2)
    k = _keys(ws)
    _set_fast(ws, {key: (5.0, cfgs.get(dt, {})) for dt, key in k.items()})
    return ws, k, _anchored(ws, [k["a03", 100.0], k["a04", 100.0]])


def _spy_maps(monkeypatch) -> list:
    """Record ``(fn, number of calls)`` of every pool map, and of every stream (all the calls it ran)."""
    seen, orig, orig_stream = [], runner._Pool.map, runner._Pool.stream

    def spy(self, fn, calls):
        seen.append((fn, len(calls)))
        return orig(self, fn, calls)

    def spy_stream(self, fn, calls, more):
        n = [0]

        def counted(key, result):
            n[0] += 1
            return more(key, result)
        try:
            return orig_stream(self, fn, calls, counted)
        finally:
            seen.append((fn, n[0]))
    monkeypatch.setattr(runner._Pool, "map", spy)
    monkeypatch.setattr(runner._Pool, "stream", spy_stream)
    return seen


def _spectra(log, batch) -> dict:
    return {(r["stage"], r["unit"]): r for r in log.batches[batch].end["timing"]["spectra"]}


def test_a_spectrum_sends_its_next_pass_while_another_spectrums_job_still_runs(tmp_path, monkeypatch):
    """No barrier: a03's one job is slow; a04's three passes all go to the pool before a03's job is back."""
    from concurrent.futures import ThreadPoolExecutor
    monkeypatch.setenv("ISAR_WORKERS", "2")
    monkeypatch.setattr(runner, "ProcessPoolExecutor", ThreadPoolExecutor)    # in process: the fake core is importable
    events, orig = [], runner._Run.fly

    def fly(self, key, on):
        events.append((key, on))
        return orig(self, key, on)
    monkeypatch.setattr(runner._Run, "fly", fly)
    ws, k, spec = _ring1(tmp_path, {("a03", 150.0): {"triggered": True, "price_sleep": 1.0},
                                    ("a04", 150.0): {"triggered": True, "iterations": 3}})
    slow, quick = k["a03", 150.0], k["a04", 150.0]
    log = Bench(ws).run(spec, cores=2)
    price = [(key, on) for key, on in events if isinstance(key, tuple)]
    slow_back = price.index(next((key, False) for key, on in price if key[0] == slow))
    quick_sent = [i for i, (key, on) in enumerate(price) if key[0] == quick and on]
    assert len(quick_sent) == 3 and quick_sent[-1] < slow_back      # a04's third pass flew before a03's job was back
    rows = _spectra(log, spec.batch_id)
    assert (rows["ring1", quick]["iterations"], rows["ring1", quick]["candidates"]) == (3, 3)
    assert (rows["ring1", slow]["iterations"], rows["ring1", slow]["candidates"]) == (1, 1)
    t = log.batches[spec.batch_id].end["timing"]
    assert t["pool"]["iterations"] == 4 and t["pool"]["workers"] == 2 and t["search_s"] >= 0.0
    assert t["pool"]["busy_s"] <= t["pool"]["map_wall_s"] * 2           # utilisation stays a fraction


class _ScriptSearch:
    """A search that asks scripted passes and records every price it is fed, in order."""

    def __init__(self, passes) -> None:
        self.passes, self.fed = list(passes), []

    @property
    def done(self) -> bool:
        return not self.passes

    def next_jobs(self) -> tuple:
        return tuple(self.passes[0]) if self.passes else ()

    def feed(self, priced) -> None:
        self.passes.pop(0)                    # the test compares what was fed with the job order
        self.fed.extend((p.key, p.ln_z, p.status, p.depth) for p in priced)

    def result(self):
        from types import SimpleNamespace as NS
        return NS(coverage={})


def _job(unit, key, depth="full"):
    from isar.analyze.coupled.types import PricingJob
    return PricingJob(unit, "s", 1, ("B",), key, depth=depth)


def _scripted_price(job):
    from isar.analyze.coupled.types import Priced
    return Priced(job.key, job.count, tuple(job.slots), -float(len(job.key)) - (job.depth == "screen"), "ok", None,
                  None, 1, 0.01, depth=job.depth)


class _OrderPool:
    """Stands in for the run's ``_Pool``: ``workers`` calls in flight, the next to come back picked by ``order``
    (``fifo``, ``reversed`` = newest first, ``interleaved`` = oldest and newest in turn); ``raises``: job keys
    whose call raises."""

    def __init__(self, order, workers=3, raises=()) -> None:
        self.order, self.workers, self.raises, self.ship_s = order, workers, set(raises), 0.0

    def stream(self, fn, calls, more) -> None:
        queue, flying, n = list(calls), [], 0
        while queue or flying:
            while queue and len(flying) < self.workers:
                flying.append(queue.pop(0))
            i = {"fifo": 0, "reversed": -1, "interleaved": 0 if n % 2 else -1}[self.order]
            key, (snap, job), _kw = flying.pop(i)
            n += 1
            queue.extend(more(key, RuntimeError("boom") if job.key in self.raises else _scripted_price(job)))


def _scripted_run(pool, searches: dict, cache: dict):
    from types import SimpleNamespace as NS
    run = runner._Run(Path("."), NS(spec={"batch_id": "b", "kind": "anchored"}), None, None, None,
                      NS(open_search=lambda s: searches[s.unit]), 30.0)
    run.pool, run.index = pool, 1
    snaps = {u: NS(unit=u, terms=(), aliases={}) for u in searches}
    return run, run.assign(snaps, cache)


def _scripts():
    """x: a pass the cache answers alone, then one with a cache hit among fresh jobs; y: a screen of a candidate,
    then its promotion (the full key, depth "full") beside a new job; z: one fresh pass."""
    x = [[_job("x", "x-c1"), _job("x", "x-c2")], [_job("x", "x-a"), _job("x", "x-c3"), _job("x", "x-bb")],
         [_job("x", "x-ccc")]]
    y = [[_job("y", "y-1"), _job("y", "y-2:screen", "screen")], [_job("y", "y-2:full"), _job("y", "y-3")],
         [_job("y", "y-4444")]]
    z = [[_job("z", "z-1"), _job("z", "z-22"), _job("z", "z-333")]]
    return {"x": x, "y": y, "z": z}


def _cached():
    from isar.analyze.coupled.types import Priced
    return {"x": {key: Priced(key, 1, ("B",), 7.0, "ok", None, None, 1, 0.5) for key in ("x-c1", "x-c2", "x-c3")}}


def test_each_search_sees_the_same_prices_in_job_order_whatever_order_the_pool_returns_them():
    """B's ordering (S1U4 check 1): over three completion orders, each search's feed is identical and is what a
    barrier per pass fed it: every pass's prices in its job order, cache hits in their place."""
    feeds = {}
    for order in ("fifo", "reversed", "interleaved"):
        searches = {u: _ScriptSearch(p) for u, p in _scripts().items()}
        run, (A, errors) = _scripted_run(_OrderPool(order), searches, _cached())
        assert errors == {} and sorted(A) == ["x", "y", "z"]
        feeds[order] = {u: s.fed for u, s in searches.items()}
    cached = _cached()
    expect = {u: [(j.key, *((7.0, "ok", "full") if j.key in cached.get(u, {}) else
                            (_scripted_price(j).ln_z, "ok", j.depth))) for p in passes for j in p]
              for u, passes in _scripts().items()}
    assert feeds["fifo"] == feeds["reversed"] == feeds["interleaved"] == expect
    assert ("y-2:screen", -11.0, "ok", "screen") in expect["y"] and ("y-2:full", -8.0, "ok", "full") in expect["y"]
    rows = {t.unit: t for t in run.tallies.values()}
    assert (rows["x"].iterations, rows["x"].candidates, rows["x"].cached) == (2, 3, 3)   # the cache-only pass is no iteration
    assert run.timing["pool"]["iterations"] == 6


def test_a_failed_screen_job_comes_back_as_a_failed_screen_price():
    searches, cache = {"y": _ScriptSearch([[_job("y", "y-1"), _job("y", "y-2:screen", "screen")]])}, {}
    run, (A, errors) = _scripted_run(_OrderPool("fifo", raises={"y-2:screen"}), searches, cache)
    assert searches["y"].fed == [("y-1", -3.0, "ok", "full"), ("y-2:screen", None, "no_evidence", "screen")]
    assert set(cache["y"]) == {"y-1"} and run.tallies["ring1", "y"].failures == 1     # a failure is never cached


def test_a_simultaneous_round_with_unchanged_inputs_prices_nothing(tmp_path, monkeypatch):
    """x moves in round 1 and y keeps; round 2 re-prices x (its own value changed) and y (a neighbour's did) and
    serves z (nobody it reads moved) from the cache, with no pool work for it. The fake's keys are values, never
    fit ids; the real core's carry the ids (so an accepted sweep renews them), which the runner does not look at."""
    cfg = {"triggered": True}
    ws, x, y, z = _chain(tmp_path, ({**cfg, "toy": "average"}, {**cfg, "toy": "keep"}, {**cfg, "toy": "keep"}))
    seen = _spy_maps(monkeypatch)
    spec = _simultaneous(ws, (x, y, z))
    log = Bench(ws).run(spec)
    assert log.batches[spec.batch_id].end["rounds"] == 2
    rows = _spectra(log, spec.batch_id)
    assert [(rows["round1", u]["candidates"], rows["round1", u]["cached"]) for u in (x, y, z)] == [(1, 0)] * 3
    assert [(rows["round2", u]["candidates"], rows["round2", u]["cached"]) for u in (x, y, z)] == [(1, 0), (1, 0), (0, 1)]
    assert rows["round2", z]["iterations"] == 0
    assert [m for m in seen if m[0] == "price"] == [("price", 3), ("price", 2)]


def test_a_pool_exception_becomes_a_recorded_failure_reason_and_is_priced_again(tmp_path):
    cfg = {"triggered": True, "toy": "keep"}
    ws, x, y, z = _chain(tmp_path, ({**cfg, "price_raise": [0]}, cfg, cfg))
    spec = _simultaneous(ws, (x, y, z))
    bench = Bench(ws)
    log = bench.run(spec)
    assert log.batches[spec.batch_id].end["state"] == "settled" and bench.end().status == "done"
    rows = _spectra(log, spec.batch_id)
    reason = [["pricing failed (RuntimeError: boom)", 1]]
    assert (rows["round1", x]["failures"], rows["round1", x]["failure_reasons"]) == (1, reason)
    # never cached: round 2 asks again (and fails again); the others are served from the cache
    assert (rows["round2", x]["candidates"], rows["round2", x]["cached"], rows["round2", x]["failures"]) == (1, 0, 1)
    assert [rows["round2", u]["candidates"] for u in (y, z)] == [0, 0]


def test_a_pool_exception_in_a_live_worker_fails_only_that_candidate(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(HERE), os.environ.get("PYTHONPATH")])))
    monkeypatch.setenv("ISAR_WORKERS", "2")
    ws, k, spec = _ring1(tmp_path, {("a03", 150.0): {"triggered": True, "price_raise": [0]},
                                    ("a04", 150.0): {"triggered": True}})
    bench = Bench(ws)
    log = bench.run(spec, cores=2)                                  # a real two-worker process pool
    rows = _spectra(log, spec.batch_id)
    assert rows["ring1", k["a03", 150.0]]["failure_reasons"] == [["pricing failed (RuntimeError: boom)", 1]]
    assert rows["ring1", k["a04", 150.0]]["failures"] == 0
    assert log.batches[spec.batch_id].end["done"] == 2 and bench.end().status == "done"


@pytest.mark.parametrize("kind", ["anchored", "simultaneous"])
@pytest.mark.parametrize("where", ["open", "next", "feed", "result"])
def test_a_search_that_raises_fails_only_its_own_spectrum(tmp_path, kind, where):
    bad = {"triggered": True, "search_raise": where, "toy": "keep"}
    good = {"triggered": True, "toy": "keep"}
    if kind == "anchored":
        ws, k, spec = _ring1(tmp_path, {("a03", 150.0): bad, ("a04", 150.0): good})
        failed, other = k["a03", 150.0], k["a04", 150.0]
    else:
        ws, failed, other, _z = _chain(tmp_path, (bad, good, good))
        spec = _simultaneous(ws, (failed, other, _z))
    bench = Bench(ws)
    log = bench.run(spec)
    end = log.batches[spec.batch_id].end
    assert bench.end().status == "done_warning"
    assert [f["unit"] for f in end["failed_units"]] == [failed]
    assert "the fake search failed" in end["failed_units"][0]["reason"] and "RuntimeError" in end["failed_units"][0]["reason"]
    by_unit = {r.unit: r for r in log.refits if r.batch == spec.batch_id}
    assert other in by_unit and by_unit[other].status in records.ELIGIBLE
    if kind == "anchored":
        assert by_unit[failed].status == "failed" and end["state"] == "completed"
    else:
        assert failed not in by_unit and end["state"] == "settled"
    assert end["timing"] and _spectra(log, spec.batch_id)[(f"ring1" if kind == "anchored" else "round1", failed)][
        "error"].startswith("RuntimeError")


def test_a_ring_whose_every_search_fails_ends_without_error(tmp_path):
    bad = {"triggered": True, "search_raise": "open"}
    ws, k, spec = _ring1(tmp_path, {("a03", 150.0): bad, ("a04", 150.0): bad})
    bench = Bench(ws)
    end = bench.run(spec).batches[spec.batch_id].end
    assert (end["state"], end["rings_done"], end["failed"], end["done"]) == ("completed", 1, 2, 0)
    assert bench.end().status == "done_warning"


def test_a_failed_ringmate_is_no_identity_neighbour_of_its_survivor(tmp_path, monkeypatch):
    """A ring member whose search raised is not in the ring's stage, so its own fast fit must not reach the
    identity reconciliation as an earlier neighbour: it would hand its branches to the survivor."""
    from isar.analyze.coupled import identity
    seen, orig = [], identity.propagate_ids

    def spy(order, links, modes, settings, **kw):
        seen.append((tuple(order), set(kw["previous"])))
        return orig(order, links, modes, settings, **kw)
    monkeypatch.setattr(identity, "propagate_ids", spy)
    bad = {"triggered": True, "search_raise": "open"}
    ws, k, spec = _ring1(tmp_path, {("a03", 150.0): bad, ("a04", 150.0): {"triggered": True}})
    failed, other = k["a03", 150.0], k["a04", 150.0]
    Bench(ws).run(spec)
    ring_calls = [prev for order, prev in seen if order == (other,)]
    assert ring_calls and all(failed not in prev for prev in ring_calls)


@pytest.mark.timeout(20)
def test_a_search_that_repeats_a_job_is_failed_not_looped(tmp_path):
    ws, k, spec = _ring1(tmp_path, {("a03", 150.0): {"triggered": True, "iterations": 2, "search_repeat": True},
                                    ("a04", 150.0): {"triggered": True}})
    log = Bench(ws).run(spec)
    row = _spectra(log, spec.batch_id)["ring1", k["a03", 150.0]]
    assert "repeated a job" in row["error"] and row["candidates"] == 1


def test_the_timing_block_reports_contract_10(tmp_path, monkeypatch):
    slow = {"triggered": True, "iterations": 2, "jobs_per_iteration": 2, "starts": 3, "fits": 5, "price_sleep": 0.03,
            "stopped": {"down": "behind_margin:1", "up": "behind_margin:3"}}
    quick = {"triggered": True, "iterations": 2, "jobs_per_iteration": 2, "starts": 3, "fits": 5}
    ws, k, spec = _ring1(tmp_path, {("a03", 150.0): slow, ("a04", 150.0): quick})
    seen = _spy_maps(monkeypatch)
    bench = Bench(ws)
    t = bench.run(spec, target_s=0.0).batches[spec.batch_id].end["timing"]
    a, b = (next(r for r in t["spectra"] if r["unit"] == k[d, 150.0]) for d in ("a03", "a04"))
    assert a["stage"] == "ring1" and len(t["spectra"]) == 2          # ring 1 only: nothing else was searched
    for r in (a, b):
        assert (r["iterations"], r["candidates"], r["cached"], r["starts"], r["fits"], r["failures"]) == (2, 4, 0, 12, 20, 0)
        assert r["starts_per_candidate"] == 3.0 and r["failure_reasons"] == [] and r["error"] is None
        assert r["refine_s"] >= 0.0 and r["branches_seen"] == 1 and r["dormant_seen"] == 0
    assert a["stopped"] == {"down": "behind_margin:1", "up": "behind_margin:3"} and a["evidence_s"] >= 0.1
    assert b["stopped"] == {"down": "fake", "up": "fake"} and b["evidence_s"] < a["evidence_s"]
    assert t["slowest"]["spectrum"]["unit"] == a["unit"] and t["slowest"]["spectrum"]["stage"] == "ring1"
    assert t["slowest"]["stage"]["kind"] == "ring" and t["slowest"]["stage"]["wall_s"] == max(
        s["wall_s"] for s in t["stages"])
    assert [m for m in seen if m[0] == "price"] == [("price", 8)]               # one stream for the ring, its 8 jobs
    assert t["pool"]["iterations"] == 4                                         # two passes of each spectrum
    assert t["pool"]["busy_s"] == pytest.approx(a["evidence_s"] + b["evidence_s"])
    assert (t["pool"]["workers"], t["over_target"]) == (1, True) and bench.end().status == "done"


def test_the_branches_a_spectrum_saw_are_counted_once_under_their_canonical_names():
    from types import SimpleNamespace as NS
    mode = lambda b: NS(branch=b)                   # noqa: E731
    h1 = NS(modes=(mode("p"), mode("q")), dormant=(NS(branch="r"),))
    h2 = NS(modes=(mode("p2"),), dormant=(NS(branch="r"), NS(branch="s")))
    snap = NS(terms=(NS(held=(h1,)), NS(held=(h2,))), aliases={"p2": "p"})
    assert runner._seen(snap) == (2, 2)


class _DeadPool:
    """Stands in for ProcessPoolExecutor: the first submit runs, then a worker dies and every later submit raises."""
    made: list = []

    def __init__(self, max_workers):
        self.sent = 0
        _DeadPool.made.append(self)

    def submit(self, fn, *args):
        if self.sent >= 1:
            raise runner.BrokenProcessPool("a worker died")
        self.sent += 1
        f = Future()
        f.set_result(fn(*args))
        return f

    def shutdown(self, *a, **k):
        pass


def test_a_dead_worker_fails_its_jobs_and_ends_the_batch_at_the_next_pool_call(tmp_path, monkeypatch):
    monkeypatch.setenv("ISAR_WORKERS", "2")
    monkeypatch.setattr(runner, "ProcessPoolExecutor", _DeadPool)
    _DeadPool.made = []
    cfg = {"triggered": True, "iterations": 2}
    ws, k, spec = _ring1(tmp_path, {("a03", 150.0): cfg, ("a04", 150.0): cfg})
    bench = Bench(ws)
    log = bench.run(spec, cores=2)
    end = bench.end()
    assert end.status == "failed" and "BrokenProcessPool" in end.reason       # the second pass reached the dead pool
    assert len(_DeadPool.made) == 1                                           # and no replacement was opened
    assert [r for r in log.refits if r.batch == spec.batch_id] == []          # nothing of the stage was saved
    rows = [r for r in end.payload["end"]["timing"]["spectra"]]
    failed = [r for r in rows if r["failures"]]
    assert len(failed) == 1 and failed[0]["candidates"] == 1
    assert "BrokenProcessPool" in failed[0]["failure_reasons"][0][0]
    assert sum(r["candidates"] for r in rows) == 2 and sum(r["failures"] for r in rows) == 1   # one price survived


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
                 "uncertainty_s", "starts", "search_s", "pool", "spectra", "slowest"):
        assert name in t
    assert t["pool"]["workers"] == 1
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
        for r in end["timing"]["spectra"]:               # the search ran on every spectrum and told why it stopped
            assert r["stopped"] is not None and r["error"] is None
            assert r["failures"] == sum(n for _, n in r["failure_reasons"])
        lines = [r for r in log.refits if r.batch == spec.batch_id]
        # a later sweep may decline a move at equal J (a flat direction): a reasoned verdict, not a failure
        assert lines and all(r.status in records.ELIGIBLE or (r.status, r.reason, r.index > 1)
                             == ("rejected", "material_change_without_gain", True) for r in lines)
        assert all(records.read_payload(ws.root, r)["overlay"]["fast_pass"]["source"] == "inter_refit"
                   for r in lines if r.status in records.ELIGIBLE)
        if spec.kind == "anchored":
            assert end["done"] == 2 and {r.accept.decision for r in lines} == {"authoritative"}
            continue
        # the T-linked fast solos share one branch id and keep their coupling factor after E18 (amendment 21)
        came_out = {r.unit: records.read_payload(ws.root, r)["came_out"] for r in lines if r.index == 1}
        for det in ("a03", "a04"):
            cold, hot = came_out[k[det, 100.0]], came_out[k[det, 200.0]]
            assert cold["assignment"]["slots"] == hot["assignment"]["slots"]
            assert cold["assignment"]["factors"] and hot["assignment"]["factors"]


def test_the_chosen_width_mask_survives_the_stage_factor_rewrites(monkeypatch):
    """N1: ``canonical_factors`` and ``rebuilt_factors`` rewrite factors and slot ids, never the decision: the chosen
    basin, the change and the mask-only mark come out as they went in (the mask positions are slot positions)."""
    from types import SimpleNamespace

    from isar.analyze.coupled.engine import Factor
    from isar.analyze.coupled.types import Assignment
    f = Factor("T-change:x|y:q", "q", ("x", "y"), (1.0, -1.0), 2.0, 12.0, "dT")
    chosen = (True, True, False, (1,), (5.0, 0.4, 20.0, 11.0, 0.001, 20.0, 100.0, 2.0))
    a = Assignment(unit="y", snapshot_id="b:ring1", count=2, slots=("q", "r"), origins=("matched", "matched"),
                   factors=(f,), discrete="priced", triggers=(), candidates=(), coverage={}, search_limited=False,
                   changed=True, reasons=(), selected_model=chosen, mask_only=True)
    ids = SimpleNamespace(alternatives={"aliases": {"p": ["q"]}}, ids={("y", "f_y", 0): "p", ("y", "f_y", 1): "r"})
    out, rename = runner.canonical_factors({"y": a}, ids, {"y": (("y", "f_y", 0), ("y", "f_y", 1))},
                                           {"x": SimpleNamespace(modes=(SimpleNamespace(branch="q"),))})
    assert rename == {"q": "p"} and out["y"].slots == ("p", "r")
    monkeypatch.setattr(runner, "_renamed_snapshot", lambda snap, rn: snap)
    core = SimpleNamespace(stage_factors=lambda snap: {})
    built = runner.rebuilt_factors(core, out, {"y": object()}, rename)
    for got in (out["y"], built["y"]):
        assert (got.selected_model, got.changed, got.mask_only) == (chosen, True, True)


# ---------------------------------------------------------------------------
# the task scheduler: inner work and lookahead on the one pool (S1U3)
# ---------------------------------------------------------------------------
class _CountingPool:
    """Stands in for ProcessPoolExecutor in process (threads): records the most tasks submitted and not yet back."""

    def __init__(self, workers):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.ex, self.lock = ThreadPoolExecutor(workers), threading.Lock()
        self.now = self.peak = 0
        self._processes = {}              # no worker processes to stop: shutdown below awaits the threads, as before

    def submit(self, fn, *args):
        with self.lock:
            self.now += 1
            self.peak = max(self.peak, self.now)
        fut = self.ex.submit(fn, *args)
        fut.add_done_callback(self._back)
        return fut

    def _back(self, _fut):
        with self.lock:
            self.now -= 1

    def shutdown(self, *a, **k):
        self.ex.shutdown(wait=True)


def test_inner_work_of_one_candidate_uses_the_spare_workers_and_never_more_than_the_pool():
    from inter_hidden_case import hidden_snapshot

    from isar.analyze.coupled import refit
    from isar.analyze.coupled.types import PricingJob, Start
    snap = hidden_snapshot(3.0, asimov=True)
    job = PricingJob("y", snap.snapshot_id, 2, ("b1", "b2"), "k",
                     (Start("neighbour", ((8.0, 0.4, None), (7.2, 0.4, None))),))
    pool = runner._Pool(refit, 4)
    pool.ex = _CountingPool(4)
    got = []
    pool.stream("price", [(("y", "k"), (snap, job), {})], lambda key, r: got.append((key, r)) or ())
    ex = pool.ex
    pool.close()                       # waits for the node tasks nobody read, which then drop out of the orphans
    ((key, priced),) = got
    assert key == ("y", "k") and not isinstance(priced, Exception) and priced.ln_z is not None
    assert 1 < ex.peak <= 4, ex.peak          # one candidate live: its fits and split nodes filled the pool, no more
    assert ex.now == 0 and pool.busy_s > 0.0 and pool.orphans == set()


def _fake_core(monkeypatch, script):
    """A module importable by name here with ``price(snap, job, tasks)`` and ``run_task``: the price runs
    ``script[("before", key)]``, submits ``script[("n", key)]`` (default 1) tasks of kind ``key`` and waits for them,
    then runs ``script[("after", key)]``; a task runs ``script[("run", kind)](args)``. ``core.log`` has every task."""
    import types

    from isar.analyze.coupled.types import Priced
    core, log = types.ModuleType("fake_task_core"), []
    nothing = lambda *a: None       # noqa: E731

    def price(snap, job, tasks=None):
        script.get(("before", job.key), nothing)()
        for h in [tasks.submit((b"ctx", job.key, (i,))) for i in range(script.get(("n", job.key), 1))]:
            tasks.wait(h)
        script.get(("after", job.key), nothing)()
        return Priced(job.key, job.count, tuple(job.slots), -1.0, "ok", None, None, 1, 0.0, depth=job.depth)

    def run_task(ctx, kind, args):
        log.append((kind, args))
        script.get(("run", kind), nothing)(args)
        return kind
    core.price, core.run_task, core.log = price, run_task, log
    monkeypatch.setitem(sys.modules, "fake_task_core", core)
    return core


class _KeyEvents(dict):
    """A cache that sets ``events[key]`` when ``key`` is stored."""

    def __init__(self, events):
        super().__init__()
        self.events = events

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        if key in self.events:
            self.events[key].set()


def test_idle_workers_price_the_next_promotions_and_the_later_request_is_served_from_the_cache(monkeypatch):
    import threading
    b_started, b_cached = threading.Event(), threading.Event()

    def need(ev):
        assert ev.wait(10), "lookahead never priced B while A ran"
    core = _fake_core(monkeypatch, {("run", "B"): lambda a: b_started.set(),
                                    ("run", "A"): lambda a: need(b_started),     # A's task ends once B's task has run
                                    ("after", "A"): lambda: need(b_cached)})     # and A's price returns once B is cached
    search = _ScriptSearch([[_job("u", "A")], [_job("u", "B")]])
    search.likely_next = lambda n: (_job("u", "B"), _job("u", "C"))              # C is named, the search never asks it
    pool = runner._Pool(core, 4)
    pool.ex = _CountingPool(4)
    run, (A, errors) = _scripted_run(pool, {"u": search}, {"u": _KeyEvents({"B": b_cached})})
    pool.close()
    assert errors == {} and "u" in A
    assert [f[0] for f in search.fed] == ["A", "B"]                # the search was fed what it asked, in its order
    assert [a for k, a in core.log if k == "B"] == [(0,)]          # B was priced once, ahead of the request
    (row,) = run.tallies.values()
    assert (row.candidates, row.cached) == (1, 1)                  # and its request was a cache hit


def test_a_requested_task_is_sent_before_any_lookahead_task_and_rank_zero_before_rank_one(monkeypatch):
    import pickle
    from concurrent.futures import Future

    class Hold:                      # records what is sent; a task is back when the test says so
        def __init__(self):
            self.sent, self.last = [], None

        def submit(self, fn, core_name, blob):
            self.sent.append(pickle.loads(blob)[1])
            self.last = Future()
            return self.last
    pool = runner._Pool(_fake_core(monkeypatch, {}), 1)               # one worker: tasks go one at a time
    pool.ex = Hold()
    sched = runner._Sched(pool, [], lambda k, r: (), None, None)
    sched.ex = pool.ex
    look = runner._JobPort(sched, "L", None, "lookahead")
    req = runner._JobPort(sched, "R", None, "requested")
    look.submit((b"c", "look1", ()), rank=1)
    req.submit((b"c", "req1", ()), rank=1)
    req.submit((b"c", "req0", ()))
    look.submit((b"c", "look0", ()))
    sched.live.update((look, req))
    while not sched.events.empty():
        sched._handle(sched.events.get_nowait())
    order = []
    for _ in range(4):
        sched._fill()
        order.append(pool.ex.sent[-1])
        pool.ex.last.set_result(("v", 0.0))
        sched._handle(sched.events.get(timeout=5))
    assert order == ["req0", "req1", "look0", "look1"], order
    sched.live.clear()


def test_a_task_left_running_by_a_finished_stream_is_an_orphan_the_next_map_works_around(monkeypatch):
    import threading
    import time
    b_started, gate = threading.Event(), threading.Event()
    state = {"now": 0, "peak": 0, "lock": threading.Lock()}

    def slow(x=None):
        with state["lock"]:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with state["lock"]:
            state["now"] -= 1
        return x
    core = _fake_core(monkeypatch, {("run", "B"): lambda a: (b_started.set(), gate.wait(10)),
                                    ("run", "A"): lambda a: b_started.wait(10)})
    core.slow = slow
    pool = runner._Pool(core, 4)
    pool.ex = _CountingPool(4)
    search = _ScriptSearch([[_job("u", "A")]])
    search.likely_next = lambda n: (_job("u", "B"),)
    _scripted_run(pool, {"u": search}, {})
    assert len(pool.orphans) == 1                                     # B's task is still running; nobody waits for it
    out = pool.map("slow", [(i, (i,), {}) for i in range(8)])
    assert list(out.values()) == list(range(8)) and not gate.is_set()
    assert state["peak"] == 3 and pool.ex.peak == 4                  # the workers less the orphan; the total held
    gate.set()
    pool.close()
    assert pool.orphans == set()


def test_close_stops_an_orphan_instead_of_waiting_for_it():
    import pickle
    import threading
    pool = runner._Pool(None, 1)
    pool._executor()
    fut = pool.ex.submit(runner._work, "time", "sleep", pickle.dumps(((60,), {})))    # a real worker, stuck for a minute
    pool.orphans.add(fut)
    procs = list(pool.ex._processes.values())
    deadline = time.perf_counter() + 30
    while not fut.running():
        assert time.perf_counter() < deadline, "the stuck task never started"
        time.sleep(0.01)
    closer = threading.Thread(target=pool.close, daemon=True)
    closer.start()
    closer.join(30)
    assert not closer.is_alive(), "close waited for the abandoned task"
    for proc in procs:
        proc.join(10)
    assert not any(proc.is_alive() for proc in procs) and pool.ex is None


def test_a_dead_pool_fails_the_jobs_in_flight_and_raises_from_the_stream_for_calls_queued_after(monkeypatch):
    from concurrent.futures import Future

    class Dying:                     # the first task runs, then a worker dies
        sent = 0

        def submit(self, fn, *args):
            Dying.sent += 1
            if Dying.sent > 1:
                raise runner.BrokenProcessPool("a worker died")
            f = Future()
            f.set_result(fn(*args))
            return f
    pool = runner._Pool(_fake_core(monkeypatch, {("n", "A"): 3}), 4)
    pool.ex = Dying()
    got = []

    def more(key, r):
        got.append((key, r))
        return [(("u", "Z"), (None, _job("u", "Z")), {})] if key == ("u", "A") else ()
    with pytest.raises(runner.BrokenProcessPool):
        pool.stream("price", [(("u", "A"), (None, _job("u", "A")), {})], more)
    assert [k for k, _r in got] == [("u", "A")] and isinstance(got[0][1], runner.BrokenProcessPool)


def test_a_spare_worker_run_and_a_one_worker_run_reach_the_same_decision(tmp_path, monkeypatch):
    from test_inter_service_e2e import _fast_solos
    monkeypatch.setenv("ISAR_WORKERS", "4")
    seen = {}
    for cores in (1, 4):
        root = _fast_solos(tmp_path / f"cores{cores}")           # the same generated batch, in a workspace of its own
        k = _keys(root)
        spec = _anchored(root, [k["a03", 100.0], k["a04", 100.0]])
        log = Bench(root).run(spec, core=None, cores=cores)
        end = log.batches[spec.batch_id].end
        assert end["state"] == "completed" and end["timing"]["pool"]["workers"] == cores
        seen[cores] = {r.unit: (r.status, r.accept.decision, p["came_out"]["assignment"]["slots"],
                                p["came_out"]["assignment"]["count"], p["came_out"]["status"])
                       for r, p in _lines(root, spec.batch_id)}
    assert len(seen[1]) == 2 and seen[1].keys() == seen[4].keys()
    assert seen[1] == seen[4]
