"""CardDock: loads the card, saves valid TOML, refuses to write invalid TOML, empty-state safe."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar.workspace_api import Workspace
from isar_gui.main_window import _EmptyWorkspace
from isar_gui.views.card_dock import CardDock


def _ws_with_card(make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    (root / "card.policy.toml").write_text('title = "hello"\n', encoding="utf-8")
    return Workspace.open(root), root


def test_loads_card_text(qtbot, make_ws):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    assert 'title = "hello"' in dock.text
    assert "loaded" in dock._status.text()


def test_saves_valid_toml_to_disk(qtbot, make_ws):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    dock.set_text('title = "edited"\n[fitting]\nmin_fit_points = 6\n')
    assert dock.save() is True
    on_disk = (root / "card.policy.toml").read_text(encoding="utf-8")
    assert 'title = "edited"' in on_disk
    assert "min_fit_points = 6" in on_disk


def test_invalid_toml_is_not_written(qtbot, make_ws):
    ws, root = _ws_with_card(make_ws)
    dock = CardDock(ws)
    qtbot.addWidget(dock)
    dock.set_text("this is [not valid toml")
    assert dock.save() is False
    assert "TOML error" in dock._status.text()
    # The on-disk card is untouched (still the original valid content).
    assert 'title = "hello"' in (root / "card.policy.toml").read_text(encoding="utf-8")


def test_empty_workspace_disables_editor(qtbot):
    dock = CardDock(_EmptyWorkspace())
    qtbot.addWidget(dock)
    assert dock._editor.isReadOnly()
    assert not dock._save_btn.isEnabled()
    assert dock.save() is False


def test_set_workspace_rebinds_to_new_card(qtbot, make_ws):
    ws1, _ = _ws_with_card(make_ws)
    dock = CardDock(_EmptyWorkspace())
    qtbot.addWidget(dock)
    assert not dock._save_btn.isEnabled()
    dock.set_workspace(ws1)
    assert dock._save_btn.isEnabled()
    assert 'title = "hello"' in dock.text
