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


def test_view_help_mentions_recent_and_empty(capsys):
    # `isar view -h` documents the no-argument default and the empty-window fallback; there is no
    # temporary work left to recover.
    import pytest as _pytest

    with _pytest.raises(SystemExit):
        build_parser().parse_args(["view", "-h"])
    out = " ".join(capsys.readouterr().out.split())  # collapse argparse line-wrapping
    assert "recovery" not in out and "temporary" not in out
    assert "most recent workspace" in out
    assert "empty window if none is openable" in out


def test_view_parses_workspace_arg():
    args = build_parser().parse_args(["view", "/some/ws"])
    assert args.command == "view"
    assert args.workspace == "/some/ws"


def test_view_workspace_arg_is_optional():
    args = build_parser().parse_args(["view"])
    assert args.command == "view"
    assert args.workspace is None


def test_missing_gui_extra_reports_helpful_message(monkeypatch, capsys):
    # Exercise the REAL decision path: the [gui] extra is missing == one of PySide6/pyqtgraph/watchdog
    # not importable, which view_cmd probes with importlib.util.find_spec BEFORE touching isar_gui.app.
    # Make find_spec report PySide6 absent (passing others through) and assert the hint + nonzero exit.
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


def test_missing_gui_extra_reports_helpful_message_for_pyqtgraph(monkeypatch, capsys):
    # Same contract, but the missing dep is pyqtgraph rather than PySide6 — all three gui-extra deps
    # are probed, not just PySide6.
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *a, **k):
        if name == "pyqtgraph":
            return None
        return real_find_spec(name, *a, **k)

    monkeypatch.setattr(view_cmd.importlib.util, "find_spec", fake_find_spec)
    rc = view_cmd.run(SimpleNamespace(workspace="/some/ws"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "GUI extra not installed - pip install -e .[gui]" in err


def test_missing_gui_extra_reports_helpful_message_for_watchdog(monkeypatch, capsys):
    # Same contract again, missing dep is watchdog.
    real_find_spec = importlib.util.find_spec

    def fake_find_spec(name, *a, **k):
        if name == "watchdog":
            return None
        return real_find_spec(name, *a, **k)

    monkeypatch.setattr(view_cmd.importlib.util, "find_spec", fake_find_spec)
    rc = view_cmd.run(SimpleNamespace(workspace="/some/ws"))
    assert rc == 2
    err = capsys.readouterr().err
    assert "GUI extra not installed - pip install -e .[gui]" in err


def test_genuine_import_failure_inside_isar_gui_app_propagates(monkeypatch):
    # All three gui-extra deps report present, but importing isar_gui.app itself fails for a real
    # reason (a genuine bug, not a missing extra) — this must traceback, not be swallowed into the
    # "GUI extra not installed" exit-2 path. Force the import to fail via sys.modules poisoning, which
    # makes `from isar_gui.app import main` raise a real ImportError.
    import sys

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(
        view_cmd.importlib.util, "find_spec",
        lambda name, *a, **k: object() if name in ("PySide6", "pyqtgraph", "watchdog")
        else real_find_spec(name, *a, **k))

    monkeypatch.setitem(sys.modules, "isar_gui.app", None)

    import pytest as _pytest
    with _pytest.raises(ImportError):
        view_cmd.run(SimpleNamespace(workspace="/some/ws"))


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
    # No workspace given: run() forwards an empty argv. Recent/empty-start resolution lives
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


def test_resolve_recent_opens_most_recent(make_ws, tmp_path, monkeypatch):
    import tempfile

    import isar_gui.app as gui_app
    import isar_gui.session as gui_session

    # _resolve_recent_workspace skips recents under the OS temp dir; the synthetic workspace lives
    # under tmp_path, so point the perceived temp dir elsewhere to model a real saved recent.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "_not_temp"))

    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    monkeypatch.setattr(
        gui_session, "load_session",
        lambda *a, **k: gui_session.Session(recent_workspaces=[str(root)]))

    ws = gui_app._resolve_recent_workspace()
    assert ws is not None
    assert Path(ws.root) == Path(root)


def test_resolve_recent_skips_temp_dir_recent(make_ws, monkeypatch, caplog):
    # Defense in depth: a recent under the OS temp dir (e.g. a leaked pytest tmp_path workspace) is
    # skipped even though it opens cleanly, so a polluted recents list can't auto-open a scratch
    # workspace over the empty-start surface. make_ws roots live under tmp_path (== the OS temp dir
    # here), so this is a real openable workspace that must still be skipped.
    import logging

    import isar_gui.app as gui_app
    import isar_gui.session as gui_session

    root = make_ws(rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    monkeypatch.setattr(
        gui_session, "load_session",
        lambda *a, **k: gui_session.Session(recent_workspaces=[str(root)]))

    with caplog.at_level(logging.INFO, logger="isar_gui.app"):
        assert gui_app._resolve_recent_workspace() is None
    assert any("transient recent" in r.getMessage() for r in caplog.records)


def test_resolve_recent_skips_missing_and_stale(tmp_path, make_ws, monkeypatch):
    # A removed recent (gone from disk) AND a stale recent (a dir that is no longer a workspace) are
    # both skipped silently in favor of the next openable one — a stale recent must never error a
    # no-arg launch.
    import tempfile

    import isar_gui.app as gui_app
    import isar_gui.session as gui_session

    # This test exercises the missing/stale skip, not the temp-dir skip; point the perceived temp dir
    # away from tmp_path so `good` (a synthetic workspace under tmp_path) is treated as a real recent.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "_not_temp"))

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


def test_legacy_temporary_workspaces_get_one_startup_pointer_and_nothing_else(tmp_path, monkeypatch):
    """Earlier builds' temporary folders are named once in the Log; nothing opens or deletes them."""
    import isar_gui.app as gui_app
    import isar_gui.workspace_manager as wm

    temproot = tmp_path / "temp_workspaces"
    monkeypatch.setattr(wm, "temp_root", lambda: temproot)
    assert gui_app._legacy_temp_note() is None
    left = temproot / "20260801-101500-ab12"
    left.mkdir(parents=True)
    (left / "session.json").write_text("{}", encoding="utf-8", newline="\n")
    note = gui_app._legacy_temp_note()
    assert str(temproot) in note and "Open experiment…" in note and "Save experiment copy…" in note
    assert left.exists()
