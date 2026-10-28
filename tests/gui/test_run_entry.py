"""Amendment 7 / 8: one window-level entry point for starting an analysis, and the Run button in the setup area.

Offscreen Qt, the real MainWindow. The geometry of the button at 1108x851 and 1920x1040 is in
``test_stage2_walkthrough.py`` (it needs the BL43 intake fixture); the BL43 readiness states are in its scenario 3.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from isar_gui.main_window import MainWindow
from isar_gui.run_dialog import NO_DATA_REASON, RunDialog
from isar_gui.session import Session


def _win(qtbot, root=None):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    if root is not None:
        with qtbot.waitSignal(w.intake.worker_changed, timeout=15000):
            assert w.load_workspace(str(root))
    return w


def test_every_way_to_start_an_analysis_goes_through_start_analysis(qtbot, bl43_ws, monkeypatch):
    calls = []
    monkeypatch.setattr(MainWindow, "start_analysis", lambda self: calls.append("start"))
    monkeypatch.setattr(MainWindow, "run_analysis", lambda self: calls.append("dialog"))   # never reached directly
    w = _win(qtbot, bl43_ws)
    assert w.setup_section._run.isEnabled() and w.processing_panel._run.isEnabled() and w._run_action.isEnabled()
    w._run_action.trigger()                                  # Run > Run analysis…
    w.processing_panel._run.click()                          # the Processing panel's button
    w.setup_section._run.click()                             # the visible button in the setup area
    w.readiness_line.run_requested.emit()                    # the readiness list's "Run analysis…"
    w.attention_list.run_requested.emit()                    # the attention group's "Run analysis…"
    assert calls == ["start"] * 5


def test_start_analysis_opens_the_run_dialog_today(qtbot, bl43_ws, monkeypatch):
    """Until the queue job rebinds it, the entry point is the existing Run route."""
    opened = []
    monkeypatch.setattr(MainWindow, "run_analysis", lambda self: opened.append(1))
    w = _win(qtbot, bl43_ws)
    w.start_analysis()
    assert opened == [1]


def test_the_run_button_is_off_with_its_reason_in_text_until_something_can_run(qtbot, bl43_ws):
    w = _win(qtbot)                                          # no experiment
    run, note = w.setup_section._run, w.setup_section._run_note
    assert not run.isEnabled() and note.isVisible() and note.full_text() == "Open or create an experiment first."
    w = _win(qtbot, bl43_ws)                                 # data, and nothing to wait for
    run, note = w.setup_section._run, w.setup_section._run_note
    assert run.isEnabled() and run.toolTip()
    w._operations.is_busy = lambda: True                     # another operation holds the writer gate
    w._sync_run_actions()
    assert not run.isEnabled() and note.full_text() == "ISAR is busy with another operation."
    assert run.toolTip() == note.full_text()                 # the same words on hover
    # visible beside the button, unless the amber curves line (which then says what is wrong) already shows
    assert note.isVisible() or w.setup_section._problem.isVisible()
    w.setup_section._problem.set_line(None)
    w.setup_section._fit_run_note()
    assert note.isVisible()


def test_the_run_dialog_is_prefilled_from_where_the_experiments_data_were_read_from(qtbot, bl43_ws, monkeypatch):
    seen = []
    monkeypatch.setattr(RunDialog, "exec", lambda self: seen.append(self._data_edit.text()) or 0)
    w = _win(qtbot, bl43_ws)
    w.run_analysis()                                        # a first run of this experiment: no earlier run to copy
    folders = w.scan_list_vm.source_folders()
    assert folders and seen == [os.pathsep.join(folders)]
    assert Path(folders[0]).name == "incoming" and Path(folders[0]).is_dir()    # where the files were read from


def test_the_run_dialog_with_nothing_to_run_on_is_off_and_says_why(qtbot):
    dlg = RunDialog(data="")
    qtbot.addWidget(dlg)
    dlg.show()
    assert not dlg._run_btn.isEnabled() and dlg._reason.isVisible() and dlg._reason.text() == NO_DATA_REASON
    assert dlg.data_paths == []
    dlg._data_edit.setText("D:\\data")
    assert dlg._run_btn.isEnabled() and not dlg._reason.isVisible() and dlg.data_paths == ["D:\\data"]
    dlg._data_edit.setText(os.pathsep.join(["D:\\a", "D:\\b"]))
    assert dlg.data_paths == ["D:\\a", "D:\\b"]
