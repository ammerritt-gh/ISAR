"""GUI session persistence: round-trip, corruption tolerance, recent-workspace dedup, file location.

Pure (no Qt): the save/load API is deliberately Qt-free so it can be exercised without a QApplication.
"""
from __future__ import annotations

from isar_gui import session as sess


def test_round_trip(tmp_path):
    p = tmp_path / "session.json"
    s = sess.Session(recent_workspaces=["/ws/a", "/ws/b"], dock_layout_b64="Zm9v",
                     view_state={"heatmap": {"zoom": 2}})
    sess.save_session(s, p)
    got = sess.load_session(p)
    assert got.recent_workspaces == ["/ws/a", "/ws/b"]
    assert got.dock_layout_b64 == "Zm9v"
    assert got.view_state == {"heatmap": {"zoom": 2}}
    assert got.version == sess.SESSION_VERSION


def test_missing_file_is_default(tmp_path):
    got = sess.load_session(tmp_path / "nope.json")
    assert got == sess.Session()


def test_corrupt_file_falls_back_to_defaults(tmp_path):
    p = tmp_path / "session.json"
    p.write_text("{not valid json ]]", encoding="utf-8")
    got = sess.load_session(p)
    assert got == sess.Session()


def test_partial_garbage_fields_degrade(tmp_path):
    p = tmp_path / "session.json"
    # Wrong types for every field: should not raise, should coerce/drop to defaults.
    p.write_text('{"recent_workspaces": 5, "dock_layout_b64": 3, "view_state": "x"}', encoding="utf-8")
    got = sess.load_session(p)
    assert got.recent_workspaces == []
    assert got.dock_layout_b64 is None
    assert got.view_state == {}


def test_atomic_save_leaves_no_tmp(tmp_path):
    p = tmp_path / "session.json"
    sess.save_session(sess.Session(), p)
    assert p.exists()
    assert not list(tmp_path.glob("*.tmp"))  # no leftover temp file (any pid suffix)


def test_note_workspace_dedup_and_order():
    s = sess.Session()
    s.note_workspace("/ws/a")
    s.note_workspace("/ws/b")
    s.note_workspace("/ws/a")  # re-note moves to front, no duplicate
    assert s.recent_workspaces[0].endswith("a")
    assert s.recent_workspaces.count(str(__import__("pathlib").Path("/ws/a"))) == 1


def test_session_path_uses_appdata(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    p = sess.session_path()
    assert p.parent.name == "isar_gui"
    assert p.name == "session.json"


def test_session_path_falls_back_without_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    p = sess.session_path()
    assert p.name == "session.json"
    assert ".isar_gui" in p.parts
