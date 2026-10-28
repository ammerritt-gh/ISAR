"""The scan inspector: its lines, its plot, its mount inside the left panel.

Offscreen Qt with the real widgets. The scan-list read is monkeypatched to the shared hand-written
snapshot (``bl43_fixture.sample_snapshot``) over a real BL43LXU-shaped workspace, so the stored unit
data, the raw-counts plot and the window are real.
"""
from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDockWidget, QFormLayout, QLabel

import bl43_fixture
from bl43_fixture import KEY_A, KEY_B, KEY_SINGLE, STEM_A, sample_snapshot
from gui.window_frame_checks import assert_frame_geometry
from isar.workspace_api import Workspace
from isar.workspace_api.scan_list import (Fact, PooledRow, ScanListSnapshot, Status)
from isar_gui import main_window as mw_mod
from isar_gui.main_window import MainWindow
from isar_gui.session import Session
from isar_gui.viewmodels.inspector import InspectorViewModel, _fmt_fact
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.elided_label import ElidedLabel
from isar_gui.views.scan_inspector import (Line, ScanInspectorPanel, _Note, inspector_lines,
                                           scan_numbers_text)

A06 = f"{KEY_A}:a06"
A05 = f"{KEY_A}:a05"
A04 = f"{KEY_A}:a04"
B03 = f"{KEY_B}:a03"
SINGLE = f"{KEY_SINGLE}:a06"
LAPTOP = (1108, 851)
DESKTOP = (2560, 1392)


@pytest.fixture
def patched(monkeypatch, bl43_ws):
    """The scan-list read returns the hand-written snapshot of this test's workspace copy, with the
    fit sentinels dropped: a selection then shows the raw unit (the fit sentinel is not a FitView)."""
    snapshot = sample_snapshot(bl43_ws)
    snapshot = dataclasses.replace(snapshot, sets=tuple(
        dataclasses.replace(s, spectra=tuple(dataclasses.replace(sp, fit=None) for sp in s.spectra))
        for s in snapshot.sets))
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda ws, **_reads: snapshot)
    return snapshot


def _panel(qtbot, bl43_ws):
    vm = ScanListViewModel(Workspace.open(bl43_ws))
    panel = ScanInspectorPanel(vm)
    qtbot.addWidget(panel)
    panel.resize(300, 700)
    panel.show()
    return vm, panel


def _fields(panel):
    """label -> the value widget of every line the panel shows (the full label, never its elided form)."""
    out = {}
    for form in (panel._facts, panel._processing, panel._metadata):
        for i in range(form.rowCount()):
            label = form.itemAt(i, QFormLayout.ItemRole.LabelRole).widget()
            name = label.full_text() if isinstance(label, ElidedLabel) else label.text()
            out[name] = form.itemAt(i, QFormLayout.ItemRole.FieldRole).widget()
    return out


def _text(field) -> str:
    if isinstance(field, _Note):          # a wrapped line (the energy range) holds its whole text
        return field.text()
    return (field if isinstance(field, ElidedLabel) else field.findChildren(ElidedLabel)[0]).full_text()


def _tooltip(field) -> str:
    return (field if isinstance(field, ElidedLabel) else field.findChildren(ElidedLabel)[0]).toolTip()


def _reason(field) -> str | None:
    notes = field.findChildren(_Note)
    return notes[0].text() if notes else None


def _has_dot(field) -> bool:
    return any(lbl.pixmap() is not None and not lbl.pixmap().isNull()
               for lbl in field.findChildren(QLabel))


# ---------------------------------------------------------------------------
# a scan-set row is not "nothing picked"
# ---------------------------------------------------------------------------
def test_a_picked_scan_set_selects_its_spectra_and_inspects_the_first(qtbot, patched, bl43_ws):
    vm, panel = _panel(qtbot, bl43_ws)
    set_row = patched.sets[0]
    vm.click(f"set:{set_row.key}")
    assert vm.selection.keys == tuple(sp.key for sp in set_row.spectra)
    assert panel._key == vm.selection.active == set_row.spectra[0].key
    assert panel._body.isVisible() and not panel._message.isVisible()
    vm.select(None)
    assert panel._key is None and panel._message.isVisible()


