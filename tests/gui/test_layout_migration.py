"""Saved-layout migration to the Pass-3 arrangement (versioned saveState).

A pre-Pass-3 session carries an UNVERSIONED ``saveState`` blob naming docks that no longer exist;
``restoreState(data, _LAYOUT_VERSION)`` must reject it atomically and the window must fall back to
``_apply_default_layout`` (Files small on top-left, Scans stack under it, compact Log). A current-
version blob must keep round-tripping, and garbage must never block the window — guarantees that
predate this file and must survive it.
"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import Qt

from isar.workspace_api import Workspace
from isar_gui import main_window as mw_mod
from isar_gui.main_window import MainWindow, _LAYOUT_VERSION
from isar_gui.session import Session


@pytest.fixture
def ws(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    return Workspace.open(root)


def _window(qtbot, ws, session=None):
    w = MainWindow(ws, session=session or Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    qtbot.waitExposed(w)
    return w


def _flush_queued(qtbot, w):
    # The ready-layout pass runs on a queued singleShot(0); one processEvents turn lands it.
    qtbot.wait(50)


def _assert_pass3_default(w):
    files_area = w.dockWidgetArea(w.files_dock)
    scans_area = w.dockWidgetArea(w.scans_dock)
    assert files_area == Qt.LeftDockWidgetArea
    assert scans_area == Qt.LeftDockWidgetArea
    # Files sits ABOVE the Scans stack in the left column.
    assert w.files_dock.y() < w.scans_dock.y()
    # Browser / Card / Scan Table ride as tabs of the Scans stack (same geometry bucket).
    for dock in (w.browser_dock, w.card_dock, w.scan_table_dock):
        assert w.dockWidgetArea(dock) == Qt.LeftDockWidgetArea
    assert set(w.tabifiedDockWidgets(w.scans_dock)) >= {w.browser_dock, w.card_dock,
                                                        w.scan_table_dock}
    # The Log is a compact ticker, not a workspace surface.
    assert w.log_dock.height() <= mw_mod._DEFAULT_LOG_HEIGHT + 40
    for dock in (w.files_dock, w.scans_dock, w.browser_dock, w.card_dock,
                 w.scan_table_dock, w.log_dock):
        assert not dock.isHidden()


def test_unversioned_blob_falls_back_to_default(qtbot, ws, caplog):
    w1 = _window(qtbot, ws)
    _flush_queued(qtbot, w1)
    # A pre-Pass-3 session snapshot: saveState() WITHOUT a version argument.
    old_blob = bytes(w1.saveState().toBase64().data()).decode("ascii")
    w1.close()

    session = Session()
    session.dock_layout_b64 = old_blob
    with caplog.at_level(logging.INFO, logger="isar_gui.main_window"):
        w2 = _window(qtbot, ws, session)
        _flush_queued(qtbot, w2)
    _assert_pass3_default(w2)
    upgrades = [r for r in caplog.records if "Pass-3 arrangement" in r.getMessage()]
    assert len(upgrades) == 1


def test_current_version_blob_round_trips(qtbot, ws):
    w1 = _window(qtbot, ws)
    _flush_queued(qtbot, w1)
    w1.card_dock.setFloating(True)          # something distinctive to survive the round trip
    session = w1._snapshot_session()
    assert session.dock_layout_b64
    w1.close()

    w2 = _window(qtbot, ws, session)
    _flush_queued(qtbot, w2)
    assert w2.card_dock.isFloating()


def test_garbage_blob_never_blocks_the_window(qtbot, ws):
    session = Session()
    session.dock_layout_b64 = "not-even-base64!!"
    w = _window(qtbot, ws, session)
    _flush_queued(qtbot, w)
    _assert_pass3_default(w)                # fallback applied; window fully constructed


def test_restore_false_without_exception_falls_back(qtbot, ws, monkeypatch, caplog):
    w1 = _window(qtbot, ws)
    _flush_queued(qtbot, w1)
    session = w1._snapshot_session()
    w1.close()

    monkeypatch.setattr(MainWindow, "restoreState", lambda self, *a, **k: False)
    with caplog.at_level(logging.INFO, logger="isar_gui.main_window"):
        w2 = _window(qtbot, ws, session)
        _flush_queued(qtbot, w2)
    _assert_pass3_default(w2)
    assert any("Pass-3 arrangement" in r.getMessage() for r in caplog.records)


def test_version_constant_is_current():
    # M2-6 bumps this to 3 when the Campaign dock joins the default layout; this pin makes that
    # bump a conscious edit here rather than a silent drift.
    assert _LAYOUT_VERSION == 2
