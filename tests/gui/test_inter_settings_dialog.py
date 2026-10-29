"""The Inter-spectra fitting settings dialog (S1U10): its rows, its deltas, and what a save sends."""
from __future__ import annotations

from isar.analyze.coupled.settings import SETTING_DEFS, InterSettings
from isar_gui.inter_words import CALIBRATED_SYNTHETIC, UNCALIBRATED
from isar_gui.viewmodels.inter_settings import LABELS, changes, default_text, rows

SHOWN = [d.name for d in SETTING_DEFS if d.group != "hidden"]


def _shown_rows(**overrides):
    return rows(SETTING_DEFS, {**InterSettings().to_values(), **overrides})


def test_shown_rows_follow_table_order_and_hide_the_hidden_settings():
    names = [r.setting.name for r in _shown_rows()]
    assert names == SHOWN
    assert names[:3] == ["q_tolerance_meV", "t_tolerance_meV", "flag_threshold"]
    assert "reference_T_K" not in names


def test_every_shown_setting_has_a_label_and_a_help_sentence():
    for name in SHOWN:
        assert LABELS[name].strip(), name
        assert next(d for d in SETTING_DEFS if d.name == name).help.strip(), name


def test_default_text_names_unit_and_mark():
    by_name = {r.setting.name: r for r in _shown_rows()}
    assert default_text(by_name["q_tolerance_meV"]) == f"Default: 2 meV · {UNCALIBRATED}"
    assert default_text(by_name["flag_threshold"]) == f"Default: 54 · {CALIBRATED_SYNTHETIC}"
    assert default_text(by_name["shape_family"]) == f"Default: welsch · {UNCALIBRATED}"


def test_calibrated_rows_are_marked_and_the_rest_are_uncalibrated():
    by_name = {r.setting.name: r for r in _shown_rows()}
    assert by_name["flag_threshold"].mark == CALIBRATED_SYNTHETIC
    assert by_name["t_tolerance_meV"].mark == CALIBRATED_SYNTHETIC
    assert by_name["q_tolerance_meV"].mark == UNCALIBRATED


def test_a_save_sends_only_the_value_that_changed():
    edited = {r.setting.name: r.value for r in _shown_rows()}
    edited["sweep_cap"] = 40
    assert changes(_shown_rows(), edited) == {"sweep_cap": 40}


def test_typing_the_default_back_sends_none_and_nothing_edited_sends_nothing():
    shown = _shown_rows(q_tolerance_meV=3.0)
    edited = {r.setting.name: r.value for r in shown}
    assert changes(shown, edited) == {}
    edited["q_tolerance_meV"] = 2.0          # the default: back to default, not a stored copy of it
    assert changes(shown, edited) == {"q_tolerance_meV": None}