# ---------------------------------------------------------------------------
# 1-3: the lines
# ---------------------------------------------------------------------------
def test_a06_of_a_summed_set_shows_every_line(qtbot, patched, bl43_ws):
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    f = _fields(panel)
    assert _text(f["File"]) == "CsV3Sb5_97K_0p5_0p5_3_sum_a06"
    assert _text(f["Scan numbers"]) == "summed (2 scans merged; the files do not list their numbers)"
    assert _text(f["Analyzer"]) == "a06"
    assert _text(f["Temperature"]) == "97 K — file (header)"
    assert _text(f["Q"]) == "20.6 nm⁻¹ — file (header)"
    assert _text(f["Slit"]) == "— not in the file"
    assert _text(f["Points"]) == "60"
    energy = _text(f["Energy range"])
    assert energy.startswith("-4 to 7.8 meV") and "raw axis, zero not applied" in energy
    assert _text(f["Parsed"]) == "ok" and _has_dot(f["Parsed"])
    assert _text(f["Fast pass"]) == "search limit reached" and _has_dot(f["Fast pass"])
    assert _text(f["Deep pass"]) == "not run" and _has_dot(f["Deep pass"])
    # the full file path is the file line's tooltip; the hashed placeholder number is nowhere
    assert _tooltip(f["File"]).endswith("CsV3Sb5_97K_0p5_0p5_3_sum_a06")
    shown = " ".join(_text(w) for w in f.values()) + panel._title.full_text()
    assert "1842762476" not in shown
    assert panel._title.full_text() == f"a06 · {STEM_A}"


def test_a_single_scan_set_shows_its_number_and_a_q_in_rlu(qtbot, patched, bl43_ws):
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(SINGLE)
    f = _fields(panel)
    assert _text(f["Scan numbers"]) == "58"
    assert _text(f["Q"]) == "[4, 0.075, 0] rlu — file (filename)"
    assert _text(f["Temperature"]) == "26 K — file (filename)"


def test_a_missing_q_shows_a_dash_with_its_reason(qtbot, patched, bl43_ws):
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A05)
    f = _fields(panel)
    assert _text(f["Q"]).startswith("—") and _tooltip(f["Q"]) == "not in the file"
    assert _text(f["Parsed"]) == "no Q" and _reason(f["Parsed"]) == "read; Q is not in the file"
    assert _text(f["Fast pass"]) == "not run"


# The rows the old centre metadata panel showed (design 2.7, amendment 4): every unit metadata entry
# is drawn, in the view model's order, except the hashed unit key and the rows the inspector draws itself.
CAMPAIGN = {"scan flags": "missing_sidecar (warning): no .var file",
            "spectrum flags": "bin_absent (info): a06: 2 bins absent",
            "target #Q": "[1.5, 0, 0]", "command": "scan deltaE -2 4 200", "counting mode": "monitor"}
ALL_ROWS = {"type": "measurement", "status": "fitted", "energy status": "needs_zero",
            "resolution slit": "60x60 (file)", "slit candidates": "summed deviance 60x60: 1.0",
            "energy zero": "0.1 meV (elastic_fit)", "elastic fit": "centre 0.1 meV, amplitude 3",
            "calibration flags": "bin_width (info): 0.2 meV", "efficiency": "0.54 (sidecar)",
            "preset": "200", "B": "7 T", "Q rung": "4 (none)"}


def test_a_unit_shows_its_status_calibration_facts_and_campaign_rows(qtbot, patched, bl43_ws, monkeypatch):
    md = {"unit": A06, "analyzer": "a06", "T": "97 K", "points": "60", **ALL_ROWS, **CAMPAIGN}
    monkeypatch.setattr(InspectorViewModel, "_unit_metadata", staticmethod(lambda *_args: md))
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    f = _fields(panel)
    for label, value in {**ALL_ROWS, **CAMPAIGN}.items():
        assert _text(f[label]) == value and _tooltip(f[label]) == value, label
    # the hashed unit key stays hidden, and the lines the inspector draws itself are not repeated
    assert "unit" not in f and "analyzer" not in f and "T" not in f and "points" not in f


def test_a_fact_whose_sources_disagree_keeps_its_row(qtbot, patched, bl43_ws, monkeypatch):
    # built through the view model's own formatter, so a wording change cannot silently pass
    t_conflict = _fmt_fact({"value": 97, "source": "file", "conflicts": [{"value": 95, "source": "card"}]})
    slit = _fmt_fact({"value": None, "source": "file", "conflicts": [{"value": "60x60", "source": "card"}]})
    assert t_conflict.startswith("97 (file)") and "95 (card)" in t_conflict
    md = {"unit": A06, "T_K": t_conflict, "Q_nm": "1.2 (file)", "slit": slit}
    monkeypatch.setattr(InspectorViewModel, "_unit_metadata", staticmethod(lambda *_args: md))
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    f = _fields(panel)
    assert _text(f["T_K"]) == t_conflict and _text(f["slit"]) == slit
    assert "Q_nm" not in f      # no conflict: shown once, on its own line


