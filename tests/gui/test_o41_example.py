from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from isar.cli.main import main
from isar.workspace_api import Workspace


def test_o41_seed_opens_and_compiles_through_the_normal_workspace_path(tmp_path):
    repo = Path(__file__).parents[2]
    seed = (repo / "docs" / "campaigns" /
            "2026-08-04_EIG4_SLICE8_SHADOW_GATE_REPAIR_V3" /
            "artifacts" / "s8-c01-strong-confirmed")
    resources = repo / "isar_gui" / "resources" / "eig4-o41-authoritative"
    assert "test_data" not in seed.parts
    assert sum(path.stat().st_size for path in seed.rglob("*") if path.is_file()) < 500_000
    assert list(Workspace.open(seed).session_events())

    workspace = tmp_path / "isar-eig4-o41-authoritative"
    shutil.copytree(seed, workspace)
    shutil.copy2(resources / "card.policy.toml", workspace / "card.policy.toml")
    shutil.copy2(resources / "campaign-program.json", workspace / "campaign-program.json")
    (workspace / "report" / "session_log.jsonl").write_text("", encoding="utf-8")
    assert list(Workspace.open(workspace).session_events()) == []

    program_path = tmp_path / "campaign-program.json"
    assert main([
        "drive", "--program", str(workspace / "campaign-program.json"),
        "--workspace", str(workspace), "--scan-window-eig-mode", "authoritative",
        "--analysis-limit", "10m",
        "--compile-only", str(program_path),
    ]) == 0
    compiled = json.loads(program_path.read_text(encoding="utf-8"))
    assert compiled["schema"] == "isar.campaign-program/1"
    objective = compiled["steps"][0]["payload"]
    assert objective["path"] == [
        [round(2.0 + index * 0.05, 10), 0.0, 0.0] for index in range(21)]
    assert objective["analysis_limit_seconds"] == 600.0
    assert objective["steering"]["scan_window_eig_mode"] == "authoritative"
    assert objective["scan_window_eig"]["scoring_config"]["simulation_count"] == 128
    assert objective["scan_window_eig"]["scoring_config"]["block_count"] == 8
    policy = objective["scan_window_eig"]["policy_set"]["policies"]
    assert [(item["policy_id"], [v["variant_id"] for v in item["variants"]])
            for item in policy] == [
                ("eig4_slice8_excitation_energy", ["primary_0p5_mev"])]


def test_file_action_creates_and_prefills_the_normal_campaign_workspace(
        qtbot, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session

    window = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(window)
    monkeypatch.setattr(
        "isar_gui.main_window.QFileDialog.getExistingDirectory",
        lambda *_args, **_kwargs: str(tmp_path))

    window._create_o41_example()

    destination = tmp_path / "isar-eig4-o41-authoritative"
    assert window._ws.root == destination
    assert (destination / "example-manifest.json").is_file()
    assert (destination / "report" / "session_log.jsonl").read_text(
        encoding="utf-8") == ""
    assert window.campaign_dock._eig_mode.currentData() == "authoritative"
    assert window.campaign_dock._program_path.text() == str(
        destination / "campaign-program.json")
    # Budget and analysis limit are left blank so the canonical tier defaults
    # (unlimited neutrons, 1 h deterministic wall clock) govern the example.
    assert window.campaign_dock._analysis_limit.text() == ""
    assert window.campaign_dock._budget.text() == ""


def _window(qtbot, tmp_path, monkeypatch):
    pytest.importorskip("PySide6")
    from isar_gui.main_window import MainWindow
    from isar_gui.session import Session

    window = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(window)
    monkeypatch.setattr(
        "isar_gui.main_window.QFileDialog.getExistingDirectory",
        lambda *_args, **_kwargs: str(tmp_path))
    return window


def test_failure_part_way_through_leaves_no_partial_workspace(qtbot, tmp_path, monkeypatch):
    # A partial workspace would trip the destination.exists() guard on the next attempt, whose
    # message sends the operator hunting for a name clash instead of the real failure.
    window = _window(qtbot, tmp_path, monkeypatch)
    logged = []
    monkeypatch.setattr(window, "log", logged.append)
    monkeypatch.setattr("isar_gui.main_window.shutil.copy2",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("resource missing")))

    window._create_o41_example()

    assert list(tmp_path.iterdir()) == []                       # no partial, no staging leftover
    assert window._ws.root is None
    assert any("Could not create O41 example" in line for line in logged)   # not status-bar-only
    assert "Could not create O41 example" in window.statusBar().currentMessage()


def test_existing_destination_warns_without_touching_it(qtbot, tmp_path, monkeypatch):
    # QMessageBox.warning is modal: unmocked it would hang the offscreen suite silently.
    window = _window(qtbot, tmp_path, monkeypatch)
    warnings = []

    class _FakeMB:
        @staticmethod
        def warning(*args, **_kwargs):
            warnings.append(args[-1])

    monkeypatch.setattr("isar_gui.main_window.QMessageBox", _FakeMB)
    destination = tmp_path / "isar-eig4-o41-authoritative"
    destination.mkdir()
    (destination / "keep-me.txt").write_text("mine", encoding="utf-8")

    window._create_o41_example()

    assert len(warnings) == 1 and str(destination) in warnings[0]
    assert [p.name for p in destination.iterdir()] == ["keep-me.txt"]
    assert window._ws.root is None
