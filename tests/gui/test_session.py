"""GUI session persistence: round-trip, corruption tolerance, recent-workspace dedup, file location.

Pure (no Qt): the save/load API is deliberately Qt-free so it can be exercised without a QApplication.
"""
from __future__ import annotations

import pytest

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


def test_rename_workspace_follows_the_recents():
    from pathlib import Path

    a, b, c = (str(Path(p)) for p in ("/exp/a", "/exp/b", "/exp/c"))
    s = sess.Session(recent_workspaces=[a, b, c])
    s.rename_workspace("/exp/b", "/exp/renamed")
    renamed = str(Path("/exp/renamed"))
    assert s.recent_workspaces == [a, renamed, c]                  # same position, new path


def test_rename_workspace_never_adds_and_never_duplicates():
    from pathlib import Path

    a, b = str(Path("/exp/a")), str(Path("/exp/b"))
    s = sess.Session(recent_workspaces=[a])
    s.rename_workspace("/exp/unrecorded", "/exp/new")             # an old that was never a recent
    assert s.recent_workspaces == [a]
    s = sess.Session(recent_workspaces=[a, b])
    s.rename_workspace("/exp/a", "/exp/b")                        # the new path is already a recent
    assert s.recent_workspaces == [b]


def test_note_workspace_refuses_temp_dir_path(tmp_path, monkeypatch):
    """A workspace under the system temp dir (e.g. a leaked pytest tmp_path) must not be recorded as
    a recent: a later no-arg launch would auto-open a dead scratch dir instead of the empty-start
    surface. tmp_path is itself under the OS temp dir, so use it directly as the transient path."""
    import tempfile

    # tmp_path lives under gettempdir() on the runner; assert that assumption so the test is honest.
    assert __import__("pathlib").Path(tmp_path).resolve().is_relative_to(
        __import__("pathlib").Path(tempfile.gettempdir()).resolve())
    s = sess.Session()
    s.note_workspace(tmp_path / "ws1")
    assert s.recent_workspaces == []
    # A non-temp path is still recorded normally.
    s.note_workspace("/persistent/ws")
    assert len(s.recent_workspaces) == 1


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


# ---- Autosaver failure handling (Qt: needs the qtbot QApplication) -----------
def test_autosaver_swallows_save_failure_and_reports(qtbot):
    """A snapshot/serialize/write failure inside the Qt timer callback must not propagate out of it;
    the message goes to the on_error callback instead (never silent, never raising)."""
    pytest.importorskip("PySide6")

    def _snapshot():
        raise OSError("disk gone")

    errors = []
    saver = sess.Autosaver(_snapshot, on_error=errors.append)
    saver.flush_now()  # must not raise
    assert errors and "disk gone" in errors[0] and "autosave failed" in errors[0]


def test_autosaver_pins_its_file_when_built(qtbot, tmp_path, monkeypatch):
    """The target is fixed at construction. pytest-qt closes a left-over window after the per-test
    monkeypatch is undone; a path resolved at write time then reached the operator's real profile."""
    pytest.importorskip("PySide6")
    built = tmp_path / "built"
    monkeypatch.setenv("APPDATA", str(built))
    saver = sess.Autosaver(lambda: sess.Session())
    monkeypatch.setenv("APPDATA", str(tmp_path / "later"))
    saver.flush_now()
    assert (built / "isar_gui" / "session.json").exists()
    assert not (tmp_path / "later").exists()


def test_autosaver_save_failure_without_callback_goes_to_stderr(qtbot, capsys, tmp_path):
    """With no on_error callback, a save failure is written to stderr rather than swallowed silently."""
    pytest.importorskip("PySide6")

    def _bad_save(*_a, **_k):
        raise OSError("write blocked")

    import isar_gui.session as s
    saver = s.Autosaver(lambda: sess.Session(), path=tmp_path / "session.json")
    orig = s.save_session
    s.save_session = _bad_save
    try:
        saver.flush_now()  # must not raise
    finally:
        s.save_session = orig
    assert "write blocked" in capsys.readouterr().err
