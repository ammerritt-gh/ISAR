"""Open Report + Export View as PNG (D3 Operator-Contract wins)."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import isar_gui.main_window as mw
from isar.workspace_api import Workspace
from isar_gui.main_window import MainWindow
from isar_gui.session import Session


def _win(qtbot, make_ws):
    root = make_ws(rows=[{"scan": 1, "detector": "det", "sample": "s", "T": 100.0,
                          "h": 2.0, "k": 0.0, "l": 0.0, "status": "fitted", "omega0": 4.0}])
    w = MainWindow(Workspace.open(root), session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    return w, root


def test_open_report_missing_sets_status(qtbot, make_ws):
    w, _ = _win(qtbot, make_ws)
    assert w._open_report() is False
    assert "No report" in w.statusBar().currentMessage()


def test_open_report_present_launches_browser(qtbot, make_ws, monkeypatch):
    w, root = _win(qtbot, make_ws)
    report = root / "report" / "report.html"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("<html></html>", encoding="utf-8")
    opened = []
    # _open_report does `import webbrowser` internally, resolving the singleton module — patch it.
    import webbrowser
    monkeypatch.setattr(webbrowser, "open", lambda uri: opened.append(uri))
    assert w._open_report() is True
    assert opened and opened[0].startswith("file:")


def test_open_report_empty_workspace(qtbot):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w._open_report() is False


def test_export_view_writes_png(qtbot, make_ws, tmp_path):
    w, _ = _win(qtbot, make_ws)
    out = tmp_path / "plane.png"
    assert w._export_view("Plane (q, E)", path=str(out)) is True
    assert out.exists() and out.stat().st_size > 0


def test_export_scans_writes_png(qtbot, make_ws, tmp_path):
    w, _ = _win(qtbot, make_ws)
    out = tmp_path / "scans.png"
    assert w._export_view("Scan(s)", path=str(out)) is True
    assert out.exists() and out.stat().st_size > 0


def test_export_view_empty_workspace(qtbot, tmp_path):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    assert w._export_view("Plane (q, E)", path=str(tmp_path / "x.png")) is False
