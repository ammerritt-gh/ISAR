"""Stage two's newcomer walkthrough: four scenarios through the real ``MainWindow``, offscreen.

Generated data only (hard rule 10): the S2 fixture (``tests/bl43_intake_fixture.py``) is built once per
module in its variants, and each scenario works on a private copy. Every number a scenario asserts is
the backend's own read (``Workspace.fact_rows``, ``fit_inputs``, ``needs_attention``, ``import_summary``)
or a widget's text compared with it, never a hand-computed count.

1. A value is entered from the attention list, then superseded from the inspector: the file's value
   stays, both values stay on record, the gap and its fallback close, no data file changes.
2. A changed input marks the old fit, and the export records the inputs that fit used.
3. Files that all parse but have no resolution curves: the setup section and the readiness line say
   so, and *Locate…* to a folder of curves changes the counts as the re-read says.
4. The laptop's hardest combination with the attention list open: the new controls are inside the
   window and apart, and the display, the scan list and its rows stay usable. Offscreen Qt has no
   fonts, so this asserts mapped rectangles, not text.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import shutil
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QCheckBox, QFileDialog, QLabel, QPushButton

import bl43_fixture
import bl43_intake_fixture
from gui.window_frame_checks import assert_frame_geometry, rect_in
from isar.intake import store
from isar.report.export import export_workspace
from isar.workspace_api import Workspace, import_summary, needs_attention
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.theme import TONE_COLOR, Tone
from isar_gui.viewmodels.scan_list import COLUMNS
from isar_gui.views.attention_list import _BODY_MAX, USER_TEMPERATURE
from isar_gui.views.fact_table import FactTable, _Cell
from isar_gui.views.value_entry import SetValueDialog
from intake_fixtures import write_library

REPO = Path(__file__).resolve().parents[2]
LAPTOP = (1108, 851)
WAIT_MS = 15000
WARN = TONE_COLOR[Tone.WARN]
A, B = bl43_intake_fixture.KEYS_NO_T            # the variant without a temperature anywhere
A_T, B_T = bl43_fixture.KEY_A, bl43_fixture.KEY_B   # the standard one: 97 K from the headers
A03, A03_T = f"{A}:a03", f"{A_T}:a03"
DETECTORS = ("a03", "a04", "a06")               # the fast-passed analyzers of set A (the fixture's FITTED)


# ---- the fixtures: built once per module, copied per scenario ----------------------------------------------
def _build(tmp_path_factory, name, **kw):
    return bl43_intake_fixture.build_intake_workspace(tmp_path_factory.mktemp(name), **kw)


@pytest.fixture(scope="module")
def built_no_t(tmp_path_factory):
    return _build(tmp_path_factory, "wt1", with_t=False)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    return _build(tmp_path_factory, "wt2")


@pytest.fixture(scope="module")
def built_no_curves(tmp_path_factory):
    return _build(tmp_path_factory, "wt3", curves=False)


@pytest.fixture(scope="module")
def built_hard(tmp_path_factory):
    """The laptop's worst data: 28 analyzers, long set names, no curves, no temperatures."""
    return _build(tmp_path_factory, "wt4", n_analyzers=28, curves=False, with_t=False)


@pytest.fixture(scope="module")
def built_hard_curves(tmp_path_factory):
    """The same, with resolution curves found (no problem line, but the other setup lines)."""
    return _build(tmp_path_factory, "wt5", n_analyzers=28, with_t=False)


def _copy(built_root: Path, tmp_path: Path) -> Path:
    """A private copy (its manifest and curve check still name the original ``incoming/``)."""
    copy = tmp_path / "wt"
    shutil.copytree(built_root.parent, copy)
    return copy / built_root.name