def test_a_dict_and_a_long_list_read_as_text_with_the_full_value_in_the_tooltip(qtbot, patched, bl43_ws,
                                                                                 monkeypatch):
    bins = list(range(1, 155)) + [1001]   # a count past three digits prints exactly
    fit = {"origin": "y_over_e_squared", "family": "_i", "bins_tested": 155, "bins_off": 1}
    md = {"unit": A06, "fit_spec": _fmt_fact({"value": fit, "source": "file"}),
          "bins": _fmt_fact({"value": bins, "source": "file"}),
          "modes": _fmt_fact({"value": ["fork", "fold"], "source": "file"})}
    monkeypatch.setattr(InspectorViewModel, "_unit_metadata", staticmethod(lambda *_args: md))
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    f = _fields(panel)
    assert _text(f["fit_spec"]) == "origin: y_over_e_squared, family: _i, bins_tested: 155, bins_off: 1 (file)"
    assert _tooltip(f["fit_spec"]) == _text(f["fit_spec"])
    assert _text(f["bins"]) == "155 values: 1, 2, 3, 4, ... (file)"
    assert _tooltip(f["bins"]) == "[" + ", ".join(map(str, bins)) + "] (file)"
    assert _text(f["modes"]) == "[fork, fold] (file)"
    for label in ("fit_spec", "bins"):
        assert not {"{", "[", "'"} & set(_text(f[label])), label


def test_a_unit_without_those_values_shows_none_of_their_rows(qtbot, patched, bl43_ws, monkeypatch):
    md = {"unit": A06, "analyzer": "a06"}
    monkeypatch.setattr(InspectorViewModel, "_unit_metadata", staticmethod(lambda *_args: md))
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    f = _fields(panel)
    assert set(f) >= {"File", "Q", "Parsed", "Fast pass", "Deep pass"}
    assert not (set(ALL_ROWS) | set(CAMPAIGN)) & set(f)
    assert not panel._metadata_header.isVisibleTo(panel)


def test_a_bl43_scan_shows_no_empty_or_nan_rows(qtbot, patched, bl43_ws):
    # The BL43 headers stamp a counting mode, the set's QC flags and a status; a campaign command, a target Q,
    # a preset and a B field are never in a BL43 file, and a missing fact is not a row.
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    f = _fields(panel)
    assert _text(f["counting mode"]) == "monitor"
    assert _text(f["scan flags"]).startswith("pre_combined_input (warning)")
    assert not {"command", "target #Q", "spectrum flags", "preset", "B"} & set(f)
    shown = " ".join(_text(w) for w in f.values())
    assert "not available" not in shown and "nan" not in shown
    assert "slit" not in f and "time" not in f and "scan_numbers" not in f    # missing facts: no row


def test_a_long_metadata_label_elides_with_its_full_name_in_the_tooltip(qtbot, patched, bl43_ws, monkeypatch):
    md = {"unit": A06, "count_recovery_max_deviation": "4.139e-07 (inferred)"}
    monkeypatch.setattr(InspectorViewModel, "_unit_metadata", staticmethod(lambda *_args: md))
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    qtbot.wait(30)
    label = panel._metadata.itemAt(0, QFormLayout.ItemRole.LabelRole).widget()    # the only row
    assert label.full_text() == "count_recovery_max_deviation"
    assert label.text() != label.full_text() and "…" in label.text()
    assert label.toolTip() == "count_recovery_max_deviation"


def test_many_metadata_rows_scroll_inside_the_panel_at_the_laptop_width(qtbot, patched, bl43_ws, monkeypatch):
    md = {"unit": A06, **{f"fact_{i:02d}": f"{i} (sidecar)" for i in range(40)}}
    monkeypatch.setattr(InspectorViewModel, "_unit_metadata", staticmethod(lambda *_args: md))
    vm, panel = _panel(qtbot, bl43_ws)
    panel.resize(mw_mod._INSPECTOR_MIN, 600)
    panel.show_key(A06)
    qtbot.wait(30)
    assert panel._scroll.verticalScrollBar().maximum() > 0
    assert panel.minimumSizeHint().width() <= mw_mod._INSPECTOR_MIN


