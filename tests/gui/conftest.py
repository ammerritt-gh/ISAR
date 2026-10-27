"""Shared setup for the isar_gui tests.

Forces the Qt offscreen platform BEFORE any Qt import so the suite runs headless and xdist-safe, and
exposes the shared synthetic-workspace factory (from tests/synthetic_ws) as a ``make_ws`` fixture so
the GUI tests build workspaces the same way the facade tests do — no copy.
"""
from __future__ import annotations

import os
import threading

# Must precede any PySide6 import (test modules importorskip PySide6 at module scope).
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from synthetic_ws import stamp_ws


@pytest.fixture(autouse=True)
def _isolated_user_dirs(tmp_path, monkeypatch):
    """Redirect the per-user directories the GUI writes into so tests never touch the operator's real
    profile. Without this, MainWindow's session Autosaver writes ``%APPDATA%/isar_gui/session.json``
    and workspace_manager reserves temp workspaces under ``%LOCALAPPDATA%/isar_gui/temp_workspaces``
    — GUI tests were polluting the live session's recents (a stale pytest ``tmp_path`` workspace even
    got auto-opened on the next real launch) and leaving temp dirs in the real AppData tree.

    ``APPDATA`` covers ``session_path()`` (isar_gui.session) and ``gui_log_dir()``
    (isar_gui.logging_setup); ``LOCALAPPDATA`` covers ``temp_workspaces_root()``
    (isar_gui.workspace_manager). Both are set to fresh per-test subdirs. Autouse so every GUI test is
    isolated by default; a test that sets/deletes these envs itself (e.g. the session_path tests)
    still wins because its own monkeypatch runs after this fixture.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "localappdata"))
    # Where a new experiment folder is created (``experiments_root()`` honours it first): no GUI test
    # may create one under the operator's real ``Documents\ISAR``.
    monkeypatch.setenv("ISAR_EXPERIMENTS_ROOT", str(tmp_path / "experiments"))


@pytest.fixture(autouse=True)
def _no_live_health_probes(monkeypatch):
    """Stop every MainWindow from spawning a real network probe.

    ``MainWindow._init_health`` wires ``HealthProbe(lambda url: ping_http(url))`` and starts a
    timer, so *constructing a window at all* launches a daemon thread that calls ``urlopen``
    against the operator's real LLMM/TAVI ports. Across a GUI run those threads accumulate and
    race Qt teardown; the suite died with "Windows fatal exception: access violation" with the
    crashing thread parked in ``socket.create_connection``. Tests must never touch the network:
    it makes results depend on which services happen to be running on the developer's machine.

    Autouse so the default is offline, matching ``_isolated_user_dirs`` above. A test that wants
    specific probe behaviour still patches ``main_window.ping_http`` itself and wins, because its
    monkeypatch runs after this fixture (see ``test_backends`` health tests).

    Patching alone is not enough. ``HealthProbe.shutdown`` only makes late results *inert* — it never
    joins the worker (isar_gui/health.py), and the worker resolves ``main_window.ping_http`` at call
    time through the lambda. A probe thread still in flight when ``monkeypatch`` restores the real
    function therefore calls the real one, which is exactly the offline guarantee this fixture exists
    to give. So the surviving workers are joined *after the yield and before the undo*: fixture
    finalization runs in reverse dependency order, and ``monkeypatch`` is a dependency here, so its
    teardown is strictly later. With the offline prober in place the join is free — the workers have
    already returned; the point is that no thread can outlive the patch.
    """
    from isar_gui import main_window
    from isar_gui.health import Health

    monkeypatch.setattr(main_window, "ping_http",
                        lambda url, *a, **k: Health(False, "unreachable (offline in tests)",
                                                    reachable=False))
    # ``isar-gui-health`` is the name HealthProbe._start_locked gives every worker it spawns, so it
    # identifies the threads but not *whose* they are. Snapshotting first scopes the join to the ones
    # this test started: a straggler from an earlier test already had its own bounded join and is not
    # this fixture's to wait on.
    preexisting = {id(t) for t in threading.enumerate() if t.name == "isar-gui-health"}
    yield
    for thread in threading.enumerate():
        if (thread.name == "isar-gui-health" and id(thread) not in preexisting
                and thread.is_alive()):
            # Bounded: a hung real-socket probe must not wedge the suite. It cannot hang under the
            # offline prober, so a timeout here means a test installed a blocking prober of its own.
            thread.join(timeout=5.0)


@pytest.fixture(autouse=True)
def _collector_back_after_each_test():
    """A window closed with a background thread still running leaves its hold; the next call finds the
    thread ended. Make that call here so no test inherits a disabled collector."""
    yield
    from isar_gui import collector_guard
    collector_guard.release()


@pytest.fixture
def make_ws(tmp_path):
    """Return a factory that stamps a synthetic workspace under a fresh subdir and returns its path."""
    counter = {"n": 0}

    def _make(**parts):
        counter["n"] += 1
        root = tmp_path / f"ws{counter['n']}"
        root.mkdir()
        return stamp_ws(root, **parts)

    return _make