def _data_digest(built_root: Path) -> dict[str, str]:
    """The sha256 of every data file the workspace was ingested from (the originals are never copied)."""
    incoming = built_root.parent / "incoming"
    return {str(p.relative_to(incoming)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(incoming.rglob("*")) if p.is_file()}


# ---- driving the real window ------------------------------------------------------------------------------
def _window(qtbot, monkeypatch, root: Path, size=LAPTOP) -> MainWindow:
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
        assert w.load_workspace(str(root))
    QApplication.processEvents()
    return w


def _after_save(qtbot, w: MainWindow, click) -> None:
    """Run ``click`` (a save from the window) and wait for the re-read the window makes after it."""
    with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
        click()
    QApplication.processEvents()


def _group(w, key):
    return w.attention_list._groups[key]


def _open_group(w, key):
    group = _group(w, key)
    if key not in w.attention_list._open:
        group.toggle.click()
    return _group(w, key)


def _members(w, key) -> list[str]:
    return [r.id for r in _group(w, key).spec.rows]


def _tick(group, member) -> None:
    for box in group.findChildren(QCheckBox):
        if box.property("member") == member:
            box.click()
            return
    raise AssertionError(f"no box for {member}")


def _fact(w, fact: str, column: str) -> str:
    cell = w.scan_inspector._fact_table.findChild(_Cell, f"fact_{fact}_{column}")
    assert cell is not None, f"no {fact}/{column} cell in the inspector"
    return cell.text()


def _set_my_value(w, monkeypatch, fact: str, *texts, reason=None) -> None:
    """Click *Set my value…* in the inspector's fact table and type into the real dialog (no modal loop)."""
    def run(table, dialog):
        assert isinstance(dialog, SetValueDialog)
        for field, text in zip(dialog.entry._fields, texts):
            field.setText(text)
        if reason is not None:
            dialog.entry._reason.setText(reason)
        dialog._save.click()
        return dialog.result()
    monkeypatch.setattr(FactTable, "_run", run)
    button = w.scan_inspector._fact_table.findChild(QPushButton, f"fact_action_set_{fact}")
    assert button is not None and button.isEnabled()
    button.click()


def _value_lines(root, fact="temperature") -> list[dict]:
    """The value entries on record, read through the backend's own store reader."""
    return [e for e in store.read_all(root, store.ENTRIES).entries if e.get("kind") == "value"
            and e.get("fact") == fact]


def _fallbacks(root, scan) -> set:
    return {use.fallback for a in needs_attention(root).scans[scan].analyzers.values()
            for use in a.uses.values() if use.fallback}


def test_the_worktree_is_the_import_root():
    import isar_gui
    print("isar_gui imported from", isar_gui.__file__)
    assert Path(isar_gui.__file__).resolve().is_relative_to(REPO)


# ---- scenario 1: a value is superseded -----------------------------------------------------------------------
def test_scenario1_a_value_is_entered_then_superseded_and_the_files_value_stays(
        qtbot, monkeypatch, built_no_t, tmp_path):
    root = _copy(built_no_t, tmp_path)
    digest = _data_digest(built_no_t)
    w = _window(qtbot, monkeypatch, root)
    before = Workspace(root).fact_rows([A])
    row = next(r for r in before if r.fact == "temperature")
    assert row.file_source == "missing" and row.used_source == "missing" and row.fallback == "bose_t0_limit"
    gap = next(g for g in needs_attention(root).groups if g.kind == "temperature")
    assert A in {i.scan for i in gap.items}                                  # the gap is the backend's
    assert A in _members(w, "temperature")

    # the attention list: tick set A, enter 100 K with a reason, Save
    group = _open_group(w, "temperature")
    _tick(group, A)
    foot = group.foot
    foot.entry._fields[0].setText("100")
    foot.entry._reason.setText("logbook")
    _after_save(qtbot, w, foot.buttons["save"].click)
    assert A not in _members(w, "temperature")                          # the gap closed on the backend's re-read
    assert _members(w, USER_TEMPERATURE) == [A]
    assert [(e["value"], e["reason"], e["targets"]) for e in _value_lines(root)] == [(100.0, "logbook", [A])]

    # the inspector: Set my value… 101 K supersedes it
    w.scan_list_vm.inspect(A03)
    QApplication.processEvents()
    assert (_fact(w, "temperature", "yours"), _fact(w, "temperature", "used")) == ("100 K\nlogbook", "100 K\nyours")
    _set_my_value(w, monkeypatch, "temperature", "101", reason="typo")
    QApplication.processEvents()
    assert _fact(w, "temperature", "files") == "—\nmissing"           # the file's value is untouched: it had none
    assert _fact(w, "temperature", "yours") == "101 K\ntypo"
    assert _fact(w, "temperature", "used") == "101 K\nyours"

    # what the backend holds
    row = next(r for r in Workspace(root).fact_rows([A]) if r.fact == "temperature")
    assert (row.file_value, row.file_source, row.user_value, row.used_value, row.used_source) == (
        None, "missing", 101.0, 101.0, "user")
    assert row.fallback is None and row.user_reason == "typo"
    assert [(e["value"], e["reason"]) for e in _value_lines(root)] == [(100.0, "logbook"), (101.0, "typo")]
    assert _digest_equal(digest, built_no_t)                            # no data file changed, byte for byte
    # readiness: A's temperature gap and its fallback are gone for every use; the others' stay
    assert "bose_t0_limit" not in _fallbacks(root, A)
    assert A not in _members(w, "temperature") and B in _members(w, "temperature")
    assert w.attention_list._header.text() == f"Needs attention · {len(w.intake.attention.groups)}"


def _digest_equal(before: dict, built_root: Path) -> bool:
    return before == _data_digest(built_root)


# ---- scenario 2: a changed input marks the old fit; the export records its inputs -------------------------------
def _scan_cell(w, unit: str, column: str):
    panel = w.scan_list
    first = panel._by_id[f"spec:{unit}"]
    parent = first.parent() or panel._model.invisibleRootItem()
    return parent.child(first.row(), COLUMNS.index(column))


def _marked(w, unit: str, column: str) -> bool:
    return _scan_cell(w, unit, column).foreground().color().name() == WARN


def test_scenario2_a_changed_input_marks_the_fit_and_the_export_records_its_inputs(
        qtbot, monkeypatch, built, tmp_path):
    root = _copy(built, tmp_path)
    w = _window(qtbot, monkeypatch, root)
    for column in ("fast", "deep"):
        w.scan_list_vm.set_column_visible(column, True)
    units = [f"{A_T}:{d}" for d in DETECTORS]
    fits = {u.unit: u for u in Workspace(root).fit_inputs()}
    assert set(fits) == set(units) and all(f.fast.state == "finished" and not f.fast.changes for f in fits.values())
    assert not any(_marked(w, u, "fast") for u in units)                 # fresh fits are current

    # save 100 K on set A (its files say 97 K) from the inspector
    w.scan_list_vm.inspect(A03_T)
    QApplication.processEvents()
    assert _fact(w, "temperature", "files") == "97 K\nheader"
    _after_save(qtbot, w, lambda: _set_my_value(w, monkeypatch, "temperature", "100", reason="thermometer"))

    # the old fits keep their stored word, in the warning colour, with a tooltip naming input, from and to
    fits = {u.unit: u for u in Workspace(root).fit_inputs()}
    for unit in units:
        change, = fits[unit].fast.changes
        assert (change.field, change.used, change.now) == ("temperature", 97.0, 100.0)
        cell = _scan_cell(w, unit, "fast")
        assert cell.text() and _marked(w, unit, "fast")
        assert "temperature: 97 K (file) → 100 K (yours)" in cell.toolTip()
        assert fits[unit].deep is None and not _marked(w, unit, "deep")      # the deep pass is judged on its own
        assert "Inputs changed" not in _scan_cell(w, unit, "deep").toolTip()
    assert not _marked(w, f"{A_T}:a05", "fast")                          # never fitted: no pass, no mark
    assert not any(_marked(w, f"{B_T}:{d}", "fast") for d in ("a03", "a04"))       # another set: untouched

    # the export, in process, records what each fit actually used
    summary = export_workspace(root)
    doc = json.loads((summary.folder / "fits.json").read_text(encoding="utf-8"))
    assert doc["currentness"]["assessed"] is True
    records = {r["spectrum_id"]: r for r in doc["spectra"]}             # keyed by the unit
    for unit in units:
        inputs = records[unit]["inputs"]
        assert inputs["fast"]["current"] is False
        assert inputs["fast"]["used"]["temperature"]["value"] == 97.0
        assert inputs["fast"]["used"]["temperature"]["source"] == "file"
        assert inputs["fast"]["changed"] == [{"input": "temperature", "from": 97.0, "to": 100.0,
                                              "to_source": "user"}]
        assert inputs["deep"] in (None, {"current": None, "used": None, "changed": []})   # judged on its own
    assert records[f"{A_T}:a05"]["inputs"]["fast"] is None               # never fitted: no inputs to record
    modes = (summary.folder / "modes.csv").read_text(encoding="utf-8").splitlines()
    column = modes[0].split(",").index("inputs_current")
    assert {line.split(",")[column] for line in modes[1:]} == {"false"}   # every exported mode row is marked


# ---- scenario 3: files parse, no resolution curves -------------------------------------------------------------
def _blocked_locate(root) -> int:
    """How many analyzers the backend says wait for resolution curves."""
    return sum(a.action == "locate_folder" for s in needs_attention(root).scans.values()
               for a in s.analyzers.values())


def _link(line, prefix) -> str:
    return next(h for h, key in line._links.items() if key.startswith(prefix))


def test_scenario3_files_parse_without_curves_and_locate_changes_the_counts(
        qtbot, monkeypatch, built_no_curves, tmp_path):
    root = _copy(built_no_curves, tmp_path)
    w = _window(qtbot, monkeypatch, root)
    n = import_summary(root).n_analyzers
    m = _blocked_locate(root)
    assert import_summary(root).curves.n_curves == 0 and n > 0 and m == n       # nothing could be matched
    assert w.setup_section._resolution.full_text() == "Resolution curves: not found"
    assert WARN in w.setup_section._resolution.styleSheet()
    assert w.setup_section._problem.full_text() == import_summary(root).curves.problem
    line = w.readiness_line
    assert line.text.startswith(f"{n} spectra read · 0 ready for analysis · {m} need resolution curves")

    # the list behind the count: every spectrum with the backend's reason, and Locate…
    line._label.linkActivated.emit(_link(line, "blocked:locate_folder"))
    popup = line._popup
    assert popup.isVisible() and popup.table.rowCount() == m
    reasons = {a.uses["fit"].reason for s in needs_attention(root).scans.values() for a in s.analyzers.values()
               if a.action == "locate_folder"}
    assert {popup.table.item(i, 1).text() for i in range(m)} == reasons
    locate = popup.findChild(QPushButton, "readiness_locate_folder")
    assert locate is not None and locate.isEnabled()

    # Locate… to a folder of curves for the fixture's analyzers
    curves = tmp_path / "curves"
    write_library(curves, analyzers=bl43_intake_fixture.analyzer_ids(4))
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(curves))
    _after_save(qtbot, w, locate.click)
    assert not popup.isVisible()

    # the counts change as the re-read says (R6): the folder is saved but not checked until the next
    # import or analysis start, every scan waits only for its calibration, and all of them are ready
    summary = import_summary(root)
    assert summary.curves.folder == str(curves.resolve()) and summary.curves.check_stale
    assert _blocked_locate(root) == 0
    assert w.setup_section._resolution.full_text() == f"Resolution curves: {curves.resolve()} (chosen by you)"
    assert w.setup_section._stale.isVisible() and w.setup_section._stale.full_text()
    ready = sum(a.can_start is True for s in needs_attention(root).scans.values() for a in s.analyzers.values())
    assert ready > 0
    assert line.text.startswith(f"{n} spectra read · {ready} ready for analysis")
    assert "need resolution curves" not in line.text
    kinds = [g.kind for g in needs_attention(root).groups]
    assert "calibration_pending" in kinds and "resolution_folder" not in kinds
    assert all(a.uses["fit"].state == "pending" for s in needs_attention(root).scans.values()
               for a in s.analyzers.values() if a.can_start is True)