def test_a_long_unit_in_the_laptop_window_keeps_the_frame_and_scrolls(qtbot, patched, bl43_ws, monkeypatch):
    md = {"unit": A06, **{f"fact_{i:02d}": f"{i} (sidecar)" for i in range(40)}}
    monkeypatch.setattr(InspectorViewModel, "_unit_metadata", staticmethod(lambda *_args: md))
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    w.scan_list_vm.inspect(A06)
    qtbot.wait(50)
    assert_frame_geometry(w, LAPTOP)
    assert w.scan_inspector._scroll.verticalScrollBar().maximum() > 0


def test_scan_numbers_text_for_each_kind_of_set():
    a = sample_snapshot(Path("x") / "ws").scan_set(KEY_A)
    assert scan_numbers_text(dataclasses.replace(a, scan_numbers=(101, 102))) == "summed: scans 101, 102"
    assert scan_numbers_text(dataclasses.replace(a, n_merged=None)) == "summed"
    one = dataclasses.replace(a, summed=False, scan_numbers=(58,), n_merged=None)
    assert scan_numbers_text(one) == "58"
    assert scan_numbers_text(dataclasses.replace(one, scan_numbers=())) == "— not in the files"


def test_inspector_lines_is_pure_and_marks_statuses():
    snap = sample_snapshot(Path("x") / "ws")
    row = snap.spectrum(B03)
    lines = inspector_lines(row, snap.scan_set(row.set_key), None)
    by = {ln.label: ln for ln in lines}
    assert by["Fast pass"].column == "fast" and by["Fast pass"].text == "held: needs resolution curves"
    assert by["Fast pass"].word == "held" and "held" in by["Fast pass"].tooltip     # the fitting code's word
    assert by["Fast pass"].reason == "a03: no resolution curve for a03 at slit 60x60"
    assert by["Fast pass"].group == "processing" and by["Parsed"].group == "facts"
    assert by["Energy range"].text == "— no parsed counts for this spectrum"
    assert isinstance(lines[0], Line)


# ---------------------------------------------------------------------------
# 4: the plot
# ---------------------------------------------------------------------------
def test_the_raw_counts_plot_is_titled_from_the_snapshot(qtbot, patched, bl43_ws):
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    plot = panel._plot
    assert len(plot.listDataItems()) >= 1
    assert "raw energy" in plot.getAxis("bottom").labelText
    title = plot.titleLabel.text
    assert title == f"{STEM_A} · a06 · raw counts"
    assert "1842762476" not in title and "not fitted" not in title
    # the centre's own view model still titles a raw unit "(parsed, not fitted)": it is not changed
    centre = InspectorViewModel(Workspace.open(bl43_ws))
    centre.set_unit(A06)
    assert "not fitted" in centre.state.main_spec.title and A06 not in plot.titleLabel.text


# ---------------------------------------------------------------------------
# 5: following the list, close
# ---------------------------------------------------------------------------
def test_the_panel_follows_the_list_and_emits_close(qtbot, patched, bl43_ws):
    vm, panel = _panel(qtbot, bl43_ws)
    vm.inspect(f"{KEY_A}:a04")
    assert panel._title.full_text() == f"a04 · {STEM_A}"
    f = _fields(panel)
    assert _text(f["Fast pass"]) == "close call"
    assert _text(f["Deep pass"]) == "clear" and _reason(f["Deep pass"]) == "count 1 (P 0.91)"

    vm.select(B03)                                  # the panel is open: it follows
    f = _fields(panel)
    assert panel._title.full_text() == f"a03 · {bl43_fixture.STEM_B}"
    assert _text(f["Fast pass"]) == "held: needs resolution curves"
    assert _reason(f["Fast pass"]) == "a03: no resolution curve for a03 at slit 60x60"

    vm.select(None)                                 # nothing selected: it keeps what it shows
    assert panel._title.full_text() == f"a03 · {bl43_fixture.STEM_B}"
    vm.select("not:in:snapshot")                    # not a spectrum: same
    assert panel._title.full_text() == f"a03 · {bl43_fixture.STEM_B}"

    with qtbot.waitSignal(panel.close_requested, timeout=1000):
        panel._close.click()
    assert panel._close.accessibleName() == "Close inspector"


