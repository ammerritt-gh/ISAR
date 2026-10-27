"""A save dialog offered a bare file name starts in the experiment's folder, never in the folder the shared dialog
was last used in. Offscreen Qt; the dialog's exec is replaced, so nothing opens."""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QFileDialog

from isar_gui import file_dialogs
from isar_gui.main_window import MainWindow
from isar_gui.session import Session


@pytest.fixture
def opened(qtbot, bl43_ws, monkeypatch):
    """The window on an experiment, and a list that records where each save dialog opens and with what name."""
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    with qtbot.waitSignal(w.intake.worker_changed, timeout=15000):
        assert w.load_workspace(str(bl43_ws))
    seen = []

    def fake_exec(self):
        seen.append((Path(self.directory().absolutePath()).resolve(), [Path(f).name for f in self.selectedFiles()]))
        return QDialog.DialogCode.Rejected
    monkeypatch.setattr(QFileDialog, "exec", fake_exec)
    return w, seen


def test_a_bare_file_name_opens_in_the_experiments_folder_not_the_last_used_one(opened, tmp_path):
    w, seen = opened
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    file_dialogs.get_save_file_name(w, "Save something", str(elsewhere / "other.txt"))     # last used: elsewhere
    file_dialogs.get_save_file_name(w, "Save a view", "view.png", "PNG image (*.png)")
    assert seen[0][0] == elsewhere.resolve()
    folder, names = seen[1]
    assert folder == Path(w._ws.root).resolve() and names == ["view.png"]


def test_the_png_export_and_the_campaign_log_save_start_in_the_experiments_folder(opened, tmp_path):
    w, seen = opened
    file_dialogs.get_save_file_name(w, "Save something", str(tmp_path / "other.txt"))      # last used: elsewhere
    w._export_view("Inspector")                                      # the File menu's "Save view as image"
    w.campaign_dock._save_feed()                                     # the campaign log's "Save log…"
    root = Path(w._ws.root).resolve()
    assert [s[0] for s in seen[1:]] == [root, root]
    assert seen[2][1] == ["campaign-log.txt"] and seen[1][1][0].endswith(".png")


def test_an_absolute_default_is_left_alone_and_no_experiment_changes_nothing(qtbot, monkeypatch, tmp_path):
    seen = []
    monkeypatch.setattr(QFileDialog, "exec", lambda self: seen.append(Path(self.directory().absolutePath()).resolve())
                        or QDialog.DialogCode.Rejected)
    w = MainWindow(None, session=Session(), watch=False)             # no experiment: there is no folder to start in
    qtbot.addWidget(w)
    assert w.default_save_folder() is None
    file_dialogs.get_save_file_name(w, "Save", str(tmp_path / "named.txt"))
    assert seen == [tmp_path.resolve()]
