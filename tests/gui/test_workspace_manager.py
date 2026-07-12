"""WorkspaceManager lifecycle + Qt-free helpers (Phase C).

The pure path/copy/scan helpers are exercised directly; the QObject transitions (new_temp / open /
save_as / discard_temp) through a manager. ``temp_root`` is monkeypatched to a tmp subdir so nothing
touches the real per-user recovery root. Offscreen platform is forced by tests/gui/conftest.py.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

import isar_gui.workspace_manager as wm
from isar_gui.workspace_manager import WorkspaceManager
from synthetic_ws import stamp_ws


@pytest.fixture
def temproot(tmp_path, monkeypatch):
    """A controlled temp_root under tmp_path (a *subdir*, so a dest outside it is non-temporary)."""
    root = tmp_path / "temp_workspaces"
    monkeypatch.setattr(wm, "temp_root", lambda: root)
    return root


def _temp_ws(temproot, name="ws-stamped", **parts):
    """Stamp a full synthetic workspace directory under temp_root (so it reads as temporary)."""
    root = temproot / name
    root.mkdir(parents=True)
    return stamp_ws(root, **parts)


# ---- Qt-free helpers --------------------------------------------------------
def test_reserve_temp_dir_naming(temproot):
    d = wm.reserve_temp_dir()
    assert d.parent == temproot
    assert d.name.startswith("ws-")
    assert d.is_dir()
    assert wm.is_under(d, temproot)


def test_reserve_temp_dir_no_collision(temproot, monkeypatch):
    # Freeze the timestamp so both reservations would collide on the same second; the second must
    # still get its own directory (numeric suffix), never reuse the first.
    monkeypatch.setattr(wm, "datetime", _FrozenDatetime)
    a = wm.reserve_temp_dir()
    b = wm.reserve_temp_dir()
    assert a != b
    assert a.is_dir() and b.is_dir()


def test_has_content(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not wm.has_content(empty)

    with_card = tmp_path / "card"
    with_card.mkdir()
    (with_card / "card.policy.toml").write_text("title='x'\n", encoding="utf-8")
    assert wm.has_content(with_card)

    with_session = tmp_path / "sess"
    with_session.mkdir()
    (with_session / "session.json").write_text("{}", encoding="utf-8")
    assert wm.has_content(with_session)


def test_recovery_candidates_finds_only_content_bearing(temproot):
    temproot.mkdir(parents=True)
    # Two content-bearing dirs, one empty dir, one stray file — only the two dirs are candidates.
    good1 = temproot / "ws-20260101-000001"
    good1.mkdir()
    (good1 / "card.policy.toml").write_text("title='a'\n", encoding="utf-8")
    good2 = temproot / "ws-20260101-000002"
    good2.mkdir()
    (good2 / "session.json").write_text("{}", encoding="utf-8")
    (temproot / "ws-empty").mkdir()
    (temproot / "loose.txt").write_text("x", encoding="utf-8")

    cands = wm.recovery_candidates()
    assert set(cands) == {good1, good2}
    # Newest-first ordering (reverse-sorted by name / timestamp).
    assert cands[0] == good2


def test_recovery_candidates_empty_when_no_root(temproot):
    # temp_root not yet created -> no candidates, no error.
    assert wm.recovery_candidates() == []


def test_discard_dir_refuses_outside_temp_root(temproot, tmp_path):
    temproot.mkdir(parents=True)
    outside = tmp_path / "real_workspace"
    outside.mkdir()
    (outside / "card.policy.toml").write_text("title='keep'\n", encoding="utf-8")

    assert wm.discard_dir(outside) is False
    assert outside.exists()  # a non-temp dir is never deleted

    inside = temproot / "ws-x"
    inside.mkdir()
    assert wm.discard_dir(inside) is True
    assert not inside.exists()


# ---- QObject lifecycle ------------------------------------------------------
def test_new_temp_enters_pending(qtbot, temproot):
    m = WorkspaceManager()
    states, workspaces = [], []
    m.state_changed.connect(states.append)
    m.workspace_changed.connect(workspaces.append)

    root = m.new_temp()
    assert m.state() == "pending"
    assert m.root() == root
    assert m.is_temporary()
    assert states == ["pending"]
    assert workspaces == [None]        # pending binds the empty facade


def test_open_ready_and_not_temporary(qtbot, temproot, tmp_path):
    # A real workspace OUTSIDE temp_root opens ready and reads as non-temporary.
    root = tmp_path / "saved_ws"
    root.mkdir()
    stamp_ws(root, rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root) is True
    assert m.state() == "ready"
    assert not m.is_temporary()


def test_open_bad_path_returns_false(qtbot, temproot, tmp_path):
    m = WorkspaceManager()
    assert m.open(tmp_path / "not-a-workspace") is False
    assert m.state() == "none"


def test_save_as_copies_tree_and_discards_temp(qtbot, temproot, tmp_path):
    root = _temp_ws(temproot, rows=[{"scan": 1, "detector": "det", "status": "fitted",
                                     "omega0": 5.0}])
    (root / "report").mkdir(exist_ok=True)
    (root / "report" / "results.csv").write_text("scan\n1\n", encoding="utf-8")
    m = WorkspaceManager()
    assert m.open(root) is True
    assert m.is_temporary()

    dest = tmp_path / "kept"                      # outside temp_root
    assert m.save_as(dest) is True
    assert (dest / "card.policy.toml").exists()
    assert (dest / "session.json").exists()
    assert (dest / "report" / "results.csv").read_text(encoding="utf-8") == "scan\n1\n"
    assert not root.exists()                      # temp source discarded after the copy
    assert m.root() == dest
    assert not m.is_temporary()                   # the saved copy is a real workspace


def test_save_as_refuses_non_empty_dest(qtbot, temproot, tmp_path):
    root = _temp_ws(temproot, rows=[{"scan": 1, "detector": "det", "status": "fitted",
                                     "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root) is True

    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "something.txt").write_text("busy", encoding="utf-8")
    assert m.save_as(dest) is False
    assert root.exists()                          # nothing lost on refusal
    assert m.root() == root                       # binding unchanged


def test_discard_temp_only_under_temp_root(qtbot, temproot, tmp_path):
    # Bound to a real workspace OUTSIDE temp_root: discard_temp must refuse (no rmtree).
    root = tmp_path / "real"
    root.mkdir()
    stamp_ws(root, rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root) is True
    m.discard_temp()
    assert root.exists()

    # Bound to a temp workspace: discard_temp removes it.
    temp = _temp_ws(temproot, name="ws-discardme",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    assert m.open(temp) is True
    m.discard_temp()
    assert not temp.exists()


class _FrozenDatetime:
    """A datetime stand-in whose ``now().strftime`` is constant (collision test)."""

    @staticmethod
    def now():
        return _FrozenDatetime()

    def strftime(self, _fmt):
        return "20260101-000000"
