"""The link queue item through the analysis service: a link start's protocol, its refusals and the link lock
(C-link-queue example 5), and one run of the real targets on a generated workspace, driven through the real
AnalysisQueue. The protocol tests use the scripted fakes of ``_link_service_fakes``. Every wait is bounded, and
every service a test starts is killed in its teardown. Nothing reads ``real_data/``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import isar
from inter_fixtures import build_inter_workspace, scan_keys, spectrum_keys
from isar.analyze.prior import PRIOR_RECIPE_VERSION
from isar.session.analysis_queue import AnalysisQueue
from isar.session.inter_spectra import records
from isar.session.workspace import Workspace

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FAKE = ["--link-target", "_link_service_fakes:run_link", "--link-save-target",
        "_link_service_fakes:save_link_payloads"]


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


class _Svc:
    """One service process; a thread collects its protocol lines as they arrive."""

    def __init__(self, root: Path, *, fake: bool) -> None:
        self.root = root
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.environ.get("PYTHONPATH")]))}
        argv = [sys.executable, "-m", "isar.session.analysis_service", "--workspace", str(root),
                "--log", str(root / "service.log"), *(FAKE if fake else [])]
        self.proc = subprocess.Popen(argv, cwd=REPO, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.events: list[dict] = []
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            with self._cv:
                self.events.append(json.loads(raw))
                self._cv.notify_all()

    def send(self, **cmd) -> None:
        self.proc.stdin.write((json.dumps(cmd) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def start(self, item_id: str, link: dict) -> None:
        self.send(op="start", item_id=item_id, unit_key=f"link:{link['link_id']}:link", cores=1,
                  **{"pass": "link"}, link=link)

    def wait(self, ev: str, item_id: str | None = None, timeout_s: float = 120.0) -> dict:
        def find():
            return next((e for e in self.events if e.get("ev") == ev
                         and (item_id is None or e.get("item_id") == item_id)), None)
        with self._cv:
            assert self._cv.wait_for(lambda: find() is not None, timeout=timeout_s), \
                f"no {ev} {item_id or ''} within {timeout_s} s; events: {self.events}"
            return find()

    def of(self, item_id: str) -> list[dict]:
        with self._cv:
            return [e for e in self.events if e.get("item_id") == item_id]

    def shutdown(self) -> int:
        self.send(op="shutdown")
        return self.proc.wait(60)


@pytest.fixture
def launch():
    """``launch(root, fake=True) -> _Svc``, ready; teardown kills every service still running."""
    launched: list[_Svc] = []

    def make(root: Path, *, fake: bool = True) -> _Svc:
        svc = _Svc(root, fake=fake)
        launched.append(svc)
        svc.wait("ready")
        return svc

    yield make
    for svc in launched:
        if svc.proc.poll() is None:
            svc.proc.kill()
            svc.proc.wait(10)


@pytest.fixture
def root(tmp_path):
    """A folder the service accepts: the fakes write beside it."""
    (tmp_path / "card.policy.toml").write_text("", encoding="utf-8")
    return tmp_path


def _link(link_id: str = "l1", **fake) -> dict:
    """A link object; the fake's script (``hang``, ``save_fail``, ...) rides along under ``fake``."""
    return {"link_id": link_id, "scans": ["E:1"], "series": False, "reason": "user", "fake": fake}


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def test_a_link_start_runs_its_result_is_saved_and_the_item_finishes(root, launch):
    svc = launch(root)
    svc.start("l1", _link("l1"))
    assert svc.wait("finished", "l1")["end"] == "completed"
    assert svc.wait("saved", "l1")["link"] == "l1"                  # a save may answer after the end
    names = [e["ev"] for e in svc.of("l1")]
    assert names[:3] == ["started", "progress", "result"]
    result = next(e for e in svc.of("l1") if e["ev"] == "result")
    assert (result["pass"], result["status"], result["saving"], result["reason"]) == \
        ("link", "done", True, "one summary line")
    saved = next(e for e in svc.of("l1") if e["ev"] == "saved")
    assert (saved["pass"], saved["status"], saved["link"], saved["new"]) == ("link", "done", "l1", True)
    assert [p["link_id"] for p in _jsonl(root / "link_saved.jsonl")] == ["l1"]
    assert svc.shutdown() == 0


REFUSED = [
    ("link:l1:link", None, "a link object with a link_id"),
    ("link:l1:link", {"link_id": "", "scans": ["E:1"], "series": False}, "a link object with a link_id"),
    ("link:other:link", {"link_id": "l1", "scans": ["E:1"], "series": False}, "is not link:l1:link"),
    ("link:l1:link", {"link_id": "l1", "scans": [], "series": False}, "link.scans"),
    ("link:l1:link", {"link_id": "l1", "scans": ["E:1"], "series": "yes"}, "link.series"),
]


