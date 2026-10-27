"""Saved-layout migration to the one arrangement (versioned saveState).

A session from an older ISAR carries an UNVERSIONED (or version-3) ``saveState`` blob naming docks that
no longer exist; ``restoreState(data, _LAYOUT_VERSION)`` must reject it atomically and the window must
fall back to ``_apply_default_arrangement`` (full-height left column, Processing | Campaign tabs on the
right, Log | Output bottom bar, the old panels hidden) with exactly one "Window layout:" Log line. A
current-version blob must keep round-tripping, and garbage must never block the window -- guarantees
that predate this file and must survive it.
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
    qtbot.wait(50)


def _assert_default_arrangement(w):
    # The left column owns the left area; the old panels are parked (hidden, out of the layout), and
    # View > Panels shows one as a tab of the left column.
    assert w.dockWidgetArea(w.left_column_dock) == Qt.LeftDockWidgetArea
    old = {w.files_dock, w.scans_dock, w.browser_dock, w.card_dock, w.scan_table_dock}
    for dock in old:
        assert w.dockWidgetArea(dock) == Qt.NoDockWidgetArea
        assert dock.isHidden()
    # Processing | Campaign are tabs of the right area. The offscreen screen is laptop-sized, so the
    # right panel starts collapsed to its rail; expanding it shows both tabs, Processing in front.
    assert w.dockWidgetArea(w.processing_dock) == Qt.RightDockWidgetArea
    assert w.dockWidgetArea(w.campaign_dock) == Qt.RightDockWidgetArea
    assert w._right_panel_collapsed()
    # The Log is a compact ticker with the Output beside it.
    assert w.dockWidgetArea(w.log_dock) == Qt.BottomDockWidgetArea
    assert w.dockWidgetArea(w.output_dock) == Qt.BottomDockWidgetArea
    assert w.log_dock.height() <= mw_mod._ARRANGEMENTS["laptop"].log_height + 40
    for dock in (w.left_column_dock, w.log_dock, w.output_dock):
        assert not dock.isHidden()
    w.set_right_panel_collapsed(False)
    assert w.campaign_dock in w.tabifiedDockWidgets(w.processing_dock)
    assert w.processing_dock.isVisible()
    w._show_panel(w.scans_dock)
    assert w.dockWidgetArea(w.scans_dock) == Qt.LeftDockWidgetArea
    assert w.scans_dock in w.tabifiedDockWidgets(w.left_column_dock)


def _window_layout_lines(caplog):
    return [r for r in caplog.records if "Window layout:" in r.getMessage()]


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
    _assert_default_arrangement(w2)
    assert len(_window_layout_lines(caplog)) == 1


def test_current_version_blob_round_trips(qtbot, ws):
    w1 = _window(qtbot, ws)
    _flush_queued(qtbot, w1)
    w1._show_panel(w1.card_dock)            # parked by default; something distinctive to survive
    w1.card_dock.setFloating(True)
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
    _assert_default_arrangement(w)          # fallback applied; window fully constructed


def test_restore_false_without_exception_falls_back(qtbot, ws, monkeypatch, caplog):
    w1 = _window(qtbot, ws)
    _flush_queued(qtbot, w1)
    session = w1._snapshot_session()
    w1.close()

    monkeypatch.setattr(MainWindow, "restoreState", lambda self, *a, **k: False)
    with caplog.at_level(logging.INFO, logger="isar_gui.main_window"):
        w2 = _window(qtbot, ws, session)
        _flush_queued(qtbot, w2)
    _assert_default_arrangement(w2)
    assert len(_window_layout_lines(caplog)) == 1


def test_version_constant_is_current():
    # A dock-inventory change must bump this consciously (v2 = M1 CenterHost migration, v3 = M2
    # Campaign dock, v4 = the one arrangement); the pin makes the bump an explicit edit here rather
    # than a silent drift.
    assert _LAYOUT_VERSION == 4
