"""The Scan(s) page with saved mode links, on real widgets (offscreen): every mode of a spectrum whose links apply is
labelled with its phonon (P3), a phonon keeps one colour in every spectrum, the energy-order switch wins over the
links, labels sit at each mode's peak E*, and a spectrum whose links do not apply falls back to energy order with
its note.

Prevents: a mode named by a split candidate or a link note on the plot, a phonon drawn in two colours, names drawn on
another fit or with stale links kept as if current, and a page with no link for its spectrum drawing differently.
"""
from __future__ import annotations

from dataclasses import replace

import pyqtgraph as pg
import pytest

from gui.inter_fakes import FakeInterState, make_link_view, make_overview, make_unit_links
from isar.plotspec import spec as ps
from isar.workspace_api import Workspace
from isar.workspace_api.inter_spectra import SpectrumInter
from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.scans_page import ScansPage
from scans_view_fixture import ONE_MODE, TWO_MODE


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


def _mode_colours(page, label):
    return {s.color for s in page._drawn_state.main_spec.series if s.label == label}


def _texts(page) -> set[str]:
    return {i.toPlainText() for i in page._main_plot.items if isinstance(i, pg.TextItem)}


def test_every_mode_of_a_linked_spectrum_is_labelled_with_its_phonon(setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6)))
    assert _mode_names(_select(page, TWO_MODE), TWO_MODE) == ["P3", "P2"]


def test_a_lone_phonon_is_labelled_with_its_own_name_beside_linked_ones(setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B9"), peak_E=(2.4, 5.6)))
    assert _mode_names(_select(page, TWO_MODE), TWO_MODE) == ["P3", "P9"]


def test_a_phonon_keeps_one_colour_in_every_spectrum_in_ascending_number(setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(ONE_MODE, names=("B3",), peak_E=(4.0,)),
                                 make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6)))
    drawn = _select(page, ONE_MODE, TWO_MODE)
    assert drawn is not None
    p3, p2 = _mode_colours(page, "P3"), _mode_colours(page, "P2")
    assert len(p3) == 1 and len(p2) == 1 and p3 != p2
    assert p2 == {ps.TRACE_COLORS[0]} and p3 == {ps.TRACE_COLORS[1]}          # P2 is the lower number


def test_energy_order_switch_labels_every_mode_by_number_and_no_phonon_name_is_drawn(setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6)))
    page.vm.set_display_option("energy_order", True)
    drawn = _select(page, TWO_MODE)
    assert _mode_names(drawn, TWO_MODE) == ["Mode 1", "Mode 2"]
    assert not [line.text for line in drawn.legend if "P3" in line.text or "P2" in line.text]
    assert _texts(page) >= {"Mode 1", "Mode 2"} and not _texts(page) & {"P3", "P2"}


def test_peak_labels_sit_with_the_phonons_and_go_with_the_peak_lines_switch(qtbot, setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6)))
    _select(page, TWO_MODE)
    assert {"P3", "P2"} <= _texts(page)
    page.vm.set_display_option("peak_lines", False)
    page.flush_redraw()
    assert not {"P3", "P2"} & _texts(page)
    assert _mode_names(page.drawn(), TWO_MODE) == ["P3", "P2"]                 # the legend keeps the names


def test_a_spectrum_without_links_is_labelled_by_energy_order_at_its_peak(setup):
    page, _ = setup
    _select(page, TWO_MODE)
    assert _mode_names(page.drawn(), TWO_MODE) == ["Mode 1", "Mode 2"]
    assert {"Mode 1", "Mode 2"} <= _texts(page)


def test_names_are_not_drawn_on_another_fit_and_the_page_says_which_fit_owns_them(setup):
    page, inter = setup
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6), fit_id="r2"))
    drawn = _select(page, TWO_MODE)
    assert _mode_names(drawn, TWO_MODE) == ["Mode 1", "Mode 2"]
    assert "phonon names belong to r2; not drawn here" in _legend(drawn, TWO_MODE)


def test_stale_links_keep_their_names_and_say_so(setup):
    page, inter = setup
    unit = make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6))
    inter.links = make_link_view(replace(unit, stale=True, stale_text="made from fits that have changed since"))
    drawn = _select(page, TWO_MODE)
    assert _mode_names(drawn, TWO_MODE) == ["P3", "P2"]
    assert "links out of date" in _legend(drawn, TWO_MODE)


def test_a_stale_link_whose_own_fit_changed_says_so_and_names_nothing(setup):
    page, inter = setup
    unit = make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6), current=False, fit_id="r2")
    inter.links = make_link_view(replace(unit, stale=True, stale_text="made from fits that have changed since"))
    drawn = _select(page, TWO_MODE)
    assert _mode_names(drawn, TWO_MODE) == ["Mode 1", "Mode 2"]
    assert "links out of date" in _legend(drawn, TWO_MODE)


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
    inter.links = make_link_view(make_unit_links(TWO_MODE, names=("B3", "B2"), peak_E=(2.4, 5.6), fit_id="r2"))
    drawn = _select(page, TWO_MODE)
    assert _mode_names(drawn, TWO_MODE) == ["Mode 1", "Mode 2"]
    assert "phonon names belong to Batch 1 · ring 2; not drawn here" in _legend(drawn, TWO_MODE)
