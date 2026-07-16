"""Shared setup for the isar_gui tests.

Forces the Qt offscreen platform BEFORE any Qt import so the suite runs headless and xdist-safe, and
exposes the shared synthetic-workspace factory (from tests/synthetic_ws) as a ``make_ws`` fixture so
the GUI tests build workspaces the same way the facade tests do — no copy.
"""
from __future__ import annotations

import os

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
