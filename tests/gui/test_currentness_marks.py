"""Currentness and readiness in the window (S2U4), offscreen Qt with the real widgets.

* the out-of-date mark: a pass whose inputs changed keeps its stored word, drawn in the warning colour with
  a tooltip naming input, from and to; fast and deep judged separately; continuity through the update;
* the readiness line of the Processing panel: its text for constructed readiness values (the backend's
  worked examples R1 and R4) and for the real no-curves workspace (R6), the list behind each count and its
  resolving button, "checking readiness…" before the first result, and its geometry in a narrow panel.

Everything the backend decides is awaited through ``intake.worker_changed``; every save goes through the
real intake writers into a private copy of the generated S2 workspace (hard rule 10).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QPushButton

import bl43_fixture
import bl43_intake_fixture
from isar.intake.point_masks import point_ids, save_selection
from isar.session.unit_data import read_unit_data_for_unit
from isar.workspace_api import (AnalyzerReadiness, AttentionGroup, AttentionItem, ImportSummary, IncompleteSet,
                                NeedsAttention, ScanReadiness, UseState, Workspace)
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.viewmodels.intake import IntakeState
from isar_gui.viewmodels.scan_list import COLUMNS, ScanListViewModel
from isar_gui.views.processing_panel import ProcessingPanel
from isar_gui.views.readiness_line import ReadinessLine
from isar_gui.views.scan_list import _KEY_ROLE, ScanListPanel

REPO = Path(__file__).resolve().parents[2]
WAIT_MS = 15000
A, B = bl43_fixture.KEY_A, bl43_fixture.KEY_B
A03, A04, A05, A06 = (f"{A}:a{n:02d}" for n in (3, 4, 5, 6))
WARN = TONE_COLOR[Tone.WARN]
USES = ("read", "reduce", "calibrate", "fit", "steer")


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("cur"))


@pytest.fixture(scope="module")
def no_curves(tmp_path_factory):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp("curnc"), curves=False)


@pytest.fixture
def ws_root(built, tmp_path):
    """A private copy (its manifest and curve check still name the original ``incoming/``)."""
    copy = tmp_path / "cur"
    shutil.copytree(built.parent, copy)
    return copy / built.name


def test_the_worktree_is_the_import_root():
    import isar_gui
    print("isar_gui imported from", isar_gui.__file__)
    assert Path(isar_gui.__file__).resolve().is_relative_to(REPO)


# ---- the out-of-date mark ----------------------------------------------------------------------------
def _worker(qtbot, intake) -> None:
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        pass


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


def _save(qtbot, intake, value):
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        outcome = intake.save_value("temperature", [A], value, None)
    QApplication.processEvents()
    assert outcome.ok, outcome.message
    return outcome


def _cell(panel, node_id, column):
    first = panel._by_id[node_id]
    parent = first.parent() or panel._model.invisibleRootItem()
    return parent.child(first.row(), COLUMNS.index(column))


def _marked(panel, node_id, column) -> bool:
    return _cell(panel, node_id, column).foreground().color().name() == WARN


def test_a_changed_input_draws_the_stored_word_in_the_warning_colour(qtbot, ws_root):
    intake, vm, panel = _list(qtbot, ws_root)
    assert not any(_marked(panel, f"spec:{u}", "fast") for u in (A03, A04, A06))      # fresh fits are current
    _save(qtbot, intake, 100)
    for unit, word in ((A03, "clear"), (A04, "close call"), (A06, "search limit reached")):
        cell = _cell(panel, f"spec:{unit}", "fast")
        assert cell.text() == f"{word} · stale" and _marked(panel, f"spec:{unit}", "fast")
        assert cell.toolTip().startswith("Inputs changed since this fast pass:")
        assert "temperature: 97 K (file) → 100 K (yours)" in cell.toolTip()
    assert not _marked(panel, f"spec:{A04}", "deep")            # the deep pass never ran on other inputs: not judged
    assert "Inputs changed" not in _cell(panel, f"spec:{A04}", "deep").toolTip()
    assert not _marked(panel, f"spec:{A05}", "fast")            # never fitted: no pass, no mark
    assert not _marked(panel, f"spec:{B}:a03", "fast")          # another set: untouched
    # the set rows carry the mark when any of their passes does
    assert _marked(panel, f"set:{A}", "fast") and not _marked(panel, f"set:{B}", "fast")
    assert "3 of 3 fast-pass results have changed inputs" in _cell(panel, f"set:{A}", "fast").toolTip()
    assert not _marked(panel, f"set:{A}", "deep")


def test_the_mark_leaves_when_the_value_is_the_files_again(qtbot, ws_root):
    intake, _vm, panel = _list(qtbot, ws_root)
    _save(qtbot, intake, 100)
    assert _marked(panel, f"spec:{A03}", "fast")
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        assert intake.use_file_value("temperature", [A], None).ok
    QApplication.processEvents()
    assert not _marked(panel, f"spec:{A03}", "fast") and not _marked(panel, f"set:{A}", "fast")


def test_saving_the_files_own_value_marks_nothing(qtbot, ws_root):
    intake, _vm, panel = _list(qtbot, ws_root)
    _save(qtbot, intake, 97)
    assert not any(_marked(panel, f"spec:{u}", "fast") for u in (A03, A04, A06))
    assert not _marked(panel, f"set:{A}", "fast")


def test_the_mark_arrives_without_moving_selection_or_expansion(qtbot, ws_root):
    intake, vm, panel = _list(qtbot, ws_root)
    index = panel._model.indexFromItem(panel._by_id[f"set:{A}"])
    panel._view.expand(index)
    panel._view.expand(panel._model.indexFromItem(panel._by_id[f"set:{B}"]))
    panel._view.collapse(panel._model.indexFromItem(panel._by_id[f"set:{B}"]))
    vm.select(A04)
    QApplication.processEvents()
    before = (vm.selection.active, set(panel._expanded), panel._view.isExpanded(index),
              panel._view.currentIndex().siblingAtColumn(0).data(_KEY_ROLE))
    shown = []
    vm.selection.changed.connect(shown.append)
    _save(qtbot, intake, 100)
    assert _marked(panel, f"spec:{A04}", "fast")
    after = (vm.selection.active, set(panel._expanded), panel._view.isExpanded(index),
             panel._view.currentIndex().siblingAtColumn(0).data(_KEY_ROLE))
    assert after == before and shown == []                      # the display is never asked to move


def test_the_window_marks_the_list_after_a_save(qtbot, monkeypatch, ws_root):
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: (1108, 851))
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(1108, 851)
    w.show()
    qtbot.waitExposed(w)
    with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
        assert w.load_workspace(str(ws_root))
    w.scan_list_vm.set_column_visible("fast", True)
    with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
        assert w.intake.save_value("temperature", [A], 100, None).ok
    QApplication.processEvents()
    assert _marked(w.scan_list, f"spec:{A03}", "fast")
    assert "97 K (file) → 100 K (yours)" in _cell(w.scan_list, f"spec:{A03}", "fast").toolTip()


def test_a_lapsed_mask_is_named_in_the_stale_tip_until_the_fit_is_redone(qtbot, ws_root):
    """A fit that recorded a mask says so once the data under it change; a refit records only what still applies."""
    exp, n, det = A03.rsplit(":", 2)
    unit = read_unit_data_for_unit(ws_root / "data", exp, int(n), det)
    assert save_selection(ws_root, {A03: [point_ids(unit["x"], unit["counts"], unit["variance"])[-1]]}).ok
    bl43_intake_fixture.write_run(ws_root, [A03])          # the fit now records the mask
    bl43_intake_fixture.reimport_counts(ws_root, A03)      # the point under it is gone
    _intake, _vm, panel = _list(qtbot, ws_root)
    tip = _cell(panel, f"spec:{A03}", "fast").toolTip()
    assert "manual point mask" in tip and "1 of its masks no longer match the current data" in tip
    bl43_intake_fixture.write_run(ws_root, [A03])          # the refit records only the masks that apply
    _intake, _vm, panel = _list(qtbot, ws_root)
    tip = _cell(panel, f"spec:{A03}", "fast").toolTip()
    assert "no longer match" not in tip and not _marked(panel, f"spec:{A03}", "fast")


# ---- the readiness line ------------------------------------------------------------------------------
def _use(state="ready", reason=None, fallback=None) -> UseState:
    return UseState(state, reason, fallback)


def _an(scan, ana, *, can_start=True, action="start_analysis", fit=None, running=False) -> AnalyzerReadiness:
    uses = {u: _use() for u in USES}
    uses["fit"] = fit or _use()
    return AnalyzerReadiness(f"{scan}:{ana}", ana, uses, None if running else can_start, action, running, ())


def _scan(key, *analyzers) -> ScanReadiness:
    return ScanReadiness(key, {a.analyzer: a for a in analyzers}, {u: _use() for u in USES},
                         any(a.can_start for a in analyzers), "start_analysis")


def _att(scans, groups=()) -> NeedsAttention:
    return NeedsAttention(tuple(groups), {s.scan: s for s in scans}, True, None)


def _group(kind, action, scan, analyzer=None) -> AttentionGroup:
    item = AttentionItem(scan, analyzer, None, "r", None, False, False, ())
    return AttentionGroup(kind, kind, kind, ("fit",), "pending", action, True, False, 1, (), (item,))


R1_FALLBACK = _use("fallback", "temperature: zero-temperature (T → 0⁺) Bose limit", "bose_t0_limit")
NO_CURVE = _use("held", "no resolution curve for a04 at slit 60x60")


def _r1() -> NeedsAttention:
    return _att([
        _scan("Smp:1001", _an("Smp:1001", "a03"), _an("Smp:1001", "a04")),     # their Q is missing: group q_missing
        _scan("Smp:1002", _an("Smp:1002", "a03", action="enter_value", fit=R1_FALLBACK),
              _an("Smp:1002", "a04", can_start=False, action="locate_folder", fit=NO_CURVE))],
        [_group("curve_missing", "locate_folder", "Smp:1002", "a04"),
         _group("temperature", "enter_value", "Smp:1002"), _group("q_missing", "none", "Smp:1001", "a03")])


def _fed(qtbot, att, n_analyzers=4):
    """A line over an IntakeState holding the given readiness read (no experiment read from disk)."""
    intake = IntakeState()
    intake._root = Path("constructed")
    intake._summary = ImportSummary(2, n_analyzers, (), (), {}, None, None)
    intake._attention = att
    line = ReadinessLine(intake)
    qtbot.addWidget(line)
    line.show()
    return intake, line


def _link(line, prefix):
    return next(h for h, key in line._links.items() if key.startswith(prefix))


def test_the_line_counts_a_fallback_as_ready_and_a_missing_q_as_no_block(qtbot):
    _intake, line = _fed(qtbot, _r1())
    assert line.text == "4 spectra read · 3 ready for analysis (1 will use a stand-in value) · 1 needs resolution curves"


def test_a_declared_dead_analyzer_is_named_not_blocked_generally(qtbot):
    att = _att([_scan("Smp:1001", _an("Smp:1001", "a03"), _an("Smp:1001", "a04")),
                _scan("Smp:1002", _an("Smp:1002", "a03"),
                      _an("Smp:1002", "a04", can_start=False, action="reverse_declaration",
                          fit=_use("held", "declared dead")))])
    _intake, line = _fed(qtbot, att)
    assert line.text == "4 spectra read · 3 ready for analysis · 1 left out by you"


def test_running_spectra_are_counted_and_zero_counts_are_left_out(qtbot):
    att = _att([_scan("Smp:1001", _an("Smp:1001", "a03", running=True), _an("Smp:1001", "a04", running=True))])
    _intake, line = _fed(qtbot, att, n_analyzers=2)
    assert line.text == "2 spectra read · 0 ready for analysis · 2 running"


def test_no_readiness_read_says_only_what_was_read_and_why(qtbot):
    att = NeedsAttention((), {}, False, "readiness is computed for BL43LXU workspaces only")
    _intake, line = _fed(qtbot, att)
    assert line.text == "4 spectra read"
    assert line._label.toolTip() == "readiness is computed for BL43LXU workspaces only"
    _intake._attention = None
    _intake.changed.emit()
    assert line.text == "4 spectra read · checking readiness…"


def test_an_analyzer_whose_file_never_arrived_is_not_a_readiness_category(qtbot):
    # 3 spectra read; a04 of scan 1002 has no file: the backend lists it, but it is the setup section's
    # incomplete-set count, so no category can exceed "spectra read"
    att = _att([_scan("Smp:1001", _an("Smp:1001", "a03"), _an("Smp:1001", "a04")),
                _scan("Smp:1002", _an("Smp:1002", "a03"),
                      _an("Smp:1002", "a04", can_start=False, action="import_again",
                          fit=_use("held", "a04: no file")))])
    intake, line = _fed(qtbot, att, n_analyzers=3)
    intake._summary = ImportSummary(2, 3, (IncompleteSet("Smp:1002", "set", {"a04": "no file"}, "read", None),),
                                    (), {}, None, None)
    intake.changed.emit()
    assert line.text == "3 spectra read · 3 ready for analysis"
    line._label.linkActivated.emit(_link(line, "ready"))
    assert line._popup.table.rowCount() == 3


def test_the_popup_names_a_scan_set_by_the_lookup_it_was_given(qtbot):
    names = {"Smp:1002": "Warm set"}
    intake = IntakeState()
    intake._root = Path("constructed")
    intake._summary = ImportSummary(2, 4, (), (), {}, None, None)
    intake._attention = _r1()
    line = ReadinessLine(intake, labels=lambda key: names.get(key, key))
    qtbot.addWidget(line)
    line.show()
    line._label.linkActivated.emit(_link(line, "blocked:locate_folder"))
    assert line._popup.table.item(0, 0).text() == "Warm set / a04"
    names["Smp:1002"] = "Renamed"                                  # never cached: read when a list is filled
    intake.worker_changed.emit()
    assert line._popup.table.item(0, 0).text() == "Renamed / a04"


def test_a_failed_worker_read_is_shown_not_checking_forever(qtbot):
    intake, line = _fed(qtbot, None)
    assert line.text == "4 spectra read · checking readiness…"
    intake._error = "worker: ValueError: boom"
    intake.worker_changed.emit()
    assert line.text == "4 spectra read · not checked: worker: ValueError: boom"
    assert line._label.toolTip() == "worker: ValueError: boom"
    assert WARN in line._label.styleSheet()
    intake.changed.emit()                                        # a reload: the worker has not answered yet
    assert line.text == "4 spectra read · checking readiness…" and line._label.styleSheet() == ""


def test_the_line_says_checking_before_the_first_worker_result(qtbot, ws_root):
    intake = IntakeState()
    line = ReadinessLine(intake)
    qtbot.addWidget(line)
    line.show()
    assert line.isHidden() is False and line.text == ""            # nothing open: nothing to say
    intake.bind(ws_root)
    assert line.text.endswith("spectra read · checking readiness…")
    _worker(qtbot, intake)
    assert "checking" not in line.text and "ready for analysis" in line.text


def test_the_real_no_curves_workspace_counts_its_own_spectra(qtbot, no_curves, tmp_path):
    copy = tmp_path / "nc"
    shutil.copytree(no_curves.parent, copy)
    root = copy / no_curves.name
    intake = IntakeState()
    line = ReadinessLine(intake)
    qtbot.addWidget(line)
    line.show()
    with qtbot.waitSignal(intake.worker_changed, timeout=WAIT_MS):
        intake.bind(root)
    n = intake.summary.n_analyzers
    m = sum(a.action == "locate_folder" for s in intake.attention.scans.values() for a in s.analyzers.values())
    assert intake.attention.groups[0].kind == "resolution_folder" and n > 0 and m > 0
    assert line.text.startswith(f"{n} spectra read · 0 ready for analysis · {m} need resolution curves")
    popup = line._popup
    assert not popup.isVisible()
    line._label.linkActivated.emit(_link(line, "blocked:locate_folder"))
    assert popup.isVisible() and popup.table.rowCount() == m
    assert popup.table.item(0, 1).text() and "/" in popup.table.item(0, 0).text()
    locates = []
    line.locate_requested.connect(lambda: locates.append(1))
    popup.findChild(QPushButton, "readiness_locate_folder").click()
    assert locates == [1] and not popup.isVisible()


def test_the_list_behind_a_count_gives_the_backends_reason_and_the_resolving_button(qtbot):
    att = _r1()
    _intake, line = _fed(qtbot, att)
    reveals, runs = [], []
    line.reveal_gap.connect(reveals.append)
    line.run_requested.connect(lambda: runs.append(1))
    line._label.linkActivated.emit(_link(line, "ready"))
    popup = line._popup
    assert popup.isVisible() and popup.table.rowCount() == 3
    reasons = {popup.table.item(i, 1).text() for i in range(3)}
    assert "temperature: zero-temperature (T → 0⁺) Bose limit" in reasons
    assert popup.findChild(QPushButton, "readiness_enter_value") is not None
    popup.findChild(QPushButton, "readiness_start_analysis").click()
    assert runs == [1] and not popup.isVisible()
    line._label.linkActivated.emit(_link(line, "ready"))
    popup.findChild(QPushButton, "readiness_enter_value").click()
    assert reveals == ["temperature"]                          # the group that holds the action for that scan


def test_an_accept_counts_count_reveals_the_counts_group(qtbot):
    att = _att([_scan("Smp:1001", _an("Smp:1001", "a03"),
                      _an("Smp:1001", "a04", can_start=False, action="accept_counts",
                          fit=_use("held", "counts may not be raw")))])
    _intake, line = _fed(qtbot, att, n_analyzers=2)
    assert line.text == "2 spectra read · 1 ready for analysis · 1 needs its counts accepted"
    reveals = []
    line.reveal_gap.connect(reveals.append)
    line._label.linkActivated.emit(_link(line, "blocked:accept_counts"))
    line._popup.findChild(QPushButton, "readiness_accept_counts").click()
    assert reveals == ["counts_unaccepted"]


def test_an_open_list_follows_the_read_and_closes_when_its_spectra_are_gone(qtbot):
    intake, line = _fed(qtbot, _r1())
    line._label.linkActivated.emit(_link(line, "blocked:locate_folder"))
    assert line._popup.isVisible() and line._popup.table.rowCount() == 1
    intake._attention = _att([_scan("Smp:1001", _an("Smp:1001", "a03"))])
    intake.worker_changed.emit()
    assert not line._popup.isVisible()


def test_the_line_fits_a_narrow_processing_panel_without_clipping(qtbot):
    intake, line = _fed(qtbot, _r1())
    scans = [_scan(f"CsV3Sb5_97K_0p5_0p5_3_rep_sum:{n}",
                   *[_an(f"s{n}", f"a{k:02d}", can_start=k % 5 != 0, action="locate_folder" if k % 5 == 0 else "none")
                     for k in range(3, 31)]) for n in range(1, 4)]
    intake._attention = _att(scans)
    intake._summary = ImportSummary(3, 84, (), (), {}, None, None)
    panel = ProcessingPanel()
    qtbot.addWidget(panel)
    panel.set_readiness_widget(line)
    for width in (230, 300, 420):
        panel.resize(width, 500)
        panel.show()
        intake.changed.emit()
        QApplication.processEvents()
        label = line._label
        assert line.geometry().right() < panel.width() and line.geometry().left() >= 0
        assert label.height() >= label.heightForWidth(label.width()) > 0           # every wrapped line is inside
        assert line.geometry().bottom() < panel._run.geometry().top()             # above Run analysis…, no overlap
        assert label.minimumSizeHint().width() == 0                                # never pins the panel's width
