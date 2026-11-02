"""The saved mode links on the Analyzer array's cells, on real widgets (offscreen): caption, dot, stale, absent.

The facade's words come in through ``FakeInterState`` (S3U1's builders); the cells read them and compute nothing.
Spectra are the scans-view fixture's (made-up data, hard rule 10).
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from gui.array_helpers import open_dock
from gui.inter_fakes import FakeInterState, make_link_view, make_unit_links
from isar.workspace_api.inter_spectra import MarkerView
from isar_gui.theme import Tone, dot_icon
from isar_gui.viewmodels.analyzer_array import CellLinks
from isar_gui.views.analyzer_cell import GESTURE_TIP, STALE_INK
from scans_view_fixture import SPECTRA_A

A3, A4, A5, A6, A7, A8, A9 = SPECTRA_A

CHANGE = MarkerView("change", 2, 0, "energy", "energy differs from neighbours",
                    "Mode 1 is at a different energy from its neighbours", "warn")
UNLINKED = MarkerView("unlinked", 3, 2, None, "no link to a neighbour", "Mode 3 follows no neighbour", "warn")
CONTRADICTION = MarkerView("contradiction", 2, 1, None, "links contradict each other",
                           "Modes 2 and 3 are on one branch", "serious")


@pytest.fixture
def env_for(qtbot, scans_view_ws_readonly):
    def make(*units):
        inter = FakeInterState()
        inter.links = make_link_view(*units)
        env = open_dock(qtbot, scans_view_ws_readonly, inter=inter)
        env.selection.set((A6,))
        env.dock.flush_redraw()
        QApplication.processEvents()                          # the new cells are shown on the event loop
        return env
    return make


def test_a_current_link_names_the_branches_with_its_notes_and_a_warn_dot(env_for):
    env = env_for(make_unit_links(A3, names=("B1", "B2", None), markers=(CHANGE, UNLINKED)))
    cell = env.dock.cell_at("a03")
    assert cell.cell.links == CellLinks("B1 · B2", cell.cell.links.tip, "warn", 1, 1, False)
    assert cell.link_strip.isVisible() and cell.link_label.full_text() == "B1 · B2"
    assert cell.link_dot.isVisible()
    assert cell.link_dot.pixmap().toImage() == dot_icon(Tone.WARN).pixmap(10, 10).toImage()
    assert cell.cell.links.tip.splitlines()[0] == "Links: B1 · B2 (1 change note, 1 unlinked mode)"
    assert "Mode 1 is at a different energy from its neighbours" in cell.cell.links.tip
    assert cell.link_label.toolTip() == cell.cell.links.tip
    assert "branches B1 · B2" in cell.accessibleDescription()
    assert "Mode 3 follows no neighbour" in cell.toolTip() and GESTURE_TIP in cell.toolTip()


def test_a_stale_record_greys_its_names_and_drops_the_dot(env_for):
    env = env_for(make_unit_links(A4, names=("B1", None), stale=True))
    cell = env.dock.cell_at("a04")
    assert cell.cell.links.stale and cell.cell.links.caption == "B1" and cell.cell.links.severity is None
    assert cell.link_strip.isVisible() and cell.link_label.full_text() == "B1"
    assert STALE_INK in cell.link_label.styleSheet()                  # grey, and the dot is gone instead
    assert cell.link_dot.isHidden()
    assert "branch names from earlier links" in cell.link_label.toolTip()
    assert "links out of date" in cell.accessibleDescription()


def test_no_saved_link_leaves_an_empty_strip_that_keeps_its_height(env_for):
    env = env_for(make_unit_links(A4, names=("B1", None)), make_unit_links(A7, current=False))
    absent, not_current = env.dock.cell_at("a06"), env.dock.cell_at("a07")
    for cell in (absent, not_current):
        assert cell.cell.links is None and cell.link_strip.isHidden()
        assert cell.link_strip.sizePolicy().retainSizeWhenHidden()     # the plots of a row keep their line
    assert "branches" not in absent.accessibleDescription()


def test_the_worst_note_sets_the_dot_and_unresolved_pairs_show_both_names(env_for):
    names, unresolved = ("B2", "B4"), (("B2", "B3"), ())
    env = env_for(make_unit_links(A3, names=names, unresolved=unresolved, markers=(CHANGE, CONTRADICTION)),
                  make_unit_links(A5, names=("B1",)))
    worst = env.dock.cell_at("a03")
    assert worst.cell.links.caption == "B2 / B3? · B4" and worst.cell.links.severity == "serious"
    assert worst.cell.links.changes == 2 and worst.cell.links.unlinked == 0
    assert worst.link_dot.pixmap().toImage() == dot_icon(Tone.ERR).pixmap(10, 10).toImage()
    plain = env.dock.cell_at("a05")
    assert plain.cell.links.severity is None and plain.cell.links.caption == "B1"
    assert plain.link_dot.pixmap().toImage() == dot_icon(Tone.OK).pixmap(10, 10).toImage()
