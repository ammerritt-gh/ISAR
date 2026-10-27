"""WorkspaceManager: the experiment lifecycle and its Qt-free helpers.

The naming/copy helpers are exercised directly; the QObject transitions (new_experiment / open /
rename / save_copy) through a manager. ``ISAR_EXPERIMENTS_ROOT`` is pointed at a per-test folder by
tests/gui/conftest.py, so no test creates a folder under the operator's Documents. Offscreen
platform is forced by the same conftest.
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import isar_gui.workspace_manager as wm
from isar_gui.gui_store import GuiStore
from isar_gui.workspace_manager import WorkspaceManager
from synthetic_ws import stamp_ws

_ROWS = [{"scan": 1, "detector": "det", "status": "fitted", "omega0": 5.0}]


def _ready_ws(tmp_path, name="exp", **parts):
    """A stamped synthetic experiment folder (a card, so it opens ready)."""
    root = tmp_path / name
    root.mkdir()
    return stamp_ws(root, **({"rows": _ROWS} | parts))


def _record(m):
    """Collect (signal, value) pairs from every manager signal, in emission order."""
    seen = []
    m.workspace_changed.connect(lambda ws: seen.append(("workspace", None if ws is None else Path(ws.root))))
    m.state_changed.connect(lambda s: seen.append(("state", s)))
    m.experiment_created.connect(lambda r, f: seen.append(("created", (r, f))))
    return seen


# ---- naming helpers ---------------------------------------------------------
@pytest.mark.parametrize("raw, want", [
    ("leTacon_SP8_CsV3Sb5", "leTacon_SP8_CsV3Sb5"),
    ("run: 3/4 <T>", "run_ 3_4 _T"),
    ("  ..hidden.  ", "hidden"),
    ("CON", "CON_"),
    ("lpt3.txt", "lpt3_.txt"),           # reserved before a dot too
    ("???", ""),
    ("a" * 100, "a" * 80),
    ("tab\there", "tab_here"),
])
def test_clean_experiment_name(raw, want):
    assert wm.clean_experiment_name(raw) == want


def test_clean_experiment_name_cap_is_stripped_again():
    # the 80-character cut can land on a separator; it must not leave one behind
    assert wm.clean_experiment_name("a" * 79 + "_" + "b" * 10) == "a" * 79


def test_experiment_name_for_one_folder(tmp_path):
    folder = tmp_path / "leTacon_SP8_CsV3Sb5"
    folder.mkdir()
    assert wm.experiment_name_for([str(folder)]) == ("leTacon_SP8_CsV3Sb5", "folder")


def test_experiment_name_for_files_of_one_set():
    a, b = r"D:\beam\CsV3Sb5_97K_0p5_0p5_3_sum_a03", r"D:\beam\CsV3Sb5_97K_0p5_0p5_3_sum_a04"
    want = ("CsV3Sb5_97K_0p5_0p5_3_sum", "data")
    assert wm.experiment_name_for([a, b]) == want
    # a sidecar named exactly like the set keeps the whole name as the common prefix: same result
    assert wm.experiment_name_for([a, b, r"D:\beam\CsV3Sb5_97K_0p5_0p5_3_sum.var"]) == want
    assert wm.experiment_name_for([a]) == (Path(a).stem, "data")


def test_experiment_name_for_unrelated_files_use_the_parent_folder():
    paths = [r"D:\ILL IN8\055177", r"D:\ILL IN8\055181"]
    assert wm.experiment_name_for(paths) == ("ILL IN8", "data_folder")


def test_experiment_name_for_falls_back_to_the_dated_default(tmp_path):
    assert wm.experiment_name_for([]) == (wm.default_experiment_name(), "default")
    root = Path(tmp_path.anchor)                              # a drive root: no usable name anywhere
    assert wm.experiment_name_for([str(root)]) == (wm.default_experiment_name(), "default")
    assert wm.default_experiment_name().startswith("Experiment_")


def test_unique_experiment_dir_suffixes_and_creates_the_parent(tmp_path):
    parent = tmp_path / "not" / "yet"
    first = wm.unique_experiment_dir(parent, "X")
    second = wm.unique_experiment_dir(parent, "X")
    third = wm.unique_experiment_dir(parent, "X")
    assert [p.name for p in (first, second, third)] == ["X", "X-2", "X-3"]
    assert all(p.is_dir() for p in (first, second, third))


def test_experiments_root_honours_the_env_and_otherwise_ends_in_isar(tmp_path, monkeypatch):
    monkeypatch.setenv("ISAR_EXPERIMENTS_ROOT", str(tmp_path / "somewhere"))
    assert wm.experiments_root() == tmp_path / "somewhere"
    monkeypatch.delenv("ISAR_EXPERIMENTS_ROOT")
    assert wm.experiments_root().name == "ISAR" and wm.experiments_root().is_absolute()


def test_openable_reasons(tmp_path):
    assert "not an ISAR experiment" in wm.openable(tmp_path)              # an arbitrary empty folder
    assert "not an ISAR experiment" in wm.openable(tmp_path / "missing")
    (tmp_path / "unstarted").mkdir()
    GuiStore(tmp_path / "unstarted").save()
    assert wm.openable(tmp_path / "unstarted") is None                    # created, not yet read into
    assert wm.openable(_ready_ws(tmp_path)) is None                       # has a card


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


def test_legacy_temp_workspaces_finds_only_content_bearing(tmp_path, monkeypatch):
    root = tmp_path / "temp_workspaces"
    monkeypatch.setattr(wm, "temp_root", lambda: root)
    assert wm.legacy_temp_workspaces() == []                  # not created yet: no candidates, no error
    root.mkdir()
    good1 = root / "ws-20260101-000001"
    good1.mkdir()
    (good1 / "card.policy.toml").write_text("title='a'\n", encoding="utf-8")
    good2 = root / "ws-20260101-000002"
    good2.mkdir()
    (good2 / "session.json").write_text("{}", encoding="utf-8")
    (root / "ws-empty").mkdir()
    (root / "loose.txt").write_text("x", encoding="utf-8")
    cands = wm.legacy_temp_workspaces()
    assert cands == [good2, good1]                            # newest first, empties and strays out
    assert good1.exists() and good2.exists()                  # the pointer never deletes anything


def test_manager_has_no_temporary_workspace_api():
    """H1: the temporary workspace and its recovery/discard paths are removed, not kept beside the
    real folder; the manager also keeps no subprocess API."""
    for gone in ("new_temp", "save_as", "discard_temp", "discard_empty_temp", "is_temporary",
                 "has_content", "recovery_candidates", "bootstrap_from_data"):
        assert not hasattr(WorkspaceManager, gone), gone
    for gone in ("reserve_temp_dir", "recovery_candidates", "discard_dir"):
        assert not hasattr(wm, gone), gone


# ---- new_experiment ---------------------------------------------------------
def test_new_experiment_makes_a_real_pending_folder(qtbot, tmp_path):
    m = WorkspaceManager()
    seen = _record(m)
    gens = []
    m.generation_changed.connect(gens.append)

    root = m.new_experiment("leTacon: CsV3Sb5", named_from="folder")
    assert root == tmp_path / "experiments" / "leTacon_ CsV3Sb5"          # under the isolated root
    assert root.is_dir() and GuiStore.path_for(root).exists()             # the marker of an unstarted one
    assert (m.state(), m.root(), m.workspace()) == ("pending", root, None)
    assert seen == [("workspace", None), ("state", "pending"), ("created", (str(root), "folder"))]
    assert gens == [m.generation()]


def test_new_experiment_twice_suffixes_and_blank_name_gets_the_default(qtbot):
    m = WorkspaceManager()
    a = m.new_experiment("???", named_from="default")
    b = m.new_experiment("???", named_from="default")
    assert a.name == wm.default_experiment_name() and b.name == a.name + "-2"


def test_new_experiment_named_from_is_keyword_only(qtbot):
    with pytest.raises(TypeError):
        WorkspaceManager().new_experiment("x", "folder")


def test_new_experiment_raises_oserror_and_binds_nothing(qtbot, tmp_path, monkeypatch):
    blocker = tmp_path / "a-file"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv("ISAR_EXPERIMENTS_ROOT", str(blocker))            # a file where the root must be
    m = WorkspaceManager()
    seen = _record(m)
    with pytest.raises(OSError):
        m.new_experiment("x", named_from="default")
    assert m.state() == "none" and m.root() is None and seen == []


# ---- open -------------------------------------------------------------------
def test_open_ready(qtbot, tmp_path):
    root = _ready_ws(tmp_path)
    m = WorkspaceManager()
    seen = _record(m)
    assert m.open(root) is True
    assert m.state() == "ready" and m.root() == root
    assert seen == [("workspace", root), ("state", "ready")]


def test_open_an_unstarted_experiment_is_pending(qtbot):
    made = WorkspaceManager().new_experiment("fresh", named_from="default")
    m = WorkspaceManager()
    seen = _record(m)
    assert m.open(made) is True
    assert (m.state(), m.root(), m.workspace()) == ("pending", made, None)
    assert seen == [("workspace", None), ("state", "pending")]


def test_open_refuses_an_arbitrary_folder_and_keeps_the_binding(qtbot, tmp_path):
    bound = _ready_ws(tmp_path)
    m = WorkspaceManager()
    assert m.open(bound)
    stray = tmp_path / "empty-folder"
    stray.mkdir()
    seen = _record(m)
    assert m.open(stray) is False
    assert "not an ISAR experiment (no card.policy.toml)" in (m.last_error() or "")
    assert str(stray) in (m.last_error() or "")
    assert m.open(tmp_path / "missing") is False
    assert m.state() == "ready" and m.root() == bound and seen == []      # nothing rebound, nothing emitted
    assert not (stray / "gui.state.json").exists()                        # and nothing adopted


def test_manager_no_longer_owns_bootstrap(qtbot):
    """The init->ingest->open bootstrap lives in the IngestController; the manager keeps only pure
    lifecycle and no subprocess API."""
    m = WorkspaceManager()
    assert not hasattr(m, "bootstrap_from_data")
    assert not hasattr(m, "is_bootstrapping")
    assert not hasattr(m, "_spawn")


# ---- rename -----------------------------------------------------------------
def test_rename_a_ready_experiment_moves_the_folder_and_rebinds(qtbot, tmp_path):
    root = _ready_ws(tmp_path, name="old name")
    m = WorkspaceManager()
    assert m.open(root)
    seen = _record(m)
    gen = m.generation()

    assert m.rename("New: name") is True
    new = tmp_path / "New_ name"
    assert not root.exists() and new.is_dir()
    assert m.root() == new and m.state() == "ready" and Path(m.workspace().root) == new
    assert m.generation() > gen
    assert seen == [("workspace", new), ("state", "ready")]


def test_rename_a_pending_experiment_rebinds_without_a_created_note(qtbot):
    m = WorkspaceManager()
    root = m.new_experiment("first", named_from="default")
    seen = _record(m)
    assert m.rename("second") is True
    assert m.root() == root.parent / "second" and m.state() == "pending"
    assert GuiStore.path_for(m.root()).exists() and not root.exists()
    assert seen == [("workspace", None), ("state", "pending")]            # no experiment_created


def test_rename_to_the_same_name_is_a_silent_success(qtbot, tmp_path):
    root = _ready_ws(tmp_path)
    m = WorkspaceManager()
    assert m.open(root)
    seen = _record(m)
    assert m.rename("exp") is True and m.rename("  exp. ") is True
    assert m.root() == root and seen == []


def test_rename_case_only_change_is_the_same_folder_not_a_clash(qtbot, tmp_path):
    root = _ready_ws(tmp_path, name="exp")
    m = WorkspaceManager()
    assert m.open(root)
    assert m.rename("EXP") is True
    assert m.root() == tmp_path / "EXP" and "EXP" in [p.name for p in tmp_path.iterdir()]


def test_rename_refusals_change_nothing(qtbot, tmp_path):
    root = _ready_ws(tmp_path)
    (tmp_path / "taken").mkdir()
    m = WorkspaceManager()
    assert WorkspaceManager().rename("x") is False                        # nothing open
    assert m.open(root)
    seen = _record(m)
    assert m.rename("???") is False and "letters or digits" in m.last_error()
    assert m.rename("taken") is False
    assert "already exists" in m.last_error() and str(tmp_path) in m.last_error()
    assert root.is_dir() and m.root() == root and m.state() == "ready" and seen == []


def test_rename_os_failure_leaves_folder_binding_and_signals_alone(qtbot, tmp_path, monkeypatch):
    root = _ready_ws(tmp_path)
    m = WorkspaceManager()
    assert m.open(root)
    seen = _record(m)
    gen = m.generation()

    def fail(src, dst):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(wm.os, "rename", fail)
    assert m.rename("other") is False
    assert "could not rename" in m.last_error() and "Access is denied" in m.last_error()
    assert "try again" in m.last_error()
    assert root.is_dir() and not (tmp_path / "other").exists()
    assert m.root() == root and m.state() == "ready" and m.generation() == gen and seen == []


def test_rename_whose_new_path_cannot_open_is_renamed_back(qtbot, tmp_path, monkeypatch):
    root = _ready_ws(tmp_path)
    new = tmp_path / "other"
    m = WorkspaceManager()
    assert m.open(root)
    real_open = WorkspaceManager.open

    def open_fails_for_new(self, path):
        if Path(path) == new:
            self._last_error = "card unreadable"
            return False
        return real_open(self, path)

    monkeypatch.setattr(WorkspaceManager, "open", open_fails_for_new)
    seen = _record(m)
    assert m.rename("other") is False
    assert root.is_dir() and not new.exists()                             # the folder is back
    assert m.root() == root and m.state() == "ready"                      # the old root is bound again
    assert Path(m.workspace().root) == root
    assert "card unreadable" in m.last_error() and "the rename was undone" in m.last_error()
    assert seen == [("workspace", root), ("state", "ready")]              # re-bound on the old path


def test_rename_back_failing_too_goes_to_none_and_names_the_folder(qtbot, tmp_path, monkeypatch):
    root = _ready_ws(tmp_path)
    new = tmp_path / "other"
    m = WorkspaceManager()
    assert m.open(root)
    real_open, real_rename = WorkspaceManager.open, wm.os.rename
    calls = []

    def open_fails_for_new(self, path):
        if Path(path) == new:
            self._last_error = "card unreadable"
            return False
        return real_open(self, path)

    def rename_once(src, dst):
        calls.append((src, dst))
        if len(calls) > 1:
            raise OSError("in use")
        return real_rename(src, dst)

    monkeypatch.setattr(WorkspaceManager, "open", open_fails_for_new)
    monkeypatch.setattr(wm.os, "rename", rename_once)
    assert m.rename("other") is False
    assert m.state() == "none" and m.root() is None
    assert new.is_dir() and not root.exists()                             # where the folder really is
    msg = m.last_error()
    assert str(new) in msg and "renaming it back also failed" in msg and "card unreadable" in msg


# ---- save copy --------------------------------------------------------------
def test_save_copy_copies_the_tree_and_leaves_the_original_bound(qtbot, tmp_path):
    root = _ready_ws(tmp_path)
    log_bytes = b'{"kind":"decide","payload":{"action":"campaign_start"}}\n'
    (root / "report" / "session_log.jsonl").write_bytes(log_bytes)
    (root / "report" / "results.csv").write_text("scan\n1\n", encoding="utf-8")
    m = WorkspaceManager()
    assert m.open(root)
    seen = _record(m)
    gen = m.generation()

    dest = tmp_path / "kept"
    assert m.save_copy(dest) == dest.resolve()
    assert (dest / "card.policy.toml").exists() and (dest / "session.json").exists()
    assert (dest / "report" / "results.csv").read_text(encoding="utf-8") == "scan\n1\n"
    assert (dest / "report" / "session_log.jsonl").read_bytes() == log_bytes
    assert root.is_dir()                                                  # the original is kept…
    assert m.root() == root and m.state() == "ready" and m.generation() == gen   # …and still bound
    assert seen == []                                                     # no signal fires
    assert not list(tmp_path.glob(".kept.isar-saving-*"))


def test_save_copy_creates_a_nested_destination_that_does_not_exist(qtbot, tmp_path):
    """The operator names a folder in the save dialog; its parents need not exist yet."""
    root = _ready_ws(tmp_path)
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "2026-08" / "beamtime-7" / "acoustic-map"

    assert m.save_copy(dest) == dest.resolve()
    assert (dest / "card.policy.toml").exists()
    assert m.root() == root


def test_save_copy_works_from_anywhere_not_just_a_special_root(qtbot, tmp_path):
    other = tmp_path / "somewhere" / "else"
    other.mkdir(parents=True)
    root = _ready_ws(other)
    m = WorkspaceManager()
    assert m.open(root)
    assert m.save_copy(tmp_path / "copy") is not None and root.is_dir()


def test_save_copy_is_refused_until_there_is_a_card(qtbot, tmp_path):
    m = WorkspaceManager()
    assert m.save_copy(tmp_path / "c") is None and "need an experiment" in m.last_error()
    m.new_experiment("fresh", named_from="default")
    assert m.save_copy(tmp_path / "c") is None
    assert "no files read into this experiment yet" in m.last_error()
    assert not (tmp_path / "c").exists()


def test_save_copy_refuses_non_empty_dest(qtbot, tmp_path):
    root = _ready_ws(tmp_path)
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "occupied"
    dest.mkdir()
    (dest / "something.txt").write_text("busy", encoding="utf-8")
    assert m.save_copy(dest) is None
    assert "not empty" in (m.last_error() or "")
    assert root.exists() and m.root() == root                             # nothing lost, binding unchanged
    assert (dest / "something.txt").read_text(encoding="utf-8") == "busy"


def test_save_copy_refuses_source_and_descendant(qtbot, tmp_path):
    root = _ready_ws(tmp_path)
    m = WorkspaceManager()
    assert m.open(root)
    assert m.save_copy(root) is None and "must not" in (m.last_error() or "")
    assert m.save_copy(root / "saved-inside-source") is None and "must not" in (m.last_error() or "")
    assert not (root / "saved-inside-source").exists() and m.root() == root


def test_save_copy_failure_leaves_no_created_directories_behind(qtbot, tmp_path, monkeypatch):
    """A failed transaction must leave the filesystem exactly as it found it — no half-made tree."""
    root = _ready_ws(tmp_path)
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "2026-08" / "beamtime-7" / "acoustic-map"

    def fail_open(path):
        raise OSError("cannot validate staged workspace")

    monkeypatch.setattr(wm.Workspace, "open", staticmethod(fail_open))
    assert m.save_copy(dest) is None
    assert not (tmp_path / "2026-08").exists()                # validation failed before anything was made
    assert not list(tmp_path.glob("**/.acoustic-map.isar-saving-*"))
    assert root.exists() and m.root() == root

    # …and the same holds when the promotion itself is what fails, after the tree was created.
    monkeypatch.undo()
    monkeypatch.setattr(wm.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("rename boom")))
    assert m.save_copy(dest) is None
    assert "rename boom" in m.last_error()
    assert not (tmp_path / "2026-08").exists()
    assert root.exists() and m.root() == root


def test_save_copy_copy_failure_keeps_source_and_cleans_stage(qtbot, tmp_path, monkeypatch):
    root = _ready_ws(tmp_path)
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "kept"

    def fail_copy(source, stage):
        Path(stage).mkdir()
        raise OSError("copy boom")

    monkeypatch.setattr(wm.shutil, "copytree", fail_copy)
    assert m.save_copy(dest) is None
    assert "copy boom" in m.last_error()
    assert root.exists() and m.root() == root
    assert not dest.exists()
    assert not list(tmp_path.glob(".kept.isar-saving-*"))


def test_save_copy_promotion_failure_keeps_source_and_cleans_stage(qtbot, tmp_path, monkeypatch):
    root = _ready_ws(tmp_path)
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "kept"
    monkeypatch.setattr(wm.os, "replace", lambda *_a: (_ for _ in ()).throw(OSError("rename boom")))
    assert m.save_copy(dest) is None
    assert root.exists() and m.root() == root
    assert not dest.exists()
    assert not list(tmp_path.glob(".kept.isar-saving-*"))


def test_save_copy_pauses_watching_in_the_copy_only(qtbot, tmp_path):
    root = _ready_ws(tmp_path)
    store = GuiStore(root)
    store.add_watched(str(tmp_path / "beamline"))
    store.save()
    m = WorkspaceManager()
    assert m.open(root)
    dest = tmp_path / "kept"
    assert m.save_copy(dest) is not None

    copy = GuiStore.load(dest)
    assert copy.watched_folders() == [str(tmp_path / "beamline")] and copy.watch_paused() is True
    original = GuiStore.load(root)
    assert original.watched_folders() == [str(tmp_path / "beamline")] and original.watch_paused() is False


def test_save_copy_without_watched_folders_does_not_touch_the_store(qtbot, tmp_path):
    root = _ready_ws(tmp_path)
    before = b'{"version": 1}'
    GuiStore.path_for(root).write_bytes(before)
    m = WorkspaceManager()
    assert m.open(root)
    assert m.save_copy(tmp_path / "kept") is not None
    assert GuiStore.path_for(tmp_path / "kept").read_bytes() == before


def test_save_copy_that_cannot_pause_watching_is_not_offered(qtbot, tmp_path, monkeypatch):
    root = _ready_ws(tmp_path)
    store = GuiStore(root)
    store.add_watched(str(tmp_path / "beamline"))
    store.save()
    m = WorkspaceManager()
    assert m.open(root)
    monkeypatch.setattr(GuiStore, "save", lambda self: None)             # a write that logs and fails
    assert m.save_copy(tmp_path / "kept") is None
    assert "pause folder watching" in m.last_error()
    assert not (tmp_path / "kept").exists() and not list(tmp_path.glob(".kept.isar-saving-*"))
    assert root.exists() and m.root() == root


def test_pending_promotion_still_works_on_a_real_folder(qtbot):
    """The virtual campaign's reserve -> launch -> promote lifecycle (G19) now starts from a real
    experiment folder; promote_pending_if_ready is unchanged."""
    m = WorkspaceManager()
    root = m.new_experiment("Campaign_x", named_from="default")
    gen = m.generation()
    assert m.promote_pending_if_ready(root, gen) is False                 # init not finished
    stamp_ws(root, rows=_ROWS)
    assert m.promote_pending_if_ready(root, gen) is True
    assert m.state() == "ready" and m.root() == root