# ---- scenario 4: the laptop's hardest combination ---------------------------------------------------------------
PLOT_MIN = (240, 250)       # S1's floor for the fit page's plot when Processing is collapsed
DISPLAY_MIN = (300, 250)    # S1's floor for the display when Processing is expanded


def _hardest(qtbot, monkeypatch, root, *, processing_collapsed: bool) -> MainWindow:
    """Inspector open on a fitted analyzer, every optional column on, Processing as asked and the attention
    list open with a value typed on a ticked scan set, over 28 analyzers with long set names and no curves."""
    w = _window(qtbot, monkeypatch, root)
    for column in ("fast", "deep", "slit", "file"):
        w.scan_list._column_actions[column].trigger()
    _after_save(qtbot, w, lambda: w.intake.save_value("temperature", [A], 100.0, "logbook"))   # set A has a value of yours
    w.scan_list_vm.inspect(bl43_intake_fixture.FITTED_NO_T[0])
    w.set_right_panel_collapsed(processing_collapsed)       # opening the inspector collapses it on a laptop
    w.attention_list.set_expanded(True)
    for key in list(w.attention_list._groups):
        _open_group(w, key)
    _tick(_group(w, "temperature"), B)                      # and set B is ticked with a value typed, not saved
    _group(w, "temperature").foot.entry._fields[0].setText("100")
    w.scan_list._view.expandAll()
    qtbot.wait(80)
    return w


