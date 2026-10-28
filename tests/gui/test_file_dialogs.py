"""The non-native file dialogs do not freeze the window when used again (offscreen, real dialogs).

A fresh non-native ``QFileDialog`` per call (what the static ``QFileDialog.get…`` calls make) is destroyed
on return, and destroying its file-system model can block the GUI thread for seconds on a slow volume,
so *Open…* and *Recent ▾* lagged and fell out of step. ``isar_gui.file_dialogs`` keeps one per window.
The stall depends on the machine's drives, so the deterministic check is that the one dialog is reused;
the longest gap between two 5 ms ticks of a GUI-thread timer is asserted as well, from the second open on:
the first may pay for a dialog an earlier test's window took down with it.
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QEvent, QTimer
from PySide6.QtWidgets import QApplication, QFileDialog, QWidget

from isar_gui import file_dialogs

STALL_S = 0.5           # the defect froze for 1 s and 5 s; a reused dialog shows in milliseconds


@pytest.fixture(autouse=True)
def _settled(qapp):
    """Pay for earlier tests' teardown before timing: a destroyed window's dialog frees its file-info
    thread through a chain of deferred deletes, which would otherwise land inside this test's dialogs."""
    for _ in range(5):
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()


def _answer(choose):
    """Once a file dialog is shown, run ``choose`` on it (accept or reject); poll on the GUI thread."""
    def poll():
        for widget in QApplication.topLevelWidgets():
            if isinstance(widget, QFileDialog) and widget.isVisible():
                choose(widget)
                return
        QTimer.singleShot(5, poll)
    QTimer.singleShot(0, poll)


def _longest_gap(call) -> float:
    """``call()`` with a 5 ms GUI-thread timer running; the longest gap between its ticks, in s."""
    gaps, last = [0.0], [time.perf_counter()]

    def tick():
        now = time.perf_counter()
        gaps.append(now - last[0])
        last[0] = now
    timer = QTimer()
    timer.setInterval(5)
    timer.timeout.connect(tick)
    timer.start()
    last[0] = time.perf_counter()
    try:
        call()
    finally:
        timer.stop()
    return max(gaps)


def test_opening_the_folder_dialog_again_does_not_freeze_the_window(qtbot, tmp_path):
    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    (tmp_path / "experiment").mkdir()
    results, gaps = [], []
    for _ in range(3):
        _answer(lambda d: QTimer.singleShot(50, d.reject))
        gaps.append(_longest_gap(lambda: results.append(
            file_dialogs.get_existing_directory(host, "Open experiment", str(tmp_path)))))
    assert results == ["", "", ""]                          # cancelled: nothing chosen
    assert max(gaps[1:]) < STALL_S, f"the GUI thread stalled {max(gaps[1:]):.2f} s (per open: {gaps})"
    assert len(host.findChildren(QFileDialog)) == 1         # the one dialog, reused


def test_open_cancelled_then_chosen_from_a_fresh_window_keeps_the_strip_in_step(qtbot, monkeypatch, make_ws):
    """The operator's sequence on a fresh window: *Open…* cancelled twice, then a folder chosen. No
    stall, the strip stays enabled, and each chosen folder opens once."""
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session

    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    ws = make_ws()
    opened = []
    real_load = MainWindow.load_workspace
    monkeypatch.setattr(MainWindow, "load_workspace", lambda self, p: opened.append(p) or real_load(self, p))
    strip = w.experiment_strip
    gaps = []
    for _ in range(2):
        _answer(lambda d: QTimer.singleShot(50, d.reject))
        gaps.append(_longest_gap(strip._open.click))
        assert strip._open.isEnabled() and strip._recent.isEnabled()
    assert opened == []                                      # a cancelled dialog opens nothing
    assert len(w.findChildren(QFileDialog)) == 1             # the one dialog, kept between the two

    def choose(dialog):
        dialog.selectFile(str(ws))
        QTimer.singleShot(0, dialog.accept)
    _answer(choose)
    strip._open.click()
    assert [Path(p) for p in opened] == [ws]
    assert w._wsm.root() == ws
    assert max(gaps[1:]) < STALL_S, f"the GUI thread stalled {max(gaps[1:]):.2f} s (per open: {gaps})"


def test_the_reused_dialog_returns_what_was_chosen_each_time(qtbot, tmp_path):
    host = QWidget()
    qtbot.addWidget(host)
    host.show()
    folder = tmp_path / "experiment"
    folder.mkdir()

    def choose(name):
        def run(dialog):
            dialog.selectFile(name)
            QTimer.singleShot(0, dialog.accept)
        return run

    _answer(choose(str(folder)))
    assert Path(file_dialogs.get_existing_directory(host, "Open experiment", str(tmp_path))) == folder
    _answer(choose("copy"))
    path, _filter = file_dialogs.get_save_file_name(host, "Save copy as", str(tmp_path / "copy"), "",
                                                    QFileDialog.Option.DontConfirmOverwrite)
    assert Path(path) == tmp_path / "copy"
    _answer(lambda d: QTimer.singleShot(0, d.reject))
    assert file_dialogs.get_existing_directory(host, "Open experiment", str(tmp_path)) == ""
