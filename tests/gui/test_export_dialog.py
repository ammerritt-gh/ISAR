"""Real offscreen export controls, persisted choices and the main-window action."""
import json
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QDialog, QMessageBox

from isar_gui.export_dialog import ExportDialog
from isar_gui.session import Session, save_session, load_session


def test_options_persist_globally_destinations_per_experiment(qtbot, tmp_path):
    session = Session()
    root = tmp_path / "experiment"
    dialog = ExportDialog(root, session.view_state)
    qtbot.addWidget(dialog)
    dialog.choices["origin"].setChecked(True)
    dialog.choices["parameters_separate"].setChecked(True)
    dialog.scale.setCurrentIndex(1)
    dialog.sides.setCurrentIndex(1)
    dialog.folder_name.setText("My export")
    dialog.folder_path.setText(str(tmp_path / "results"))
    dialog._save()
    assert dialog.result() == QDialog.DialogCode.Accepted
    dialog.save_preferences(session.view_state)
    saved = tmp_path / "session.json"
    save_session(session, saved)
    loaded = load_session(saved)
    again = ExportDialog(root, loaded.view_state)
    other = ExportDialog(tmp_path / "other", loaded.view_state)
    for widget in (again, other):
        qtbot.addWidget(widget)
        assert widget.choices["origin"].isChecked()
        assert widget.choices["csv"].isChecked()
        assert widget.scale.currentData() == "normalised"
        assert widget.sides.currentData() is True
    assert again.folder_name.text() == "My export"
    assert again.folder_path.text() == str(tmp_path / "results")
    assert other.folder_name.text().startswith("other-export-")
    assert other.folder_path.text() == str(tmp_path / "other/report/exports")


def test_cancel_overwrite_no_and_choose_elsewhere(qtbot, tmp_path, monkeypatch):
    from isar_gui import file_dialogs
    target = tmp_path / "existing"
    target.mkdir()
    (target / "export.json").write_text(json.dumps({"schema": "isar.export/2"}), encoding="utf-8", newline="\n")
    dialog = ExportDialog(tmp_path / "experiment", {})
    qtbot.addWidget(dialog)
    dialog.folder_name.setText("existing")
    monkeypatch.setattr(file_dialogs, "get_existing_directory", lambda *a: str(tmp_path))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    dialog._elsewhere()
    assert dialog.request is None
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    dialog._save()
    assert dialog.request["overwrite"] is True
    assert Path(dialog.request["destination"]) == target
    cancelled = ExportDialog(tmp_path / "experiment", {})
    qtbot.addWidget(cancelled)
    cancelled.reject()
    state = {}
    cancelled.save_preferences(state)
    assert state == {}


def test_main_window_opens_dialog_before_holding_writer(qtbot, make_ws, monkeypatch):
    from isar.workspace_api import Workspace
    from isar_gui.main_window import MainWindow
    root = make_ws(rows=[{"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
                          "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 4.0}])
    window = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(window)
    captured = []
    monkeypatch.setattr(window._export, "start", lambda root, request: captured.append(request) or True)
    monkeypatch.setattr(window._queue, "hold_writes", lambda then: then())
    original_exec = ExportDialog.exec

    def accept(dialog):
        assert not window._export_hold
        QTimer.singleShot(0, dialog._save)
        return original_exec(dialog)

    monkeypatch.setattr(ExportDialog, "exec", accept)
    window._on_export_requested()
    assert len(captured) == 1 and captured[0]["options"]["spectra"] is True
    assert window._session.view_state["export_options"]["curves"] is True
    window._release_export_hold()
    window.close()
