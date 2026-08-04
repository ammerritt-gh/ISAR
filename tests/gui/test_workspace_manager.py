"""WorkspaceManager lifecycle + Qt-free helpers (Phase C).

The pure path/copy/scan helpers are exercised directly; the QObject transitions (new_temp / open /
save_as / discard_temp) through a manager. ``temp_root`` is monkeypatched to a tmp subdir so nothing
touches the real per-user recovery root. Offscreen platform is forced by tests/gui/conftest.py.
"""
from __future__ import annotations

from pathlib import Path

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


def test_reserve_temp_dir_no_collision(temproot):
    # Freeze ONLY the timestamp used for the directory name (scoped to this block) so both
    # reservations collide on the same second; the second must still get its own directory (numeric
    # suffix), never reuse the first. Other datetime users in the module are left untouched.
    from unittest import mock

    with mock.patch.object(wm, "datetime") as md:
        md.now.return_value.strftime.return_value = "20260101-000000"
        a = wm.reserve_temp_dir()
        b = wm.reserve_temp_dir()
    assert a != b
    assert a.is_dir() and b.is_dir()
    assert a.name == "ws-20260101-000000"
    assert b.name == "ws-20260101-000000-1"


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
    log_bytes = b'{"kind":"decide","payload":{"action":"campaign_start"}}\n'
    (root / "report" / "session_log.jsonl").write_bytes(log_bytes)
    m = WorkspaceManager()
    assert m.open(root) is True
    assert m.is_temporary()

    dest = tmp_path / "kept"                      # outside temp_root
    assert m.save_as(dest) is True
    assert (dest / "card.policy.toml").exists()
    assert (dest / "session.json").exists()
    assert (dest / "report" / "results.csv").read_text(encoding="utf-8") == "scan\n1\n"
    assert (dest / "report" / "session_log.jsonl").read_bytes() == log_bytes
    assert not root.exists()                      # temp source discarded after the copy
    assert m.root() == dest
    assert not m.is_temporary()                   # the saved copy is a real workspace


def test_save_as_creates_a_nested_destination_that_does_not_exist(qtbot, temproot, tmp_path):
    """The operator names a folder in the save dialog; its parents need not exist yet."""
    root = _temp_ws(temproot, name="ws-nested-dest",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root) is True
    dest = tmp_path / "2026-08" / "beamtime-7" / "acoustic-map"

    assert m.save_as(dest) is True
    assert (dest / "card.policy.toml").exists()
    assert m.root() == dest.resolve() and not m.is_temporary()
    assert not root.exists()                          # the temp source is discarded only on success


def test_save_as_failure_leaves_no_created_directories_behind(qtbot, temproot, tmp_path,
                                                              monkeypatch):
    """A failed transaction must leave the filesystem exactly as it found it — no half-made tree."""
    root = _temp_ws(temproot, name="ws-nested-failure",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "2026-08" / "beamtime-7" / "acoustic-map"

    def fail_open(path):
        raise OSError("cannot validate staged workspace")

    monkeypatch.setattr(wm.Workspace, "open", staticmethod(fail_open))
    assert not m.save_as(dest)
    assert not (tmp_path / "2026-08").exists()        # validation failed before anything was made
    assert not list(tmp_path.glob("**/.acoustic-map.isar-saving-*"))
    assert root.exists() and m.root() == root

    # …and the same holds when the promotion itself is what fails, after the tree was created.
    monkeypatch.undo()
    monkeypatch.setattr(wm.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("rename boom")))
    assert not m.save_as(dest)
    assert not (tmp_path / "2026-08").exists()
    assert root.exists() and m.root() == root


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


def test_manager_no_longer_owns_bootstrap(qtbot, temproot):
    """Phase D moved the init->ingest->open bootstrap out of the manager into the IngestController;
    the manager keeps only pure lifecycle (reserve/open/save/discard) and no subprocess API."""
    m = WorkspaceManager()
    assert not hasattr(m, "bootstrap_from_data")
    assert not hasattr(m, "is_bootstrapping")
    assert not hasattr(m, "_spawn")


