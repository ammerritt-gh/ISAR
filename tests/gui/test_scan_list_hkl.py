"""The scan list's hkl, |Q|, gpi and rf columns, offscreen with the real widgets.

The read is the real facade on generated data (hard rule 10); the one monkeypatch replaces the read only
where a test must change one cell in place. The BL43 set holds a03 to a05 whose gpi hkl is
(0.5, 0.5, 3 + 0.5 i) and whose |Q| is 10, 11.46 and 12.92 nm⁻¹.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bl43_fixture import GPI_SET, build_bl43_gpi_workspace
from isar.workspace_api import Workspace
from isar.workspace_api.intake_view import import_summary
from isar.workspace_api.scan_list import ATTENTION_NOT_READ, Fact, scan_list
from isar_gui.theme import state_icon
from isar_gui.viewmodels.scan_list import COLUMNS, ScanListViewModel
from isar_gui.views.scan_list import GUTTER, ScanListPanel

ACCESS = Qt.ItemDataRole.AccessibleTextRole
HKL = {"a03": "0.5, 0.5, 3", "a04": "0.5, 0.5, 3.5", "a05": "0.5, 0.5, 4"}
ABS_Q = {"a03": "10", "a04": "11.46", "a05": "12.92"}


def _panel(qtbot, root: Path, size=(360, 520)):
    vm = ScanListViewModel(Workspace.open(root))
    panel = ScanListPanel(vm)
    qtbot.addWidget(panel)
    panel.resize(*size)
    panel.show()
    QApplication.processEvents()
    return vm, panel


def _rows(vm):
    (ss,) = vm.snapshot.sets
    return ss, {sp.analyzer: sp for sp in ss.spectra}


def _item(panel, node_id, column):
    first = panel._by_id[node_id]
    parent = first.parent() or panel._model.invisibleRootItem()
    return parent.child(first.row(), COLUMNS.index(column))


def _index(panel, node_id):
    return panel._model.indexFromItem(panel._by_id[node_id])


def _header(panel, column, role=Qt.ItemDataRole.DisplayRole):
    return panel._model.headerData(COLUMNS.index(column), Qt.Orientation.Horizontal, role)


def _is(item, good: bool) -> bool:
    return item.icon().cacheKey() == state_icon(good).cacheKey()


def _click(panel, node_id):
    """A real select-zone click on a row's scan cell, as a user's click."""
    index = _index(panel, node_id)
    parent = index.parent()
    while parent.isValid():
        panel._view.expand(parent)
        parent = parent.parent()
    panel._view.scrollTo(index)
    QApplication.processEvents()
    rect = panel._view.visualRect(index)
    QTest.mouseClick(panel._view.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(rect.left() + GUTTER + 8, rect.center().y()))


# ---- the BL43 set: accepted, rejected, a curve missing -----------------------------------------
def test_accepted_gpi_shows_each_analyzers_hkl_checks_and_an_optional_absq(qtbot, tmp_path):
    vm, panel = _panel(qtbot, build_bl43_gpi_workspace(tmp_path))
    ss, spectra = _rows(vm)
    assert _header(panel, "hkl") == "hkl" and "r.l.u." in _header(panel, "hkl", Qt.ItemDataRole.ToolTipRole)
    assert "Q" not in {_header(panel, c) for c in COLUMNS}                   # no bare Q header
    for a, text in HKL.items():
        sp = spectra[a]
        hkl = _item(panel, f"spec:{sp.key}", "hkl")
        assert hkl.text() == text and hkl.toolTip() == f"{text} r.l.u. (from the gpi file)"
        gpi = _item(panel, f"spec:{sp.key}", "gpi")
        assert _is(gpi, True) and gpi.data(ACCESS) == "gpi: accepted" and gpi.toolTip() == f"gpi.{GPI_SET}"
        rf = _item(panel, f"spec:{sp.key}", "rf")
        assert _is(rf, True) and rf.data(ACCESS) == "rf: curve in force" and rf.toolTip().startswith("sarf_z_")
    assert _item(panel, f"set:{ss.key}", "hkl").text() == ""
    assert _is(_item(panel, f"set:{ss.key}", "gpi"), True)
    assert _item(panel, f"set:{ss.key}", "gpi").data(ACCESS) == "gpi: all analyzers good"
    assert _is(_item(panel, f"set:{ss.key}", "rf"), True)

    assert panel._view.isColumnHidden(COLUMNS.index("absq"))               # |Q| is off by default
    assert _header(panel, "absq") == "|Q| (nm⁻¹)"
    panel._column_actions["absq"].trigger()
    assert not panel._view.isColumnHidden(COLUMNS.index("absq"))
    for a, text in ABS_Q.items():
        assert _item(panel, f"spec:{spectra[a].key}", "absq").text() == text


def test_a_rejected_gpi_file_blanks_every_hkl_and_crosses_every_analyzer_and_the_scan(qtbot, tmp_path):
    vm, panel = _panel(qtbot, build_bl43_gpi_workspace(tmp_path, gpi="rejected"))
    ss, spectra = _rows(vm)
    for sp in spectra.values():
        hkl = _item(panel, f"spec:{sp.key}", "hkl")
        assert hkl.text() == "—" and hkl.toolTip() == sp.q.reason
        gpi = _item(panel, f"spec:{sp.key}", "gpi")
        assert _is(gpi, False) and gpi.data(ACCESS) == "gpi: not used"
    assert _is(_item(panel, f"set:{ss.key}", "gpi"), False)
    assert _item(panel, f"set:{ss.key}", "gpi").data(ACCESS) == "gpi: 3 of 3 analyzers not good"
    panel._column_actions["absq"].trigger()                                 # |Q| is still there
    assert _item(panel, f"spec:{spectra['a03'].key}", "absq").text() == "10"


def test_a_missing_curve_crosses_that_analyzer_and_the_scan_row_on_rf_only(qtbot, tmp_path):
    vm, panel = _panel(qtbot, build_bl43_gpi_workspace(tmp_path, drop_curve="a04"))
    ss, spectra = _rows(vm)
    rf = {a: _item(panel, f"spec:{sp.key}", "rf") for a, sp in spectra.items()}
    assert [_is(rf[a], True) for a in ("a03", "a04", "a05")] == [True, False, True]
    assert rf["a04"].data(ACCESS) == "rf: curve missing" and "sarf_z_s1_40_40_a04" in rf["a04"].toolTip()
    assert _is(_item(panel, f"set:{ss.key}", "rf"), False)
    assert _item(panel, f"set:{ss.key}", "rf").data(ACCESS) == "rf: 1 of 3 analyzers not good"
    assert _is(_item(panel, f"set:{ss.key}", "gpi"), True)                 # a missing curve leaves gpi alone
    assert _item(panel, f"spec:{spectra['a04'].key}", "hkl").text() == HKL["a04"]


# ---- a TAVI workspace: no gpi file, no curves; file hkl from the header -------------------------
def _tavi(root: Path, *, member_q=((2.0, 0.0, 0.0), (2.0, 0.0, 0.0))) -> Path:
    """A pooled master of two members whose file headers give the hkl ``member_q``."""
    from synthetic_ws import detector_outcome, scan_entry, stamp_ws
    root.mkdir(parents=True, exist_ok=True)
    members = ["tavi:11:det", "tavi:12:det"]
    scans = {
        "tavi:11:det": scan_entry(11, detectors={"det": detector_outcome(omega0=5.0, gamma=0.5, q=list(member_q[0]))},
                                  provenance={"M": 200, "seed": 11, "q_source": "header"}),
        "tavi:12:det": scan_entry(12, detectors={"det": detector_outcome(omega0=5.1, gamma=0.5, q=list(member_q[1]))},
                                  provenance={"M": 200, "seed": 12, "q_source": "header"}),
        "tavi:900:det": scan_entry(900, detectors={"det": detector_outcome(
            omega0=5.05, gamma=0.5, pooled=True, n_members=2, members=members, q=[2.0, 0.0, 0.0], T=200.0)}),
    }
    return stamp_ws(root, scans=scans)


def test_a_card_without_gpi_or_curves_has_no_gpi_or_rf_column_and_shows_the_file_hkl(qtbot, tmp_path):
    vm, panel = _panel(qtbot, _tavi(tmp_path / "plain"))
    assert not vm.column_present("gpi") and not vm.column_present("rf")
    assert "gpi" not in vm.visible_columns() and "rf" not in vm.visible_columns()
    assert not panel._column_actions["gpi"].isVisible() and not panel._column_actions["rf"].isVisible()
    hkl = _item(panel, "spec:tavi:11:det", "hkl")
    assert hkl.text() == "2, 0, 0" and hkl.toolTip() == "2, 0, 0 r.l.u. (from the header)"
    assert _item(panel, "spec:tavi:11:det", "gpi").data(ACCESS) is None


def test_a_pool_of_agreeing_members_shows_their_mean_and_one_whose_members_differ_shows_blank(qtbot, tmp_path):
    vm, panel = _panel(qtbot, _tavi(tmp_path / "agree"))
    pool = _item(panel, "pool:tavi:900:det", "hkl")
    assert pool.text() == "2, 0, 0" and pool.toolTip() == "2, 0, 0 r.l.u. (mean of the pool's members)"

    vm, panel = _panel(qtbot, _tavi(tmp_path / "differ", member_q=((2.0, 0.0, 0.0), (2.0, 0.5, 0.0))))
    pool = _item(panel, "pool:tavi:900:det", "hkl")
    assert pool.text() == "—" and pool.toolTip() == "the members' hkl differ"
    assert _item(panel, "spec:tavi:12:det", "hkl").text() == "2, 0.5, 0"    # each member keeps its own


# ---- remembered hiding, and continuity ----------------------------------------------------------
def test_hiding_gpi_or_showing_absq_is_remembered_across_a_reload(qtbot, tmp_path):
    root = build_bl43_gpi_workspace(tmp_path)
    vm, panel = _panel(qtbot, root)
    panel._column_actions["gpi"].trigger()                                 # hidden by hand
    panel._column_actions["absq"].trigger()                                # |Q| on by hand
    session = SimpleNamespace(view_state={})
    vm.save_view_state(session)
    assert session.view_state["scan_list"] == {"columns": ["absq"], "sort": "set", "hidden": ["gpi"],
                                               "show_removed": False}

    vm2, panel2 = _panel(qtbot, root)
    assert panel2._view.isColumnHidden(COLUMNS.index("absq"))
    vm2.load_view_state(session)
    assert panel2._view.isColumnHidden(COLUMNS.index("gpi"))
    assert not panel2._view.isColumnHidden(COLUMNS.index("absq")) and not panel2._view.isColumnHidden(COLUMNS.index("rf"))
    assert panel2._column_actions["absq"].isChecked() and not panel2._column_actions["gpi"].isChecked()


def _copy_set(base, i):
    key = f"{base.key}-{i}"
    spectra = tuple(dataclasses.replace(sp, key=f"{sp.key}-{i}", set_key=key) for sp in base.spectra)
    return dataclasses.replace(base, key=key, label=f"{base.label}-{i}", spectra=spectra)


def test_a_refresh_that_changes_an_hkl_cell_keeps_selection_scroll_and_expansion(qtbot, monkeypatch, tmp_path):
    real = scan_list(Workspace.open(build_bl43_gpi_workspace(tmp_path)))
    sets = [_copy_set(real.sets[0], i) for i in range(6)]                  # enough rows to scroll at this height
    box = {"snap": dataclasses.replace(real, sets=tuple(sets))}
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda _ws, **_reads: box["snap"])
    vm = ScanListViewModel(SimpleNamespace())
    panel = ScanListPanel(vm)
    qtbot.addWidget(panel)
    panel.resize(360, 190)
    panel.show()
    QApplication.processEvents()
    for s in sets[:2]:
        panel._view.expand(_index(panel, f"set:{s.key}"))
    _click(panel, f"spec:{sets[1].spectra[1].key}")
    panel._view.verticalScrollBar().setValue(3)
    QApplication.processEvents()
    anchor = panel._scroll_anchor()
    assert anchor is not None
    changes = []
    vm.selection.changed.connect(lambda *a: changes.append(a))
    selected = vm.selection.keys

    moved = dataclasses.replace(sets[1].spectra[1], q=Fact((0.5, 0.5, 9.0), "rlu", "gpi", "file"))
    box["snap"] = dataclasses.replace(real, sets=tuple(
        dataclasses.replace(s, spectra=(s.spectra[0], moved, *s.spectra[2:])) if s is sets[1] else s
        for s in sets))
    vm.on_workspace_changed()
    QApplication.processEvents()

    assert _item(panel, f"spec:{moved.key}", "hkl").text() == "0.5, 0.5, 9"
    assert panel._view.isExpanded(_index(panel, f"set:{sets[0].key}"))
    assert panel._view.isExpanded(_index(panel, f"set:{sets[1].key}"))
    assert vm.selection.keys == selected and vm.selection.active == moved.key
    assert panel._scroll_anchor() == anchor
    assert changes == []


def test_a_refresh_before_the_worker_has_read_readiness_never_reads_it_on_the_window_thread(qtbot, monkeypatch, tmp_path):
    root = build_bl43_gpi_workspace(tmp_path)
    ws = Workspace.open(root)
    evaluated = []

    def spy(path):
        evaluated.append(path)
        return ATTENTION_NOT_READ
    monkeypatch.setattr("isar.workspace_api.intake_view.needs_attention", spy)
    intake = SimpleNamespace(fact_rows=tuple(ws.fact_rows()), fit_inputs=tuple(ws.fit_inputs()),
                             summary=import_summary(root), attention=None)
    vm = ScanListViewModel(ws)                  # its own constructor read comes before the window binds the intake
    vm.set_intake(intake)
    evaluated.clear()
    vm.on_workspace_changed()
    assert evaluated == []
    (ss,) = vm.snapshot.sets
    assert {(sp.rf.good, sp.rf.text) for sp in ss.spectra} == {(None, "rf: not read yet")}
