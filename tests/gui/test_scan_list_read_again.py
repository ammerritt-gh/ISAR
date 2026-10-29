"""Scan-list ``Read source file again`` on the real window (offscreen Qt). Ingest is stubbed at the coordinator's
``request_ingest``, so nothing here runs ``isar``; the failure path is driven through the coordinator's own
``ingest_started`` and ``ingest_failed`` signals.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from gui.test_scan_list_hide import _menu, _window
from scans_view_fixture import ONE_MODE

READ_AGAIN, REMOVE = "Read source file again", "Remove from list"


def test_read_source_file_again_sends_the_analyzers_own_file(qtbot, monkeypatch, scans_view_ws):
    window = _window(qtbot, monkeypatch, scans_view_ws)
    sent = []
    monkeypatch.setattr(window._ingest_controller, "request_ingest", lambda paths: sent.append(list(paths)))
    window.selection.set((ONE_MODE,), clicked=ONE_MODE)
    _menu(window, ONE_MODE, READ_AGAIN)
    own = window.scan_list_vm.snapshot.spectrum(ONE_MODE).file_path
    assert sent == [[own]] and Path(own).name.endswith("_a03")


def test_missing_or_unrecorded_source_says_to_drag_it_again(qtbot, monkeypatch, scans_view_ws, tmp_path):
    window = _window(qtbot, monkeypatch, scans_view_ws)
    sent, lines = [], []
    monkeypatch.setattr(window._ingest_controller, "request_ingest", lambda paths: sent.append(paths))
    monkeypatch.setattr(window, "log", lines.append)
    window.selection.set((ONE_MODE,), clicked=ONE_MODE)

    gone = str(tmp_path / "gone_a03")
    monkeypatch.setattr(window.scan_list_vm, "source_paths", lambda key: (gone,))
    _menu(window, ONE_MODE, READ_AGAIN)
    tail = "Drag the file into the window to add it again."
    message = f"Could not read gone_a03 again (the file is no longer at {gone}). {tail}"
    assert sent == [] and window.statusBar().currentMessage() == message and lines[-1] == message

    monkeypatch.setattr(window.scan_list_vm, "source_paths", lambda key: ())
    _menu(window, ONE_MODE, READ_AGAIN)
    assert sent == [] and window.statusBar().currentMessage().endswith(tail)
    assert "no source file is recorded" in window.statusBar().currentMessage()


def test_ingest_failure_of_the_reread_names_the_file(qtbot, monkeypatch, scans_view_ws):
    window = _window(qtbot, monkeypatch, scans_view_ws)
    sent = []
    monkeypatch.setattr(window._ingest_controller, "request_ingest", lambda paths: sent.append(list(paths)))
    window.selection.set((ONE_MODE,), clicked=ONE_MODE)
    _menu(window, ONE_MODE, READ_AGAIN)
    window._operations.ingest_started.emit(sent[0])                            # the request's own job starts ...
    window._operations.ingest_failed.emit(1, "the file could not be parsed")  # ... and fails
    name = Path(sent[0][0]).name
    assert window.statusBar().currentMessage() == (
        f"Could not read {name} again (the file could not be parsed). "
        "Drag the file into the window to add it again.")


def test_a_hidden_scan_read_again_stays_hidden(qtbot, monkeypatch, scans_view_ws):
    window = _window(qtbot, monkeypatch, scans_view_ws)
    sent = []
    monkeypatch.setattr(window._ingest_controller, "request_ingest", lambda paths: sent.append(list(paths)))
    window.selection.set((ONE_MODE,), clicked=ONE_MODE)
    _menu(window, ONE_MODE, REMOVE)
    window.scan_list._removed_button.click()                                  # Show removed
    window.selection.set((ONE_MODE,), clicked=ONE_MODE)
    _menu(window, ONE_MODE, READ_AGAIN)
    assert len(sent) == 1 and window.scan_list_vm.is_removed(ONE_MODE)