@pytest.mark.parametrize("unit,link,why", REFUSED)
def test_a_bad_link_start_finishes_failed_with_the_reason_and_never_starts(root, launch, unit, link, why):
    svc = launch(root)
    svc.send(op="start", item_id="q1", unit_key=unit, cores=1, **{"pass": "link"}, link=link)
    end = svc.wait("finished", "q1")
    assert end["end"] == "failed" and why in end["reason"], end
    assert [e["ev"] for e in svc.of("q1")] == ["finished"]
    assert svc.shutdown() == 0


def test_a_second_link_start_is_refused_while_the_first_holds_the_link_lock(root, launch):
    svc = launch(root)
    svc.start("l1", _link("l1", hang=60))
    svc.wait("started", "l1")
    svc.start("l2", _link("l2"))
    assert svc.wait("finished", "l2")["reason"] == "link:all is held by a running item (l1)"
    svc.send(op="stop", item_id="l1")
    assert svc.wait("finished", "l1")["end"] == "stopped"
    assert svc.shutdown() == 0


def test_a_refused_link_save_is_reported_and_nothing_is_written(root, launch):
    svc = launch(root)
    svc.start("l1", _link("l1", save_fail="the fake disk is full"))
    failed = svc.wait("save_failed", "l1")
    assert (failed["pass"], failed["error"]) == ("link", "the fake disk is full")
    assert svc.wait("finished", "l1")["end"] == "completed"
    assert _jsonl(root / "link_saved.jsonl") == []
    assert svc.shutdown() == 0


def _linked_workspace(tmp_path: Path) -> Path:
    """Two temperatures of one q setting, three analyzers each, every analyzer with a plain stored fast fit (the
    fixture of ``test_linking_session``, generated here)."""
    root = build_inter_workspace(tmp_path, temperatures=(100, 200), setups=("0p5_0p5_3",), n_analyzers=3)
    for unit in spectrum_keys(root):
        _plain_fast(root, unit)
    return root


def _plain_fast(root: Path, unit: str) -> None:
    """Rewrite the unit's stored fast record with two modes, the resolution and the recipe a link reads."""
    ws = Workspace.open(root)
    card, session = ws.load()
    entry = session.scans[unit]
    det = unit.rsplit(":", 1)[1]
    peaks = [{"index": i, "omega0": o, "Gamma": 0.2, "height": 5.0, "area_stokes": 10.0, "sigma_area": 1.0,
              "sigma_omega0": 0.05, "sigma_Gamma": 0.02, "regime": "underdamped",
              "mode_status": "determined_by_own_data", "at_width_cap": False, "confident": True}
             for i, o in enumerate((4.0, 8.0))]
    record = {**entry.detectors[det]["fast_pass"], "peaks": peaks, "flag_statistics": {"fwhm": 0.4},
              "prior_provenance": {"version": PRIOR_RECIPE_VERSION, "energy_support_meV": [0.5, 12.0]}}
    entry.detectors = {det: {**entry.detectors[det], "fast_pass": record}}
    ws.save(card, session)


def _feed(q: AnalysisQueue, e: dict) -> None:
    """The queue's answer to one protocol event, as the GUI controller gives it."""
    ev, iid = e["ev"], e.get("item_id")
    if ev == "started":
        q.on_started(iid, at=time.monotonic())
    elif ev == "result":
        q.on_result(iid, e["pass"], e["status"], e["reason"], saving=e["saving"])
    elif ev == "saved":
        q.on_saved(iid, e["pass"])
    elif ev == "save_failed":
        q.on_save_failed(iid, e["pass"], e["error"])
    elif ev == "finished":
        q.on_finished(iid, e["end"], e["reason"])


def test_a_link_item_on_a_generated_workspace_runs_the_real_targets_through_the_queue(tmp_path, launch):
    root = _linked_workspace(tmp_path)
    scans = scan_keys(root)
    svc = launch(root, fake=False)
    q = AnalysisQueue(4)
    q.add_link("l1")
    spec = {"link_id": "l1", "scans": scans, "series": False, "reason": "user"}
    for st in q.next_starts():
        svc.send(op="start", item_id=st.item_id, unit_key=st.unit_key, cores=st.cores, **{"pass": st.pass_kind},
                 link=spec)
    seen = 0
    while not q.snapshot().finished:
        with svc._cv:
            assert svc._cv.wait_for(lambda: len(svc.events) > seen, timeout=120), svc.events
            new = svc.events[seen:]
        for e in new:
            seen += 1
            _feed(q, e)
    (done,) = q.snapshot().finished
    assert (done.state, done.pass_kind) == ("done", "link")
    assert done.reason.startswith(f"{len(scans)} scans, {len(spectrum_keys(root))} spectra: ")
    (line,) = records.read_links(root)
    assert set(records.read_link_payload(root, line).made_from) == set(spectrum_keys(root))
    assert records.read_log(root).damaged == ()
    assert svc.shutdown() == 0


def test_the_link_lock_is_one_literal_in_the_queue_the_service_and_the_linking_module():
    from isar.session import analysis_queue, analysis_service
    from isar.session.inter_spectra import linking
    assert analysis_queue.LINK_LOCK == analysis_service.LINK_LOCK == linking.LINK_LOCK == "link:all"
