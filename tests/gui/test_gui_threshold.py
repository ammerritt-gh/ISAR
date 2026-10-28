"""The GUI threshold, batch 1: a control says what it is and a greyed one says why, in text on the screen.

Offscreen Qt, the real widgets. These are the checks that are not already part of a pane's own test file:
the window's menus and status bar, the Run dialog, the Processing panel, the Campaign Start and the strips.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.run_dialog import RunDialog
from isar_gui.session import Session
from isar_gui.views.experiment_strip import ExperimentStrip
from isar_gui.views.processing_panel import ProcessingPanel


def _win(qtbot, make_ws=None):
    if make_ws is None:
        w = MainWindow(None, session=Session(), watch=False)
    else:
        root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
        w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    return w


def test_a_greyed_run_menu_entry_says_why_in_its_own_text(qtbot, make_ws):
    w = _win(qtbot)                                          # no experiment
    assert not w._rerun_action.isEnabled()
    assert w._rerun_action.text() == "Re-run (only what changed) — needs an experiment"
    w = _win(qtbot, make_ws)                                 # an experiment, but no run yet
    assert w._rerun_action.text() == "Re-run (only what changed) — run the analysis once first"
    assert "new, changed or held" in w._rerun_action.toolTip()


def test_the_status_bar_says_tavi_is_not_running_and_keeps_the_exact_error_on_hover(qtbot, make_ws):
    w = _win(qtbot, make_ws)
    w._apply_health({"llm": (True, True, "ok", "http://llm"),
                     "tavi": (False, False, "unreachable (URLError)", "http://127.0.0.1:8642/api/v1/health")})
    assert w.statusBar().currentMessage() == "TAVI (instrument control) is not running. Only needed for campaigns."
    assert "URLError" in w.statusBar().toolTip() and "8642" in w.statusBar().toolTip()
    assert w._tavi_health.text() == "TAVI (campaigns) ●" and "red = not reachable" in w._tavi_health.toolTip()


def test_the_file_and_backend_menus_name_what_they_open(qtbot, make_ws):
    w = _win(qtbot, make_ws)
    names = {a.text() for menu in w.menuBar().findChildren(type(w._view_menu)) for a in menu.actions()}
    assert {"Open HTML report", "Save experiment copy…", "Save view as image", "Launch TAVI (instrument control)"} <= names
    assert w._launch_tavi_action.toolTip() == "Only needed for campaigns that measure."


def test_the_processing_panel_says_why_stop_is_off(qtbot):
    panel = ProcessingPanel()
    qtbot.addWidget(panel)
    panel.show()
    panel.set_state(can_run=True, run_reason="", analysis_running=False, activity="", last_run=None)
    assert panel._stop.text() == "Stop analysis" and not panel._stop.isEnabled()
    assert panel._stop_reason.isVisible() and panel._stop_reason.text() == "No analysis is running."
    panel.set_state(can_run=False, run_reason="an analysis is running", analysis_running=True, activity="", last_run=None)
    assert panel._stop.isEnabled() and not panel._stop_reason.isVisible()


def test_the_experiment_strip_names_its_buttons_and_shows_why_rename_is_off(qtbot, tmp_path):
    strip = ExperimentStrip()
    qtbot.addWidget(strip)
    strip.show()
    strip.set_experiment(tmp_path / "run1")
    assert strip._open.full_text() == "Open experiment…" and strip._rename.full_text() == "Rename…"
    assert strip._details.full_text() == "Experiment info…"
    for button in (strip._new, strip._open, strip._rename, strip._details, strip._recent):
        assert button.toolTip(), button.objectName()                  # every control says what it affects
    strip.set_busy("files are being read into the experiment")
    assert not strip._rename.isEnabled()
    assert strip._busy.isVisible() and strip._busy.text() == "files are being read into the experiment"


def test_the_run_dialog_labels_name_their_object_and_carry_a_sentence_each(qtbot):
    dlg = RunDialog(data="D:\\data")
    qtbot.addWidget(dlg)
    labels = {b.text() for b in dlg.findChildren(QPushButton)}
    assert {"Run", "Cancel"} <= labels
    assert dlg._errors.toolTip() and all(dlg._errors.itemData(i, 3) for i in range(dlg._errors.count()))
    assert dlg._M.toolTip() and dlg._detectors.toolTip() and dlg._llm.toolTip() and dlg._force.toolTip()
    assert dlg._llm.text() == "Ask the LLM to review QC flags (optional)"
    assert dlg._force.text() == "Refit every scan, even unchanged ones"
    QApplication.processEvents()
