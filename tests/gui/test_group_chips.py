"""Saved groups change only the shared scan selection and the per-experiment GUI store."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication, QLineEdit

import bl43_fixture
from bl43_fixture import KEY_A
from isar_gui.gui_store import GuiStore
from isar_gui.selection import ORIGIN_GROUP, ORIGIN_LIST
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.group_chips import GroupChips


@pytest.fixture
def snapshot(bl43_ws_readonly):
    return bl43_fixture.sample_snapshot(bl43_ws_readonly)


def _view_model(monkeypatch, snapshot):
    monkeypatch.setattr("isar_gui.viewmodels.scan_list.scan_list",
                        lambda _ws, **_reads: snapshot)
    return ScanListViewModel(SimpleNamespace())


def _shown(qtbot, vm, store=None, width=640):
    widget = GroupChips(vm)
    qtbot.addWidget(widget)
    widget.set_store(store)
    widget.resize(width, 28)
    widget.show()
    qtbot.wait(10)
    return widget


def _keys(snapshot):
    return [spectrum.key for scan_set in snapshot.sets for spectrum in scan_set.spectra]


def _a_keys(snapshot):
    return [spectrum.key for scan_set in snapshot.sets if scan_set.key == KEY_A
            for spectrum in scan_set.spectra]


def test_save_recall_edit_and_undo_keep_the_store_and_selection_in_step(qtbot, monkeypatch,
                                                                        snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    keys = _keys(snapshot)[:2]
    vm.selection.set(keys, origin=ORIGIN_LIST, clicked=keys[0])
    store = GuiStore(tmp_path)
    widget = _shown(qtbot, vm, store)
    dirty = []
    changes = []
    widget.store_dirty.connect(lambda: dirty.append(True))
    vm.selection.changed.connect(changes.append)

    widget.key_pressed(3, True)
    saved = store.slot_group(3)
    assert saved is not None and saved.members == keys
    assert widget.chip(3).text() == "3 · 2 spectra"
    assert widget._notice.text() == "Group 3 saved"
    assert widget._undo_button.isVisible()
    assert dirty == [True]

    vm.selection.clear()
    dirty.clear()
    widget.key_pressed(3, False)
    assert vm.selection.keys == tuple(keys)
    assert vm.selection.active == keys[0]
    assert changes[-1].origin == ORIGIN_GROUP
    assert dirty == []
    assert store.slot_group(3).members == keys

    third = _keys(snapshot)[2]
    vm.selection.set((*keys, third), clicked=third, origin=ORIGIN_LIST)
    assert widget.chip(3).text() == "3 · 2 spectra · edited"
    assert widget.save_button(3) is not None
    saved_id = store.slot_group(3).id
    widget.save_button(3).click()
    assert store.slot_group(3).members == list(vm.selection.keys)
    assert store.slot_group(3).id == saved_id
    assert widget._notice.text() == "Group 3 replaced"
    widget._undo_button.click()
    assert store.slot_group(3).members == keys
    assert store.slot_group(3).id == saved_id
    assert not widget._undo_button.isVisible()


def test_full_store_keeps_edited_slot_savable_and_disables_plus(qtbot, monkeypatch,
                                                                 snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    keys = _keys(snapshot)
    store = GuiStore(tmp_path)
    for slot in range(1, 10):
        store.save_slot(slot, keys[:2])
    widget = _shown(qtbot, vm, store)
    widget.reload()
    widget.chip(3).click()
    vm.selection.set((*vm.selection.keys, keys[2]), clicked=keys[2], origin=ORIGIN_LIST)
    assert widget.save_button(3) is not None
    widget.save_button(3).click()
    assert store.slot_group(3).members == list(vm.selection.keys)
    assert not widget._plus.isEnabled()
    assert widget._plus.toolTip() == "all 9 groups used: recall one and save over it"


def test_plus_saves_to_the_lowest_free_slot(qtbot, monkeypatch, snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    keys = _keys(snapshot)[:2]
    vm.selection.set(keys, origin=ORIGIN_LIST)
    store = GuiStore(tmp_path)
    store.save_slot(1, [_keys(snapshot)[2]])
    widget = _shown(qtbot, vm, store)
    dirty = []
    widget.store_dirty.connect(lambda: dirty.append(True))
    widget._plus.click()
    assert store.slot_group(2).members == list(vm.selection.keys)
    assert dirty == [True]


def test_missing_members_are_named_and_dropped_only_with_notice(qtbot, monkeypatch,
                                                                snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    key = _keys(snapshot)[0]
    store = GuiStore(tmp_path)
    store.save_slot(2, [key, "gone:1:a99"])
    widget = _shown(qtbot, vm, store)
    chip = widget.chip(2)
    assert chip.text() == "2 · 1 of 2 present"
    assert widget._labels_by_key[key] in chip.toolTip()
    assert "gone:1:a99 (missing)" in chip.toolTip()
    chip.click()
    assert vm.selection.keys == (key,)
    saved_id = store.slot_group(2).id
    widget.key_pressed(2, True)
    assert store.slot_group(2).members == [key]
    assert store.slot_group(2).id == saved_id
    assert widget._notice.text() == "Group 2 replaced · 1 missing dropped"


def test_legacy_scan_level_member_recalls_every_analyzer(qtbot, monkeypatch,
                                                           snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    store = GuiStore(tmp_path)
    store.save_slot(4, [KEY_A])
    widget = _shown(qtbot, vm, store)
    assert widget.chip(4).text() == "4 · 1 spectrum"
    widget.chip(4).click()
    assert vm.selection.keys == tuple(_a_keys(snapshot))


def test_empty_selection_and_empty_slots_explain_what_to_do(qtbot, monkeypatch,
                                                             snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    store = GuiStore(tmp_path)
    widget = _shown(qtbot, vm, store)
    widget.key_pressed(4, True)
    assert store.slot_group(4) is None
    assert widget._notice.text() == "Nothing selected: group 4 not saved"
    before = vm.selection.keys
    widget.key_pressed(5, False)
    assert vm.selection.keys == before
    assert widget._notice.text() == "Group 5 is empty: Ctrl+5 saves the selection as group 5"


def test_bare_digit_ignores_editor_but_control_digit_saves(qtbot, monkeypatch,
                                                            snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    keys = _keys(snapshot)[:2]
    vm.selection.set(keys, origin=ORIGIN_LIST)
    store = GuiStore(tmp_path)
    widget = _shown(qtbot, vm, store)
    editor = QLineEdit()
    qtbot.addWidget(editor)
    editor.show()
    editor.setFocus()
    QApplication.processEvents()
    widget.key_pressed(3, False)
    assert vm.selection.keys == tuple(keys)
    assert store.slot_group(3) is None
    widget.key_pressed(3, True)
    assert store.slot_group(3).members == list(vm.selection.keys)


def test_slots_survive_store_reload(qtbot, monkeypatch, snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    keys = _keys(snapshot)[:2]
    vm.selection.set(keys, origin=ORIGIN_LIST)
    store = GuiStore(tmp_path)
    widget = _shown(qtbot, vm, store)
    widget.key_pressed(3, True)
    store.save()
    reloaded = GuiStore.load(tmp_path)
    second = _shown(qtbot, vm, reloaded)
    assert second.chip(3) is not None
    assert reloaded.slot_group(3).members == list(keys)


def test_overflow_pins_the_edited_chip_and_lists_hidden_slots(qtbot, monkeypatch,
                                                              snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    keys = _keys(snapshot)
    store = GuiStore(tmp_path)
    for slot in range(1, 10):
        store.save_slot(slot, keys[:2])
    widget = _shown(qtbot, vm, store)
    widget.chip(3).click()
    vm.selection.set((*vm.selection.keys, keys[2]), clicked=keys[2], origin=ORIGIN_LIST)
    widget.resize(140, 28)
    QApplication.processEvents()
    assert widget.minimumSizeHint().width() <= 140
    assert widget.chip(3) is not None
    assert widget.save_button(3) is not None
    assert widget.save_button(3).isVisible()
    for slot in range(1, 10):
        chip = widget.chip(slot)
        if chip is not None:
            assert chip.width() >= chip.sizeHint().width()
            if chip.text() != widget._full_text(slot, store.slot_group(slot)):
                assert widget._full_text(slot, store.slot_group(slot)) in chip.toolTip()
    hidden = set(range(1, 10)) - widget._visible_slots
    actions = widget._more_menu.actions()
    assert widget._more.isVisible() == bool(hidden)
    assert all(any(action.text().startswith(f"Group {widget._full_text(slot, store.slot_group(slot))}")
                   for action in actions) for slot in hidden)


def test_accessible_names_and_no_store_reason(qtbot, monkeypatch, snapshot, tmp_path):
    vm = _view_model(monkeypatch, snapshot)
    widget = _shown(qtbot, vm)
    assert not widget._plus.isEnabled()
    assert widget._plus.toolTip() == "Open an experiment to save groups"
    widget.key_pressed(3, True)
    widget.key_pressed(3, False)
    assert widget._notice.text() == ""

    store = GuiStore(tmp_path)
    store.save_slot(3, _keys(snapshot)[:2])
    widget.set_store(store)
    chip = widget.chip(3)
    assert chip.accessibleName() == "Group 3, 2 spectra"
    assert widget._plus.accessibleName() == "Save the selection as a new group"
    assert widget._more.accessibleName() == "More groups"
    widget.chip(3).click()
    vm.selection.set((*vm.selection.keys, _keys(snapshot)[2]), clicked=_keys(snapshot)[2],
                     origin=ORIGIN_LIST)
    assert widget.save_button(3).accessibleName() == "Save group 3"
    assert "edited" in widget.chip(3).accessibleName()
