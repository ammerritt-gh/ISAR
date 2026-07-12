"""FolderWatcher tests — the size+mtime stability algorithm (D4).

Drives ``poll()`` by hand (no live QTimer) against real files in ``tmp_path``: a growing file never
emits, a file that holds steady for ``stable_polls`` emits exactly once, pause defers emission, and a
file the workspace already knows is skipped.
"""
from __future__ import annotations

import os
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
