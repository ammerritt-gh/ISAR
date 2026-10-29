"""The window's wiring of inter-spectra fitting (offscreen Qt, the real MainWindow and widgets).

The box, the anchors list and the batch previews are checked in ``test_inter_batch_box.py`` against fakes. This file
holds what only the window does: it builds one InterState and hands it to the scan list, the Processing dock and the
Scan(s) page; it binds it to each experiment; it reloads it when results are saved; a rename waits for its worker; and
it sends a batch Start and the Config settings item to where they belong. The reads and writers are the real ones,
over the BL43 sample workspace.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QWidget

from bl43_fixture import KEY_A
from gui.inter_fakes import make_preview
from inter_fixtures import build_inter_workspace, spectrum_keys
from isar.workspace_api import inter_spectra as inter_reads
from isar_gui import main_window as mw_mod
from isar_gui import queue_controller as qc
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.views.inter_settings_dialog import InterSettingsDialog
from isar_gui.views.rename_dialog import RenameDialog

WAIT_MS = 15000


@pytest.fixture
def inter_ws(tmp_path):
    """Four analyzers of one scan, each with a stored fast fit and the import record an anchor is written against
    (the BL43 sample has no import record, so a save of an anchor there is refused)."""
    return build_inter_workspace(tmp_path / "inter", temperatures=(100,), setups=("0p5_0p5_3",), n_analyzers=4)


def _win(qtbot, root=None):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    if root is not None:
        with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
            assert w.load_workspace(str(root))
    return w


def test_the_window_hands_one_inter_state_to_the_scan_list_the_dock_and_the_scans_page(qtbot, bl43_ws):
    w = _win(qtbot, bl43_ws)
    box = w.processing_panel.inter_box
    assert w.scan_list_vm._inter is w.inter
    assert box._inter is w.inter and box._scan_list is w.scan_list_vm
    assert w.scans_page.vm.inter is w.inter


def test_the_inter_state_is_read_for_each_experiment_and_emptied_for_none(qtbot, bl43_ws):
    w = _win(qtbot)
    assert w.inter.overview is None and w.inter.anchors == ()
    w = _win(qtbot, bl43_ws)
    qtbot.waitUntil(lambda: w.inter.overview is not None, timeout=WAIT_MS)
    assert w.inter.anchors == ()


def test_a_spectrum_set_as_anchor_from_the_scan_list_reaches_the_dock_list(qtbot, inter_ws):
    spec = spectrum_keys(inter_ws)[0]
    w = _win(qtbot, inter_ws)
    box = w.processing_panel.inter_box
    w.scan_list_vm.select(spec)
    qtbot.waitUntil(lambda: box._add.isEnabled(), timeout=WAIT_MS)
    box._add.click()                                                    # the dock's own *Add selected* button
    qtbot.waitUntil(lambda: spec in w.inter.anchors, timeout=WAIT_MS)
    qtbot.waitUntil(lambda: box._anchors.count() == 1, timeout=WAIT_MS)
    assert box._rows[0][0] == spec
    assert w.scan_list_vm.is_anchor(spec)                               # one fact, shown in both places


def test_saved_results_reload_the_inter_state(qtbot, bl43_ws, monkeypatch):
    w = _win(qtbot, bl43_ws)
    reloads = []
    monkeypatch.setattr(w.inter, "reload", lambda: reloads.append(1))
    w._queue.results_saved.emit((f"{KEY_A}:a03",))
    assert reloads == [1]


def test_a_rename_waits_for_the_inter_worker_as_it_waits_for_the_intake(qtbot, monkeypatch, bl43_ws):
    w = _win(qtbot, bl43_ws)
    qtbot.waitUntil(lambda: w.inter.overview is not None, timeout=WAIT_MS)   # its read has ended before the rename
    monkeypatch.setattr(RenameDialog, "ask", lambda root, ws=None, parent=None: "renamed")
    waited = []
    monkeypatch.setattr(w.intake, "wait_idle", lambda: waited.append("intake"))
    monkeypatch.setattr(w.inter, "wait_idle", lambda: waited.append("inter"))
    w.rename_experiment()
    assert waited == ["intake", "inter"] and w._wsm.root() == bl43_ws.parent / "renamed"


def test_the_config_entry_follows_the_cores_entry_is_greyed_without_an_experiment_and_opens_the_settings(
        qtbot, monkeypatch, bl43_ws):
    w = _win(qtbot)
    action = w._inter_settings_action
    menu_actions = w._cores_action.parent().actions()
    assert menu_actions[menu_actions.index(w._cores_action) + 1] is action
    assert action.text() == "Inter-spectra fitting settings…"
    assert not action.isEnabled() and action.toolTip() == "Open an experiment first."
    w = _win(qtbot, bl43_ws)
    opened = []
    monkeypatch.setattr(InterSettingsDialog, "exec", lambda self: opened.append(self) or 0)
    assert w._inter_settings_action.isEnabled() and "belong to this experiment" in w._inter_settings_action.toolTip()
    w._inter_settings_action.trigger()
    assert len(opened) == 1 and opened[0].objectName() == "inter_settings_dialog"


def test_a_batch_start_becomes_one_queued_row_and_the_leave_question_names_it(qtbot, monkeypatch, inter_ws):
    spec = spectrum_keys(inter_ws)[0]
    preview = make_preview(can_start=True, members=(spec,), line="1 anchor · spread in q · 1 spectrum · 1 ring")
    monkeypatch.setattr(inter_reads, "batch_preview", lambda root, req: preview)     # the dock's preview
    monkeypatch.setattr(qc, "batch_preview", lambda root, req: preview)              # the queue's own re-read
    w = _win(qtbot, inter_ws)
    monkeypatch.setattr(w._queue, "_pump", lambda: None)            # no analysis process: the batch stays waiting
    try:
        w.inter.set_anchors([spec], True)
        box = w.processing_panel.inter_box
        qtbot.waitUntil(lambda: box._start_anchored.isEnabled(), timeout=WAIT_MS)
        box._start_anchored.click()                                  # the dock's Start, with its own request
        qtbot.waitUntil(lambda: len(w._queue.view().batches) == 1, timeout=WAIT_MS)
        (row,) = w._queue.view().batches
        assert (row.kind, row.direction, row.status) == ("anchored", "q", "waiting")
        qtbot.waitUntil(lambda: w.processing_panel.findChild(QWidget, f"queue_batch_{row.item_id}") is not None,
                        timeout=WAIT_MS)                             # the panel paints the row on its next tick
        summary = w._queue.leave_summary()
        assert (summary.n_waiting, summary.n_batches_waiting, summary.n_batches) == (1, 1, 0)
        asked = []
        monkeypatch.setattr(mw_mod, "_confirm_leave", lambda parent, title, text, stay, go: asked.append(text) or False)
        assert w._confirm_leave_queue("close") is False
        assert asked == ["1 inter-spectra batch is waiting. Leaving forgets the waiting batch."]
    finally:
        _forget_queue(w)


def _forget_queue(w) -> None:
    """Leave no process and no queued work behind: closing the window at teardown then asks nothing."""
    w._queue._drop_service()
    w._queue.bind(None, auto_fast=True)
