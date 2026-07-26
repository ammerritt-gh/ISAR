"""Persistent analysis worker robustness (mighty-starfish §A, §F.4/F.6).

Spawns the REAL ``isar.session.worker`` process on a tiny synthetic S30 workspace and drives its JSONL
protocol directly: protocol round-trip (ping), a real run job (ok:true + timings + run_timings.json +
results.csv), malformed-line survival, a garbage-data ok:false traceback (worker stays alive), and
clean EOF / shutdown exit. Also the A/B state-free-reuse identity (SubprocessRunner vs WorkerRunner)
and the timing contract (reply timings == run_timings.json).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from isar.parsers import spec_s30 as s30
from isar.session.workspace import Workspace
from isar.synth_format import run_builder

REPO = Path(__file__).resolve().parents[2]


def _scaffold_ws(root: Path, tmp_path: Path):
    """An isar-init'd S30 workspace restricted to one detector (ana5) for a fast fit, plus a 1-scan
    synthetic data file. Mirrors tests/session/test_driver_smoke._init_workspace."""
    ws = Workspace.open(root)
    ws.root.mkdir(parents=True, exist_ok=True)
    card_text = Path(s30._CARD_PATH).read_text(encoding="utf-8").replace(
        '# detectors = ["ana7"]', 'detectors = ["ana5"]')
    ws.policy_path.write_text(card_text, encoding="utf-8")
    run = run_builder.build_s30_run("SYN", n_scans=1, seed0=1000)
    data = tmp_path / "SYN.spec_supplementary.dat"
    data.write_bytes(s30.emit_file(run, s30.card_template()))
    return ws, data


def _corrupt_data_row(payload: bytes) -> bytes:
    """``payload`` with one non-numeric token planted strictly inside the file's data-row list
    (equivalently, inside the scan's data block here, since this file has exactly one scan).

    Truncating the file is NOT garbage to this reader: a single short FINAL data row is the
    aborted-mid-write artifact SPEC really produces, and `isar.parsers.spec_base._resolve_block_rows`
    deliberately drops just that row with a warning, so half a file still parses clean. A non-numeric
    token in an otherwise full-width row is the failure that same policy explicitly keeps — `float()`
    raises inside `plugin.parse_file`, and `ingest.parse_all` (unlike `ingest_batch`) does not isolate
    it, so a real exception reaches `run_batch`."""
    lines = payload.decode("utf-8").splitlines(keepends=True)
    data_rows = [i for i, line in enumerate(lines) if line.strip() and not line.startswith("#")]
    assert len(data_rows) >= 3, "emitted run has no multi-row data block to corrupt"
    victim = data_rows[len(data_rows) // 2]
    assert data_rows[0] < victim < data_rows[-1], (
        "corruption must land interior to the file's data-row list, not at its edges "
        "(equivalently, interior to the scan's block here, since this file has one scan)")
    tokens = lines[victim].split()
    assert len(tokens) >= 3
    tokens[len(tokens) // 2] = "NOT_A_NUMBER"   # mid-record: the row keeps its declared width
    lines[victim] = " ".join(tokens) + "\n"
    return "".join(lines).encode("utf-8")


class _RawWorker:
    """Minimal JSONL client speaking the worker protocol directly (not WorkerRunner)."""

    def __init__(self, ws_root: Path):
        (ws_root / "report").mkdir(parents=True, exist_ok=True)
        self._log = open(ws_root / "report" / "worker.stderr.log", "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "isar.session.worker", "--workspace", str(ws_root)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self._log,
            text=True, encoding="utf-8", bufsize=1)

    def send(self, obj):
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def send_raw(self, line):
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def recv(self):
        line = self.proc.stdout.readline()
        assert line, "worker closed stdout without replying"
        return json.loads(line)

    def close_stdin(self):
        self.proc.stdin.close()

    def wait(self, timeout=15):
        return self.proc.wait(timeout=timeout)

    def kill(self):
        try:
            self.proc.kill()
        except Exception:
            pass
        finally:
            try:
                self._log.close()
            except Exception:
                pass


def test_run_job_passes_errors_tier_verbatim_to_run_batch(tmp_path, monkeypatch):
    # Fast unit-level check (no worker subprocess, no real fit): _run_job must forward the requested
    # errors_tier string unchanged into driver.run_batch — including the new "bayes" tier, which the
    # worker itself does not validate (isar.session.driver decides what each tier means).
    from isar.session import worker as worker_mod
    from isar.session import driver as driver_mod

    ws, data = _scaffold_ws(tmp_path / "ws", tmp_path)

    captured = {}

    class _FakeSummary:
        n_reprocessed = 0
        n_fitted_this_run = 0
        counts = {}
        outcomes = []
        gaps = []
        context_lines = []

    def fake_run_batch(ws_, data_paths, *, force, M, workers, errors_tier, formats, timer):
        captured["errors_tier"] = errors_tier
        return _FakeSummary()

    monkeypatch.setattr(driver_mod, "run_batch", fake_run_batch)

    summary, timings = worker_mod._run_job(
        ws.root, {"id": 1, "data_paths": [str(data)], "M": 10, "errors_tier": "bayes"})

    assert captured["errors_tier"] == "bayes"


def test_run_job_forwards_optional_role_tiers(tmp_path, monkeypatch):
    from isar.session import driver as driver_mod
    from isar.session import worker as worker_mod

    ws, data = _scaffold_ws(tmp_path / "ws-role", tmp_path)
    captured = {}

    class _FakeSummary:
        n_reprocessed = n_fitted_this_run = 0
        counts = {}
        outcomes = gaps = context_lines = []

    def fake_run_batch(ws_, data_paths, **kwargs):
        captured.update(kwargs)
        return _FakeSummary()

    monkeypatch.setattr(driver_mod, "run_batch", fake_run_batch)
    worker_mod._run_job(ws.root, {
        "id": 2, "data_paths": [str(data)], "errors_tier": "full",
        "scan_errors_tier": "fast", "master_errors_tier": "bayes"})
    assert captured["errors_tier"] == "full"
    assert captured["scan_errors_tier"] == "fast"
    assert captured["master_errors_tier"] == "bayes"


# --------------------------------------------------------------------------- fast protocol tests
def test_ping_round_trip(tmp_path):
    ws, _ = _scaffold_ws(tmp_path / "ws", tmp_path)
    w = _RawWorker(ws.root)
    try:
        w.send({"id": 1, "cmd": "ping"})
        reply = w.recv()
        assert reply == {"id": 1, "ok": True}
    finally:
        w.send({"id": None, "cmd": "shutdown"})
        assert w.wait() == 0
        w.kill()


def test_bad_json_line_survives(tmp_path):
    ws, _ = _scaffold_ws(tmp_path / "ws", tmp_path)
    w = _RawWorker(ws.root)
    try:
        w.send_raw("{ this is not json")
        reply = w.recv()
        assert reply["id"] is None and reply["ok"] is False
        assert reply["error"]["type"]  # carries an exception type
        # Worker still answers after a malformed line.
        w.send({"id": 7, "cmd": "ping"})
        assert w.recv() == {"id": 7, "ok": True}
    finally:
        w.send({"id": None, "cmd": "shutdown"})
        assert w.wait() == 0
        w.kill()


def test_shutdown_exits_zero(tmp_path):
    ws, _ = _scaffold_ws(tmp_path / "ws", tmp_path)
    w = _RawWorker(ws.root)
    w.send({"id": None, "cmd": "shutdown"})
    assert w.wait() == 0
    w.kill()


def test_stdin_eof_exits_zero(tmp_path):
    ws, _ = _scaffold_ws(tmp_path / "ws", tmp_path)
    w = _RawWorker(ws.root)
    w.close_stdin()   # driver crash / pipe close → worker exits cleanly (zombie guard)
    assert w.wait() == 0
    w.kill()


# --------------------------------------------------------------------------- run-job tests (slow)
@pytest.mark.slow
def test_run_job_ok_writes_timings_and_results(tmp_path):
    ws, data = _scaffold_ws(tmp_path / "ws", tmp_path)
    w = _RawWorker(ws.root)
    try:
        w.send({"id": 42, "cmd": "run", "data_paths": [str(data)], "M": 10, "errors_tier": "full"})
        reply = w.recv()
        assert reply["ok"] is True, reply
        assert reply["id"] == 42
        assert reply["error"] is None
        assert reply["summary"]["n_reprocessed"] >= 1

        timings = reply["timings"]
        assert timings["worker_job"] is True and timings["job_id"] == 42
        stage_names = {s["stage"] for s in timings["stages"]}
        assert {"ingest_parse", "fit_scans", "write_results"} <= stage_names

        # run_timings.json written to disk BEFORE the reply, and the inline copy matches it (F.6).
        run_timings = ws.report_dir / "run_timings.json"
        assert run_timings.exists()
        assert json.loads(run_timings.read_text(encoding="utf-8")) == timings
        assert (ws.report_dir / "results.csv").exists()

        # Worker is reusable: a second job on the same warm process works.
        w.send({"id": 43, "cmd": "ping"})
        assert w.recv() == {"id": 43, "ok": True}
    finally:
        w.send({"id": None, "cmd": "shutdown"})
        assert w.wait() == 0
        w.kill()


@pytest.mark.slow
def test_garbage_data_replies_error_worker_alive(tmp_path):
    ws, _ = _scaffold_ws(tmp_path / "ws", tmp_path)
    # A supplementary file carrying non-numeric text mid-scan is discovered as a measurement but
    # blows up in parse_all — a deterministic run_batch exception.
    valid = s30.emit_file(run_builder.build_s30_run("SYN", n_scans=1, seed0=1000), s30.card_template())
    garbage = tmp_path / "GARBAGE.spec_supplementary.dat"
    garbage.write_bytes(_corrupt_data_row(valid))
    w = _RawWorker(ws.root)
    try:
        w.send({"id": 5, "cmd": "run", "data_paths": [str(garbage)], "M": 10})
        reply = w.recv()
        assert reply["id"] == 5 and reply["ok"] is False
        assert reply["error"]["traceback"]  # a real run_batch traceback rode home
        # …and it is the planted corruption that failed, not some incidental workspace error.
        assert "NOT_A_NUMBER" in reply["error"]["message"], reply["error"]
        # A job failure does NOT kill the worker.
        w.send({"id": 6, "cmd": "ping"})
        assert w.recv() == {"id": 6, "ok": True}
    finally:
        w.send({"id": None, "cmd": "shutdown"})
        assert w.wait() == 0
        w.kill()


# --------------------------------------------------------------------------- A/B state-free identity
@pytest.mark.slow
def test_ab_subprocess_vs_worker_results_identical(tmp_path):
    """Same data + same card through SubprocessRunner (workspace A) and WorkerRunner (workspace B)
    produce byte-identical results.csv — proof the warm worker carries no cross-job state (F.2)."""
    from isar.drive.isar_io.runner import SubprocessRunner
    from isar.drive.isar_io.worker_client import WorkerRunner

    ws_a, data = _scaffold_ws(tmp_path / "wsA", tmp_path)
    ws_b, _ = _scaffold_ws(tmp_path / "wsB", tmp_path)   # same card + same data file, fresh workspace

    sub = SubprocessRunner()
    sub.run(ws_a.root, [data], errors_tier="full", render=False)

    worker = WorkerRunner(ws_b.root, fallback=SubprocessRunner())
    try:
        worker.run(ws_b.root, [data], errors_tier="full", render=False)
    finally:
        worker.close()

    csv_a = (ws_a.report_dir / "results.csv").read_bytes()
    csv_b = (ws_b.report_dir / "results.csv").read_bytes()
    assert csv_a == csv_b, "worker and subprocess results.csv diverged (hidden cross-job state?)"
