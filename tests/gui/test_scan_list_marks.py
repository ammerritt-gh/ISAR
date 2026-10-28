"""The scan list's queue marks and coverage counts (S2U3), offscreen Qt with the real widgets.

* a queue mark (*queued*, *running*, *saving*) replaces a pass's stored word in its cell, the stored word moves
  to the hover, and a set's tone takes live work;
* the coverage count ("3/4") is the number of spectra holding a current stored result, read from the intake's
  C-current map, so it agrees with the out-of-date mark and the duplicate rule;
* a queue update rebuilds the list in place, never moving the selection or the expansion, and does not loop back
  through a controller that listens to the list.

The controller is a stand-in with ``changed`` and ``marks()``; the intake and the workspace are real. Every save
goes through the real intake writers into a private copy of the generated workspace (hard rule 10).
"""
from __future__ import annotations

import shutil

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

import bl43_fixture
import bl43_intake_fixture
from isar.session.file_fit import FileItem, fit_file
from isar.session.writer import save_results
from isar.workspace_api import Workspace
from isar_gui import status_words
from isar_gui.theme import Tone
from isar_gui.viewmodels.intake import IntakeState
from isar_gui.viewmodels.scan_list import COLUMNS, ScanListViewModel
from isar_gui.views.scan_list import _KEY_ROLE, _TONE_ROLE, ScanListPanel

WAIT_MS = 15000
A, B, S = bl43_fixture.KEY_A, bl43_fixture.KEY_B, bl43_fixture.KEY_SINGLE
A03, A04, A05, A06 = (f"{A}:a{n:02d}" for n in (3, 4, 5, 6))


class FakeQueue(QObject):
    """What the scan list reads of the controller: ``marks()`` and ``changed``."""

    changed = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.shown: dict = {}

    def marks(self):
        return self.shown

    def set_marks(self, marks) -> None:
        self.shown = marks
        self.changed.emit()


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    """The generated workspace with a real fast pass saved for a03, a04 and a06: C-current counts stored results
    only when a fit made them by the engine (the stamped records of the fixture are not current)."""
    root = bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("marks"))
    for unit in (A03, A04, A06):
        out = []
        fit_file(root, FileItem("fit", unit, "fast", 1), progress=None, emit=out.append)
        results = [r.payload for r in out if r.status not in ("attempt", "cores") and r.payload is not None]
        assert save_results(root, results) == [None] * len(results)
    return root


@pytest.fixture
def ws_root(built, tmp_path):
    """A private copy (its manifest and curve check still name the original ``incoming/``)."""
    copy = tmp_path / "marks"
    shutil.copytree(built.parent, copy)
    return copy / built.name


def _list(qtbot, root):
    """The scan list over a view model fed by a real IntakeState, as the window wires it; bound and read."""
    intake = IntakeState()
    vm = ScanListViewModel(Workspace.open(root))
    vm.set_intake(intake)
    intake.changed.connect(lambda: vm.on_workspace_changed(None))
    intake.worker_changed.connect(lambda: vm.on_workspace_changed(None))
    panel = ScanListPanel(vm)
    qtbot.addWidget(panel)
    panel.resize(560, 620)
    panel.show()
    vm.set_column_visible("fast", True)
    vm.set_column_visible("deep", True)
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.bind(root)
    QApplication.processEvents()
    return intake, vm, panel


def _cell(panel, node_id, column):
    first = panel._by_id[node_id]
    parent = first.parent() or panel._model.invisibleRootItem()
    return parent.child(first.row(), COLUMNS.index(column))


