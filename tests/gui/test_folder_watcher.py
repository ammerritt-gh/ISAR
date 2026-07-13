"""FolderWatcher tests — the size+mtime stability algorithm (D4).

Drives ``poll()`` by hand (no live QTimer) against real files in ``tmp_path``: a growing file never
emits, a file that holds steady for ``stable_polls`` emits exactly once, pause defers emission, and a
file the workspace already knows is skipped.
"""
from __future__ import annotations

import os
from pathlib import Path
import time

import pytest

pytest.importorskip("PySide6")

from isar_gui.folder_watcher import FolderWatcher


def _touch(path, data: bytes):
    path.write_bytes(data)
    # Nudge mtime so a size-preserving rewrite still reads as "changed" where a test needs it to.
    t = time.time()
    os.utime(path, (t, t))


def test_stable_file_emits_exactly_once(qtbot, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    emitted = []
    fw = FolderWatcher(poll_ms=5, stable_polls=3)
    fw.files_stable.connect(lambda paths: emitted.append(list(paths)))
    fw.set_folders([str(folder)])              # empty at this point (poll sees nothing)

    (folder / "scan_1.dat").write_bytes(b"complete numor")
    fw.poll()                                   # count 1
    fw.poll()                                   # count 2
    assert emitted == []
    fw.poll()                                   # count 3 -> emit
    assert len(emitted) == 1 and emitted[0][0].endswith("scan_1.dat")

    fw.poll()                                   # already emitted -> no re-emit
    assert len(emitted) == 1


def test_growing_file_never_emits(qtbot, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    emitted = []
    fw = FolderWatcher(poll_ms=5, stable_polls=3)
    fw.files_stable.connect(lambda paths: emitted.append(list(paths)))
    fw.set_folders([str(folder)])

    f = folder / "growing.dat"
    for i in range(6):
        _touch(f, b"x" * (i + 1))               # size changes every poll
        fw.poll()
    assert emitted == []                        # never 3 consecutive equal observations


def test_pause_defers_emission_until_unpause(qtbot, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    emitted = []
    fw = FolderWatcher(poll_ms=5, stable_polls=2)
    fw.files_stable.connect(lambda paths: emitted.append(list(paths)))
    fw.set_folders([str(folder)])
    fw.set_paused(True)

    (folder / "s.dat").write_bytes(b"done")
    fw.poll()
    fw.poll()                                   # would cross the threshold, but paused
    assert emitted == []

    fw.set_paused(False)                        # unpause flushes the accumulated stable set
    assert len(emitted) == 1 and emitted[0][0].endswith("s.dat")


def test_known_file_is_skipped(qtbot, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    f = folder / "known.dat"
    f.write_bytes(b"already ingested")
    size = f.stat().st_size

    emitted = []
    fw = FolderWatcher(poll_ms=5, stable_polls=2,
                       is_known=lambda p, s, m: p.endswith("known.dat") and s == size)
    fw.files_stable.connect(lambda paths: emitted.append(list(paths)))
    fw.set_folders([str(folder)])
    for _ in range(4):
        fw.poll()
    assert emitted == []                        # known-with-matching-size -> never emitted


def test_set_folders_drops_files_deferred_while_paused(qtbot, tmp_path):
    """A file that settled while paused under one folder set must NOT flush on the next unpause after
    set_folders re-seeded the watcher (a workspace switch) — its deferred entry is cleared."""
    folder = tmp_path / "data"
    folder.mkdir()
    emitted = []
    fw = FolderWatcher(poll_ms=5, stable_polls=2)
    fw.files_stable.connect(lambda paths: emitted.append(list(paths)))
    fw.set_folders([str(folder)])
    fw.set_paused(True)

    (folder / "s.dat").write_bytes(b"done")
    fw.poll()
    fw.poll()                                   # crosses the threshold, but paused -> deferred
    assert emitted == []

    fw.set_folders([])                          # a switch re-seeds the watcher -> deferred cleared
    fw.set_paused(False)                        # unpause has nothing to flush now
    assert emitted == []


def test_hidden_and_transient_files_are_ignored(qtbot, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    (folder / ".hidden").write_bytes(b"x")
    (folder / "part.tmp").write_bytes(b"x")
    emitted = []
    fw = FolderWatcher(poll_ms=5, stable_polls=2)
    fw.files_stable.connect(lambda paths: emitted.append(list(paths)))
    fw.set_folders([str(folder)])
    for _ in range(4):
        fw.poll()
    assert emitted == []


def test_atomic_config_preserves_deferred_but_rebind_resets_it(qtbot, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    emitted = []
    fw = FolderWatcher(poll_ms=5, stable_polls=2)
    fw.files_stable.connect(lambda paths: emitted.extend(paths))
    fw.configure([str(folder)], paused=True, reset_binding=True)
    (folder / "s.dat").write_bytes(b"done")
    fw.poll()
    fw.poll()
    assert fw.status().deferred and emitted == []

    # A normal save/pause edit is not a workspace switch: it must retain settled files.
    fw.configure([str(folder)], paused=False)
    assert [Path(p).name for p in emitted] == ["s.dat"]

    fw.configure([str(folder)], paused=True)
    (folder / "other.dat").write_bytes(b"done")
    fw.poll()
    fw.poll()
    fw.configure([str(folder)], paused=False, reset_binding=True)
    assert [Path(p).name for p in emitted] == ["s.dat"]


def test_parse_failures_retry_then_exhaust_and_content_change_rearms(qtbot, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    now = [0.0]
    emitted = []
    fw = FolderWatcher(poll_ms=5, stable_polls=2, clock=lambda: now[0])
    fw.files_stable.connect(lambda paths: emitted.extend(paths))
    fw.configure([str(folder)], paused=False, reset_binding=True)
    path = folder / "bad.dat"
    path.write_bytes(b"bad")
    fw.poll()
    fw.poll()
    assert len(emitted) == 1

    report = type("Report", (), {"files": ({"path": str(path), "error": "bad header"},)})()
    for delay in (5.0, 15.0, 30.0):
        fw.record_ingest_report(report)
        assert fw.status().retrying == (str(path),)
        now[0] += delay
        fw.poll()
        assert len(emitted) >= 2
    fw.record_ingest_report(report)
    assert fw.status().exhausted == (str(path),)

    _touch(path, b"fixed")
    fw.poll()  # changed -> re-arm
    fw.poll()  # stable -> emit again
    assert not fw.status().exhausted
    assert len(emitted) == 5


def test_missing_folder_is_persistent_visible_status(qtbot, tmp_path):
    folder = tmp_path / "late"
    fw = FolderWatcher(poll_ms=5, stable_polls=2)
    fw.configure([str(folder)], paused=False, reset_binding=True)
    assert fw.status().unavailable == (str(folder),)

    folder.mkdir()
    fw.poll()
    assert fw.status().unavailable == ()


def test_manual_or_stale_ingest_error_does_not_create_watcher_retry(qtbot, tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    path = folder / "manual.dat"
    path.write_bytes(b"bad")
    fw = FolderWatcher(poll_ms=5, stable_polls=2)
    fw.configure([str(folder)], paused=False, reset_binding=True)
    report = type("Report", (), {"files": ({"path": str(path), "error": "bad header"},)})()

    # The file was manually ingested, not emitted by the watcher, so it must not acquire a retry.
    fw.record_ingest_report(report)
    assert fw.status().retrying == ()

    # A late report after the workspace binding changes is equally irrelevant.
    fw.configure([], paused=False, reset_binding=True)
    fw.record_ingest_report(report)
    assert fw.status().retrying == () and fw.status().exhausted == ()


def test_stat_failure_does_not_prune_existing_retry_state(qtbot, tmp_path, monkeypatch):
    folder = tmp_path / "data"
    folder.mkdir()
    path = folder / "bad.dat"
    path.write_bytes(b"bad")
    now = [0.0]
    fw = FolderWatcher(poll_ms=5, stable_polls=2, clock=lambda: now[0])
    fw.files_stable.connect(lambda _paths: None)
    fw.configure([str(folder)], paused=False, reset_binding=True)
    fw.poll()
    fw.poll()
    report = type("Report", (), {"files": ({"path": str(path), "error": "share busy"},)})()
    fw.record_ingest_report(report)
    assert fw.status().retrying == (str(path),)

    original_stat = Path.stat

    def _deny_child_stat(self, *args, **kwargs):
        if self == path:
            raise PermissionError("temporary share lock")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _deny_child_stat)
    fw.poll()
    assert fw.status().retrying == (str(path),)
