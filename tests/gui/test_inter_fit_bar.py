"""The Scan(s) inter-spectra bar on real widgets (offscreen): the fit menu, the buttons' reasons and signals."""
from __future__ import annotations

from PySide6.QtCore import Qt

from gui.inter_fakes import make_entry, make_history
from gui.inter_helpers import assert_described
from isar.workspace_api.inter_spectra import FlagInfo, FlagReason
from isar_gui.viewmodels.inter_fits import bar_state
from isar_gui.views.inter_fit_bar import InterFitBar


def _refit(fit_id, batch_id, batch_label, stage, **overrides):
    return make_entry(fit_id, f"{batch_label} · {stage}", batch_id=batch_id, batch_label=batch_label,
                      stage_label=stage, **overrides)


def _history():
    lost = FlagInfo("serious", 4.1, 2.9, 1.2, (FlagReason("branch_lost_mid_window", "serious", "branch B, 3.2 meV"),),
                    flagged=True)
    return make_history(_refit("f1", "b1", "Batch 1", "ring 1"),
                        _refit("f2", "b1", "Batch 1", "ring 2", changed=False),
                        _refit("f3", "b2", "Batch 2", "sweep 3", authoritative=True, flag=lost))


def _bar(qtbot, state):
    bar = InterFitBar()
    qtbot.addWidget(bar)
    bar.show_state(state)
    return bar


def test_flat_dropdown_shows_every_fit_with_batch_names_and_unchanged_refits(qtbot):
    bar = _bar(qtbot, bar_state(_history(), "f1"))
    actions = bar.menu.actions()
    assert [a.text() for a in actions] == [
        "Fast solo", "Batch 1 · ring 1", "Batch 1 · ring 2 · unchanged", "Batch 2 · sweep 3 · authoritative"]
    assert all(a.menu() is None for a in actions)
    assert [a.data() for a in actions if a.isChecked()] == ["f1"]
    assert_described(bar)


def test_choosing_an_entry_emits_its_fit_id(qtbot):
    bar = _bar(qtbot, bar_state(_history(), "f1"))
    chosen = []
    bar.fit_chosen.connect(chosen.append)
    bar.show()
    bar.menu.popup(bar.menu_button.mapToGlobal(bar.menu_button.rect().bottomLeft()))
    action = bar.menu.actions()[2]
    qtbot.mouseClick(bar.menu, Qt.MouseButton.LeftButton, pos=bar.menu.actionGeometry(action).center())
    assert chosen == ["f2"]


def test_arrows_cross_batch_boundaries_and_refresh_does_not_emit(qtbot):
    history = _history()
    bar = _bar(qtbot, bar_state(history, "f1"))
    bar.show()
    chosen = []
    bar.fit_chosen.connect(chosen.append)
    bar.activateWindow()
    bar.menu_button.setFocus()
    qtbot.waitUntil(bar.menu_button.hasFocus)
    for fit in ("f2", "f3"):
        qtbot.keyClick(bar.menu_button, Qt.Key.Key_Down)
        bar.show_state(bar_state(history, fit))
    qtbot.keyClick(bar.menu_button, Qt.Key.Key_Down)  # last row stays put
    qtbot.keyClick(bar.menu_button, Qt.Key.Key_Up)
    assert chosen == ["f2", "f3", "f2"]
    assert bar.menu_button.hasFocus()


def test_open_menu_arrows_enter_escape_and_refresh_preserve_navigation(qtbot):
    history = _history()
    bar = _bar(qtbot, bar_state(history, "f1"))
    bar.show()
    chosen = []
    bar.fit_chosen.connect(chosen.append)
    bar.menu.popup(bar.menu_button.mapToGlobal(bar.menu_button.rect().bottomLeft()))
    assert bar.menu.activeAction().data() == "f1"
    qtbot.keyClick(bar.menu, Qt.Key.Key_Down)
    assert bar.menu.activeAction().data() == "f2"
    bar.show_state(bar_state(history, "f1"))
    assert bar.menu.activeAction().data() == "f2"
    qtbot.keyClick(bar.menu, Qt.Key.Key_Down)
    qtbot.keyClick(bar.menu, Qt.Key.Key_Return)
    assert chosen == ["f3"] and not bar.menu.isVisible()
    bar.show_state(bar_state(history, "f3"))
    bar.menu.popup(bar.menu_button.mapToGlobal(bar.menu_button.rect().bottomLeft()))
    qtbot.keyClick(bar.menu, Qt.Key.Key_Up)
    qtbot.keyClick(bar.menu, Qt.Key.Key_Escape)
    assert chosen == ["f3"] and not bar.menu.isVisible()


