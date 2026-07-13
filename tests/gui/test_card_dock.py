"""CardDock: validation, atomic human saves, conflict safety, and empty-state behavior."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar.workspace_api import Workspace
from isar_gui.main_window import _EmptyWorkspace
from isar_gui.views.card_dock import CardDock


def _ws_with_card(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    return Workspace.open(root), root


def test_loads_card_text(qtbot, make_ws):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    assert 'facility = "TAVI"' in dock.text
    assert "loaded" in dock._status.text()


def test_saves_valid_toml_to_disk(qtbot, make_ws):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    dock.set_text(dock.text + '\n[fitting]\nmin_fit_points = 6\n')
    assert dock.save() is True
    on_disk = (root / "card.policy.toml").read_text(encoding="utf-8")
    assert 'facility = "TAVI"' in on_disk
    assert "min_fit_points = 6" in on_disk


def test_invalid_toml_is_not_written(qtbot, make_ws):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    dock.set_text("this is [not valid toml")
    assert dock.save() is False
    assert "TOML error" in dock._status.text()
    # The on-disk card is untouched (still the original valid content).
    assert 'facility = "TAVI"' in (root / "card.policy.toml").read_text(encoding="utf-8")


def test_empty_workspace_disables_editor(qtbot):
    dock = CardDock(_EmptyWorkspace())
    qtbot.addWidget(dock)
    assert dock._editor.isReadOnly()
    assert not dock._save_btn.isEnabled()
    assert dock.save() is False


def test_editor_rejects_drops_so_mainwindow_sees_them(qtbot):
    dock = CardDock(_EmptyWorkspace())
    qtbot.addWidget(dock)
    assert dock._editor.acceptDrops() is False


def test_set_workspace_rebinds_to_new_card(qtbot, make_ws):
    ws1, _ = _ws_with_card(make_ws)
    dock = CardDock(_EmptyWorkspace())
    qtbot.addWidget(dock)
    assert not dock._save_btn.isEnabled()
    dock.set_workspace(ws1)
    assert dock._save_btn.isEnabled()
    assert 'facility = "TAVI"' in dock.text


def test_semantically_invalid_card_is_not_written(qtbot, make_ws):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    dock.set_text(dock.text.replace('energy_status = "absolute"', 'energy_status = "bogus"'))
    assert dock.save() is False
    assert "energy_status" in dock._status.text()
    assert 'energy_status = "absolute"' in (root / "card.policy.toml").read_text(encoding="utf-8")


def test_dirty_and_saved_signals_follow_atomic_save(qtbot, make_ws):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    dirty = []
    saved = []
    dock.dirty_changed.connect(dirty.append)
    dock.saved.connect(saved.append)
    dock.set_text(dock.text + '\n[sample]\nname = "edited"\n')
    assert dock.is_dirty and dirty == [True]
    assert dock.save() is True
    assert not dock.is_dirty and dirty == [True, False]
    assert saved == [str(root / "card.policy.toml")]


def test_atomic_write_failure_keeps_previous_card_and_cleans_temp(qtbot, make_ws, monkeypatch):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    original = (root / "card.policy.toml").read_text(encoding="utf-8")
    dock.set_text(dock.text + '\n[sample]\nname = "draft"\n')

    def fail_replace(_source, _dest):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("isar_gui.views.card_dock.os.replace", fail_replace)
    assert dock.save() is False
    assert (root / "card.policy.toml").read_text(encoding="utf-8") == original
    assert not list(root.glob("card.policy.toml.tmp.*"))


def test_external_change_never_overwrites_without_confirmation(qtbot, make_ws):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    dock.set_text(dock.text + '\n[sample]\nname = "draft"\n')
    (root / "card.policy.toml").write_text(dock.text.replace("draft", "external"), encoding="utf-8")
    dock._resolve_external_change = lambda: False
    assert dock.save() is False
    assert 'name = "external"' in (root / "card.policy.toml").read_text(encoding="utf-8")


def test_busy_card_can_be_drafted_but_not_saved_or_reloaded(qtbot, make_ws):
    ws, _ = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    dock.set_operation_busy(True)
    assert not dock._editor.isReadOnly()
    assert not dock._save_btn.isEnabled() and not dock._reload_btn.isEnabled()
    dock.set_text(dock.text + '\n[sample]\nname = "draft"\n')
    assert dock.is_dirty and dock.save() is False and dock.reload() is False


def test_rebind_cannot_silently_replace_dirty_card(qtbot, make_ws):
    ws1, _ = _ws_with_card(make_ws)
    ws2, _ = _ws_with_card(make_ws)
    dock = CardDock(ws1)
    qtbot.addWidget(dock)
    dock.set_text(dock.text + '\n[sample]\nname = "draft"\n')
    dock.resolve_dirty = lambda action: False
    assert dock.set_workspace(ws2) is False
    assert 'name = "draft"' in dock.text


def test_discard_restores_the_on_disk_policy_before_analysis_can_continue(qtbot, make_ws):
    ws, _ = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    on_disk = dock.text
    dock.set_text(on_disk + '\n[sample]\nname = "discard-me"\n')
    assert dock.is_dirty
    assert dock._discard_draft() is True
    assert dock.text == on_disk
    assert not dock.is_dirty