def test_a_hidden_panel_ignores_the_list_until_it_is_shown(qtbot, patched, bl43_ws):
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    panel.hide()
    vm.select(A04)                                  # not followed while hidden
    assert panel._title.full_text() == f"a06 · {STEM_A}"
    panel.show()                                    # opened without a key: it follows the list
    assert panel._title.full_text() == f"a04 · {STEM_A}"


def test_a_key_that_left_the_snapshot_says_so(qtbot, patched, bl43_ws, monkeypatch):
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda ws, **_reads: ScanListSnapshot())
    vm.on_workspace_changed({"snapshot"})
    assert panel._message.isVisibleTo(panel)
    assert panel._message.text() == "This spectrum is no longer in the experiment"
    assert not panel._body.isVisibleTo(panel)


def test_a_background_update_redraws_the_same_key_in_place(qtbot, patched, bl43_ws, monkeypatch):
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(A06)
    assert _text(_fields(panel)["Deep pass"]) == "not run"
    snap = sample_snapshot(bl43_ws)
    new = []
    for s in snap.sets:
        spectra = tuple(dataclasses.replace(sp, deep=Status("resolved", True, "resolved: count 2"))
                        if sp.key == A06 else sp for sp in s.spectra)
        new.append(dataclasses.replace(s, spectra=spectra))
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list",
                        lambda ws, **_reads: dataclasses.replace(snap, sets=tuple(new)))
    vm.on_workspace_changed({"results"})
    f = _fields(panel)
    assert _text(f["Deep pass"]) == "clear" and _reason(f["Deep pass"]) == "count 2"
    assert panel._title.full_text() == f"a06 · {STEM_A}"


# ---------------------------------------------------------------------------
# 6: a pooled master
# ---------------------------------------------------------------------------
def test_a_pooled_master_shows_its_lines_and_the_display_note(qtbot, bl43_ws, monkeypatch):
    pooled = PooledRow(key="tavi:900:det", label="pooled master (2 members)",
                       temperature=Fact(200.0, "K", "fit", "fit"),
                       q=Fact((2.0, 0.0, 0.0), "rlu", "fit", "fit"), n_members=2,
                       fast=Status("not recorded", False, "no fast-pass record"),
                       deep=Status("not run", False))
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list",
                        lambda ws, **_reads: ScanListSnapshot(pooled=(pooled,)))
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key("tavi:900:det")
    f = _fields(panel)
    assert _text(f["Pooled"]) == "pooled master (2 members)"
    assert _text(f["Members"]) == "2"
    assert _text(f["Temperature"]) == "200 K — fit" and _text(f["Q"]) == "[2, 0, 0] rlu — fit"
    assert _text(f["Fast pass"]) == "not recorded" and _text(f["Deep pass"]) == "not run"
    assert "pooled master (2 members)" in panel._title.full_text()
    assert panel._plot_note.isVisibleTo(panel) and not panel._glw.isVisibleTo(panel)
    assert panel._plot_note.text() == "pooled spectrum: shown in the display"


# ---------------------------------------------------------------------------
# the fact table (S2U3) takes over the stage-one fact lines
# ---------------------------------------------------------------------------
def test_the_window_gives_the_inspector_its_fact_table_and_unimported_data_keeps_the_stage_one_lines(
        qtbot, patched, bl43_ws, monkeypatch):
    """bl43_ws was built without the intake record, so the backend holds no facts for it: the table stays
    empty and the Temperature, Q and Slit lines are not lost; with facts the lines give way (test_fact_table)."""
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    table = w.scan_inspector._fact_table
    assert table is not None and w.scan_inspector._rows_layout.indexOf(table) == 1
    w.scan_list_vm.inspect(A06)
    qtbot.wait(50)
    assert not table.has_facts and table.isHidden()
    f = _fields(w.scan_inspector)
    assert _text(f["Temperature"]) == "97 K — file (header)" and "Slit" in f and "Q" in f


# ---------------------------------------------------------------------------
# nothing clips
# ---------------------------------------------------------------------------
def test_the_panel_fits_its_minimum_width_and_elides_long_values(qtbot, patched, bl43_ws):
    vm, panel = _panel(qtbot, bl43_ws)
    panel.show_key(B03)                              # a long hold reason
    assert panel.minimumSizeHint().width() <= mw_mod._INSPECTOR_MIN
    panel.resize(mw_mod._INSPECTOR_MIN, 700)
    qtbot.wait(30)
    file_label = _fields(panel)["File"]
    assert file_label.text() != file_label.full_text() and "…" in file_label.text()
    assert file_label.toolTip().endswith(file_label.full_text())