def _apart(w, widgets, what: str) -> None:
    """No two of ``widgets`` share a pixel of the window (mapped rectangles)."""
    rects = [(x, rect_in(w, x)) for x in widgets if x.isVisible()]
    for (a, ra), (b, rb) in itertools.combinations(rects, 2):
        assert not ra.intersects(rb), f"{what}: {a.objectName() or a} {ra} overlaps {b.objectName() or b} {rb}"


def _reachable(w, control, scroll=None, *, enabled=True) -> None:
    """``control`` is shown, enabled (or, for ``enabled=False``, shown and meant to be disabled) and wholly
    inside the window, scrolling its own scroll area if need be; ``visibleRegion`` also drops what a
    sibling or a clipping parent hides."""
    if scroll is not None:      # ensureWidgetVisible leaves the last pixels of a control under the edge
        scroll.verticalScrollBar().setValue(max(0, control.mapTo(scroll.widget(), QPoint(0, 0)).y() - 20))
        QApplication.processEvents()
    rect = rect_in(w, control)
    name = control.objectName() or type(control).__name__
    assert control.isVisible() and control.isEnabled() == enabled and rect.width() > 0, name
    assert w.rect().contains(rect), (name, rect)
    assert control.visibleRegion().boundingRect().size() == control.size(), (
        name, "is clipped or covered", control.visibleRegion().boundingRect(), control.size())


