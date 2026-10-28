"""Amendment 7: plain status words in the scan list and the inspector, the fitting code's word on hover and in the
export, and the Help page generated from the same table. Offscreen Qt where a widget is involved."""
from __future__ import annotations

import csv
from typing import get_args

import pytest

import isar_gui.status_words as sw
from isar.analyze.fast_pass import CountState
from isar.workspace_api import scan_list as facade
from isar_gui.theme import DEEP, FAST, PARSED
from isar_gui.viewmodels.scan_list import _status_cell, spectrum_matches

# The deep pass writes these (isar/session/deep_delivery.py: deliver and _refused).
DEEP_STATES = ("resolved", "close_call", "search_limited", "refused")


def _facade_words() -> dict[str, set[str]]:
    """Every word the scan-list facade can put in a Status, by column: its own constants, the fast pass's count
    states and the deep pass's states, and the Parsed gap words it builds from the missing T and Q."""
    gaps = {"no T", "no Q", "no T, no Q"}
    return {PARSED: {facade.PARSED_OK, facade.NOT_READ} | gaps,
            FAST: set(get_args(CountState)) | {facade.HELD, facade.NOT_FITTED, facade.NOT_RECORDED, facade.NOT_RUN},
            DEEP: set(DEEP_STATES) | {facade.NOT_RUN}}


def test_every_word_the_facade_can_produce_has_a_plain_word_and_a_meaning():
    for column, words in _facade_words().items():
        missing = words - set(sw.TABLE[column])
        assert not missing, f"{column}: no entry for {sorted(missing)}"
        for word in words:
            assert sw.plain_word(column, word, "a03: no resolution curve for a03") and sw.meaning(column, word), (column, word)
    # and nothing stale: every entry is a word the facade can produce
    for column, entries in sw.TABLE.items():
        assert set(entries) <= _facade_words()[column], (column, set(entries) - _facade_words()[column])


def test_the_plain_words_the_amendment_names():
    assert sw.plain_word(FAST, "resolved") == "clear" and sw.plain_word(FAST, "close_call") == "close call"
    assert sw.plain_word(FAST, "search_limited") == "search limit reached"
    assert sw.plain_word(FAST, "not run") == "not run"
    assert sw.plain_word(FAST, "held", "a03: no resolution curve for a03 at slit 60x60") == "held: needs resolution curves"
    assert sw.plain_word(FAST, "held", "energy axis uncalibrated (needs_conversion_and_zero)") == "held: needs an energy zero"
    assert sw.plain_word(FAST, "held", "a04: counts may not be raw: 3 of 60 bins are off an integer") == \
        "held: needs its counts accepted"
    assert sw.plain_word(FAST, "held", "something new") == "held: see the reason"      # not guessed at
    assert sw.plain_word(FAST, "a word from the future") == "a word from the future"   # shown as stored


def test_a_deep_search_limit_names_the_fast_pass_ceiling_or_a_suspect_model():
    meaning = sw.meaning(DEEP, "search_limited")
    assert "fast pass hit its peak-count ceiling" in meaning and "suspect after restarts" in meaning


def test_a_cell_shows_the_plain_word_and_its_hover_the_code_word_and_the_meaning():
    from isar.workspace_api.scan_list import Status
    cell = _status_cell(Status("close_call", True, "close call: preferred count 2 (P 0.64)"), FAST)
    assert cell.text == "close call"
    assert "close_call" in cell.tooltip and sw.meaning(FAST, "close_call") in cell.tooltip
    held = _status_cell(Status("held", False, "a03: no resolution curve for a03"), FAST)
    assert held.text == "held: needs resolution curves" and "(the fitting code's word: held)" in held.tooltip


def test_the_filter_matches_both_the_plain_word_and_the_fitting_codes_word():
    from isar.workspace_api.scan_list import Status
    from dataclasses import dataclass

    @dataclass
    class Row:
        analyzer: str = "a03"
        file_name: str = "f"
        parsed: Status = Status("ok", True)
        fast: Status = Status("search_limited", True)
        deep: Status = Status("not run", False)

    ss = type("S", (), {"label": "58", "temperature": facade.Fact(None, "K", "", "missing", None)})()
    for term in ("search limit", "search_limited", "limit reached"):
        assert spectrum_matches([term], ss, Row()), term
    assert not spectrum_matches(["close"], ss, Row())


def test_the_help_page_lists_every_table_entry_and_says_the_export_keeps_the_codes_words():
    page = sw.page_html()
    for column, entries in sw.TABLE.items():
        assert sw.TITLES[column] in page
        for word, entry in entries.items():
            assert word in page and entry.meaning.split(":")[0].split(";")[0][:30] in page, (column, word)
    assert "writes the fitting code's words" in page


def test_the_export_keeps_the_fitting_codes_words(bl43_ws):
    """Export results writes the stored words: the display table is a window matter only."""
    from isar.report.export import export_workspace
    summary = export_workspace(bl43_ws)
    with open(summary.folder / "modes.csv", encoding="utf-8", newline="") as fh:
        states = {row["count_state"] for row in csv.DictReader(fh)}
    assert {"close_call", "search_limited"} <= states
    assert not states & {"clear", "close call", "search limit reached"}


def _deep_cell(trusted: bool):
    """The cell the scan list shows for a stored deep result whose record carries this trusted flag."""
    from types import SimpleNamespace
    fv = SimpleNamespace(deep_pass={"state": "resolved", "trusted": trusted, "reason": "resolved: count 2 (P 0.85)"})
    return _status_cell(facade._deep(fv), DEEP)


def test_an_untrusted_deep_count_says_provisional_in_the_word_and_the_hover():
    """Provisional sampler settings make every deep result untrusted today: the plain word must not claim a settled count."""
    from types import SimpleNamespace
    untrusted, trusted = _deep_cell(False), _deep_cell(True)
    assert untrusted.text == "clear (provisional) · currentness not recorded" and "provisional" in untrusted.tooltip
    assert "note under Deep queue in the Processing panel" in untrusted.tooltip         # job amendment 3: the pointer
    assert trusted.text == "clear · currentness not recorded" and "provisional" not in trusted.tooltip
    assert "Deep queue" not in trusted.tooltip
    refused = facade._deep(SimpleNamespace(deep_pass={"state": "refused", "trusted": False, "reason": "no fast-pass record"}))
    assert _status_cell(refused, DEEP).text == "refused · currentness not recorded"                 # no count to qualify


def test_no_meaning_claims_a_settled_count_or_a_removed_batch_path():
    """The deep "clear" meaning claims no settled count while the calibration is provisional; no meaning names the
    removed run-again path or says a time budget ended a fresh result."""
    assert "settles" not in sw.meaning(sw.DEEP, "resolved") and "prefers" in sw.meaning(sw.DEEP, "resolved")
    every = " ".join(e.meaning for entries in sw.TABLE.values() for e in entries.values()) + sw.page_html()
    for stale in ("retried on the next run", "a re-run refits", "run the analysis again", "ran out of its time budget"):
        assert stale not in every, stale
