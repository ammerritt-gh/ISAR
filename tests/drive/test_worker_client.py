"""WorkerRunner failure-ladder unit tests (mighty-starfish §A, §F.4) — fast, no ISAR analysis.

The client is exercised against a tiny FAKE worker script (implements only the JSONL protocol, no
``run_batch``) launched by monkeypatching ``subprocess.Popen`` in the client module to substitute the
fake for ``-m isar.session.worker``. This keeps the transport / timeout / respawn / degradation logic
under test without spawning a real analysis interpreter.
"""
import subprocess
import sys

import pytest

import isar.drive.isar_io.worker_client as wc
from isar.drive.isar_io.runner import IsarRunError, RunResult

# A self-contained fake worker. Behaviour selected by ISAR_FAKE_MODE:
#   echo     — ping→ok, run→ok(+timings), shutdown/EOF→exit0   (the healthy worker)
#   timeout  — run→sleep forever (never replies)               (per-job timeout path)
#   crash    — run→exit immediately, no reply                  (worker dies mid-job → EOF)
#   okfalse  — run→ok:false with a traceback                   (clean run_batch failure)
_FAKE_WORKER = r'''
import sys, json, os, time

def _w(obj):
    sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

def main():
    mode = os.environ.get("ISAR_FAKE_MODE", "echo")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception as e:
            _w({"id": None, "ok": False, "error": {"type": type(e).__name__, "message": str(e), "traceback": ""}})
            continue
        cmd = req.get("cmd")
        if cmd == "shutdown":
            return 0
        if cmd == "ping":
            _w({"id": req.get("id"), "ok": True}); continue
        if cmd == "run":
            if mode == "timeout":
                time.sleep(30); continue
            if mode == "crash":
                return 0
            if mode == "okfalse":
                _w({"id": req.get("id"), "ok": False, "summary": None, "timings": None,
                    "error": {"type": "RuntimeError", "message": "boom", "traceback": "Traceback: boom"}})
                continue
            _w({"id": req.get("id"), "ok": True, "summary": {"n_reprocessed": 1},
                "timings": {"worker_job": True, "job_id": req.get("id"), "stages": []}, "error": None})
            continue
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''


class _RecordingFallback:
    """Stand-in cold runner: records every call, returns a recognizable RunResult."""

    def __init__(self):
        self.calls = []

    def run(self, workspace, data_paths, *, force=False, errors_tier="full", render=False, timeout=None):
        self.calls.append({"workspace": workspace, "data_paths": list(data_paths),
                           "force": force, "errors_tier": errors_tier})
        return RunResult(0, "FALLBACK", "")

    def close(self):
        pass


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """Write the fake worker script and redirect the client's Popen to launch it.

    Yields a namespace with the spawn counter and the fallback recorder."""
    script = tmp_path / "fake_worker.py"
    script.write_text(_FAKE_WORKER, encoding="utf-8")

    real_popen = subprocess.Popen
    state = type("S", (), {"spawns": 0})()

    def fake_popen(cmd, **kw):
        # cmd == [exe, "-m", "isar.session.worker", "--workspace", ws]  →  [exe, script, "--workspace", ws]
        new_cmd = [cmd[0], str(script), *cmd[3:]]
        state.spawns += 1
        return real_popen(new_cmd, **kw)

    monkeypatch.setattr(wc.subprocess, "Popen", fake_popen)
    state.ws = tmp_path / "ws"
    state.fallback = _RecordingFallback()
    return state


def _runner(fake_env, **kw):
    return wc.WorkerRunner(fake_env.ws, fallback=fake_env.fallback, **kw)


def test_happy_path_returns_worker_result(fake_env, monkeypatch):
    monkeypatch.setenv("ISAR_FAKE_MODE", "echo")
    r = _runner(fake_env)
    try:
        res = r.run(fake_env.ws, [fake_env.ws / "data"], errors_tier="full", render=False)
        assert isinstance(res, RunResult) and res.returncode == 0
        assert res.timings == {"worker_job": True, "job_id": 0, "stages": []}
        assert res.stdout != "FALLBACK"            # came from the worker, not the fallback
        assert fake_env.fallback.calls == []       # fallback untouched
        assert fake_env.spawns == 1                # spawned exactly once, reused
        # A second job reuses the same warm worker (no respawn).
        r.run(fake_env.ws, [fake_env.ws / "data"], render=False)
        assert fake_env.spawns == 1
    finally:
        r.close()


def test_finish_initial_wave_reaches_warm_worker_without_allowance(fake_env, monkeypatch):
    monkeypatch.setenv("ISAR_FAKE_MODE", "echo")
    r = _runner(fake_env)
    sent = []
    original_send = r._send

    def recording_send(message):
        sent.append(dict(message))
        original_send(message)

    monkeypatch.setattr(r, "_send", recording_send)
    try:
        result = r.run(
            fake_env.ws,
            [fake_env.ws / "data"],
            render=False,
            finish_initial_wave=True,
        )
        assert result.returncode == 0
        run_request = next(message for message in sent if message.get("cmd") == "run")
        assert run_request["finish_initial_wave"] is True
        assert "analysis_allowance_seconds" not in run_request
    finally:
        r.close()


def test_timeout_falls_back_and_respawns(fake_env, monkeypatch):
    monkeypatch.setenv("ISAR_FAKE_MODE", "timeout")
    r = _runner(fake_env, job_timeout=0.6, max_respawns=5)
    try:
        res1 = r.run(fake_env.ws, [fake_env.ws / "data"], render=False)
        assert res1.stdout == "FALLBACK"           # per-job timeout → cold fallback
        assert len(fake_env.fallback.calls) == 1
        assert fake_env.spawns == 1
        assert r.degraded is False
        # Next job respawns a fresh worker (which also times out → fallback again).
        res2 = r.run(fake_env.ws, [fake_env.ws / "data"], render=False)
        assert res2.stdout == "FALLBACK"
        assert fake_env.spawns == 2                # respawned
        assert len(fake_env.fallback.calls) == 2
    finally:
        r.close()


def test_worker_death_mid_job_falls_back_and_respawns(fake_env, monkeypatch):
    monkeypatch.setenv("ISAR_FAKE_MODE", "crash")
    r = _runner(fake_env, max_respawns=5)
    try:
        res1 = r.run(fake_env.ws, [fake_env.ws / "data"], render=False)
        assert res1.stdout == "FALLBACK"           # worker exited mid-job (EOF) → fallback
        assert fake_env.spawns == 1
        res2 = r.run(fake_env.ws, [fake_env.ws / "data"], render=False)
        assert res2.stdout == "FALLBACK"
        assert fake_env.spawns == 2                # respawned for the next job
    finally:
        r.close()


def test_clean_ok_false_raises_worker_stays_up(fake_env, monkeypatch):
    monkeypatch.setenv("ISAR_FAKE_MODE", "okfalse")
    r = _runner(fake_env)
    try:
        with pytest.raises(IsarRunError) as ei:
            r.run(fake_env.ws, [fake_env.ws / "data"], render=False)
        assert "boom" in str(ei.value)             # the worker traceback rode home
        assert fake_env.fallback.calls == []       # NOT a transport failure → no fallback
        assert r.degraded is False
        assert fake_env.spawns == 1                # worker still alive, not respawned
        # It really is still up: a ping-equivalent second job round-trips on the same process.
        with pytest.raises(IsarRunError):
            r.run(fake_env.ws, [fake_env.ws / "data"], render=False)
        assert fake_env.spawns == 1
    finally:
        r.close()


def test_close_leaves_no_zombie(fake_env, monkeypatch):
    monkeypatch.setenv("ISAR_FAKE_MODE", "echo")
    r = _runner(fake_env)
    r.run(fake_env.ws, [fake_env.ws / "data"], render=False)
    proc = r._proc
    assert proc is not None and proc.poll() is None
    r.close()
    assert proc.poll() is not None                 # exited after close() (shutdown → EOF)
    assert r._proc is None


def test_max_respawns_permanent_fallback_degraded_once(fake_env, monkeypatch):
    monkeypatch.setenv("ISAR_FAKE_MODE", "crash")
    degraded_calls = []
    r = _runner(fake_env, max_respawns=2, on_degraded=lambda: degraded_calls.append(1))
    try:
        r.run(fake_env.ws, [fake_env.ws / "data"], render=False)   # fail 1 → counter 1
        assert r.degraded is False and degraded_calls == []
        r.run(fake_env.ws, [fake_env.ws / "data"], render=False)   # fail 2 → counter 2 == max → degrade
        assert r.degraded is True
        assert degraded_calls == [1]               # callback fired exactly once
        spawns_at_degrade = fake_env.spawns
        # Now permanently degraded: further runs go straight to the fallback, no spawn attempt.
        res = r.run(fake_env.ws, [fake_env.ws / "data"], render=False)
        assert res.stdout == "FALLBACK"
        assert fake_env.spawns == spawns_at_degrade  # no new spawn
        assert degraded_calls == [1]                 # still once
    finally:
        r.close()
