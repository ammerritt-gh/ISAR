"""`isar view` CLI wiring: help lists it, and a missing [gui] extra fails with a helpful message.

No Qt import here (no importorskip) — these assertions must hold even in an env without PySide6.
"""
from __future__ import annotations

import importlib.util
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
    # Exercise the REAL decision path: the [gui] extra is missing == PySide6 not importable, which
    # view_cmd probes with importlib.util.find_spec BEFORE touching isar_gui.app. Make find_spec
    # report PySide6 absent (passing others through) and assert the hint + nonzero exit.
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *a, **k):
        if name == "PySide6":
            return None
        return real_find_spec(name, *a, **k)

    monkeypatch.setattr(view_cmd.importlib.util, "find_spec", fake_find_spec)
    rc = view_cmd.run(SimpleNamespace(workspace="/some/ws"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "GUI extra not installed - pip install -e .[gui]" in err


def test_present_gui_extra_launches(monkeypatch):
    # When PySide6 is present, run() must reach isar_gui.app.main with the workspace path — not the
    # missing-extra branch. Force find_spec to report PySide6 present (env-independent) and stub the
    # launcher so no QApplication is created. isar_gui.app is Qt-free at import scope, so importing
    # it here needs no PySide6.
    import isar_gui.app as gui_app

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        view_cmd.importlib.util, "find_spec",
        lambda name, *a, **k: object() if name == "PySide6" else real_find_spec(name, *a, **k))

    called = {}

    def fake_main(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setattr(gui_app, "main", fake_main)
    rc = view_cmd.run(SimpleNamespace(workspace="/some/ws"))
    assert rc == 0
    assert called["argv"] == ["/some/ws"]
