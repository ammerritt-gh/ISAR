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
from isar.workspace_api.inter_spectra import ChangeView, MarkerView
from isar_gui.theme import Tone
from isar_gui.views.scan_list import _TONE_ROLE

# Every kind of link note the old Flag column showed: a marker of each kind and a listed change of each kind,
# with the words it showed on hover. Their words, count, severity and filter terms must reach no part of the list.
NOTES = {
    "markers": (MarkerView("change", 2, 1, "energy", "energy differs from neighbours",
                           "Mode 2 energy differs from neighbours", "warn"),
                MarkerView("at_cap", 2, 2, "width", "at the width cap (wants to be broader)",
                           "Mode 3 is at the width cap", "info"),
                MarkerView("unlinked", 3, 3, "intensity", "no link to a neighbour",
                           "Mode 4 has no link to a neighbour", "warn"),
                MarkerView("contradiction", 2, 0, None, "links contradict each other",
                           "Mode 1 links contradict each other", "serious")),
    "changes": (ChangeView("add", None, ("B5",), 3.2, "B5 is expected near 3.2 meV here"),
                ChangeView("split", 1, ("B2", "B3"), None, "Mode 2 may be two branches, B2 and B3; not settled"),
                ChangeView("drop", 2, (), None, "Mode 3 follows no neighbour")),
}
NOTE_WORDS = tuple(note.words for note in NOTES["markers"] + NOTES["changes"])


def _links(inter, *units) -> None:
    """Publish saved links for ``units`` the way the InterState reload does: a new revision, then ``changed``."""
    inter.links = make_link_view(*units)
    inter.changed.emit()


def test_saved_links_add_nothing_to_the_flag_cell_or_its_hover(made):
    panel, vm, inter = made
    _links(inter, make_unit_links(A[4], names=("B2", "B3"), **NOTES))
    cell = _cell(panel, f"spec:{A[4]}", "flag")
    assert (cell.text(), cell.toolTip(), cell.data(_TONE_ROLE)) == ("", "", None)     # no text, hover or dot
    assert "flag" not in vm.visible_columns()                  # the column is not shown for links alone
    assert len(vm.link_for(A[4]).markers) == len(NOTES["markers"])   # the notes stay in the record for slice S4


def test_a_flagged_spectrum_keeps_its_text_and_tone_and_saved_links_leave_its_hover_alone(made):
    panel, vm, inter = made
    _publish(inter, revision="2", rows=[_row(A[4], flag=_flag("serious", "did_not_settle"))])
    _links(inter, make_unit_links(A[4], names=("B2", "B3"), **NOTES))
    cell = _cell(panel, f"spec:{A[4]}", "flag")
    assert (cell.text(), cell.data(_TONE_ROLE)) == ("serious", Tone.ERR)
    assert not any(words in cell.toolTip() for words in NOTE_WORDS)


def test_set_rows_count_review_flags_only(made):
    panel, vm, inter = made
    _publish(inter, revision="2", rows=[_row(A[4], flag=_flag("serious", "did_not_settle"))])
    _links(inter, make_unit_links(A[4], names=("B2",), **NOTES), make_unit_links(A[5], names=("B2",), **NOTES))
    flag = _cell(panel, f"set:{KEY_A}", "flag")
    assert flag.text() == "1/4"
    assert flag.toolTip() == "1 of 4 spectra are flagged for review"


def test_the_filter_finds_review_flags_and_not_link_words(made):
    panel, vm, inter = made
    _publish(inter, revision="2", rows=[_row(A[4]), _row(A[5], flag=_flag("warn", "did_not_settle"))])
    _links(inter, make_unit_links(A[4], names=("B2", "B3"), **NOTES),
           make_unit_links(A[5], names=("B2", "B5"), **NOTES))
    assert "flag" in vm.visible_columns()                 # the review flag lights the column; the links add nothing
    vm.set_filter("flagged")
    assert vm.counts()[0] == 1
    for term in ("link note", "unlinked"):                # the words the old filter matched for a link note
        vm.set_filter(term)
        assert vm.counts()[0] == 0, term


def test_link_for_reads_the_saved_link_of_a_spectrum_and_is_none_without_one(made):
    panel, vm, inter = made
    link = make_unit_links(A[4], names=("B2",))
    _links(inter, link)
    assert vm.link_for(A[4]) is link
    assert vm.link_for(A[5]) is None
    assert _cell(panel, f"spec:{A[5]}", "flag").text() == ""
