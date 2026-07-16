"""Recovery-dialog safety: discarding names the exact temporary workspace."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import isar_gui.workspace_manager as wm
from isar_gui.recovery_dialog import RecoveryDialog


def test_discard_confirmation_names_selected_workspace(qtbot, tmp_path, monkeypatch):
    root = tmp_path / "temp_workspaces"
    candidate = root / "ws-beamtime-001"
    candidate.mkdir(parents=True)
    (candidate / "session.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(wm, "temp_root", lambda: root)
    # recovery_dialog imports discard_dir directly, whose global lookup follows the patched module
    # helper above.  Record the confirmation's human-facing text before accepting it.
    asked = {}

    def answer(parent, title, text, buttons, default):
        asked.update(title=title, text=text)
        from PySide6.QtWidgets import QMessageBox
        return QMessageBox.StandardButton.Discard

    monkeypatch.setattr("isar_gui.recovery_dialog.QMessageBox.question", answer)
    dialog = RecoveryDialog([candidate])
    qtbot.addWidget(dialog)
    dialog._on_discard()

    assert candidate.name in asked["text"]
    assert "Discard" in asked["title"]
    assert not candidate.exists()
