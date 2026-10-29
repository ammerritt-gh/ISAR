"""The inter-spectra words table (C-gui-words) and the description helper the GUI tests share."""
from __future__ import annotations

import pytest

from gui.inter_helpers import assert_described
from isar.analyze.coupled.types import FLAG_CODES
from isar_gui.inter_words import REASON_WORDS, reason_sentence


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