def _text_fits(label) -> None:
    """A wrapped label's rectangle is as tall as its text needs at its width (offscreen Qt has no real
    fonts, but the same font serves the hint and the layout, so a squeezed label still shows here)."""
    if isinstance(label, QLabel) and label.wordWrap() and label.width() > 0:
        assert label.height() >= label.heightForWidth(label.width()), (
            label.objectName(), label.height(), label.heightForWidth(label.width()))


def _assert_s2_controls_usable(w) -> None:
    lst, section, inspector = w.attention_list, w.setup_section, w.scan_inspector
    # setup section: the read counts, the curves line, Locate… and the problem line, apart and whole
    setup = (section._summary, section._resolution, section._locate, section._problem)
    _apart(w, setup, "setup section")
    for control in setup:
        _reachable(w, control)
        _text_fits(control)
    # readiness line (in the Processing panel when that is open)
    if w.processing_panel.isVisible():
        _reachable(w, w.readiness_line._label)
        _text_fits(w.readiness_line._label)
        assert w.readiness_line.geometry().bottom() < w.processing_panel._run.geometry().top()
    # attention list: header, the open group's entry and Save (scrolled into view inside the list)
    _reachable(w, lst._header)
    foot = _group(w, "temperature").foot
    assert foot.buttons["save"].isEnabled()                         # a value is typed on a ticked scan set
    parts = [foot.entry._fields[0], foot.entry._reason, foot.buttons["save"]]
    for control in parts:
        _reachable(w, control, lst._scroll)
    _apart(w, parts, "attention foot")
    assert lst.width() <= w.left_column_dock.width() and lst._scroll.height() <= _BODY_MAX
    # inspector: the fact table's actions (scrolled into view inside the inspector), no cell on another
    table = inspector._fact_table
    buttons = table.findChildren(QPushButton)
    assert {b.objectName() for b in buttons} >= {"fact_action_set_temperature", "fact_action_set_slit",
                                                 "fact_action_mark_dead"}
    for button in buttons:      # no slit of yours: only that withdrawal is meant to be disabled
        _reachable(w, button, inspector._scroll, enabled=button.objectName() != "fact_action_use_file_slit")
    leaves = [x for x in table.findChildren(QLabel) if x.isVisible()] + [b for b in buttons if b.isVisible()]
    _apart(w, leaves, "fact table")
    for label in leaves:
        if isinstance(label, QLabel):
            _text_fits(label)
    assert table.width() <= inspector._scroll.viewport().width()


