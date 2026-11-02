"""The scan list's link notes (S3U2): a spectrum's saved mode-link markers in the Flag column, on the set and analyzer
counts, in the auto column rule and the filter words. They sit beside the review flag and never change it. Offscreen Qt
with the real panel and view model; the InterState is the GUI fake."""
from __future__ import annotations

from dataclasses import replace

import pytest

pytest.importorskip("PySide6")

from gui.inter_fakes import make_link_view, make_unit_links
from gui.test_inter_scan_list import A, _cell, _flag, _publish, _row, made, sample  # noqa: F401, F811 (fixtures)
from bl43_fixture import KEY_A
from isar.workspace_api.inter_spectra import LINK_WORDS, MarkerView
from isar_gui.theme import Tone
from isar_gui.views.scan_list import _TONE_ROLE

CHANGE = MarkerView("change", 2, 0, "energy", "energy differs from neighbours",
                    "mode 1 peaks at 4.00 meV; its neighbours predict 3.00 ± 0.20 meV.", "warn")
UNLINKED = MarkerView("unlinked", 3, 1, None, "no link to a neighbour",
                      "mode 2 at 9.10 meV links to no neighbouring spectrum.", "warn")
CONTRADICTION = MarkerView("contradiction", 2, 0, None, "links contradict each other",
                           "two modes of one spectrum meet on one branch.", "serious")


def _links(inter, *units) -> None:
    """Publish saved links for ``units`` the way the InterState reload does: a new revision, then ``changed``."""
    inter.links = make_link_view(*units)
    inter.changed.emit()


def test_a_link_note_shows_as_links_n_with_its_words_and_the_worst_tone(made):
    panel, vm, inter = made
    _links(inter, make_unit_links(A[4], names=("B2", "B3"), markers=(CHANGE, UNLINKED)))
    cell = _cell(panel, f"spec:{A[4]}", "flag")
    assert (cell.text(), cell.data(_TONE_ROLE)) == ("links: 2", Tone.WARN)
    assert CHANGE.words in cell.toolTip() and UNLINKED.words in cell.toolTip()
    _links(inter, make_unit_links(A[4], names=("B2", "B3"), markers=(CONTRADICTION,)))
    assert _cell(panel, f"spec:{A[4]}", "flag").data(_TONE_ROLE) == Tone.ERR


def test_a_flagged_spectrum_keeps_its_text_and_tone_and_gains_the_link_words_on_hover(made):
    panel, vm, inter = made
    _publish(inter, revision="2", rows=[_row(A[4], flag=_flag("serious", "did_not_settle"))])
    _links(inter, make_unit_links(A[4], names=("B2", "B3"), markers=(CHANGE,)))
    cell = _cell(panel, f"spec:{A[4]}", "flag")
    assert (cell.text(), cell.data(_TONE_ROLE)) == ("serious", Tone.ERR)
    assert CHANGE.words in cell.toolTip() and "4.1" in cell.toolTip()


def test_stale_links_show_nothing_in_the_flag_column_and_are_not_counted(made):
    panel, vm, inter = made
    stale = replace(make_unit_links(A[4], names=("B2",), markers=(CHANGE,)), stale=True,
                    stale_text=LINK_WORDS["stale"])
    _links(inter, stale)
    assert vm.link_for(A[4]) is stale                              # the names are still read
    assert _cell(panel, f"spec:{A[4]}", "flag").text() == ""
    assert _cell(panel, f"set:{KEY_A}", "flag").text() == ""
    assert "flag" not in vm.visible_columns()
    vm.set_filter("link note")
    assert vm.counts()[0] == 0


def test_set_rows_count_flagged_and_link_noted_spectra_once_each(made):
    panel, vm, inter = made
    _publish(inter, revision="2", rows=[_row(A[4], flag=_flag("serious", "did_not_settle"))])
    _links(inter, make_unit_links(A[4], names=("B2",), markers=(CHANGE,)),
           make_unit_links(A[5], names=("B2",), markers=(UNLINKED,)))
    flag = _cell(panel, f"set:{KEY_A}", "flag")
    assert flag.text() == "2/4"
    assert flag.toolTip() == "2 of 4 spectra are flagged for review or have a link note"


def test_a_link_note_alone_is_not_on_the_review_list_and_the_filter_finds_it(made):
    panel, vm, inter = made
    _publish(inter, revision="2", rows=[_row(A[4]), _row(A[5])])   # unflagged rows: the filter sees them
    _links(inter, make_unit_links(A[4], names=("B2", "B3"), markers=(CHANGE,)),
           make_unit_links(A[5], names=("B2", None), markers=(UNLINKED,)))
    assert "flag" in vm.visible_columns()
    vm.set_filter("flagged")
    assert vm.counts()[0] == 0
    vm.set_filter("link note")
    assert vm.counts()[0] == 2
    vm.set_filter("unlinked")
    assert vm.counts()[0] == 1


def test_link_for_reads_the_saved_link_of_a_spectrum_and_is_none_without_one(made):
    panel, vm, inter = made
    link = make_unit_links(A[4], names=("B2",))
    _links(inter, link)
    assert vm.link_for(A[4]) is link
    assert vm.link_for(A[5]) is None
    assert _cell(panel, f"spec:{A[5]}", "flag").text() == ""
