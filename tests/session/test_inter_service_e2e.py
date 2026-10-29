"""The analysis service as a real process running a real anchored batch: the real runner (S1U6) and the real
numerical core (S1U2) on two analyzers at two temperatures of tiny spectra generated here. The protocol's promises
are checked on the events: ``started``, progress, the ring saved, the ring's refits authoritative in the log, and
one end line. Not slow; the one pool is two workers, stated. Nothing reads ``real_data/``.
"""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import isar

import intake_fixtures as fx
from isar.session import ingest, writer
from isar.session.file_fit import FileItem, fit_file
from isar.session.inter_spectra import records, runner
from isar.session.inter_spectra.graph import anchored_scope
from isar.session.inter_spectra.nodes import read_nodes

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
POOL_WORKERS = 2                                    # the batch's cores: the item's pool is this many workers


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


class _Service:
    """One real service process; a thread collects its protocol lines as they arrive."""

    def __init__(self, root: Path) -> None:
        self.proc = subprocess.Popen([sys.executable, "-m", "isar.session.analysis_service", "--workspace",
                                      str(root), "--log", str(root / "service.log")], cwd=REPO,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.events: list[dict] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            try:
                ev = json.loads(raw)
            except ValueError:
                ev = {"ev": "<not json>", "line": raw.decode("utf-8", "replace")}
            with self._cv:
                self.events.append(ev)
                self._cv.notify_all()

    def send(self, cmd: dict) -> None:
        self.proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def wait(self, pred, timeout_s: float = 300.0) -> dict:
        with self._cv:
            assert self._cv.wait_for(lambda: any(pred(e) for e in self.events), timeout=timeout_s), \
                f"the event never came; events: {self.events}"
            return next(e for e in self.events if pred(e))

    def of(self, item_id: str) -> list[dict]:
        with self._cv:
            return [e for e in self.events if e.get("item_id") == item_id]


def _fast_solos(tmp_path: Path) -> Path:
    """Two analyzers at 100 K and 200 K of a generated scan set, each with a saved fast solo fit."""
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
        out: list = []
        fit_file(ws.root, FileItem("q", key, "fast", 1), progress=None, emit=out.append)
        assert writer.save_results(ws.root, [r.payload for r in out if r.payload is not None]) == [None, None]
    return ws.root


def test_an_anchored_batch_runs_end_to_end_in_the_real_service(tmp_path):
    root = _fast_solos(tmp_path)
    nodes = read_nodes(root).nodes
    keys = {(n.detector, n.T): k for k, n in nodes.items()}
    anchors = (keys["a03", 100.0], keys["a04", 100.0])
    scope, _ = anchored_scope(anchors, "T", read_nodes(root))
    spec = runner.BatchSpec(records.new_batch_id(), "anchored", "T", anchors, tuple(sorted(scope)))
    ring_one = sorted(set(spec.members) - set(anchors))            # the T-neighbours of the anchors: 200 K
    assert ring_one == sorted([keys["a03", 200.0], keys["a04", 200.0]])

    svc = _Service(root)
    try:
        svc.wait(lambda e: e.get("ev") == "ready")
        svc.send({"op": "start", "item_id": "b1", "unit_key": f"inter:{spec.batch_id}:batch", "pass": "inter",
                  "cores": POOL_WORKERS, "batch": spec.to_json()})
        done = svc.wait(lambda e: e.get("ev") == "finished" and e.get("item_id") == "b1")
        assert done["end"] == "completed", done
        svc.send({"op": "shutdown"})
        assert svc.proc.wait(60) == 0
        svc.wait(lambda e: e.get("ev") == "bye")
    finally:
        if svc.proc.poll() is None:
            svc.proc.kill()

    events = svc.of("b1")
    names = [e["ev"] for e in events]
    assert names.index("started") < names.index("progress") < names.index("saved") < names.index("finished")
    assert [e for e in events if e["ev"] == "progress"][0]["kind"] == "anchored"
    saved_keys = sorted(k for e in events if e["ev"] == "saved" for k in e.get("keys", []))
    assert saved_keys == ring_one                                   # the ring is saved; the anchors never refitted

    log = records.read_log(root)
    assert log.batches[spec.batch_id].end["state"] == "completed"
    lines = [r for r in log.refits if r.batch == spec.batch_id]
    assert sorted(r.unit for r in lines) == ring_one
    assert {r.accept.decision for r in lines} == {"authoritative"}  # acked and taken as the authoritative fits
    ends = [ln for ln in (root / records.LOG).read_text(encoding="utf-8").splitlines()
            if json.loads(ln).get("kind") == "batch_end"]
    assert len(ends) == 1                                           # the runner's own end, and the only one