def _form_rows(panel):
    for form in (panel._facts, panel._processing, panel._metadata):
        for i in range(form.rowCount()):
            yield (form.itemAt(i, QFormLayout.ItemRole.LabelRole).widget(),
                   form.itemAt(i, QFormLayout.ItemRole.FieldRole).widget())


def test_every_value_starts_past_the_end_of_its_label(qtbot, patched, bl43_ws, monkeypatch):
    """Geometry, not text: offscreen has no real fonts, so the label column must be measured."""
    md = {"unit": A06, "count_recovery_max_deviation": "4.139e-07 (inferred)"}
    monkeypatch.setattr(InspectorViewModel, "_unit_metadata", staticmethod(lambda *_args: md))
    vm, panel = _panel(qtbot, bl43_ws)
    panel.resize(mw_mod._INSPECTOR_MIN, 700)
    panel.show_key(A06)
    qtbot.wait(30)
    spacing = panel._facts.horizontalSpacing()
    rows = list(_form_rows(panel))
    assert len(rows) >= 10
    for label, field in rows:
        assert field.geometry().left() >= label.geometry().right() + spacing, label.full_text()


# ---------------------------------------------------------------------------
# 8-11: the mount inside the left panel
# ---------------------------------------------------------------------------
def _window(qtbot, monkeypatch, size, ws_root, session=None):
    monkeypatch.setattr(MainWindow, "_available_size", lambda self: size)
    w = MainWindow(Workspace.open(ws_root), session=session or Session(), watch=False)
    qtbot.addWidget(w)
    w.resize(*size)
    w.show()
    qtbot.waitExposed(w)
    qtbot.wait(50)
    return w


def _saved(w):
    snap = w._snapshot_session()
    return Session(dock_layout_b64=snap.dock_layout_b64, view_state=dict(snap.view_state))


