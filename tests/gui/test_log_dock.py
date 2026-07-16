"""LogDock.sync_warnings tests: the generation-aware warning sync shows a fresh batch in full after a
refresh (which clears + repopulates warnings), skips an identical batch on the next tick (no
steady-state spam), and shows a longer batch in full. Offscreen (conftest forces QT offscreen),
importorskip, xdist-safe."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from isar_gui.log_dock import LogDock


def _lines(dock) -> list:
    text = dock._view.toPlainText()
    return [ln for ln in text.splitlines() if ln]


def test_view_rejects_drops_so_mainwindow_sees_them(qtbot):
    dock = LogDock()
    qtbot.addWidget(dock)
    assert dock._view.acceptDrops() is False


def test_same_length_different_batch_shows_new_lines(qtbot):
    dock = LogDock()
    qtbot.addWidget(dock)
    dock.sync_warnings(["a", "b"], generation=1)
    assert _lines(dock) == ["warning: a", "warning: b"]
    # A refresh clears + repopulates warnings; a same-length but DIFFERENT batch must all show
    # (the length-based bug would have skipped it entirely).
    dock.sync_warnings(["c", "d"], generation=2)
    assert _lines(dock) == ["warning: a", "warning: b", "warning: c", "warning: d"]


def test_identical_batch_next_generation_prints_nothing(qtbot):
    dock = LogDock()
    qtbot.addWidget(dock)
    dock.sync_warnings(["parse failed: scan 7"], generation=1)
    assert _lines(dock) == ["warning: parse failed: scan 7"]
    # The same failure re-reported on the next tick is steady state — do not spam the log.
    dock.sync_warnings(["parse failed: scan 7"], generation=2)
    assert _lines(dock) == ["warning: parse failed: scan 7"]


def test_longer_batch_shows_all_lines_of_new_generation(qtbot):
    dock = LogDock()
    qtbot.addWidget(dock)
    dock.sync_warnings(["a", "b"], generation=1)
    dock.sync_warnings(["x", "y", "z"], generation=2)
    assert _lines(dock) == ["warning: a", "warning: b",
                            "warning: x", "warning: y", "warning: z"]


def test_legacy_length_based_behavior_without_generation(qtbot):
    dock = LogDock()
    qtbot.addWidget(dock)
    dock.sync_warnings(["a"])          # no generation -> legacy high-water mark
    dock.sync_warnings(["a", "b"])     # only the new tail shows
    assert _lines(dock) == ["warning: a", "warning: b"]
