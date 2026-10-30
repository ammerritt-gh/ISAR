"""The inter-spectra words table (C-gui-words) and the description helper the GUI tests share."""
from __future__ import annotations

import pytest

from gui.inter_helpers import assert_described
from isar.analyze.coupled.types import FLAG_CODES
from isar_gui.inter_words import BATCH_TIPS, BATCH_WORDS, REASON_WORDS, batch_line, batch_tip, reason_sentence


def test_every_flag_code_has_a_sentence():
    assert [code for code in FLAG_CODES if code not in REASON_WORDS] == []


def test_an_unknown_code_passes_through():
    assert reason_sentence("zzz", "x") == "zzz: x"


def test_assert_described_catches_an_undescribed_button(qtbot):
    from PySide6.QtWidgets import QPushButton, QWidget

    window = QWidget()
    qtbot.addWidget(window)
    button = QPushButton("Set as anchor", window)
    with pytest.raises(AssertionError, match="no tooltip"):
        assert_described(window)
    button.setToolTip("Mark the selected spectra as anchors")
    assert_described(window)


def test_batch_line_names_kind_stage_and_done_of_total():
    assert batch_line("anchored", "q", "ring", 2, 9, 24, None) == "Anchored batch, spread in q · ring 2 · 9 of 24 done"
    assert batch_line("simultaneous", None, "sweep", 3, 4, 5, None) == "Simultaneous batch · sweep 3 · 4 of 5 done"
    assert batch_line("anchored", "qT", "plan", 0, 0, 24, None) == (
        "Anchored batch, spread in q and T · planning · 0 of 24 done")


def test_a_sweep_or_ring_numbered_zero_reads_as_the_start_of_number_one():
    # the runner reports stage "sweep" with index 0 while a simultaneous batch assigns its first sweep
    line = batch_line("simultaneous", None, "sweep", 0, 0, 5, None)
    assert line == "Simultaneous batch · sweep 1 starting · 0 of 5 done" and "sweep 0" not in line
    assert "ring 0" not in batch_line("anchored", "q", "ring", 0, 0, 5, None)


def test_the_placeholder_before_the_first_event_has_no_stage_and_no_counts():
    assert batch_line("anchored", "T", None, None, 0, 0, None) == "Anchored batch, spread in T · starting"
    assert "of" not in batch_line("simultaneous", None, None, None, 0, 0, None)


def test_members_elsewhere_and_how_the_batch_ended():
    assert batch_line("anchored", "q", "ring", 2, 9, 24, None, 6) == (
        "Anchored batch, spread in q · ring 2 · 9 of 24 done · 6 in other scans")
    for ended, word in (("completed", "finished"), ("stopped", "stopped"), ("failed", "failed")):
        assert batch_line("simultaneous", None, "sweep", 3, 5, 5, ended).endswith(f" · {word}")
    assert batch_line("anchored", "q", None, None, 0, 0, "stopped") == "Anchored batch, spread in q · stopped"


def test_the_batch_words_cover_every_state_and_the_tips_the_three_without_a_reason():
    assert set(BATCH_WORDS) == {"waiting", "fitting", "done", "refused", "failed", "not_reached"}
    assert BATCH_WORDS["fitting"] == "fitting now" and set(BATCH_TIPS) == {"waiting", "fitting", "done"}
    tip = batch_tip({"a": "waiting", "b": "fitting", "c": "done", "d": "done", "e": "held"}, "it stopped")
    assert tip == "waiting 1 · fitting now 1 · done 2 · 1 anchors and locks not refitted\nit stopped"
    assert batch_tip({}) == "no members yet"
