"""NewExperimentDialog logic + MainWindow drag-drop routing (no real subprocess).

The actual ``isar init`` subprocess is exercised in the end-to-end walkthrough; here we cover the
dialog's detection/enablement/command logic and the window's drop routing with a fake detector.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QMimeData, QUrl

from isar.workspace_api import Workspace
from isar_gui import commands
from isar_gui.new_experiment import NewExperimentDialog


def _supported(instrument="S30"):
    return commands.DetectResult(facility="APS", instrument=instrument, fmt="raw",
                                 plugin_name="s30", probe="probe.dat")


def test_prefill_populates_data_field(qtbot):
    dlg = NewExperimentDialog(prefill=r"D:\beam\numors")
    qtbot.addWidget(dlg)
    assert dlg._data_edit.text() == r"D:\beam\numors"


def test_unrecognized_disables_create(qtbot):
    dlg = NewExperimentDialog(prefill=r"D:\beam\numors")
    qtbot.addWidget(dlg)
    dlg._apply_detect(None)                       # simulate a detect that found nothing
    assert not dlg._create_btn.isEnabled()
    assert "Could not detect" in dlg._detect_label.text()


def test_detection_fills_default_workspace_and_enables_create(qtbot):
    dlg = NewExperimentDialog(prefill=r"D:\beam\numors")
    qtbot.addWidget(dlg)
    dlg._apply_detect(_supported("S30"))
    assert "Detected APS/S30" in dlg._detect_label.text()
    assert dlg._ws_edit.text().endswith("isar_S30_" + _today())
    assert dlg._create_btn.isEnabled()


def test_unsupported_instrument_disables_create(qtbot):
    dlg = NewExperimentDialog(prefill=r"D:\beam\numors")
    qtbot.addWidget(dlg)
    dlg._apply_detect(commands.DetectResult("SP8", "BL43", "spec", None, "x"))
    assert not dlg._create_btn.isEnabled()
    assert "no card template" in dlg._detect_label.text()


def test_planned_command_builds_init(qtbot):
    dlg = NewExperimentDialog(prefill=r"D:\beam\numors", python="py.exe")
    qtbot.addWidget(dlg)
    dlg._apply_detect(_supported())
    cmd = dlg.planned_command()
    assert cmd[:4] == ["py.exe", "-m", "isar", "init"]
    assert "--out" in cmd


def test_workspace_path_none_until_success(qtbot):
    dlg = NewExperimentDialog()
    qtbot.addWidget(dlg)
    assert dlg.workspace_path is None


def test_init_failure_shows_captured_cause(qtbot):
    dlg = NewExperimentDialog()
    qtbot.addWidget(dlg)
    dlg._pending_out = r"D:\ws\exp1"
    for line in ["scaffolding...", "error: destination is not empty", "aborting"]:
        dlg._on_init_line(line)
    dlg._on_init_finished(2)
    assert dlg.workspace_path is None
    assert "destination is not empty" in dlg._detect_label.text()
    assert "destination is not empty" in dlg._log.toPlainText()
    assert "exit 2" in dlg._detect_label.text()


def test_init_failure_without_cause_falls_back_to_see_log_above(qtbot):
    dlg = NewExperimentDialog()
    qtbot.addWidget(dlg)
    dlg._pending_out = r"D:\ws\exp1"
    for line in ["scaffolding...", "aborting"]:
        dlg._on_init_line(line)
    dlg._on_init_finished(2)
    assert dlg.workspace_path is None
    assert "see log above" in dlg._detect_label.text()
    assert "see log above" in dlg._log.toPlainText()
    assert "exit 2" in dlg._detect_label.text()


def test_init_success_path_unchanged(qtbot):
    dlg = NewExperimentDialog()
    qtbot.addWidget(dlg)
    dlg._pending_out = r"D:\ws\exp1"
    dlg._on_init_line("scaffolding...")
    dlg._on_init_finished(0)
    assert dlg.workspace_path == r"D:\ws\exp1"


# ---- MainWindow drop routing -----------------------------------------------
class _FakeDropEvent:
    def __init__(self, md):
        self._md = md
        self.accepted = False

    def mimeData(self):
        return self._md

    def acceptProposedAction(self):
        self.accepted = True


def _md_for(path: str) -> QMimeData:
    md = QMimeData()
    md.setUrls([QUrl.fromLocalFile(path)])
    return md


def test_drop_existing_workspace_opens_it(qtbot, make_ws, monkeypatch):
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session

    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    (root / "card.policy.toml").write_text("title='x'\n", encoding="utf-8")
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)

    loaded, newexp = [], []
    monkeypatch.setattr(w, "load_workspace", lambda p: loaded.append(p))
    monkeypatch.setattr(w, "open_new_experiment", lambda prefill=None: newexp.append(prefill))
    w.dropEvent(_FakeDropEvent(_md_for(str(root))))
    assert loaded == [str(root)] and newexp == []


def test_drop_raw_data_routes_to_ingest_controller(qtbot, tmp_path, monkeypatch):
    """Phase D: dropping raw data (any workspace state) hands the paths to the IngestController,
    which owns the none->reserve->bootstrap / ready->ingest routing behind its FIFO queue — the
    window no longer touches the manager's lifecycle directly on a drop."""
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session

    data = tmp_path / "numors"
    data.mkdir()
    (data / "scan_1.dat").write_text("#F scan_1\n", encoding="utf-8")
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w._wsm.state() == "none"

    requested, newexp = [], []
    monkeypatch.setattr(w._ingest_controller, "request_ingest", lambda p: requested.append(list(p)))
    monkeypatch.setattr(w, "open_new_experiment", lambda prefill=None: newexp.append(prefill))
    w.dropEvent(_FakeDropEvent(_md_for(str(data))))
    assert requested == [[str(data)]]
    assert newexp == []


def _today():
    from datetime import date
    return f"{date.today():%Y%m%d}"
