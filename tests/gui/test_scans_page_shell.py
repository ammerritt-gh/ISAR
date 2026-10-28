"""Offscreen checks for the Scan(s) shell and its window routing."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import Qt

from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar.workspace_api import Workspace
from isar_gui.views.group_chips import GroupChips
from isar_gui.views.scans_page import EMPTY, EMPTY_TEXT, PLOT, ScansPage


def test_scan_page_mounts_share_the_window_selection(qtbot, bl43_ws_readonly):
    window = MainWindow(Workspace.open(bl43_ws_readonly), session=Session(), watch=False)
    qtbot.addWidget(window)
    page = window._center_host.pane("scan")

    assert isinstance(page, ScansPage)
    assert window.selection is window.scan_list_vm.selection is page.selection
    assert tuple(window._center_host._keys) == ("scan", "plane", "dispersion", "sandbox")
    assert window._center_host._buttons["scan"].text() == "Scan(s)"
    assert isinstance(page.groups, GroupChips)
    assert page.groups.parentWidget() is page.group_mount
    assert all(mount.layout().contentsMargins().left() == 0 for mount in (
        page.fit_mount, page.group_mount, page.layout_mount, page.offset_mount, page.scale_mount))
    assert page.body.currentIndex() == EMPTY
    assert page.empty_label.text() == EMPTY_TEXT
    assert page.minimumSizeHint().width() <= 300
    assert page._glw is not None and page.plot_area.isAncestorOf(page._glw)

    key = page.scan_list_vm.snapshot.sets[0].spectra[0].key
    window.selection.set([key], clicked=key, origin="list")
    page.flush_redraw()
    assert page.body.currentIndex() == PLOT


@pytest.mark.parametrize("saved_page", ["scan", "waterfall"])
def test_retired_waterfall_session_opens_scans_page(qtbot, saved_page):
    session = Session()
    session.view_state["center_view"] = saved_page
    window = MainWindow(None, session=session, watch=False)
    qtbot.addWidget(window)
    assert window._center_host.current_key == "scan"


def test_queue_show_uses_selection_and_respects_auto_follow(qtbot, bl43_ws_readonly):
    window = MainWindow(Workspace.open(bl43_ws_readonly), session=Session(), watch=False)
    qtbot.addWidget(window)
    keys = [row.key for scan_set in window.scan_list_vm.snapshot.sets for row in scan_set.spectra]
    first, second = keys[:2]

    window._center_host.show_view("plane")
    window._queue.show(first)
    assert window.selection.keys == (first,)
    assert window._center_host.current_key == "scan"

    window._center_host.show_view("plane", user=True)
    window._queue.show(second)
    assert window.selection.keys == (second,)
    assert window._center_host.current_key == "plane"


def test_window_group_shortcuts_reach_the_saved_group_bar(qtbot, bl43_ws_readonly, monkeypatch):
    window = MainWindow(Workspace.open(bl43_ws_readonly), session=Session(), watch=False)
    qtbot.addWidget(window)
    page = window._center_host.pane("scan")
    seen = []
    monkeypatch.setattr(page.groups, "key_pressed", lambda slot, save: seen.append((slot, save)))
    shortcuts = {shortcut.key().toString(): shortcut for shortcut in window._group_shortcuts}
    assert shortcuts["Ctrl+3"].context() == Qt.ShortcutContext.WindowShortcut
    shortcuts["Ctrl+3"].activated.emit()
    shortcuts["3"].activated.emit()

    shortcuts["Ctrl+0"].activated.emit()
    shortcuts["0"].activated.emit()
    assert seen == [(3, True), (3, False), (10, True), (10, False)]


def test_window_selection_never_builds_the_retired_inspector_plot(qtbot, scans_view_ws_readonly, monkeypatch):
    from scans_view_fixture import ONE_MODE

    workspace = Workspace.open(scans_view_ws_readonly)

    def forbidden(*args, **kwargs):
        pytest.fail("Scan(s) selection invoked the legacy curve reader")

    monkeypatch.setattr(workspace, "evaluate_fit", forbidden)
    window = MainWindow(workspace, session=Session(), watch=False)
    qtbot.addWidget(window)
    window.selection.set([ONE_MODE], clicked=ONE_MODE, origin="list")
    window.scans_page.flush_redraw()
    assert window.inspector_vm.current_fit is not None
    assert window.scans_page.drawn().keys == (ONE_MODE,)
