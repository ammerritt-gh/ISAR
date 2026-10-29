"""The collector stays off while a GUI background thread runs (the close-time deadlock: a collection on
``isar-gui-health`` finalized Qt wrappers off the GUI thread). The intake worker's own test is in
test_stage2_setup.py; here: the shared counter, then the health probe and the sandbox preview."""
from __future__ import annotations

import gc
import threading
import time

import pytest

from isar import sandbox_api
from isar_gui import collector_guard, health

COLLECTOR_UNDER_TEST = True          # tests/gui/conftest.py keeps the collector off for other tests


@pytest.fixture
def collector_on():
    """Start every test with the collector enabled and nothing held, and leave it that way."""
    collector_guard.release()
    was = gc.isenabled()
    gc.enable()
    yield
    collector_guard.release()
    gc.enable() if was else gc.disable()


@pytest.fixture
def collected_on():
    """Thread names of every automatic collection that starts while the test runs."""
    names: list[str] = []

    def note(phase, _info):
        if phase == "start":
            names.append(threading.current_thread().name)
    gc.callbacks.append(note)
    yield names
    gc.callbacks.remove(note)


def _churn() -> None:
    """Allocate reference cycles until an enabled collector would certainly have run on this thread."""
    for _ in range(30_000):
        cycle: list = []
        cycle.append(cycle)


def _alive(stop: threading.Event) -> threading.Thread:
    thread = threading.Thread(target=stop.wait, args=(10,), daemon=True)
    thread.start()
    return thread


def test_overlapping_holds_give_the_collector_back_only_after_the_last_ends(collector_on):
    stop_a, stop_b = threading.Event(), threading.Event()
    a = threading.Thread(target=stop_a.wait, args=(10,), daemon=True)
    b = threading.Thread(target=stop_b.wait, args=(10,), daemon=True)
    collector_guard.hold(a)
    a.start()
    assert not gc.isenabled()
    collector_guard.hold(b)               # a second owner's worker while the first still runs
    b.start()
    assert not gc.isenabled()
    stop_a.set()
    a.join(5)
    assert collector_guard.release() is True and not gc.isenabled()     # b is still alive
    stop_b.set()
    b.join(5)
    assert collector_guard.release() is False and gc.isenabled()


def test_a_collector_that_was_off_before_the_hold_stays_off(collector_on):
    gc.disable()
    stop = threading.Event()
    thread = _alive(stop)
    collector_guard.hold(thread)
    stop.set()
    thread.join(5)
    collector_guard.release()
    assert not gc.isenabled()


def test_no_collection_runs_on_the_health_threads_and_the_collector_comes_back(collector_on, collected_on):
    seen: list[str] = []

    def prober(url: str) -> health.Health:
        seen.append(threading.current_thread().name)
        _churn()
        return health.Health(True, "reachable")

    probe = health.HealthProbe(prober)
    for _ in range(2):
        gen = probe.request({"llm": "u1", "tavi": "u2"})
        results: list = []
        deadline = time.monotonic() + 10
        while not results and time.monotonic() < deadline:
            results = probe.poll()
            time.sleep(0.005)
        assert results and results[0].generation == gen and results[0].error is None
        assert gc.isenabled()             # given back by poll(), on this thread, at once
    assert any(n.startswith("isar-gui-health-endpoint") for n in seen)   # the pool really ran the prober
    assert not [n for n in collected_on if n.startswith("isar-gui-health")]


def test_shutdown_of_a_probe_with_a_blocked_worker_keeps_the_hold_until_it_ends(collector_on):
    release_prober = threading.Event()
    probe = health.HealthProbe(lambda url: (release_prober.wait(10), health.Health(True, "ok"))[1])
    probe.request({"llm": "u"})
    assert not gc.isenabled()
    probe.shutdown()                      # window closing: the worker is still blocked in its network call
    assert not gc.isenabled()
    release_prober.set()
    deadline = time.monotonic() + 10
    while collector_guard.release() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert gc.isenabled()                 # the next call after it ended gives the collector back


def test_no_collection_runs_on_the_sandbox_thread_and_the_collector_comes_back(
        qtbot, monkeypatch, collector_on, collected_on):
    from isar_gui.viewmodels import sandbox as vm_module
    ran_on: list[str] = []

    def fake_preview(_root, key, center_override=None):
        ran_on.append(threading.current_thread().name)
        _churn()
        return sandbox_api.SandboxResult(unit_key=key, status="held", reason="test")

    monkeypatch.setattr(vm_module.sandbox_api, "preview_fit", fake_preview)
    vm = vm_module.SandboxViewModel(workspace_root="workspace")
    got: list = []
    vm.result_ready.connect(lambda res: got.append((res, gc.isenabled())))
    for key in ("E:1:d", "E:2:d"):
        vm.request_fit(key)
        qtbot.waitUntil(lambda: len(got) == (1 if key == "E:1:d" else 2), timeout=10_000)
    assert ran_on == ["isar-gui-sandbox"] * 2
    assert all(on for _res, on in got)    # the collector was back before each result was delivered
    qtbot.waitUntil(lambda: not vm._timer.isActive(), timeout=5000)
    assert gc.isenabled()
    assert not [n for n in collected_on if n == "isar-gui-sandbox"]
