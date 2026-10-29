"""The BL43 gpi files in the window (S1U4): one non-modal notice per rejected file, the gpi folder chooser, the words.

Offscreen Qt with the real widgets over generated data (``tests/intake_fixtures.py``); every number here is generated
(hard rule 10). The notice is built and open()ed, never exec()ed, and the exec guard makes a regression fail, not hang.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

import intake_fixtures as fx
from isar.intake import settings
from isar.intake.gpi import check_gpi
from isar.session import ingest
from isar.session.workspace import Workspace
from isar_gui import plain_words
from isar_gui.main_window import MainWindow
from isar_gui.session import Session

REPO = Path(__file__).resolve().parents[2]
LAPTOP = (1108, 851)
WAIT_MS = 15000
ANALYZERS = tuple(f"a{n:02d}" for n in range(3, 31))          # 28, as a le Tacon set has
Q = {a: 10.0 + 0.5 * i for i, a in enumerate(ANALYZERS)}      # the data files' #Q= values, 1/nm
HKL = {a: (0.5, 0.5, 3.0 + 0.5 * i) for i, a in enumerate(ANALYZERS)}
SET = f"{fx.STEM}_A"
NOTICE = "gpi_notice"


def build(tmp_path, *, stems=("A",), bad=None):
    """``(root, data)``: a BL43 experiment with one generated scan set per stem (its `#Q=` trailers) and each set's
    gpi macro, which carries ``bad`` (analyzer -> wrong |Q|) over the right values. Imported, so the check has run."""
    ws, data = fx.bl43_workspace(tmp_path, stems=stems, analyzers=ANALYZERS)
    for stem in stems:
        name = f"{fx.STEM}_{stem}"
        fx.write_scan(data, stem=name, analyzers=ANALYZERS, seed=1, energy=fx.ENERGY_120, q_nm=Q)
        fx.write_gpi(data, name, q_nm={**Q, **(bad or {})}, hkl=HKL)
    card, session = ws.load()
    ingest.ingest_batch(card, session, [data], workspace_root=ws.root)
    ws.save(card, session)
    return ws.root, data


def _window(qtbot, monkeypatch, root) -> MainWindow:
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: LAPTOP)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*LAPTOP)
    w.show()
    qtbot.waitExposed(w)
    monkeypatch.setattr(QMessageBox, "exec", lambda self, *a, **k: pytest.fail("the gpi notice must never block"))
    with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
        assert w.load_workspace(str(root))
    return w


def _recheck(root, data) -> None:
    """A run's check: the gpi macro is judged again against the analyzer files as they are now."""
    card, _session = Workspace.open(root).load()
    check_gpi(root, card, [data], by="run")


def notices(w) -> list[QMessageBox]:
    return [b for b in w.findChildren(QMessageBox) if b.objectName() == NOTICE]


def test_the_worktree_is_the_import_root():
    import isar_gui
    assert Path(isar_gui.__file__).resolve().is_relative_to(REPO)


def test_an_accepted_file_gets_no_notice(qtbot, monkeypatch, tmp_path):
    root, _data = build(tmp_path)
    w = _window(qtbot, monkeypatch, root)
    assert all(s.accepted for s in w.intake.summary.gpi.scans.values()) and w.intake.summary.gpi.scans
    assert notices(w) == []


def test_one_notice_for_a_bad_file_judged_against_28_analyzers_and_none_on_refresh(qtbot, monkeypatch, tmp_path):
    root, _data = build(tmp_path, bad={"a10": 1.0})
    w = _window(qtbot, monkeypatch, root)
    shown = notices(w)
    assert len(shown) == 1
    assert f"gpi.{SET}" in shown[0].text() and "a10" in shown[0].text()
    assert QApplication.activeModalWidget() is None      # a window-modal notice would block the main window
    w.intake.reload()                                   # a refresh that finds the same result
    assert len(notices(w)) == 1


def test_a_changed_problem_pops_again_and_an_unchanged_one_does_not(qtbot, monkeypatch, tmp_path):
    root, data = build(tmp_path, bad={"a10": 1.0})
    w = _window(qtbot, monkeypatch, root)
    assert len(notices(w)) == 1
    fx.write_gpi(data, SET, q_nm={**Q, "a10": 1.0, "a11": 2.0}, hkl=HKL)    # a second analyzer now disagrees too
    _recheck(root, data)
    w.intake.reload()
    assert len(notices(w)) == 2
    w.intake.reload()
    assert len(notices(w)) == 2


def test_files_rejected_together_share_one_notice(qtbot, monkeypatch, tmp_path):
    root, _data = build(tmp_path, stems=("A", "B"), bad={"a10": 1.0})
    w = _window(qtbot, monkeypatch, root)
    shown = notices(w)
    assert len(shown) == 1
    assert f"gpi.{fx.STEM}_A" in shown[0].text() and f"gpi.{fx.STEM}_B" in shown[0].text()


def test_the_rejected_group_reads_in_plain_words_with_its_tooltip(qtbot, monkeypatch, tmp_path):
    root, _data = build(tmp_path, bad={"a10": 1.0})
    w = _window(qtbot, monkeypatch, root)
    group = w.attention_list._groups["gpi_rejected"]
    assert group.title.text() == "gpi file not used: 1 scan set"
    assert "nothing from it is used" in group.title.toolTip()
    assert "fix the gpi file" in group.quiet.text()
    assert plain_words.source_words("gpi") == "from the gpi file"


def test_choose_gpi_folder_saves_it_as_the_users_and_the_setup_line_says_so(qtbot, monkeypatch, tmp_path):
    root, data = build(tmp_path)
    w = _window(qtbot, monkeypatch, root)
    assert "found beside the data" in w.setup_section._gpi_locate.toolTip()
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(other))
    with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
        w.setup_section._gpi_locate.click()
    saved = settings.current_settings(root).gpi_folder
    assert (saved.source, Path(saved.path)) == ("user", other.resolve())
    assert str(other.resolve()) in w.setup_section._gpi_locate.toolTip()
    # the folder changed since the check: its values are set aside until the data is read again
    assert w.attention_list._groups["gpi_folder_changed"].title.text() == "gpi folder changed: its values are set aside"
