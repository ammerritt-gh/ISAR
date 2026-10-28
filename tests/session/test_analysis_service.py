"""The analysis service (C-service) as a real process: its protocol, worked examples S1-S6, tree death on a
stop, the write hold, shutdown and stdin EOF, the queue driving it (C-schedule), and one real fast item.

All but the last test run the service with the fake fit and save of ``_service_fakes`` (stdlib only, so an
item starts in a fraction of a second). Every wait is bounded; every process a test starts is killed in its
teardown if it is still there. Nothing reads ``real_data/``.
"""
from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import isar
from isar.session.analysis_queue import AnalysisQueue
from isar.session.atomic import WorkspaceBusy, workspace_write_lock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FAKE = ["--fit-target", "_service_fakes:fit_file", "--save-target", "_service_fakes:save_results"]
PROGRESS_KEYS = ("pass", "phase", "step", "of", "stage", "stages")


def test_import_root_is_the_worktree():
    assert Path(isar.__file__).resolve().parents[1] == REPO


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(0x1000, False, pid)             # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return not k32.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259   # STILL_ACTIVE
    finally:
        k32.CloseHandle(handle)


def _all_dead(pids, timeout_s: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while any(_alive(p) for p in pids):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _kill(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _wait_file(path: Path, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not path.exists():
        assert time.monotonic() < deadline, f"{path.name} never appeared"
        time.sleep(0.05)


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class Svc:
    """One service process; a thread collects its stdout events as they arrive."""

    def __init__(self, root: Path, *, fake: bool = True) -> None:
        self.root = root
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, [str(HERE), os.environ.get("PYTHONPATH")]))}
        argv = [sys.executable, "-m", "isar.session.analysis_service", "--workspace", str(root),
                "--log", str(root / "service.log"), *(FAKE if fake else [])]
        self.proc = subprocess.Popen(argv, cwd=REPO, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)
        self.events: list[dict] = []
        self.raw: list[str] = []
        self.at: list[float] = []                         # monotonic receipt time of each event
        self._cv = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for raw in self.proc.stdout:
            line = raw.decode("utf-8")
            try:
                ev = json.loads(line)
            except ValueError:
                ev = {"ev": "<not json>", "line": line}
            with self._cv:
                self.raw.append(line)
                self.events.append(ev)
                self.at.append(time.monotonic())
                self._cv.notify_all()

    def send(self, **cmd) -> None:
        self.send_line(json.dumps(cmd))

    def send_line(self, line: str) -> None:
        self.proc.stdin.write((line + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def start(self, item_id: str, unit_key: str, pass_kind: str = "fast", cores: int = 1) -> None:
        self.send(op="start", item_id=item_id, unit_key=unit_key, cores=cores, **{"pass": pass_kind})

    def wait(self, ev: str, item_id: str | None = None, timeout_s: float = 30.0, **match) -> dict:
        """The first event (from the start) named ``ev`` matching ``item_id`` and ``match``."""
        def find():
            return next((e for e in self.events if e.get("ev") == ev
                         and (item_id is None or e.get("item_id") == item_id)
                         and all(e.get(k) == v for k, v in match.items())), None)
        with self._cv:
            assert self._cv.wait_for(lambda: find() is not None, timeout=timeout_s), \
                f"no {ev} {item_id or ''} {match} within {timeout_s} s; events: {self.events}"
            return find()

    def index(self, ev: str, item_id: str | None = None, **match) -> int:
        e = self.wait(ev, item_id, **match)
        return next(i for i, x in enumerate(self.events) if x is e)

    def of(self, item_id: str) -> list[dict]:
        with self._cv:
            return [e for e in self.events if e.get("item_id") == item_id]

    def shutdown(self, timeout_s: float = 30.0) -> int:
        self.send(op="shutdown")
        return self.proc.wait(timeout_s)


@pytest.fixture
def workspace(tmp_path):
    """``make(plan) -> root``: a folder the service accepts, with the fakes' plan."""
    def make(plan: dict) -> Path:
        (tmp_path / "card.policy.toml").write_text("", encoding="utf-8")
        (tmp_path / "fake_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        return tmp_path
    return make


@pytest.fixture
def service():
    """``launch(root, fake=True) -> Svc``; teardown kills any service still running and every pid it saw."""
    launched: list[Svc] = []

    def launch(root: Path, *, fake: bool = True) -> Svc:
        svc = Svc(root, fake=fake)
        launched.append(svc)
        return svc

    yield launch
    for svc in launched:
        pids = [e["pid"] for e in svc.events if e.get("ev") == "started"]
        for f in svc.root.glob("pids-*.json"):
            info = json.loads(f.read_text(encoding="utf-8"))
            pids += [info["root"], *info["pool"]]
        if svc.proc.poll() is None:
            _kill(svc.proc.pid)
            svc.proc.wait(10)
        for pid in pids:
            if _alive(pid):
                _kill(pid)


# ---------------------------------------------------------------------------
# the protocol
# ---------------------------------------------------------------------------
def test_ready_first_ping_errors_and_shutdown(workspace, service):
    svc = service(workspace({}))
    ready = svc.wait("ready")
    assert svc.events[0] is ready and ready["pid"] == svc.proc.pid
    assert Path(ready["workspace"]) == svc.root.resolve()
    svc.send_line("this is not json")
    svc.send_line("[1, 2]")
    svc.send(op="dance")
    svc.send(op="start", unit_key="E:1:a01", cores=1, **{"pass": "fast"})        # no item_id
    svc.send(op="ping")
    svc.wait("pong")
    assert [e["ev"] for e in svc.events[1:5]] == ["error"] * 4
    assert svc.shutdown() == 0
    assert svc.events[-1] == {"ev": "bye"}


def test_stdin_eof_exits_cleanly(workspace, service):
    svc = service(workspace({}))
    svc.wait("ready")
    svc.proc.stdin.close()
    assert svc.proc.wait(30) == 0
    svc.wait("bye")
    assert svc.events[-1] == {"ev": "bye"}


def test_a_folder_without_a_card_is_refused(tmp_path, service):
    svc = service(tmp_path)
    assert svc.proc.wait(30) == 2
    svc.wait("log")
    time.sleep(0.2)                                  # let the reader take any further line
    assert [e["ev"] for e in svc.events] == ["log"] and svc.events[0]["level"] == "error"


# ---------------------------------------------------------------------------
# worked examples S1-S6
# ---------------------------------------------------------------------------
def test_S1_a_fast_item_relays_progress_then_its_result_and_is_saved(workspace, service):
    svc = service(workspace({"E:12:a06": {"steps": 4, "step_s": 0.05}}))
    svc.start("q1", "E:12:a06")
    svc.wait("saved", "q1")
    svc.wait("finished", "q1")
    names = [e["ev"] for e in svc.of("q1")]
    assert names[:6] == ["started", "progress", "progress", "progress", "progress", "result"]
    assert set(names[6:]) == {"finished", "saved"}
    progress = [e for e in svc.of("q1") if e["ev"] == "progress"]
    assert [{k: e[k] for k in PROGRESS_KEYS} for e in progress] == [
        {"pass": "fast", "phase": "ladder", "step": i, "of": 4, "stage": None, "stages": None} for i in (1, 2, 3, 4)]
    ts = [e["t"] for e in progress]
    assert ts == sorted(ts) and ts[0] >= 0
    assert svc.wait("result", "q1") == {"ev": "result", "item_id": "q1", "unit_key": "E:12:a06", "pass": "fast",
                                        "status": "done", "reason": None, "saving": True}
    assert svc.wait("finished", "q1") == {"ev": "finished", "item_id": "q1", "end": "completed", "reason": None}
    assert svc.wait("saved", "q1") == {"ev": "saved", "item_id": "q1", "unit_key": "E:12:a06", "pass": "fast",
                                       "status": "done", "reason": None}
    assert [p["item"] for p in _jsonl(svc.root / "saved.jsonl")] == ["q1"]
    assert svc.shutdown() == 0


def test_S2_a_deep_item_saves_its_fast_stage_first_and_relays_its_cores(workspace, service):
    reason = "comparison incomplete, 1 model unresolved after restarts"
    svc = service(workspace({"E:12:a06": {"fast_stage": True, "cores_notice": 3, "steps": 2,
                                          "status": "done_warning", "reason": reason}}))
    svc.start("q2", "E:12:a06", "deep", cores=4)
    svc.wait("saved", "q2", **{"pass": "deep"})
    svc.wait("finished", "q2")
    seq = [(e["ev"], e.get("pass")) for e in svc.of("q2") if e["ev"] not in ("saved",)]
    assert seq == [("started", None), ("progress", "fast"), ("result", "fast"), ("cores", None),
                   ("progress", "deep"), ("progress", "deep"), ("result", "deep"), ("finished", None)]
    assert svc.wait("cores", "q2") == {"ev": "cores", "item_id": "q2", "cores": 3}
    assert svc.wait("result", "q2", **{"pass": "deep"})["reason"] == reason
    saved = [e["pass"] for e in svc.of("q2") if e["ev"] == "saved"]
    assert saved == ["fast", "deep"]                                   # arrival order (rule 4)
    assert [p["pass"] for p in _jsonl(svc.root / "saved.jsonl")] == ["fast", "deep"]   # the notice never saved
    assert svc.wait("saved", "q2", **{"pass": "deep"})["status"] == "done_warning"
    assert svc.shutdown() == 0


def test_stopping_a_pooled_deep_item_kills_its_whole_tree_and_leaves_the_others_running(workspace, service):
    svc = service(workspace({"E:1:f1": {"steps": 10, "step_s": 0.15}, "E:1:f2": {"steps": 10, "step_s": 0.15},
                             "E:1:d": {"pool": True, "hang": 120}}))
    svc.start("d", "E:1:d", "deep", cores=3)
    svc.start("f1", "E:1:f1")
    svc.start("f2", "E:1:f2")
    _wait_file(svc.root / "pids-d.json")
    info = json.loads((svc.root / "pids-d.json").read_text(encoding="utf-8"))
    assert len(info["pool"]) == 3 and info["root"] == svc.wait("started", "d")["pid"]
    assert all(_alive(p) for p in [info["root"], *info["pool"]])
    svc.send(op="stop", item_id="d")
    assert svc.wait("finished", "d") == {"ev": "finished", "item_id": "d", "end": "stopped", "reason": None}
    assert _all_dead([info["root"], *info["pool"]], timeout_s=2.0)     # gone by the time finished is read
    for iid in ("f1", "f2"):
        assert svc.wait("finished", iid)["end"] == "completed"
        svc.wait("saved", iid)
    assert svc.shutdown() == 0


def test_S3_a_stopped_deep_item_keeps_its_handed_over_fast_result(workspace, service):
    svc = service(workspace({"E:12:a06": {"fast_stage": True, "hang": 120}}))
    svc.start("q2", "E:12:a06", "deep", cores=2)
    svc.wait("result", "q2", **{"pass": "fast"})
    svc.send(op="stop", item_id="q2")
    assert svc.wait("finished", "q2")["end"] == "stopped"
    assert svc.wait("saved", "q2")["pass"] == "fast"
    assert not [e for e in svc.of("q2") if e["ev"] == "result" and e["pass"] == "deep"]
    svc.send(op="stop", item_id="q2")                                  # not running any more
    svc.wait("log", text="stop: no running item 'q2'")
    assert svc.shutdown() == 0
    assert [e["ev"] for e in svc.of("q2")].count("finished") == 1


def test_S4_a_write_hold_waits_for_the_save_in_progress_and_holds_the_lock(workspace, service):
    svc = service(workspace({"E:1:q3": {"save_sleep": 1.0}, "E:1:q4": {}}))
    svc.wait("ready")
    svc.send(op="release_writes")                                      # not held: answered at once
    svc.wait("writes_released")
    svc.start("q3", "E:1:q3")
    svc.wait("result", "q3")
    time.sleep(0.3)                                                    # the save is under way
    svc.send(op="hold_writes")
    held = svc.index("writes_held")
    assert svc.index("saved", "q3") < held
    with pytest.raises(WorkspaceBusy):
        with workspace_write_lock(svc.root, timeout_s=0.2):
            pass
    svc.start("q4", "E:1:q4")
    assert svc.wait("result", "q4")["saving"] is True
    svc.wait("finished", "q4")
    svc.send(op="hold_writes")                                         # again while held
    with svc._cv:
        assert svc._cv.wait_for(lambda: [e["ev"] for e in svc.events].count("writes_held") == 2, timeout=10)
    time.sleep(0.5)
    assert not [e for e in svc.of("q4") if e["ev"] == "saved"]
    assert [p["item"] for p in _jsonl(svc.root / "saved.jsonl")] == ["q3"]
    svc.send(op="release_writes")
    released = len(svc.events)
    assert svc.index("saved", "q4") > released - 1
    assert [e["ev"] for e in svc.events[held:]].index("writes_released") < \
        [e["ev"] for e in svc.events[held:]].index("saved")
    assert [p["item"] for p in _jsonl(svc.root / "saved.jsonl")] == ["q3", "q4"]
    with workspace_write_lock(svc.root, timeout_s=5):                  # released
        pass
    assert svc.shutdown() == 0


def test_a_release_before_the_hold_was_taken_is_answered_and_no_hold_follows(workspace, service):
    svc = service(workspace({"E:1:q5": {"save_sleep": 1.0}}))
    svc.wait("ready")
    svc.start("q5", "E:1:q5")
    svc.wait("result", "q5")
    time.sleep(0.3)                                                    # the save is under way
    svc.send(op="hold_writes")
    svc.send(op="release_writes")                                      # before the writer took the hold
    svc.wait("writes_released", timeout_s=10)
    svc.wait("saved", "q5")
    time.sleep(0.5)
    assert "writes_held" not in [e["ev"] for e in svc.events]
    assert [e["ev"] for e in svc.events].count("writes_released") == 1
    with workspace_write_lock(svc.root, timeout_s=5):                  # no stale hold
        pass
    assert svc.shutdown() == 0


def test_a_failed_save_is_reported_and_the_next_save_succeeds(workspace, service):
    svc = service(workspace({"E:1:a": {"save_fail": "the disk said no"}, "E:1:b": {}}))
    svc.start("a", "E:1:a")
    assert svc.wait("save_failed", "a") == {"ev": "save_failed", "item_id": "a", "unit_key": "E:1:a",
                                            "pass": "fast", "error": "the disk said no"}
    svc.start("b", "E:1:b")
    svc.wait("saved", "b")
    assert [p["item"] for p in _jsonl(svc.root / "saved.jsonl")] == ["b"]
    assert svc.shutdown() == 0


def test_a_crashed_or_raising_fit_finishes_failed_with_the_reason(workspace, service):
    svc = service(workspace({"E:1:a": {"exit_code": 3}, "E:1:b": {"raise": "boom"},
                             "E:1:c": {"status": "already_current", "reason": "already has a current fast pass"}}))
    svc.start("a", "E:1:a")
    svc.start("b", "E:1:b")
    svc.start("c", "E:1:c")
    assert svc.wait("finished", "a") == {"ev": "finished", "item_id": "a", "end": "failed",
                                         "reason": "the fitting process ended unexpectedly (exit code 3)"}
    assert svc.wait("finished", "b")["reason"] == "RuntimeError: boom"
    assert svc.wait("result", "c")["saving"] is False
    assert svc.wait("finished", "c")["end"] == "completed"
    assert svc.shutdown() == 0
    assert not [e for e in svc.events if e["ev"] == "saved"]


def test_S5_refused_starts(workspace, service):
    svc = service(workspace({"E:12:a06": {"hang": 60}}))
    svc.start("q1", "E:12:a06")
    svc.wait("started", "q1")
    svc.start("q5", "E:12:a06")
    assert svc.wait("finished", "q5") == {"ev": "finished", "item_id": "q5", "end": "failed",
                                          "reason": "E:12:a06 already has a running item (q1)"}
    svc.start("q1", "E:1:other")
    svc.start("q6", "E:1:other", "slow")
    svc.start("q7", "E:1:other", cores=0)
    svc.start("q8", "nonsense")
    for iid in ("q6", "q7", "q8"):
        assert svc.wait("finished", iid)["end"] == "failed"
    assert "already used" in [e for e in svc.of("q1") if e["ev"] == "finished"][0]["reason"]
    assert not [e for e in svc.events if e["ev"] == "started" and e["item_id"] != "q1"]
    assert svc.shutdown() == 0
    assert [e["end"] for e in svc.of("q1") if e["ev"] == "finished"] == ["failed", "stopped"]


@pytest.mark.parametrize("pending", ["held", "saving"])
def test_S6_shutdown_stops_running_items_then_saves_what_was_handed_over(workspace, service, pending):
    svc = service(workspace({"E:1:q6": {"hang": 120}, "E:1:q7": {"save_sleep": 1.5 if pending == "saving" else 0}}))
    svc.wait("ready")
    if pending == "held":
        svc.send(op="hold_writes")
        svc.wait("writes_held")
    svc.start("q6", "E:1:q6")
    svc.wait("started", "q6")
    svc.start("q7", "E:1:q7")
    svc.wait("result", "q7")
    svc.wait("finished", "q7")
    assert svc.shutdown() == 0
    names = [(e["ev"], e.get("item_id")) for e in svc.events]
    assert names[-1] == ("bye", None)
    assert names.index(("finished", "q6")) < names.index(("saved", "q7")) < names.index(("bye", None))
    assert svc.wait("finished", "q6")["end"] == "stopped"
    if pending == "held":
        svc.wait("log", text="write hold released by shutdown")
    assert [p["item"] for p in _jsonl(svc.root / "saved.jsonl")] == ["q7"]
    assert _all_dead([svc.wait("started", "q6")["pid"]])


def test_stdin_eof_with_a_pooled_item_running_leaves_no_process(workspace, service):
    svc = service(workspace({"E:1:d": {"pool": True, "hang": 120}}))
    svc.start("d", "E:1:d", "deep", cores=2)
    _wait_file(svc.root / "pids-d.json")
    info = json.loads((svc.root / "pids-d.json").read_text(encoding="utf-8"))
    svc.proc.stdin.close()                                             # the GUI went away
    assert svc.proc.wait(30) == 0
    assert _all_dead([info["root"], *info["pool"]], timeout_s=2.0)
    assert svc.wait("finished", "d")["end"] == "stopped"


def test_stray_output_of_a_fit_never_reaches_the_protocol(workspace, service):
    svc = service(workspace({"E:1:a": {"print": True}}))
    svc.start("a", "E:1:a")
    svc.wait("saved", "a")
    assert svc.shutdown() == 0
    assert all(isinstance(json.loads(line), dict) for line in svc.raw)
    text = (svc.root / "service.log").read_text(encoding="utf-8", errors="replace")
    assert "a stray print from a fit" in text and "a stray fd-1 write from a fit" in text


def test_an_attempt_record_is_saved_but_never_shown(workspace, service):
    svc = service(workspace({"E:1:a": {"attempt": True, "hang": 120}}))
    svc.start("a", "E:1:a")
    _wait_file(svc.root / "measurements.jsonl")
    svc.send(op="stop", item_id="a")
    assert svc.wait("finished", "a")["end"] == "stopped"
    assert svc.shutdown() == 0
    assert [e["ev"] for e in svc.of("a")] == ["started", "finished"]
    assert _jsonl(svc.root / "measurements.jsonl") == [
        {"unit": "E:1:a", "pass": "attempt", "measurement_record": {"scan_ref": "E:1:a"}}]


# ---------------------------------------------------------------------------
# the queue driving the service (C-schedule + C-service), as the GUI controller does
# ---------------------------------------------------------------------------
class _Controller:
    """Hands each event to the queue, then sends what ``next_starts`` returns."""

    def __init__(self, svc: Svc, queue: AnalysisQueue) -> None:
        self.svc, self.q, self.seen = svc, queue, 0
        self.starts: list[tuple[str, int, int]] = []     # (item_id, cores, events received before it)

    def pump(self) -> None:
        for s in self.q.next_starts():
            self.starts.append((s.item_id, s.cores, self.seen))
            self.svc.start(s.item_id, s.unit_key, s.pass_kind, s.cores)

    def _on(self, e: dict) -> None:
        ev, iid, at = e["ev"], e.get("item_id"), time.monotonic()
        if ev == "started":
            self.q.on_started(iid, at=at)
        elif ev == "progress":
            self.q.on_progress(iid, {k: e[k] for k in PROGRESS_KEYS}, at=at)
        elif ev == "cores":
            self.q.on_cores(iid, e["cores"])
        elif ev == "result":
            self.q.on_result(iid, e["pass"], e["status"], e["reason"], saving=e["saving"])
        elif ev == "saved":
            self.q.on_saved(iid, e["pass"])
        elif ev == "save_failed":
            self.q.on_save_failed(iid, e["pass"], e["error"])
        elif ev == "finished":
            self.q.on_finished(iid, e["end"], e["reason"])

    def run_until(self, done, timeout_s: float = 60.0) -> None:
        deadline = time.monotonic() + timeout_s
        self.pump()
        while not done():
            with self.svc._cv:
                assert self.svc._cv.wait_for(lambda: len(self.svc.events) > self.seen,
                                             timeout=max(0.0, deadline - time.monotonic())), \
                    f"stalled; events: {self.svc.events}; queue: {self.q.snapshot()}"
                new = self.svc.events[self.seen:]
            for e in new:
                self.seen += 1
                self._on(e)
                self.pump()

    def settled(self, *ids: str) -> bool:
        done = {v.item_id: v for v in self.q.snapshot().finished}
        return all(i in done for i in ids)

    def state(self, iid: str) -> str:
        return next(v.state for v in self.q.snapshot().finished if v.item_id == iid)


def test_the_queue_and_the_service_run_a_file_s_passes_in_order_and_share_the_cores(workspace, service):
    """Acceptance demonstration 1, with amendment 1: B's deep item starts first (the fast item has ended), gives
    back the core it cannot use, and A's deep item starts on it once A's fast result is saved, while B runs."""
    svc = service(workspace({"S:1:A|fast": {"steps": 3, "step_s": 0.1, "save_sleep": 0.5},
                             "S:1:A|deep": {"fast_stage": True, "steps": 2, "step_s": 0.05},
                             "S:1:B|deep": {"cores_notice": 1, "steps": 40, "step_s": 0.1}}))
    svc.wait("ready")
    q = AnalysisQueue(2)
    ctl = _Controller(svc, q)
    (fast_a,) = q.add(["S:1:A"], "fast", automatic=True, current={}).added
    ctl.pump()
    deep_a, deep_b = q.add(["S:1:A", "S:1:B"], "deep", automatic=False, current={}).added
    ctl.run_until(lambda: ctl.settled(fast_a.item_id, deep_a.item_id, deep_b.item_id))
    order = [(iid, cores) for iid, cores, _ in ctl.starts]
    assert order == [(fast_a.item_id, 1), (deep_b.item_id, 2), (deep_a.item_id, 1)]
    a_start = next(n for iid, _c, n in ctl.starts if iid == deep_a.item_id)
    assert svc.index("saved", fast_a.item_id) < a_start                 # A's deep pass reads A's saved fast pass
    assert a_start <= svc.index("finished", deep_b.item_id)             # on B's returned core, while B ran
    assert svc.index("cores", deep_b.item_id) < a_start
    fast_payloads = [p for p in _jsonl(svc.root / "saved.jsonl") if p["unit"] == "S:1:A" and p["pass"] == "fast"]
    assert len(fast_payloads) == 1                                      # the fast stage ran once for A
    assert [ctl.state(i) for i in (fast_a.item_id, deep_a.item_id, deep_b.item_id)] == ["done"] * 3
    assert svc.shutdown() == 0


def test_a_stop_frees_the_cores_for_the_next_item(workspace, service):
    """Acceptance demonstration 2's core release."""
    svc = service(workspace({"S:1:X": {"hang": 120}, "S:1:Y": {}}))
    svc.wait("ready")
    q = AnalysisQueue(1)
    ctl = _Controller(svc, q)
    (x,) = q.add(["S:1:X"], "deep", automatic=False, current={}).added
    ctl.run_until(lambda: bool(q.snapshot().deep) and q.snapshot().deep[0].started_at is not None)
    (y,) = q.add(["S:1:Y"], "fast", automatic=True, current={}).added
    ctl.pump()
    assert [s for s, _c, _n in ctl.starts] == [x.item_id]               # Y waits for the core
    svc.send(op="stop", item_id=x.item_id)
    ctl.run_until(lambda: ctl.settled(x.item_id, y.item_id))
    assert [s for s, _c, _n in ctl.starts] == [x.item_id, y.item_id]
    assert ctl.starts[1][2] > svc.index("finished", x.item_id)
    assert (ctl.state(x.item_id), ctl.state(y.item_id)) == ("stopped", "done")
    assert svc.shutdown() == 0


# ---------------------------------------------------------------------------
# one real fast item (real fit_file, real save_results)
# ---------------------------------------------------------------------------
@pytest.mark.slow
def test_a_real_fast_item_on_a_generated_bl43_workspace_is_saved_and_reads_current(tmp_path, service):
    import intake_fixtures as fx
    from isar.intake import fit_inputs
    from isar.session import ingest
    from isar.workspace_api.currentness import current_passes

    ws, data = fx.bl43_workspace(tmp_path, stems=("A",))
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)
    unit = next(k for k in session.scans if k.endswith(":a04"))
    svc = service(ws.root, fake=False)
    svc.wait("ready", timeout_s=60)
    sent = time.monotonic()
    svc.start("q1", unit)
    saved = svc.wait("saved", "q1", timeout_s=300)
    svc.wait("finished", "q1", timeout_s=60)
    first = next(i for i, e in enumerate(svc.events) if e.get("ev") == "progress" and e.get("item_id") == "q1")
    done = svc.index("finished", "q1")
    print(f"\nU1 spawn cost: start -> first progress {svc.at[first] - sent:.2f} s "
          f"(service-side t {svc.events[first]['t']:.2f} s after started)")
    print(f"U2 fast item: start -> finished {svc.at[done] - sent:.2f} s")
    assert saved["status"] in ("done", "done_warning")
    assert svc.shutdown() == 0
    assert "fast" in current_passes(ws.root, [unit])[unit]
    stamp = ws.load()[1].get(unit).provenance["fit_inputs"]
    lines = fit_inputs.read_log(ws.root)
    for kind in ("started", "finished"):
        assert [ln["run"] for ln in lines if ln.get("kind") == kind and ln.get("unit") == unit] == [stamp["run"]]
