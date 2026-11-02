"""The Scan(s) page with saved mode links, on real widgets (offscreen): branch names on the modes of the fit the
links were made on, link glyphs at each mode's peak energy E* (not omega0), stale links keeping names only, and a
page with no link for the spectrum on it drawing exactly as before."""
from __future__ import annotations

from dataclasses import replace

import pyqtgraph as pg
import pytest

from gui.inter_fakes import FakeInterState, make_link_view, make_overview, make_unit_links
from isar.workspace_api.inter_spectra import MarkerView, SpectrumInter
from isar.plotspec import spec as ps
from isar.workspace_api import Workspace
from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.scans_page import ScansPage
from scans_view_fixture import ONE_MODE, TWO_MODE

CHANGE = MarkerView("change", 2, 1, "energy", "energy differs from neighbours",
                    "Mode 2 is expected at another energy from its neighbours", "warn")


@pytest.fixture
def setup(qtbot, scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    page = ScansPage(ScanListViewModel(ws), ws)
    qtbot.addWidget(page)
    page.set_store(GuiStore(scans_view_ws_readonly))
    inter = FakeInterState()
    page.set_inter(inter)
    page.resize(700, 600)
    page.show()
    page.vm.set_fit("fast")
    return page, inter


def _select(page, *keys):
    page.selection.set(keys)
    page.flush_redraw()
    return page.drawn()


def _mode_names(drawn, key):
    return [c.name for c in drawn.curves if c.key == key and c.role == "mode"]


def _legend(drawn, key):
    return next(line.text for line in drawn.legend if line.entry == key)


def test_modes_take_their_branch_names_on_the_fit_the_links_were_made_on(setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6)))
    assert _mode_names(_select(page, TWO_MODE), TWO_MODE) == ["Mode 1 · B3", "Mode 2 · B2"]


def test_split_candidate_and_unlinked_mode_read_as_the_page_words(setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(
        TWO_MODE, names=("B2", None), peak_E=(2.4, 5.6), unresolved=(("B2", "B3"), ())))
    assert _mode_names(_select(page, TWO_MODE), TWO_MODE) == ["Mode 1 · B2 / B3 (unresolved)", "Mode 2"]


def test_names_are_not_drawn_on_another_fit_and_the_page_says_which_fit_owns_them(setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6), fit_id="r2"))
    drawn = _select(page, TWO_MODE)
    assert _mode_names(drawn, TWO_MODE) == ["Mode 1", "Mode 2"]
    assert "branch names belong to r2; not drawn here" in _legend(drawn, TWO_MODE)


def test_stale_links_keep_names_drop_glyphs_and_say_so(setup):
    page, inter = setup
    unit = make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6), markers=(CHANGE,))
    inter.links = make_link_view(replace(unit, stale=True, stale_text="made from fits that have changed since"))
    drawn = _select(page, TWO_MODE)
    assert _mode_names(drawn, TWO_MODE) == ["Mode 1 · B3", "Mode 2 · B2"]
    assert "links out of date" in _legend(drawn, TWO_MODE)
    assert not [m for m in page._drawn_state.main_spec.markers if m.kind is ps.MarkerKind.LINK_CHANGE]


def test_a_stale_link_whose_own_fit_changed_says_so_and_names_nothing(setup):
    page, inter = setup
    unit = make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6), current=False, fit_id="r2")
    inter.links = make_link_view(replace(unit, stale=True, stale_text="made from fits that have changed since"))
    drawn = _select(page, TWO_MODE)
    assert _mode_names(drawn, TWO_MODE) == ["Mode 1", "Mode 2"]
    assert "links out of date" in _legend(drawn, TWO_MODE)


def test_link_glyph_sits_at_the_peak_energy_with_its_words_as_hover(setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 3.0), markers=(CHANGE,)))
    _select(page, TWO_MODE)
    glyphs = [m for m in page._drawn_state.main_spec.markers if m.kind is ps.MarkerKind.LINK_CHANGE]
    # Mode 2 sits at omega0 5.5 meV; its link matched on the peak at 3.0 meV, so the glyph goes there.
    assert [(m.x, m.label, m.tooltip) for m in glyphs] == [(3.0, CHANGE.label, CHANGE.words)]


def test_branch_names_are_drawn_at_the_peak_only_with_the_peak_lines_switch(qtbot, setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6)))
    _select(page, TWO_MODE)
    texts = lambda: {i.toPlainText() for i in page._main_plot.items if isinstance(i, pg.TextItem)}
    assert {"B3", "B2"} <= texts()
    page.vm.set_display_option("peak_lines", False)
    page.flush_redraw()
    assert not {"B3", "B2"} & texts()
    assert _mode_names(page.drawn(), TWO_MODE) == ["Mode 1 · B3", "Mode 2 · B2"]    # the legend keeps the names


def test_a_link_for_another_spectrum_leaves_the_page_exactly_as_before(setup):
    page, inter = setup
    _select(page, TWO_MODE)
    before = page._drawn_state.main_spec.to_dict()
    inter.links = make_link_view(make_unit_links(ONE_MODE, names=("B1",), peak_E=(4.0,)))
    inter.changed.emit()
    page.flush_redraw()
    assert page._drawn_state.main_spec.to_dict() == before
    assert _mode_names(page.drawn(), TWO_MODE) == ["Mode 1", "Mode 2"]


def test_a_refit_that_cannot_be_drawn_leaves_its_names_off_the_solo_curve(setup):
    page, inter = setup
    row = SpectrumInter(key=TWO_MODE, fit_id="r2", in_use=True, source="batch", label="Batch 1 · ring 2", flag=None,
                        locked=False, locked_fit_id=None, lock_stale=False, stale=False, stale_text=None)
    inter.overview = make_overview(by_key={TWO_MODE: row})    # r2 is in use but not in the log: it cannot be drawn
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6), fit_id="r2",
                                                 markers=(CHANGE,)))
    drawn = _select(page, TWO_MODE)
    assert _mode_names(drawn, TWO_MODE) == ["Mode 1", "Mode 2"]
    assert not [m for m in page._drawn_state.main_spec.markers if m.kind is ps.MarkerKind.LINK_CHANGE]
    assert "branch names belong to Batch 1 · ring 2; not drawn here" in _legend(drawn, TWO_MODE)
