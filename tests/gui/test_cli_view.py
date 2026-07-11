"""`isar view` CLI wiring: help lists it, and a missing [gui] extra fails with a helpful message.

No Qt import here (no importorskip) — these assertions must hold even in an env without PySide6.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

from isar.cli import view_cmd
from isar.cli.main import build_parser


def test_help_lists_view():
    assert "view" in build_parser().format_help()


def test_view_parses_workspace_arg():
    args = build_parser().parse_args(["view", "/some/ws"])
    assert args.command == "view"
    assert args.workspace == "/some/ws"


def test_missing_gui_extra_reports_helpful_message(monkeypatch, capsys):
    # Simulate the extra not being installed: a `from isar_gui.app import main` ImportError.
    monkeypatch.setitem(sys.modules, "isar_gui.app", None)
    rc = view_cmd.run(SimpleNamespace(workspace="/some/ws"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "GUI extra not installed - pip install -e .[gui]" in err
