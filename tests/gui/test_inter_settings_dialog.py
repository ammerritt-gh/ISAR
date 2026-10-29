"""The Inter-spectra fitting settings dialog (S1U10): its rows, its deltas, and what a save sends."""
from __future__ import annotations

import dataclasses

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QDialog, QLabel, QPushButton, QWidget

from gui.inter_fakes import FakeInterState
from gui.inter_helpers import assert_described
from isar.analyze.coupled.settings import SETTING_DEFS, InterSettings
from isar_gui.inter_words import CALIBRATED_SYNTHETIC, UNCALIBRATED
from isar_gui.viewmodels.inter_settings import LABELS, changes, default_text, rows
from isar_gui.views.inter_settings_dialog import InterSettingsDialog, _widget_for

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


# ---- the dialog --------------------------------------------------------------------------------------------

ACCEPTED = int(QDialog.DialogCode.Accepted)
REJECTED = int(QDialog.DialogCode.Rejected)


def _dialog(qtbot, **effective):
    """The dialog over a fake state whose effective settings are the defaults with ``effective`` applied."""
    inter = FakeInterState()
    if effective:
        inter.overview = dataclasses.replace(inter.overview, settings={**InterSettings().to_values(), **effective})
    dlg = InterSettingsDialog(inter)
    qtbot.addWidget(dlg)
    return dlg, inter


def _editor(dlg, name) -> QWidget:
    return dlg.findChild(QWidget, f"inter_{name}")


def _save(dlg) -> None:
    dlg.findChild(QPushButton, "inter_settings_save").click()


def test_the_advanced_section_holds_exactly_the_four_advanced_settings(qtbot):
    dlg, _ = _dialog(qtbot)
    section = dlg.findChild(QWidget, "inter_settings_advanced_section")
    assert [n for n in SHOWN if section.isAncestorOf(_editor(dlg, n))] == [
        "shape_family", "step_exponent", "sweep_tol_meV", "sweep_cap"]
    assert dlg.findChild(QWidget, "inter_reference_T_K") is None


def test_the_advanced_section_is_collapsed_until_its_toggle_is_pressed(qtbot):
    dlg, _ = _dialog(qtbot)
    section = dlg.findChild(QWidget, "inter_settings_advanced_section")
    assert not section.isVisibleTo(dlg)
    dlg.findChild(QPushButton, "inter_settings_advanced").click()
    assert section.isVisibleTo(dlg)


def test_each_row_names_its_object_and_explains_it(qtbot):
    dlg, _ = _dialog(qtbot)
    for name in SHOWN:
        assert _editor(dlg, name).accessibleName() == LABELS[name], name
        assert _editor(dlg, name).toolTip() == next(d for d in SETTING_DEFS if d.name == name).help, name


def test_each_row_shows_its_default_and_its_mark(qtbot):
    dlg, _ = _dialog(qtbot)
    for row in _shown_rows():
        assert dlg.findChild(QLabel, f"inter_{row.setting.name}_default_text").text() == default_text(row)
    assert dlg.findChild(QLabel, "inter_flag_threshold_default_text").text() == f"Default: 54 · {CALIBRATED_SYNTHETIC}"


def test_every_control_is_described(qtbot):
    dlg, _ = _dialog(qtbot)
    assert_described(dlg)


def test_opening_and_closing_writes_nothing(qtbot):
    dlg, inter = _dialog(qtbot)
    dlg.reject()
    assert inter.calls == []


def test_save_sends_only_the_value_that_changed(qtbot):
    dlg, inter = _dialog(qtbot)
    _editor(dlg, "sweep_cap").setValue(40)
    _save(dlg)
    assert inter.calls == [("save_settings", ({"sweep_cap": 40},))]
    assert dlg.result() == ACCEPTED


def test_saving_with_nothing_changed_writes_nothing_and_closes(qtbot):
    dlg, inter = _dialog(qtbot)
    _save(dlg)
    assert inter.calls == []
    assert dlg.result() == ACCEPTED


def test_a_stored_value_the_box_cannot_show_is_not_rewritten_by_a_save(qtbot):
    dlg, inter = _dialog(qtbot, q_tolerance_meV=2.3456)   # finer than the box's three decimals
    _save(dlg)
    assert inter.calls == []


def test_use_default_saves_none_for_a_setting_away_from_its_default(qtbot):
    dlg, inter = _dialog(qtbot, q_tolerance_meV=3.0)
    assert _editor(dlg, "q_tolerance_meV").value() == 3.0
    dlg.findChild(QPushButton, "inter_q_tolerance_meV_default").click()
    _save(dlg)
    assert inter.calls == [("save_settings", ({"q_tolerance_meV": None},))]


def test_use_default_clears_a_saved_value_that_only_looks_like_the_default(qtbot):
    # 2.00001 is stored; the box shows 2.000, so setting it to the default moves nothing. Use default must still send None.
    dlg, inter = _dialog(qtbot, q_tolerance_meV=2.00001)
    dlg.findChild(QPushButton, "inter_q_tolerance_meV_default").click()
    _save(dlg)
    assert inter.calls == [("save_settings", ({"q_tolerance_meV": None},))]


def test_an_edit_after_use_default_sends_the_edit(qtbot):
    dlg, inter = _dialog(qtbot, q_tolerance_meV=3.0)
    dlg.findChild(QPushButton, "inter_q_tolerance_meV_default").click()
    _editor(dlg, "q_tolerance_meV").setValue(4.0)
    _save(dlg)
    assert inter.calls == [("save_settings", ({"q_tolerance_meV": 4.0},))]


def test_cancel_writes_nothing(qtbot):
    dlg, inter = _dialog(qtbot)
    _editor(dlg, "sweep_cap").setValue(40)
    dlg.findChild(QPushButton, "inter_settings_cancel").click()
    assert inter.calls == []
    assert dlg.result() == REJECTED


def test_a_refused_save_keeps_the_dialog_open_and_shows_the_reason(qtbot):
    dlg, inter = _dialog(qtbot)
    inter.fail_next = "The experiment is busy with an export."
    _editor(dlg, "sweep_cap").setValue(40)
    _save(dlg)
    msg = dlg.findChild(QLabel, "inter_settings_message")
    assert msg.text() == "The experiment is busy with an export."
    assert not msg.isHidden()
    assert dlg.result() == REJECTED


def test_a_float_box_shows_a_permitted_value_exactly(qtbot):
    # The table allows any sweep tolerance above 0 up to 1, so 0.0001 is permitted and must show as itself.
    dlg, _ = _dialog(qtbot, sweep_tol_meV=0.0001)
    box = _editor(dlg, "sweep_tol_meV")
    assert box.minimum() == 0.0001
    assert box.value() == 0.0001


def test_an_unknown_kind_raises(qtbot):
    with pytest.raises(ValueError, match="bool"):
        _widget_for(dataclasses.replace(SETTING_DEFS[0], kind="bool"))
