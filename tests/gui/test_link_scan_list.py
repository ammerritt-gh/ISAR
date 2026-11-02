"""The scan list's Flag column holds review flags only: a spectrum's saved mode links, phonon names included, never
light the Flag cell, count in the set and analyzer summaries, or match the filter. Offscreen Qt with the real panel and
view model; the InterState is the GUI fake.

Prevents: a link note or "links: N" returning to the Flag column, the summary counting link-only spectra as flagged, and
the filter finding a spectrum by a link word the list no longer shows.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from gui.inter_fakes import make_link_view, make_unit_links
from gui.test_inter_scan_list import A, _cell, _flag, _publish, _row, made, sample  # noqa: F401, F811 (fixtures)
from bl43_fixture import KEY_A
from isar_gui.theme import Tone
from isar_gui.views.scan_list import _TONE_ROLE


def _links(inter, *units) -> None:
    """Publish saved links for ``units`` the way the InterState reload does: a new revision, then ``changed``."""
    inter.links = make_link_view(*units)
    inter.changed.emit()


def test_saved_links_add_nothing_to_the_flag_cell_or_its_hover(made):
    panel, vm, inter = made
    _links(inter, make_unit_links(A[4], names=("B2", "B3")))
    cell = _cell(panel, f"spec:{A[4]}", "flag")
    assert cell.text() == "" and cell.toolTip() == ""


def test_a_flagged_spectrum_keeps_its_text_and_tone_and_saved_links_leave_its_hover_alone(made):
    panel, vm, inter = made
    _publish(inter, revision="2", rows=[_row(A[4], flag=_flag("serious", "did_not_settle"))])
    _links(inter, make_unit_links(A[4], names=("B2", "B3")))
    cell = _cell(panel, f"spec:{A[4]}", "flag")
    assert (cell.text(), cell.data(_TONE_ROLE)) == ("serious", Tone.ERR)
    assert "neighbour" not in cell.toolTip()


def test_set_rows_count_review_flags_only(made):
    panel, vm, inter = made
    _publish(inter, revision="2", rows=[_row(A[4], flag=_flag("serious", "did_not_settle"))])
    _links(inter, make_unit_links(A[4], names=("B2",)), make_unit_links(A[5], names=("B2",)))
    flag = _cell(panel, f"set:{KEY_A}", "flag")
    assert flag.text() == "1/4"
    assert flag.toolTip() == "1 of 4 spectra are flagged for review"


def test_the_filter_finds_review_flags_and_not_link_words(made):
    panel, vm, inter = made
    _publish(inter, revision="2", rows=[_row(A[4]), _row(A[5], flag=_flag("warn", "did_not_settle"))])
    _links(inter, make_unit_links(A[4], names=("B2", "B3")), make_unit_links(A[5], names=("B2", "B5")))
    assert "flag" in vm.visible_columns()                 # the review flag lights the column; the links add nothing
    vm.set_filter("flagged")
    assert vm.counts()[0] == 1
    vm.set_filter("link note")
    assert vm.counts()[0] == 0


def test_link_for_reads_the_saved_link_of_a_spectrum_and_is_none_without_one(made):
    panel, vm, inter = made
    link = make_unit_links(A[4], names=("B2",))
    _links(inter, link)
    assert vm.link_for(A[4]) is link
    assert vm.link_for(A[5]) is None
    assert _cell(panel, f"spec:{A[5]}", "flag").text() == ""
