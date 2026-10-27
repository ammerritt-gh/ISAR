"""The scan list panel and its view model's tree, offscreen with the real widgets.

The read is monkeypatched to ``bl43_fixture.sample_snapshot`` unless a test says otherwise; the checks
that can use the real read (a stored fit arriving, a pooled master) take it as soon as it returns
anything, so they cannot pass vacuously once the read's body exists.
"""
from __future__ import annotations

import dataclasses
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

import bl43_fixture
from bl43_fixture import KEY_A, KEY_B, KEY_SINGLE, STEM_A, STEM_B
from isar.workspace_api import Workspace
from isar.workspace_api.scan_list import (Fact, PooledRow, ScanListSnapshot, ScanSetRow, SpectrumRow,
                                          Status, scan_list)
from isar_gui.theme import Tone
from isar_gui.viewmodels.scan_list import COLUMNS, ScanListViewModel
from isar_gui.views.scan_list import _ID_ROLE, _KEY_ROLE, _TONE_ROLE, ScanListPanel

A = {n: f"{KEY_A}:a{n:02d}" for n in (3, 4, 5, 6)}
B = {n: f"{KEY_B}:a{n:02d}" for n in (3, 4, 5, 6)}
HASHES = ("1842762476", "1126149381")


class _Spy:
    def __init__(self, signal):
        self.calls = []
        signal.connect(lambda *a: self.calls.append(a))


def _make(qtbot, monkeypatch, snap, size=(360, 520), ws=None):
    """A shown panel over a view model whose read returns ``box['snap']`` (swap it to simulate an
    update); ``snap=None`` leaves the real read in place."""
    box = {"snap": snap}
    if snap is not None:
        monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda _ws: box["snap"])
    vm = ScanListViewModel(ws if ws is not None else SimpleNamespace())
    panel = ScanListPanel(vm)
    qtbot.addWidget(panel)
    panel.resize(*size)
    panel.show()
    QApplication.processEvents()
    return panel, vm, box


@pytest.fixture
def sample(bl43_ws_readonly):
    return bl43_fixture.sample_snapshot(bl43_ws_readonly)


def _model_item(panel, node_id, column="scan"):
    first = panel._by_id[node_id]
    parent = first.parent() or panel._model.invisibleRootItem()
    return parent.child(first.row(), COLUMNS.index(column))


def _tops(panel):
    root = panel._model.invisibleRootItem()
    return [root.child(r, 0).data(_ID_ROLE) for r in range(root.rowCount())]


def _children(panel, node_id):
    item = panel._by_id[node_id]
    return [item.child(r, 0).data(_ID_ROLE) for r in range(item.rowCount())]


def _index(panel, node_id):
    return panel._model.indexFromItem(panel._by_id[node_id])


def _click(panel, node_id):
    """What a click does: the row becomes the tree's current index."""
    index = _index(panel, node_id)
    parent = index.parent()
    while parent.isValid():
        panel._view.expand(parent)
        parent = parent.parent()
    panel._view.setCurrentIndex(index)


def _current_key(panel):
    index = panel._view.currentIndex()
    return index.siblingAtColumn(0).data(_KEY_ROLE) if index.isValid() else None


def _all_text(panel):
    out = []

    def walk(parent):
        for r in range(parent.rowCount()):
            for c in range(len(COLUMNS)):
                item = parent.child(r, c)
                out.extend((item.text(), item.toolTip()))
            walk(parent.child(r, 0))
    walk(panel._model.invisibleRootItem())
    return out


