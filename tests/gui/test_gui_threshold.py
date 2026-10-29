"""The GUI threshold, batch 1: a control says what it is and a greyed one says why, in text on the screen.

Offscreen Qt, the real widgets. These are the checks that are not already part of a pane's own test file:
the window's menus and status bar, the Processing panel, the Campaign Start and the strips.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from gui.inter_helpers import assert_described
from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.views.experiment_strip import ExperimentStrip


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
    assert not w._run_action.isEnabled()
    assert w._run_action.text() == "Fast-pass all ready — Open or create an experiment first"
    assert w._run_action.toolTip() == "Open or create an experiment first."
    assert not hasattr(w, "_rerun_action")                   # no second, batch entry beside the queue


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


def test_the_greyed_entry_point_says_why_beside_the_button_and_on_hover(qtbot, make_ws):
    w = _win(qtbot)
    panel = w.processing_panel
    assert not panel._run.isEnabled() and panel._reason.isVisibleTo(panel)
    assert panel._reason.text() == "Open or create an experiment first." == panel._run.toolTip()
    panel.set_run_state(True, "Fast-pass all ready (4)", "")
    assert panel._run.isEnabled() and panel._run.full_text() == "Fast-pass all ready (4)" and panel._reason.isHidden()
    assert "fast pass" in panel._run.toolTip()


def test_the_inter_spectra_starts_say_why_they_are_greyed_in_their_own_text(qtbot):
    w = _win(qtbot)                                          # no experiment
    box = w.processing_panel.inter_box
    assert not box._start_anchored.isEnabled() and not box._start_simultaneous.isEnabled()
    assert box._start_anchored.toolTip() == "Open an experiment first." == box._anchored_reason.text()
    assert not box._anchored_reason.isHidden()
    assert_described(box)                                    # every control in the box names itself and says what it does


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
