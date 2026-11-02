"""The Analyzer array's cells carry no saved-link indicator: no strip under the plot, no dot, no link words in the hover
or the accessible description, even with links saved and stale. Picking stays as it was.

The facade's words come in through ``FakeInterState``; the cells would show them if they still drew them. Spectra are
the scans-view fixture's (made-up data, hard rule 10).
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from gui.array_helpers import open_dock
from gui.inter_fakes import FakeInterState, make_link_view, make_unit_links
from isar.workspace_api.inter_spectra import MarkerView
from isar_gui.views.analyzer_cell import GESTURE_TIP
from scans_view_fixture import SPECTRA_A

A3, A4, A5, A6, A7, A8, A9 = SPECTRA_A

CHANGE = MarkerView("change", 2, 0, "energy", "energy differs from neighbours",
                    "Mode 1 is at a different energy from its neighbours", "warn")


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


def test_a_cell_with_a_current_or_stale_link_shows_no_link_strip_dot_or_words(env_for):
    env = env_for(make_unit_links(A3, names=("B1", "B2", "B5"), markers=(CHANGE,)),
                  make_unit_links(A4, names=("B1", "B5", "B6"), stale=True))
    for key in ("a03", "a04"):
        cell = env.dock.cell_at(key)
        assert not hasattr(cell, "link_strip") and not hasattr(cell, "link_dot")
        text = f"{cell.toolTip()}\n{cell.accessibleDescription()}"
        assert "B1" not in text and "links" not in text.lower() and "Mode 1" not in text
        assert GESTURE_TIP in cell.toolTip()
