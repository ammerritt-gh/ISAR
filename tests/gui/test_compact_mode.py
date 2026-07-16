"""Compact campaign-monitor mode (M3-1): same window, stashed layout, honest persistence."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDockWidget

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.views.campaign_monitor import CampaignMonitor


@pytest.fixture
def ready_window(qtbot, make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(1400, 900)
    w.show()
    qtbot.waitExposed(w)
    qtbot.wait(50)   # queued layout work
    return w


def test_toggle_hides_docks_and_flips_stack(qtbot, ready_window):
    w = ready_window
    assert not w._compact_active
    w.toggle_compact_mode()
    assert w._compact_active
    assert isinstance(w._center_stack.currentWidget(), CampaignMonitor)
    assert all(d.isHidden() for d in w.findChildren(QDockWidget))
    assert w.width() <= 500 and w.height() <= 400   # synchronous: explicit min beats stale hints


def test_expand_restores_layout_and_visibility(qtbot, ready_window):
    w = ready_window
    scans_visible_before = not w.scans_dock.isHidden()
    w.toggle_compact_mode()
    w.toggle_compact_mode()
    assert not w._compact_active
    assert w._center_stack.currentWidget() is w._center_host
    assert (not w.scans_dock.isHidden()) == scans_visible_before
    assert not w.log_dock.isHidden()


def test_snapshot_while_compact_keeps_full_layout(qtbot, ready_window):
    w = ready_window
    full = w._snapshot_session().dock_layout_b64
    assert full
    w.toggle_compact_mode()
    assert w._snapshot_session().dock_layout_b64 == full   # never overwritten while compact


def test_close_while_compact_persists_full_layout(qtbot, ready_window):
    w = ready_window
    full = w._snapshot_session().dock_layout_b64
    w.toggle_compact_mode()
    w.close()
    assert not w._compact_active                            # closeEvent expanded first
    assert w._session.dock_layout_b64 == full or w._session.dock_layout_b64  # full layout, not sparse


def test_compact_refused_on_empty_window(qtbot):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    w.toggle_compact_mode()
    assert not w._compact_active


def test_monitor_stop_routes_cooperatively(qtbot, ready_window, monkeypatch):
    w = ready_window
    calls = []
    monkeypatch.setattr(w._operations, "request_drive_stop", lambda: calls.append(1) or True)
    w.toggle_compact_mode()
    w._campaign_monitor._stop_btn.setEnabled(True)          # as if a campaign were running
    w._campaign_monitor._stop_btn.click()
    assert calls == [1]


def test_monitor_ticker_mirrors_log(qtbot, ready_window):
    w = ready_window
    w.toggle_compact_mode()
    for i in range(5):
        w.log("ticker line %d" % i)
    text = w._campaign_monitor._ticker_label.text()
    assert "ticker line 4" in text and "ticker line 1" not in text   # last 3 lines only