def test_make_authoritative_and_lock_emit_once_each(qtbot):
    bar = _bar(qtbot, bar_state(_history(), "f1"))
    made, locked = [], []
    bar.make_authoritative_requested.connect(lambda: made.append(1))
    bar.lock_requested.connect(lambda: locked.append(1))
    bar.authoritative_button.click()
    bar.lock_button.click()
    assert made == [1] and locked == [1]


def test_greyed_buttons_say_why(qtbot):
    bar = _bar(qtbot, bar_state(_history(), "f3"))
    assert not bar.authoritative_button.isEnabled()
    assert "already" in bar.authoritative_button.toolTip()
    stale = make_history(_refit("f1", "b1", "Batch 1", "ring 1", stale=True, stale_text="its inputs changed"))
    bar.show_state(bar_state(stale, "f1"))
    assert not bar.authoritative_button.isEnabled() and "stale" in bar.authoritative_button.toolTip()


def test_lock_reads_unlock_when_the_spectrum_is_locked(qtbot):
    bar = _bar(qtbot, bar_state(_history(), "f1", locked=True, locked_fit="f3"))
    assert bar.lock_button.text() == "Unlock" and bar.lock_button.isEnabled()
    assert "Locked to f3" in bar.authoritative_button.toolTip()


def test_loading_and_fast_only_states_are_disabled_with_a_reason(qtbot):
    bar = _bar(qtbot, bar_state(None, "fast"))
    assert not bar.menu_button.isEnabled() and "Reading" in bar.menu_button.toolTip()
    bar.show_state(bar_state(make_history(), "fast"))
    assert not bar.menu_button.isEnabled()
    assert "Fast solo is the only fit" in bar.menu_button.toolTip()
    assert bar.menu.actions()[0].text() == "Fast solo · authoritative"


def test_info_line_is_elided_with_the_full_text_on_hover(qtbot):
    state = bar_state(_history(), "f3")
    bar = _bar(qtbot, state)
    bar.show()
    bar.info.resize(120, 20)
    assert bar.info.full_text() == state.info
    assert bar.info.text() != state.info and bar.info.text().endswith("…")
    assert "not seen here" in bar.info.toolTip()


def test_the_info_line_says_flagged_only_for_a_fit_on_the_review_list(qtbot):
    note = FlagInfo("warn", None, None, None, (FlagReason("release_fit_failed", "warn", ""),), flagged=False)
    bar = _bar(qtbot, bar_state(make_history(_refit("f1", "b1", "Batch 1", "ring 1", flag=note)), "f1"))
    assert bar.info.full_text().startswith("Showing Batch 1 · ring 1")
    assert "flagged" not in bar.info.full_text()


def test_waterfall_reads_its_state_and_emits_only_on_a_user_click(qtbot):
    off = bar_state(_history(), "f3", waterfall_off="Select one spectrum with a refit to see its waterfall.")
    bar = _bar(qtbot, off)
    assert not bar.waterfall_button.isEnabled()
    seen = []
    bar.waterfall_toggled.connect(seen.append)
    bar.show_state(bar_state(_history(), "f3", waterfall_on=True))
    assert seen == [] and bar.waterfall_button.isChecked()
    bar.show_state(bar_state(_history(), "f3"))
    assert bar.waterfall_button.isEnabled()
    bar.waterfall_button.click()
    assert seen == [True]