def test_opening_widens_the_left_panel_and_closing_pulls_it_back(qtbot, patched, bl43_ws, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    assert w.left_column_dock.isAncestorOf(w.scan_inspector)
    assert not w.centralWidget().isAncestorOf(w.scan_inspector)
    assert all(d.widget() is not w.scan_inspector for d in w.findChildren(QDockWidget))
    assert not any("nspector" in a.text() for a in w._docks_menu.actions())

    w0, list0, display0 = w.left_column_dock.width(), w.scan_list.width(), w._center_stack.width()
    handle = w._left_split.handleWidth()
    w.scan_list_vm.inspect(A06)
    qtbot.wait(50)
    assert not w.scan_inspector.isHidden()
    assert w.scan_inspector.width() >= mw_mod._INSPECTOR_MIN
    assert abs(w.left_column_dock.width() - (w0 + w.scan_inspector.width() + handle)) <= 4 + handle
    assert abs((display0 - w._center_stack.width()) - (w.scan_inspector.width() + handle)) <= 4 + handle
    assert abs(w.scan_list.width() - list0) <= 4
    assert w.scan_inspector._title.full_text() == f"a06 · {STEM_A}"     # the panel's own connection
    assert_frame_geometry(w, LAPTOP)

    w.scan_inspector.close_requested.emit()
    qtbot.wait(50)
    assert w.scan_inspector.isHidden()
    assert abs(w.left_column_dock.width() - w0) <= 4
    assert abs(w._center_stack.width() - display0) <= 4


def test_a_saved_layout_never_carries_the_open_width(qtbot, patched, bl43_ws, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    w0 = w.left_column_dock.width()
    closed = _saved(w).view_state["window_layout"]
    assert "left_width" not in closed
    w.scan_list_vm.inspect(A06)
    qtbot.wait(50)
    session = _saved(w)
    opened = session.view_state["window_layout"]
    assert abs(opened["left_width"] - w0) <= 4
    assert all(abs(a - b) <= 4 for a, b in zip(opened["needs"], closed["needs"]))

    w2 = _window(qtbot, monkeypatch, LAPTOP, bl43_ws, session=session)
    assert w2.scan_inspector.isHidden()
    assert abs(w2.left_column_dock.width() - w0) <= 4


def test_a_rebind_closes_the_inspector_and_the_left_panel_pulls_back(
        qtbot, patched, bl43_ws, monkeypatch, tmp_path):
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    w0 = w.left_column_dock.width()
    w.scan_list_vm.inspect(A06)
    qtbot.wait(50)
    assert w.left_column_dock.width() > w0 + mw_mod._INSPECTOR_MIN
    second = tmp_path / "second"
    shutil.copytree(bl43_ws.parent, second)
    assert w.load_workspace(str(second / bl43_ws.name))
    qtbot.wait(80)
    assert w.scan_inspector.isHidden()
    assert abs(w.left_column_dock.width() - w0) <= 4


def test_compact_mode_stashes_the_closed_arrangement_and_reopens_the_inspector(
        qtbot, patched, bl43_ws, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    w.scan_list_vm.inspect(A06)
    qtbot.wait(50)
    w.toggle_compact_mode()
    assert w.scan_inspector.isHidden() and w._compact_stash[2] is True
    w.toggle_compact_mode()
    qtbot.wait(80)
    # Back open inside the left panel, which is widened by it again. (Offscreen Qt does not always
    # restore the window's own size after compact mode, so the widths are not compared with the
    # arrangement before.)
    assert not w.scan_inspector.isHidden() and w.left_column_dock.isAncestorOf(w.scan_inspector)
    assert w.left_column_dock.width() >= (w._column_width() + w._left_split.handleWidth()
                                          + mw_mod._INSPECTOR_MIN)


def test_on_a_laptop_opening_the_inspector_collapses_processing_and_closing_does_not_expand_it(
        qtbot, patched, bl43_ws, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    w.set_right_panel_collapsed(False)
    qtbot.wait(50)
    w.scan_list_vm.inspect(A06)
    qtbot.wait(50)
    assert w._right_panel_collapsed() and not w.scan_inspector.isHidden()
    assert w._center_stack.width() >= mw_mod._CENTER_MIN[0]
    # the room is measured after the collapse, so the inspector opens at its full width, not the minimum
    assert abs(w.scan_inspector.width() - mw_mod._ARRANGEMENTS["laptop"].inspector) <= 4
    w.scan_inspector.close_requested.emit()
    qtbot.wait(50)
    assert w._right_panel_collapsed() and w.scan_inspector.isHidden()


def test_a_processing_panel_re_expanded_stays_open_while_the_inspector_is_open(
        qtbot, patched, bl43_ws, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    w.scan_list_vm.inspect(A06)
    qtbot.wait(50)
    w.set_right_panel_collapsed(False)
    qtbot.wait(50)
    w.scan_list_vm.inspect(A04)                      # another spectrum while it is open: no second collapse
    qtbot.wait(50)
    assert not w._right_panel_collapsed() and not w.scan_inspector.isHidden()
    assert_frame_geometry(w, LAPTOP)


def test_on_a_desktop_opening_the_inspector_leaves_processing_expanded(qtbot, patched, bl43_ws, monkeypatch):
    w = _window(qtbot, monkeypatch, DESKTOP, bl43_ws)
    assert not w._right_panel_collapsed()
    w.scan_list_vm.inspect(A06)
    qtbot.wait(50)
    assert not w.scan_inspector.isHidden() and not w._right_panel_collapsed()


def test_laptop_with_the_inspector_open_and_the_right_panel_expanded_fits(
        qtbot, patched, bl43_ws, monkeypatch):
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    w.scan_list_vm.inspect(A06)
    w.set_right_panel_collapsed(False)
    qtbot.wait(80)
    assert not w.scan_inspector.isHidden() and not w._right_panel_collapsed()
    assert_frame_geometry(w, LAPTOP)
    assert w._center_stack.width() >= 300 and w._center_stack.height() >= 250
    assert w.scan_list.width() >= mw_mod._LEFT_MIN
    assert w.scan_inspector.width() >= mw_mod._INSPECTOR_MIN


def test_opening_never_grows_the_window_past_the_screen(qtbot, patched, bl43_ws, monkeypatch):
    # The right panel is already open: the inspector takes only what the display can spare.
    w = _window(qtbot, monkeypatch, LAPTOP, bl43_ws)
    w.set_right_panel_collapsed(False)
    qtbot.wait(50)
    display = w._center_stack.width()
    w.scan_list_vm.inspect(A06)
    qtbot.wait(80)
    assert w.width() <= LAPTOP[0]
    assert w._center_stack.width() >= 300
    assert display - w._center_stack.width() <= max(w._inspector_width, mw_mod._INSPECTOR_MIN) + 10