def test_queue_marks_replace_the_stored_word_and_keep_it_on_hover(qtbot, ws_root):
    _intake, vm, panel = _list(qtbot, ws_root)
    queue = FakeQueue()
    vm.set_queue(queue)
    queue.set_marks({A03: {"fast": "running"}, A06: {"deep": "queued"}, A05: {"fast": "saving"}})
    stored = vm.snapshot.spectrum(A03).fast
    stored_word = status_words.plain_word("fast", stored.word, stored.reason, stored.provisional)
    running = _cell(panel, f"spec:{A03}", "fast")
    assert running.text() == "running" and running.data(_TONE_ROLE) == Tone.ACTIVE
    assert "ISAR is fitting this analyzer file now" in running.toolTip()
    assert f"Stored fast pass: {stored_word}" in running.toolTip()        # the stored word moves to the hover
    queued = _cell(panel, f"spec:{A06}", "deep")
    assert queued.text() == "queued" and queued.data(_TONE_ROLE) == Tone.IDLE
    assert "Stored deep pass: not run" in queued.toolTip()
    saving = _cell(panel, f"spec:{A05}", "fast")
    assert saving.text() == "saving" and saving.data(_TONE_ROLE) == Tone.ACTIVE
    assert "Stored fast pass: not run" in saving.toolTip()
    queue.set_marks({f"{S}:a06": {"fast": "running"}})                    # a one-analyzer set, never fitted
    assert _cell(panel, f"set:{S}", "fast").data(_TONE_ROLE) == Tone.ACTIVE  # live work is not a settled status
    queue.set_marks({})
    assert _cell(panel, f"spec:{A03}", "fast").text() == stored_word     # the stored word returns
    assert _cell(panel, f"spec:{A05}", "fast").text() == "not run"


def test_coverage_counts_current_results_and_drops_when_their_inputs_change(qtbot, ws_root):
    intake, vm, panel = _list(qtbot, ws_root)
    vm.set_queue(FakeQueue())
    before = _cell(panel, f"set:{A}", "fast")
    assert before.text() == "3/4" and "3 of 4 analyzers have a current fast-pass result" in before.toolTip()
    assert _cell(panel, f"set:{B}", "fast").text() == "0/4"
    vm.set_sort("analyzer")                       # the analyzer flip: a03 over both scan sets
    assert _cell(panel, "ana:a03", "fast").text() == "1/2"
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        assert intake.save_value("temperature", [A], 100, None).ok
    QApplication.processEvents()
    assert _cell(panel, "ana:a03", "fast").text() == "0/2"   # the fit in set A now holds changed inputs
    vm.set_sort("set")
    after = _cell(panel, f"set:{A}", "fast")
    assert after.text() == "0/4"                  # the three fits now hold changed inputs: none is current
    assert "3 of 3 fast-pass results have changed inputs" in after.toolTip()


def test_coverage_says_checking_before_the_first_read(qtbot, ws_root):
    vm = ScanListViewModel(Workspace.open(ws_root))
    vm.set_intake(IntakeState())                  # not bound: no current map yet
    panel = ScanListPanel(vm)
    qtbot.addWidget(panel)
    panel.show()
    vm.on_workspace_changed(None)
    cell = _cell(panel, f"set:{A}", "fast")
    assert cell.text() == "…" and cell.toolTip() == "checking which results are current"


def test_a_queue_update_rebuilds_once_and_keeps_selection_and_expansion(qtbot, ws_root):
    _intake, vm, panel = _list(qtbot, ws_root)
    queue = FakeQueue()
    vm.set_queue(queue)
    vm.snapshot_changed.connect(queue.changed)    # the controller listens to the list too: no loop may form
    rebuilds, shown = [], []
    vm.snapshot_changed.connect(lambda: rebuilds.append(1))
    vm.display_requested.connect(shown.append)
    index = panel._model.indexFromItem(panel._by_id[f"set:{A}"])
    panel._view.expand(index)
    vm.select(A04)
    shown.clear()
    rebuilds.clear()
    queue.set_marks({A03: {"fast": "running"}})
    assert len(rebuilds) == 1 and shown == []
    assert vm.current_key == A04 and panel._view.isExpanded(index)
    assert panel._view.currentIndex().siblingAtColumn(0).data(_KEY_ROLE) == A04
    queue.set_marks({A03: {"fast": "running"}})   # the same marks again: nothing to draw
    assert len(rebuilds) == 1
