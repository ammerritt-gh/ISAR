"""`isar view` CLI wiring: help lists it, and a missing [gui] extra fails with a helpful message.

No Qt import here (no importorskip) — these assertions must hold even in an env without PySide6.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from isar.cli import view_cmd
from isar.cli.main import build_parser


def test_help_lists_view():
    assert "view" in build_parser().format_help()


def test_view_parses_workspace_arg():
    args = build_parser().parse_args(["view", "/some/ws"])
    assert args.command == "view"
    assert args.workspace == "/some/ws"


def test_view_workspace_arg_is_optional():
    args = build_parser().parse_args(["view"])
    assert args.command == "view"
    assert args.workspace is None


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


def _make_pyside6_present(monkeypatch):
    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        view_cmd.importlib.util, "find_spec",
        lambda name, *a, **k: object() if name == "PySide6" else real_find_spec(name, *a, **k))


def test_no_workspace_arg_forwards_empty_argv(monkeypatch):
    # No workspace given: run() forwards an empty argv. The most-recent/empty-start resolution lives
    # in isar_gui.app.main (so `python -m isar_gui` and `isar view` behave identically); run() must
    # not exit or resolve on its own.
    import isar_gui.app as gui_app

    _make_pyside6_present(monkeypatch)

    called = {}

    def fake_main(argv):
        called["argv"] = argv
        return 0

    monkeypatch.setattr(gui_app, "main", fake_main)
    rc = view_cmd.run(SimpleNamespace(workspace=None))
    assert rc == 0
    assert called["argv"] == []


# ---- app.main workspace resolution (moved here from view_cmd) ----------------
def test_explicit_bad_path_exits_2(capsys):
    # An EXPLICIT nonexistent/invalid workspace stays a hard CLI error (stderr + exit 2). This path
    # returns before any Qt import, so it needs no QApplication.
    import isar_gui.app as gui_app

    rc = gui_app.main(["/no/such/isar/workspace"])
    assert rc == 2
    assert "isar view" in capsys.readouterr().err


def test_resolve_recent_opens_most_recent(make_ws, monkeypatch):
    import isar_gui.app as gui_app
    import isar_gui.session as gui_session

    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    monkeypatch.setattr(
        gui_session, "load_session",
        lambda *a, **k: gui_session.Session(recent_workspaces=[str(root)]))

    ws = gui_app._resolve_recent_workspace()
    assert ws is not None
    assert Path(ws.root) == Path(root)


def test_resolve_recent_skips_missing_and_stale(tmp_path, make_ws, monkeypatch):
    # A removed recent (gone from disk) AND a stale recent (a dir that is no longer a workspace) are
    # both skipped silently in favor of the next openable one — a stale recent must never error a
    # no-arg launch.
    import isar_gui.app as gui_app
    import isar_gui.session as gui_session

    missing = tmp_path / "gone"
    stale = tmp_path / "stale"
    stale.mkdir()  # exists but has no card.policy.toml -> not a workspace
    good = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])

    monkeypatch.setattr(
        gui_session, "load_session",
        lambda *a, **k: gui_session.Session(
            recent_workspaces=[str(missing), str(stale), str(good)]))

    ws = gui_app._resolve_recent_workspace()
    assert ws is not None
    assert Path(ws.root) == Path(good)


def test_resolve_recent_none_when_all_stale(tmp_path, monkeypatch):
    # No session / every recent removed-or-stale -> None (the caller opens empty-start, never errors).
    import isar_gui.app as gui_app
    import isar_gui.session as gui_session

    stale = tmp_path / "stale"
    stale.mkdir()
    monkeypatch.setattr(
        gui_session, "load_session",
        lambda *a, **k: gui_session.Session(recent_workspaces=[str(tmp_path / "gone"), str(stale)]))
    assert gui_app._resolve_recent_workspace() is None


def test_resolve_recent_none_without_session(monkeypatch):
    import isar_gui.app as gui_app
    import isar_gui.session as gui_session

    monkeypatch.setattr(gui_session, "load_session", lambda *a, **k: gui_session.Session())
    assert gui_app._resolve_recent_workspace() is None