def test_scenario4_laptop_hardest_combination_expanded_processing(qtbot, monkeypatch, built_hard, tmp_path):
    root = _copy(built_hard, tmp_path)
    w = _hardest(qtbot, monkeypatch, root, processing_collapsed=False)
    groups = [g.kind for g in needs_attention(root).groups]
    assert len(groups) >= 5 and set(groups) <= set(w.attention_list._groups)       # many groups, all listed
    assert import_summary(root).n_analyzers >= 28 * 2
    assert_frame_geometry(w, LAPTOP)
    host = w._center_host
    assert host.width() >= DISPLAY_MIN[0] and host.height() >= DISPLAY_MIN[1], (host.width(), host.height())
    _assert_s2_controls_usable(w)
    assert w.processing_panel.isVisible() and not w.scan_inspector.isHidden()


def test_scenario4_laptop_hardest_combination_collapsed_processing_keeps_the_plot(
        qtbot, monkeypatch, built_hard, tmp_path):
    root = _copy(built_hard, tmp_path)
    w = _hardest(qtbot, monkeypatch, root, processing_collapsed=True)
    assert_frame_geometry(w, LAPTOP)
    plot = w._center_host.glw_for("scan")
    assert plot is not None and plot.isVisible()
    assert plot.width() >= PLOT_MIN[0] and plot.height() >= PLOT_MIN[1], (plot.width(), plot.height())
    _assert_s2_controls_usable(w)


def _left_column(qtbot, monkeypatch, built, tmp_path):
    w = _hardest(qtbot, monkeypatch, _copy(built, tmp_path), processing_collapsed=False)
    column = [w.experiment_strip, w.setup_section, w.attention_list, w.files_strip, w.scan_list]
    heights = {m.objectName(): m.height() for m in column}
    print("left column at 1108x851, 28 analyzers:", heights)
    for upper, lower in zip(column, column[1:]):                         # in order, no overlap
        assert rect_in(w, upper).bottom() < rect_in(w, lower).top(), (upper.objectName(), lower.objectName(), heights)
    return w, heights


def test_scenario4_the_scan_list_shows_ten_rows_with_the_attention_list_open(
        qtbot, monkeypatch, built_hard, tmp_path):
    w, _heights = _left_column(qtbot, monkeypatch, built_hard, tmp_path)
    tree = w.scan_list._view
    assert tree.viewport().height() // tree.sizeHintForRow(0) >= 10      # S1's 10-row rule


@pytest.mark.parametrize("fixture", ["built_hard", "built_hard_curves"])
def test_scenario4_the_scan_list_keeps_S1s_400px_minimum_with_the_attention_list_open(
        qtbot, monkeypatch, request, tmp_path, fixture):
    w, heights = _left_column(qtbot, monkeypatch, request.getfixturevalue(fixture), tmp_path)
    assert w.scan_list.height() >= 400, heights                          # S1's minimum
    problem = w.setup_section._problem
    if fixture == "built_hard":                  # no curves: the check's problem is shown, elided, full on hover
        full = w.intake.summary.curves.problem
        assert problem.isVisible() and problem.toolTip() == full and problem.full_text() == full
        assert problem.text().endswith("…") and problem.text() != full
        two_lines = 2 * problem.fontMetrics().lineSpacing() + problem.contentsMargins().top() \
            + problem.contentsMargins().bottom() + 2 * problem.margin()
        assert problem.height() <= two_lines, (problem.height(), two_lines)
    else:
        assert problem.isHidden()