def test_discard_temp_resets_to_none(qtbot, temproot):
    root = _temp_ws(temproot, name="ws-reset",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root) is True
    states, workspaces = [], []
    m.state_changed.connect(states.append)
    m.workspace_changed.connect(workspaces.append)

    m.discard_temp()
    assert not root.exists()
    assert m.state() == "none"
    assert m.root() is None
    assert m.workspace() is None
    assert states == ["none"]                 # emitted the reset
    assert workspaces == [None]


def test_save_as_records_failure_reason(qtbot, temproot, tmp_path):
    root = _temp_ws(temproot, name="ws-reason",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root) is True
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "x.txt").write_text("busy", encoding="utf-8")
    assert m.save_as(dest) is False
    assert "not empty" in (m.last_error() or "")


def test_save_as_refuses_source_descendant_and_recovery_root(qtbot, temproot, tmp_path):
    root = _temp_ws(temproot, name="ws-nested",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root)

    assert not m.save_as(root)
    assert "must not" in (m.last_error() or "")
    assert not m.save_as(root / "saved-inside-source")
    assert "must not" in (m.last_error() or "")
    assert not m.save_as(temproot / "another-recovery-workspace")
    assert "temporary-workspace" in (m.last_error() or "")
    assert root.exists() and m.root() == root


def test_save_as_copy_failure_keeps_source_and_cleans_stage(qtbot, temproot, tmp_path, monkeypatch):
    root = _temp_ws(temproot, name="ws-copy-failure",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "kept"

    def fail_copy(source, stage):
        Path(stage).mkdir()
        raise OSError("copy boom")

    monkeypatch.setattr(wm.shutil, "copytree", fail_copy)
    assert not m.save_as(dest)
    assert root.exists() and m.root() == root
    assert not dest.exists()
    assert not list(tmp_path.glob(".kept.isar-saving-*"))


def test_save_as_validation_failure_keeps_source_and_cleans_stage(qtbot, temproot, tmp_path, monkeypatch):
    root = _temp_ws(temproot, name="ws-invalid-stage",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "kept"

    def fail_open(path):
        raise OSError("cannot validate staged workspace")

    monkeypatch.setattr(wm.Workspace, "open", staticmethod(fail_open))
    assert not m.save_as(dest)
    assert root.exists() and m.root() == root
    assert not dest.exists()
    assert not list(tmp_path.glob(".kept.isar-saving-*"))


def test_save_as_promotion_failure_keeps_source_and_cleans_stage(qtbot, temproot, tmp_path, monkeypatch):
    root = _temp_ws(temproot, name="ws-promote-failure",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "kept"

    def fail_replace(source, target):
        raise OSError("rename boom")

    monkeypatch.setattr(wm.os, "replace", fail_replace)
    assert not m.save_as(dest)
    assert root.exists() and m.root() == root
    assert not dest.exists()
    assert not list(tmp_path.glob(".kept.isar-saving-*"))


def test_save_as_final_open_failure_retains_source_and_promoted_copy(
        qtbot, temproot, tmp_path, monkeypatch):
    root = _temp_ws(temproot, name="ws-final-open-failure",
                    rows=[{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}])
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "kept"
    real_open = wm.Workspace.open

    def fail_only_final_open(path):
        if Path(path).resolve() == dest.resolve():
            raise OSError("cannot bind promoted workspace")
        return real_open(path)

    monkeypatch.setattr(wm.Workspace, "open", staticmethod(fail_only_final_open))
    assert not m.save_as(dest)
    assert root.exists() and m.root() == root
    assert dest.exists()                              # never delete the promoted copy on uncertainty
    assert not list(tmp_path.glob(".kept.isar-saving-*"))


def test_discard_empty_temp_removes_pending_reservation(qtbot, temproot):
    m = WorkspaceManager()
    root = m.new_temp()
    assert root.exists() and not m.has_content()
    assert m.discard_empty_temp()
    assert not root.exists()
    assert m.state() == "none"
