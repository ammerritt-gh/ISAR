"""The scan list's inter-spectra columns (Anchor, Flag, Lock, Inter-spectra fit) and the row menu's Set as anchor /
Remove anchor (S1U7, C-gui-state, C-gui-words). Offscreen Qt with the real panel and view model; the InterState is
the GUI fake, whose reads and writers belong to S1U5 and to P1."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

import bl43_fixture
from bl43_fixture import KEY_A, KEY_B
from gui.inter_fakes import FakeInterState, make_overview
from isar.workspace_api.inter_spectra import FlagInfo, FlagReason, SpectrumInter
from isar_gui.inter_words import reason_sentence
from isar_gui.theme import Tone
from isar_gui.viewmodels.scan_list import COLUMNS, ScanListViewModel
from isar_gui.views.scan_list import _TONE_ROLE, ScanListPanel

A = {n: f"{KEY_A}:a{n:02d}" for n in (3, 4, 5, 6)}
B = {n: f"{KEY_B}:a{n:02d}" for n in (3, 4, 5, 6)}
NO_FAST_TIP = "Select spectra that have a fast-pass result."
INTER = ("anchor", "flag", "lock", "inter")


def _flag(severity: str, code: str, detail: str = "") -> FlagInfo:
    return FlagInfo(severity, 4.1, 2.9, 1.2, (FlagReason(code, severity, detail),))


def _row(key, *, fit_id="fast", label="Fast solo", source="fast", flag=None, locked=False, locked_fit_id=None,
         lock_stale=False, stale=False, stale_text=None) -> SpectrumInter:
    return SpectrumInter(key=key, fit_id=fit_id, in_use=fit_id != "fast", source=source, label=label, flag=flag,
                         locked=locked, locked_fit_id=locked_fit_id, lock_stale=lock_stale, stale=stale,
                         stale_text=stale_text)


def _publish(inter, *, revision, anchors=(), rows=()) -> None:
    inter.overview = make_overview(by_key={r.key: r for r in rows}, anchors=anchors, revision=revision)
    inter.changed.emit()


@pytest.fixture
def sample(bl43_ws_readonly):
    return bl43_fixture.sample_snapshot(bl43_ws_readonly)


@pytest.fixture
def made(qtbot, monkeypatch, sample):
    """A shown panel over the sample list, with a fake InterState bound to its view model."""
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list", lambda _ws, **_reads: sample)
    vm = ScanListViewModel(SimpleNamespace())
    inter = FakeInterState()
    vm.set_inter(inter)
    panel = ScanListPanel(vm)
    qtbot.addWidget(panel)
    panel.resize(360, 520)
    panel.show()
    QApplication.processEvents()
    return panel, vm, inter


def _cell(panel, node_id, column):
    first = panel._by_id[node_id]
    parent = first.parent() or panel._model.invisibleRootItem()
    return parent.child(first.row(), COLUMNS.index(column))


def _right_click(panel, node_id) -> None:
    index = panel._model.indexFromItem(panel._by_id[node_id])
    parent = index.parent()
    while parent.isValid():
        panel._view.expand(parent)
        parent = parent.parent()
    QApplication.processEvents()
    panel._on_context_menu(panel._view.visualRect(index).center())


def _menu(panel) -> dict:
    return {a.text(): a for a in panel._row_menu.actions() if not a.isSeparator()}


# ---- columns: off without state, on by themselves, hideable -------------------------------------
def test_the_inter_columns_are_off_without_state_and_switch_on_by_themselves(made):
    panel, vm, inter = made
    assert not set(INTER) & set(vm.visible_columns())
    _publish(inter, revision="2", anchors=(A[3],))
    assert "anchor" in vm.visible_columns() and panel._column_actions["anchor"].isChecked()
    assert not {"flag", "lock", "inter"} & set(vm.visible_columns())
    _publish(inter, revision="3", anchors=(A[3],), rows=[_row(A[4], flag=_flag("serious", "branch_lost_mid_window"))])
    assert "flag" in vm.visible_columns()
    _publish(inter, revision="4", anchors=(A[3],), rows=[_row(A[4], fit_id="b2s3", label="Batch 2 · sweep 3",
                                                             source="batch", locked=True, locked_fit_id="b2s3")])
    assert {"lock", "inter"} <= set(vm.visible_columns()) and "flag" not in vm.visible_columns()


def test_an_inter_column_can_be_hidden_by_hand_and_the_choice_survives_a_session(made):
    panel, vm, inter = made
    _publish(inter, revision="2", anchors=(A[3],))
    panel._column_actions["anchor"].trigger()                    # off by hand
    assert "anchor" not in vm.visible_columns()
    _publish(inter, revision="3", anchors=(A[3], A[4]))          # another anchor arrives: the choice holds
    assert "anchor" not in vm.visible_columns()
    session = SimpleNamespace(view_state={})
    vm.save_view_state(session)
    assert session.view_state["scan_list"]["hidden"] == ["anchor"]
    again = ScanListViewModel(SimpleNamespace())
    again.set_inter(inter)
    again.load_view_state(session)
    assert "anchor" not in again.visible_columns() and "lock" not in again.visible_columns()


# ---- cells: text, tone and a hover for each state -------------------------------------------------
def test_each_inter_state_reads_in_plain_words_with_a_tone_and_a_hover(made):
    panel, vm, inter = made
    stale_text = "its own inputs changed since the refit"
    _publish(inter, revision="2", anchors=(A[3],), rows=[
        _row(A[4], fit_id="b2s3", label="Batch 2 · sweep 3", source="batch", locked=True, locked_fit_id="b2s3",
             flag=_flag("serious", "branch_lost_mid_window", "branch B, 3.2 meV")),
        _row(A[6], fit_id="b1r2", label="Batch 1 · ring 2", source="batch",
             flag=_flag("info", "neighbour_supported_split")),
        _row(B[3], stale=True, stale_text=stale_text),
        _row(B[4], locked=True, locked_fit_id="b1r2", lock_stale=True),
        _row(B[5], flag=_flag("warn", "count_changed_by_neighbours")),
    ])
    anchor = _cell(panel, f"spec:{A[3]}", "anchor")
    assert (anchor.text(), anchor.data(_TONE_ROLE)) == ("anchor", Tone.OK) and anchor.toolTip()
    assert _cell(panel, f"spec:{A[5]}", "anchor").text() == ""                             # a blank is no state

    serious = _cell(panel, f"spec:{A[4]}", "flag")
    assert (serious.text(), serious.data(_TONE_ROLE)) == ("serious", Tone.ERR)
    assert "4.1" in serious.toolTip()
    assert reason_sentence("branch_lost_mid_window", "branch B, 3.2 meV") in serious.toolTip()
    repaired = _cell(panel, f"spec:{A[6]}", "flag")
    assert (repaired.text(), repaired.data(_TONE_ROLE)) == ("repaired", Tone.OK)
    look = _cell(panel, f"spec:{B[5]}", "flag")
    assert (look.text(), look.data(_TONE_ROLE)) == ("look", Tone.WARN)

    lock = _cell(panel, f"spec:{A[4]}", "lock")
    assert (lock.text(), lock.data(_TONE_ROLE)) == ("locked", Tone.OK) and lock.toolTip()
    stale_lock = _cell(panel, f"spec:{B[4]}", "lock")
    assert (stale_lock.text(), stale_lock.data(_TONE_ROLE)) == ("locked · stale", Tone.WARN)
    assert reason_sentence("stale_locked_fit") in stale_lock.toolTip()

    fit = _cell(panel, f"spec:{A[4]}", "inter")
    assert (fit.text(), fit.data(_TONE_ROLE)) == ("Batch 2 · sweep 3", Tone.OK) and fit.toolTip()
    assert _cell(panel, f"spec:{A[5]}", "inter").text() == ""                              # the fast solo fit: blank
    stale_fit = _cell(panel, f"spec:{B[3]}", "inter")
    assert (stale_fit.text(), stale_fit.data(_TONE_ROLE)) == ("stale: fast solo stands in", Tone.WARN)
    assert stale_fit.toolTip() == stale_text


def test_set_rows_count_their_spectra_and_stay_blank_at_zero(made):
    panel, vm, inter = made
    _publish(inter, revision="2", anchors=(A[3], A[4]),
             rows=[_row(A[4], flag=_flag("serious", "did_not_settle"))])
    anchors = _cell(panel, f"set:{KEY_A}", "anchor")
    assert anchors.text() == "2/4" and anchors.toolTip() == "2 of 4 spectra are anchors"
    assert _cell(panel, f"set:{KEY_A}", "flag").text() == "1/4"
    assert _cell(panel, f"set:{KEY_B}", "anchor").text() == ""


# ---- the row menu ------------------------------------------------------------------------------------
def test_set_as_anchor_on_a_spectrum_acts_on_the_selected_spectra_with_a_fast_result(made):
    panel, vm, inter = made
    _publish(inter, revision="2")
    vm.select_keys((A[3], A[5]))                                  # A5 has no fast-pass result
    _right_click(panel, f"spec:{A[3]}")
    _menu(panel)["Set as anchor"].trigger()
    assert inter.calls == [("set_anchors", ((A[3],), True, None))]


def test_right_click_on_a_set_anchors_every_spectrum_in_it_that_has_a_fast_result(made):
    panel, vm, inter = made
    _publish(inter, revision="2")
    _right_click(panel, f"set:{KEY_A}")
    _menu(panel)["Set as anchor"].trigger()
    assert inter.calls == [("set_anchors", ((A[3], A[4], A[6]), True, None))]


def test_remove_anchor_appears_only_when_a_selected_spectrum_is_anchored(made):
    panel, vm, inter = made
    _publish(inter, revision="2", anchors=(A[3],))
    vm.select_keys((A[4],))
    _right_click(panel, f"spec:{A[4]}")
    assert "Set as anchor" in _menu(panel) and "Remove anchor" not in _menu(panel)
    vm.select_keys((A[3],))
    _right_click(panel, f"spec:{A[3]}")
    assert "Remove anchor" in _menu(panel)
    _menu(panel)["Remove anchor"].trigger()
    assert inter.calls[-1] == ("set_anchors", ((A[3],), False, None))


def test_anchoring_with_no_fast_result_is_disabled_and_says_why(made):
    panel, vm, inter = made
    _publish(inter, revision="2")
    vm.select_keys((A[5],))
    _right_click(panel, f"spec:{A[5]}")
    action = _menu(panel)["Set as anchor"]
    assert not action.isEnabled() and action.toolTip() == NO_FAST_TIP
    action.trigger()
    assert inter.calls == []


def test_set_as_anchor_acts_only_on_the_unanchored_selected_spectra_with_a_fast_result(made):
    panel, vm, inter = made
    _publish(inter, revision="2", anchors=(A[3],))
    vm.select_keys((A[3], A[4]))                                  # A3 is already an anchor
    _right_click(panel, f"spec:{A[4]}")
    _menu(panel)["Set as anchor"].trigger()
    assert inter.calls == [("set_anchors", ((A[4],), True, None))]


def test_set_as_anchor_is_disabled_when_the_only_fast_result_is_on_an_anchor(made):
    panel, vm, inter = made
    _publish(inter, revision="2", anchors=(A[3],))
    vm.select_keys((A[3], A[5]))                                  # A5 has no fast-pass result
    _right_click(panel, f"spec:{A[5]}")
    action = _menu(panel)["Set as anchor"]
    assert not action.isEnabled() and action.toolTip() == NO_FAST_TIP
    action.trigger()
    assert inter.calls == []


def test_an_anchor_without_a_fast_result_can_still_be_removed(made):
    panel, vm, inter = made
    _publish(inter, revision="2", anchors=(A[5],))
    vm.select_keys((A[5],))
    _right_click(panel, f"spec:{A[5]}")
    action = _menu(panel)["Remove anchor"]
    assert action.isEnabled()
    action.trigger()
    assert inter.calls[-1] == ("set_anchors", ((A[5],), False, None))


def test_the_row_menu_shows_the_tooltips_of_every_action(made):
    panel, vm, inter = made
    _publish(inter, revision="2", anchors=(A[3],))
    _right_click(panel, f"set:{KEY_A}")
    assert panel._row_menu.toolTipsVisible()
    for text, action in _menu(panel).items():
        assert action.toolTip(), f"{text!r} has no tooltip"
    assert {"Set as anchor", "Remove anchor"} <= set(_menu(panel))


def test_the_filter_words_find_flagged_anchored_and_locked_spectra(made):
    panel, vm, inter = made
    _publish(inter, revision="2", anchors=(A[3],), rows=[
        _row(A[4], flag=_flag("serious", "did_not_settle"), locked=True, locked_fit_id="b1r2")])
    vm.set_filter("flagged")
    assert vm.counts()[0] == 1
    vm.set_filter("anchor")
    assert vm.counts()[0] == 1
    vm.set_filter("locked")
    assert vm.counts()[0] == 1
