"""The scan inspector's fit table with saved mode links (S3U2): a Mode heading names its branch and lists its link notes
only on the fit the links were made on, and one note says when the links are out of date or whose names these are.
Offscreen Qt with the real widgets; the fit table is driven directly, and the panel once through the fake InterState."""
from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QLabel

from bl43_fixture import KEY_A
from gui.inter_fakes import FakeInterState, make_link_view, make_unit_links
from gui.test_inspector_fit_section import _label, _mode, _pass
from gui.test_scan_inspector import _panel, _select
from isar.workspace_api.inter_spectra import LINK_WORDS, MarkerView
from isar.workspace_api.spectra import Measured
from isar_gui.views.scan_inspector import FitSection

A03 = f"{KEY_A}:a03"         # a resolved fast pass with a stored mode (the fixture stamps a06 with none)
A06 = f"{KEY_A}:a06"
CHANGE = MarkerView("change", 2, 0, "energy", "energy differs from neighbours",
                    "mode 1 peaks at 4.00 meV; its neighbours predict 3.00 ± 0.20 meV.", "warn")


def _section(qtbot) -> FitSection:
    widget = FitSection()
    qtbot.addWidget(widget)
    return widget


def _stored(count: int) -> tuple:
    return tuple(_mode(n, Measured(4.0 * n, None, None, None), Measured(0.5, None, None, None),
                       Measured(30.0, None, None, None)) for n in range(1, count + 1))


def _show(widget, fast_modes: int, deep_modes: int, links, drawn_fit="fast") -> None:
    fast = _pass("fast", label="phonon+background", modes=_stored(fast_modes))
    deep = _pass("deep", label="stokes+background", modes=_stored(deep_modes))
    widget.render({"fast": fast, "deep": deep}, links=links, drawn_fit=drawn_fit)


def _note(widget) -> QLabel:
    return widget.findChild(QLabel, "scan_inspector_fit_links")


def test_a_mode_heading_names_its_branch_and_lists_its_notes_on_the_fit_the_table_shows(qtbot):
    widget = _section(qtbot)
    _show(widget, 2, 0, make_unit_links(A06, names=("B3", "B2"), markers=(CHANGE,)))
    heading = _label(widget, "fit_mode_1_header")
    assert heading.text() == "Mode 1 · B3 · energy differs from neighbours"
    assert "Branch this mode follows" in heading.toolTip() and CHANGE.words in heading.toolTip()
    assert _label(widget, "fit_mode_2_header").text() == "Mode 2 · B2"
    assert _note(widget).isHidden()                                  # the table shows the linked fit: nothing to say


def test_a_split_candidate_names_both_branches_as_unresolved(qtbot):
    widget = _section(qtbot)
    _show(widget, 1, 0, make_unit_links(A06, names=("B2",), unresolved=(("B2", "B3"),)))
    assert _label(widget, "fit_mode_1_header").text() == "Mode 1 · B2 / B3 (unresolved)"


def test_names_of_a_fit_the_table_does_not_show_are_not_drawn_and_the_note_says_whose_they_are(qtbot):
    widget = _section(qtbot)
    _show(widget, 2, 0, make_unit_links(A06, names=("B3", "B2"), fit_id="f9", markers=(CHANGE,)))
    assert _label(widget, "fit_mode_1_header").text() == "Mode 1"
    assert _label(widget, "fit_mode_1_header").toolTip() == ""
    assert _note(widget).text() == "Branch names belong to fit f9, which this table does not show"
    assert not _note(widget).isHidden() and _note(widget).toolTip() == _note(widget).text()


def test_a_stale_record_keeps_names_drops_its_notes_and_says_when_it_was_made(qtbot):
    widget = _section(qtbot)
    stale = replace(make_unit_links(A06, names=("B3", "B2"), markers=(CHANGE,)), stale=True,
                    stale_text=LINK_WORDS["stale"])
    _show(widget, 2, 0, stale)
    assert _label(widget, "fit_mode_1_header").text() == "Mode 1 · B3"
    assert _note(widget).text() == "Branch links from 2026-11-01T10:00:00; out of date"


def test_a_mode_past_the_names_of_its_link_keeps_the_plain_heading(qtbot):
    widget = _section(qtbot)
    _show(widget, 2, 0, make_unit_links(A06, names=("B3",)))         # the links name one mode; mode 2 has none
    assert _label(widget, "fit_mode_2_header").text() == "Mode 2"


def test_without_a_saved_link_the_fit_section_is_as_before(qtbot):
    widget = _section(qtbot)
    _show(widget, 2, 0, None)
    assert _label(widget, "fit_mode_1_header").text() == "Mode 1"
    assert _note(widget).isHidden()


def test_the_inspector_names_the_modes_of_the_spectrum_it_shows_from_the_saved_links(qtbot, bl43_ws):
    vm, panel = _panel(qtbot, bl43_ws)
    inter = FakeInterState()
    vm.set_inter(inter)
    inter.links = make_link_view(make_unit_links(A03, names=("B7",), markers=(CHANGE,)))
    inter.changed.emit()
    _select(panel, A03)
    heading = panel._fit_section.findChild(QLabel, "fit_mode_1_header")
    assert heading is not None
    assert heading.text() == "Mode 1 · B7 · energy differs from neighbours"