# ---------------------------------------------------------------------------------------------
# 1. the tree
# ---------------------------------------------------------------------------------------------
def test_tree_is_sets_then_files_not_read_and_hides_the_placeholder_numbers(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    assert _tops(panel) == [f"set:{KEY_SINGLE}", f"set:{KEY_B}", f"set:{KEY_A}", "section:not_read"]
    assert [panel._model.invisibleRootItem().child(r, 0).text() for r in range(4)] == [
        "58", STEM_B, STEM_A, "Files not read"]
    assert len(_children(panel, "section:not_read")) == 3 and "section:pooled" not in panel._by_id
    assert not any(h in text for text in _all_text(panel) for h in HASHES)
    assert _model_item(panel, f"set:{KEY_A}", "parsed").text() == "4/4"
    b_parsed = _model_item(panel, f"set:{KEY_B}", "parsed")
    assert b_parsed.text() == "4/5" and b_parsed.data(_TONE_ROLE) == Tone.ERR
    assert panel._status.text() == "9 spectra"
    for node_id in _children(panel, "section:not_read"):        # a file row spans all columns
        item = panel._by_id[node_id]
        assert panel._view.isFirstColumnSpanned(item.row(), _index(panel, "section:not_read"))
        assert "·" in item.text()


def test_a_long_label_keeps_its_full_text_on_hover_and_the_panel_fits_its_column(qtbot, monkeypatch,
                                                                                  sample):
    long_label = "CsV3Sb5_" + "very_long_scan_set_name_" * 4 + "sum"
    sets = (dataclasses.replace(sample.sets[2], label=long_label), *sample.sets[:2])
    panel, _, _ = _make(qtbot, monkeypatch, dataclasses.replace(sample, sets=sets), size=(280, 500))
    item = panel._by_id[f"set:{KEY_A}"]
    assert item.toolTip().startswith(long_label)
    assert panel.minimumSizeHint().width() <= 280


# ---------------------------------------------------------------------------------------------
# 2./3. columns and missing values
# ---------------------------------------------------------------------------------------------
def test_default_and_optional_columns(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    hidden = {c: panel._view.isColumnHidden(i) for i, c in enumerate(COLUMNS)}
    assert hidden == {"scan": False, "T": False, "Q": False, "parsed": False,
                      "fast": True, "deep": True, "slit": True, "file": True}
    panel._column_actions["fast"].trigger()
    assert not panel._view.isColumnHidden(COLUMNS.index("fast"))
    assert _model_item(panel, f"set:{KEY_A}", "fast").text() == "3/4"
    cell = _model_item(panel, f"spec:{A[4]}", "fast")
    assert cell.text() == "close_call" and not cell.icon().isNull()
    assert cell.data(_TONE_ROLE) == Tone.WARN and "close_call" in cell.toolTip()
    panel._column_actions["fast"].trigger()
    assert panel._view.isColumnHidden(COLUMNS.index("fast"))


def test_a_missing_value_is_a_dash_with_its_reason(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    q = _model_item(panel, f"spec:{A[5]}", "Q")
    assert q.text() == "—" and q.toolTip() == "not in the file"
    assert _model_item(panel, f"spec:{A[5]}", "parsed").text() == "no Q"
    slit = _model_item(panel, f"set:{KEY_A}", "slit")
    assert slit.text() == "—" and slit.toolTip() == "not in the file"
    assert panel._model.horizontalHeaderItem(COLUMNS.index("Q")).text() == "Q"      # mixed units


def test_q_header_carries_a_shared_unit_and_the_cells_drop_it(qtbot, monkeypatch, sample):
    only_bl43 = dataclasses.replace(sample, sets=sample.sets[1:])
    panel, vm, _ = _make(qtbot, monkeypatch, only_bl43)
    assert panel._model.horizontalHeaderItem(COLUMNS.index("Q")).text() == "Q (nm⁻¹)"
    assert _model_item(panel, f"spec:{A[3]}", "Q").text() == "20.3"


# ---------------------------------------------------------------------------------------------
# 4. identity: same-T sets, regroup
# ---------------------------------------------------------------------------------------------
def test_same_temperature_sets_stay_distinct_through_selection_and_the_analyzer_flip(
        qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    display = _Spy(vm.display_requested)
    _click(panel, f"spec:{B[4]}")
    assert display.calls == [(B[4],)]                              # not fitted: the unit key
    _click(panel, f"spec:{A[4]}")
    assert display.calls[-1][0] is sample.spectrum(A[4]).fit       # fitted: its FitView
    _click(panel, f"spec:{B[4]}")
    n = len(display.calls)

    panel._sort_actions["analyzer"].trigger()
    assert vm.sort == "analyzer"
    assert _children(panel, "ana:a04") == [f"spec:{B[4]}", f"spec:{A[4]}"]
    assert [_model_item(panel, i).text() for i in _children(panel, "ana:a04")] == [STEM_B, STEM_A]
    assert vm.current_key == B[4] and _current_key(panel) == B[4]
    assert len(display.calls) == n                                  # the regroup asked for nothing
    assert _model_item(panel, "ana:a04", "Q").text() == "2 scan sets"
    assert _model_item(panel, "ana:a04", "T").text() == ""

    panel._sort_actions["set"].trigger()
    assert _current_key(panel) == B[4] and len(display.calls) == n


def test_temperature_sort_orders_the_sets_and_keeps_the_selection_and_expansion(qtbot, monkeypatch,
                                                                                 sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    _click(panel, f"spec:{A[6]}")
    panel._sort_actions["temperature"].trigger()
    assert _tops(panel)[:3] == [f"set:{KEY_SINGLE}", f"set:{KEY_B}", f"set:{KEY_A}"]   # 26 K, then 97 K
    assert vm.current_key == A[6] and _current_key(panel) == A[6]
    assert panel._view.isExpanded(_index(panel, f"set:{KEY_A}"))


# ---------------------------------------------------------------------------------------------
# 5. filter
# ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("text, shown", [("a05", 2), ("close_call", 1), ("97K", 8), ("26K", 1),
                                         ("a05, 97k", 2), ("a05, 26K", 0), ("", 9)])
def test_filter_counts(qtbot, monkeypatch, sample, text, shown):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    panel._filter.setText(text)
    assert vm.counts() == (shown, 9)
    assert panel._status.text().split(" · ")[0] == (f"{shown} of 9 spectra" if text else "9 spectra")
    assert ("Clear filter" in panel._status.text()) is bool(text)
    panel._filter.setText("")
    assert "Clear filter" not in panel._status.text() and vm.counts() == (9, 9)


def test_filter_keeps_the_summaries_and_shows_the_matches_open(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    panel._filter.setText("a05")
    assert _children(panel, f"set:{KEY_A}") == [f"spec:{A[5]}"]
    assert _model_item(panel, f"set:{KEY_A}", "parsed").text() == "4/4"     # counts do not follow the filter
    assert panel._view.isExpanded(_index(panel, f"set:{KEY_B}"))
    assert "section:not_read" not in panel._by_id                          # no unread file matches "a05"


def test_the_selected_spectrum_outside_the_filter_keeps_the_display(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    _click(panel, f"spec:{B[4]}")
    display, current = _Spy(vm.display_requested), _Spy(vm.current_changed)
    panel._filter.setText("a05")
    assert not panel._notice.isHidden()
    assert panel._notice.text() == f"The selected spectrum (a04, {STEM_B}) is outside the filter"
    assert vm.current_key == B[4] and display.calls == [] and current.calls == []
    panel._status.linkActivated.emit("clear")
    assert panel._notice.isHidden() and _current_key(panel) == B[4]


# ---------------------------------------------------------------------------------------------
# 6. a background update keeps expansion, selection and scroll
# ---------------------------------------------------------------------------------------------
def _real_or_none(ws_root):
    """The real read of a workspace when its body exists (it is empty while the stub stands)."""
    ws = Workspace.open(ws_root)
    return ws if scan_list(ws).sets else None


def test_a_stored_fit_arriving_changes_a_cell_in_place(qtbot, monkeypatch, bl43_ws, sample):
    ws = _real_or_none(bl43_ws)
    if ws is not None:
        panel, vm, box = _make(qtbot, monkeypatch, None, size=(360, 190), ws=ws)
    else:
        panel, vm, box = _make(qtbot, monkeypatch, sample, size=(360, 190))
    panel._column_actions["fast"].trigger()
    for node_id in (f"set:{KEY_A}", f"set:{KEY_B}"):
        panel._view.expand(_index(panel, node_id))
    _click(panel, f"spec:{A[6]}")
    panel._view.verticalScrollBar().setValue(3)
    QApplication.processEvents()
    anchor = panel._scroll_anchor()
    assert anchor is not None
    assert _model_item(panel, f"spec:{B[5]}", "fast").text() == "not run"
    display = _Spy(vm.display_requested)

    if ws is not None:
        bl43_fixture.stamp_fit(bl43_ws, B[5], "resolved")
        ws.refresh()
    else:
        b = list(sample.sets[1].spectra)
        b[2] = dataclasses.replace(b[2], fast=Status("resolved", True),
                                   fit=SimpleNamespace(unit_id=B[5], pooled=False))
        sets = list(sample.sets)
        sets[1] = dataclasses.replace(sets[1], spectra=tuple(b))
        box["snap"] = dataclasses.replace(sample, sets=tuple(sets))
    vm.on_workspace_changed({"snapshot", "results"})
    QApplication.processEvents()

    assert panel._view.isExpanded(_index(panel, f"set:{KEY_A}"))
    assert vm.current_key == A[6] and _current_key(panel) == A[6]
    assert panel._scroll_anchor() == anchor
    assert _model_item(panel, f"spec:{B[5]}", "fast").text() == "resolved"
    assert display.calls == []


def test_a_new_set_arriving_does_not_collapse_or_jump(qtbot, monkeypatch, sample):
    panel, vm, box = _make(qtbot, monkeypatch, dataclasses.replace(sample, sets=sample.sets[1:]),
                           size=(360, 190))
    panel._view.expand(_index(panel, f"set:{KEY_B}"))
    _click(panel, f"spec:{A[6]}")
    panel._view.verticalScrollBar().setValue(2)
    QApplication.processEvents()
    anchor = panel._scroll_anchor()
    box["snap"] = sample                                           # set "58" arrives above everything
    vm.on_workspace_changed({"snapshot"})
    QApplication.processEvents()
    assert _tops(panel)[0] == f"set:{KEY_SINGLE}"
    assert not panel._view.isExpanded(_index(panel, f"set:{KEY_SINGLE}"))   # later sets start closed
    assert panel._view.isExpanded(_index(panel, f"set:{KEY_B}")) and panel._view.isExpanded(
        _index(panel, f"set:{KEY_A}"))
    assert panel._scroll_anchor() == anchor and _current_key(panel) == A[6]


def test_the_first_set_in_an_empty_list_opens_and_the_rest_start_closed(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    opened = [i for i in (f"set:{KEY_SINGLE}", f"set:{KEY_B}", f"set:{KEY_A}")
              if panel._view.isExpanded(_index(panel, i))]
    assert opened == [f"set:{KEY_SINGLE}"]
    assert panel._view.isExpanded(_index(panel, "section:not_read"))


def test_the_active_unit_following_selects_and_reveals_without_asking_for_the_display(
        qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    display = _Spy(vm.display_requested)
    assert not panel._view.isExpanded(_index(panel, f"set:{KEY_A}"))
    vm.set_active_key(A[6])
    assert panel._view.isExpanded(_index(panel, f"set:{KEY_A}")) and _current_key(panel) == A[6]
    vm.set_active_key("no:such:key")                               # unknown: the current one stays
    assert vm.current_key == A[6] and display.calls == []


# ---------------------------------------------------------------------------------------------
# 7. inspect
# ---------------------------------------------------------------------------------------------
def test_double_click_and_the_inspect_button_open_the_inspector(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample, size=(360, 520))
    inspect_spy = _Spy(vm.inspect_requested)
    assert not panel._inspect.isEnabled()
    _click(panel, f"spec:{A[6]}")
    assert panel._inspect.isEnabled()
    rect = panel._view.visualRect(_index(panel, f"spec:{A[6]}"))
    viewport, pos = panel._view.viewport(), rect.center()
    # the sequence a platform delivers: press, release, the double-click, release (the view needs the press)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=pos)
    QTest.mouseDClick(viewport, Qt.MouseButton.LeftButton, pos=pos)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=pos)
    assert inspect_spy.calls == [(A[6],)]
    panel._inspect.click()
    assert inspect_spy.calls == [(A[6],), (A[6],)]
    set_row = _index(panel, f"set:{KEY_A}")
    panel._view.activated.emit(set_row)                            # a set row only toggles open
    assert len(inspect_spy.calls) == 2


def test_inspect_acts_only_on_the_row_the_tree_shows_as_current(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    inspect_spy = _Spy(vm.inspect_requested)
    _click(panel, f"spec:{A[6]}")
    assert panel._inspect.isEnabled()
    _click(panel, f"set:{KEY_A}")                                # the visible current row is a set
    assert not panel._inspect.isEnabled()
    panel._inspect.click()                                         # disabled: nothing opens
    assert inspect_spy.calls == []
    _click(panel, f"spec:{A[6]}")
    assert panel._inspect.isEnabled()


def test_clicking_a_set_row_leaves_the_display_alone(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    _click(panel, f"spec:{A[6]}")
    display = _Spy(vm.display_requested)
    _click(panel, f"set:{KEY_A}")
    _click(panel, "section:not_read")
    assert display.calls == [] and vm.current_key == A[6]


# ---------------------------------------------------------------------------------------------
# 8./9. pooled and empty
# ---------------------------------------------------------------------------------------------
def _pooled_snapshot():
    row = PooledRow(key="tavi:900:det", label="pooled master (2 members)",
                    temperature=Fact(200.0, "K", "fit", "fit"),
                    q=Fact((2.0, 0.0, 0.0), "rlu", "fit", "fit"), n_members=2,
                    fast=Status("not recorded", False, "no fast-pass record"),
                    deep=Status("not run", False))
    sets = tuple(ScanSetRow(
        key=f"tavi:{n}", label=str(n), experiment_id="tavi", scan_numbers=(n,), summed=False,
        n_merged=None, temperature=Fact(200.0, "K", "fit", "fit"),
        slit=Fact(None, None, "missing", "missing", "not in the file"),
        spectra=(SpectrumRow(
            key=f"tavi:{n}:det", set_key=f"tavi:{n}", analyzer="det",
            q=Fact((2.0, 0.0, 0.0), "rlu", "fit", "fit"), parsed=Status("ok", True),
            fast=Status("resolved", True), deep=Status("not run", False), file_name=None,
            file_path=None, n_points=None),), files_received=1) for n in (11, 12))
    return ScanListSnapshot(sets=sets, pooled=(row,))


def test_a_pooled_section_appears_only_with_pooled_masters(qtbot, monkeypatch, tmp_path, sample):
    snap = _pooled_snapshot()
    real = Workspace.open(bl43_fixture.pooled_workspace(tmp_path / "pooled"))
    live = scan_list(real)
    if live.pooled:                                                 # the real read exists: use it
        panel, vm, _ = _make(qtbot, monkeypatch, None, ws=real)
    else:
        panel, vm, _ = _make(qtbot, monkeypatch, snap)
    assert _tops(panel)[-1] == "section:pooled"
    (child,) = _children(panel, "section:pooled")
    assert panel._by_id[child].text() == "pooled master (2 members)"
    display = _Spy(vm.display_requested)
    _click(panel, child)
    assert len(display.calls) == 1 and vm.current_key == panel._by_id[child].data(_KEY_ROLE)
    assert panel._inspect.isEnabled()
    assert panel._status.text() == "2 spectra"                      # a pooled master is not a spectrum


def test_the_bl43_list_has_no_pooled_section(qtbot, monkeypatch, sample):
    panel, _, _ = _make(qtbot, monkeypatch, sample)
    assert "section:pooled" not in panel._by_id


def test_the_empty_list_says_so(qtbot, monkeypatch):
    panel, vm, _ = _make(qtbot, monkeypatch, ScanListSnapshot())
    assert panel._empty.text() == "No scans yet" and not panel._empty.isHidden()
    assert panel._view.isHidden() and panel._status.text() == ""
    assert not panel._inspect.isEnabled()


# ---------------------------------------------------------------------------------------------
# 10. size diagnostic
# ---------------------------------------------------------------------------------------------
def _big_snapshot(sample, n=28):
    template = sample.sets[2]

    def one(key, label):
        sp = tuple(dataclasses.replace(template.spectra[i % 4], key=f"{key}:a{i:02d}", set_key=key,
                                       analyzer=f"a{i:02d}") for i in range(n))
        return dataclasses.replace(template, key=key, label=label, spectra=sp, unread=(),
                                   files_received=n)
    return ScanListSnapshot(sets=(one(KEY_B, STEM_B), one(KEY_A, STEM_A)))


def test_56_spectra_refresh_in_well_under_two_seconds(qtbot, monkeypatch, tmp_path, sample):
    if _real_or_none(bl43_fixture.build_bl43_workspace(tmp_path, n_analyzers=28)) is None:
        snap = _big_snapshot(sample)             # the read's body is not there yet: a hand-built case
    else:
        snap = scan_list(Workspace.open(tmp_path / "ws"))
    t0 = time.perf_counter()
    panel, vm, _ = _make(qtbot, monkeypatch, snap, size=(360, 520))      # the first build
    built = time.perf_counter() - t0
    t0 = time.perf_counter()
    vm.on_workspace_changed({"snapshot"})                                # an update that changes nothing
    QApplication.processEvents()
    refreshed = time.perf_counter() - t0
    print(f"56 spectra: first build {built:.3f} s, refresh {refreshed:.3f} s")
    assert snap.n_spectra == 56 and built < 2.0 and refreshed < 2.0


# ---------------------------------------------------------------------------------------------
# 11. view state
# ---------------------------------------------------------------------------------------------
def test_view_state_round_trips_the_columns_and_the_sort_but_not_the_filter(qtbot, monkeypatch, sample):
    panel, vm, _ = _make(qtbot, monkeypatch, sample)
    panel._column_actions["deep"].trigger()
    panel._column_actions["file"].trigger()
    panel._sort_actions["temperature"].trigger()
    panel._filter.setText("a05")
    session = SimpleNamespace(view_state={})
    vm.save_view_state(session)
    assert session.view_state["scan_list"] == {"columns": ["deep", "file"], "sort": "temperature"}

    panel2, vm2, _ = _make(qtbot, monkeypatch, sample)
    vm2.load_view_state(session)
    assert vm2.sort == "temperature" and vm2.filter_text == ""
    assert not panel2._view.isColumnHidden(COLUMNS.index("deep"))
    assert panel2._view.isColumnHidden(COLUMNS.index("fast"))
    assert panel2._sort_actions["temperature"].isChecked() and panel2._column_actions["file"].isChecked()
    vm2.load_view_state(SimpleNamespace(view_state={"scan_list": {"columns": "x", "sort": "nope"}}))
    assert vm2.sort == "temperature"                               # bad stored state is ignored


def test_a_drop_over_the_panel_is_left_to_the_window(qtbot, monkeypatch, sample):
    panel, _, _ = _make(qtbot, monkeypatch, sample)
    assert not panel.acceptDrops() and not panel._view.acceptDrops()
