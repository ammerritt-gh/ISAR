"""The Analyzer array's path gestures on real widgets (offscreen): shift-click runs, ctrl-click paths, the path line.

A generated BL43 workspace of all 28 analyzers of one scan (made-up hkl, hard rule 10), so a row and a column both
have files. The one missing-file case swaps the grid's read for a hand-made one that has a gap in its run.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from bl43_fixture import KEY_A, build_bl43_workspace
from gui.array_helpers import ROWS, make_array_view, open_dock
from isar_gui.viewmodels import analyzer_array as vm_mod
from isar_gui.viewmodels.analyzer_array import PATH_TAIL, UNALIGNED_NOTE, path_keys
from isar_gui.views.analyzer_cell import GESTURE_TIP

SHIFT = Qt.KeyboardModifier.ShiftModifier
CTRL = Qt.KeyboardModifier.ControlModifier


def spectrum(name: str) -> str:
    return f"{KEY_A}:{name}"


@pytest.fixture(scope="module")
def grid_ws(tmp_path_factory):
    """Set A with all 28 BL43 analyzers, no fits: the card's own 4 x 7 grid."""
    return build_bl43_workspace(tmp_path_factory.mktemp("grid"), n_analyzers=28, fits=False)


def _show(env, *keys) -> None:
    env.selection.set(keys)
    env.dock.flush_redraw()
    QApplication.processEvents()


@pytest.fixture
def env(qtbot, grid_ws):
    env = open_dock(qtbot, grid_ws)
    _show(env, spectrum("a03"))                               # the grid is drawn only while a scan is selected
    return env


def _click(env, name: str, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
    cell = env.dock.cell_at(name)
    QTest.mouseClick(cell, Qt.MouseButton.LeftButton, modifiers, QPoint(cell.width() // 2, 4))
    env.dock.flush_redraw()
    QApplication.processEvents()


def _picks(env) -> tuple[str, ...]:
    return tuple(key.rsplit(":", 1)[1] for key in env.selection.picks)


def test_shift_click_runs_from_the_anchor_in_click_order(env):
    _click(env, "a03")
    _click(env, "a06", SHIFT)
    assert _picks(env) == ("a03", "a04", "a05", "a06")
    assert set(env.selection.keys) == {spectrum(n) for n in ("a03", "a04", "a05", "a06")}
    assert env.selection.active == spectrum("a06")
    assert sorted(n for n, c in env.dock.cells.items() if c.selected) == ["a03", "a04", "a05", "a06"]


def test_shift_run_back_along_the_row_keeps_the_click_order(env):
    _click(env, "a06")
    _click(env, "a03", SHIFT)
    assert _picks(env) == ("a06", "a05", "a04", "a03")


def test_shift_run_down_a_column(env):
    _click(env, "a03")
    _click(env, "a25", SHIFT)                                 # position 1 in rows A, B, C: the column of a03
    assert _picks(env) == ("a03", "a14", "a25")


def test_shift_with_nothing_picked_is_a_plain_click(env):
    env.selection.clear()                                     # no pick to run from: Shift alone, Ctrl ignored
    env.vm.click_cell(spectrum("a06"), ctrl=True, shift=True)
    assert _picks(env) == ("a06",)


def test_an_unaligned_pair_selects_nothing_and_says_so(env):
    _click(env, "a03")
    before = env.selection.keys
    _click(env, "a15", SHIFT)                                 # row B, position 2: neither a03's row nor its column
    assert env.selection.keys == before and _picks(env) == ("a03",)
    assert env.dock.path_label.isVisible() and env.dock.path_label.full_text() == UNALIGNED_NOTE


def test_ctrl_click_order_is_the_path_and_a_run_starts_from_the_last_pick(env):
    _click(env, "a06")
    _click(env, "a04", CTRL)
    _click(env, "a09", CTRL)
    assert _picks(env) == ("a06", "a04", "a09")
    assert env.dock.path_label.full_text().startswith("Path: a06 → a04 → a09 (3 analyzers, click order) · ")
    assert env.dock.path_label.full_text().endswith(PATH_TAIL)
    _click(env, "a07", SHIFT)                                 # the last pick is a09: the run is a09 back to a07
    assert _picks(env) == ("a09", "a08", "a07")


def test_a_cell_with_no_file_in_the_run_is_skipped_and_named(env, monkeypatch):
    names = [n for row in ROWS for n in row if n != "a05"]    # a05 has no file in this scan
    view = make_array_view(KEY_A, present=names)
    monkeypatch.setattr(vm_mod, "array_view", lambda snapshot, key, ws: view)
    _show(env, spectrum("a03"))
    _click(env, "a03")
    _click(env, "a07", SHIFT)
    assert _picks(env) == ("a03", "a04", "a06", "a07")
    assert set(env.selection.keys) == {spectrum(n) for n in ("a03", "a04", "a06", "a07")}
    assert "a05 has no file in this scan, so it is not in the path" in env.dock.path_label.full_text()


def test_space_with_shift_is_the_same_gesture(env):
    _click(env, "a03")
    cell = env.dock.cell_at("a06")
    cell.setFocus()
    QTest.keyClick(cell, Qt.Key.Key_Space, SHIFT)
    env.dock.flush_redraw()
    assert _picks(env) == ("a03", "a04", "a05", "a06")


def test_path_keys_are_the_shown_scans_picks_in_click_order():
    cells = [SimpleNamespace(key=key) for key in ("s:a03", "s:a04", None)]
    assert path_keys(cells, ("s:a04", "t:a09", "s:a03")) == ("s:a04", "s:a03")    # another scan's pick is not on the path


def test_the_gesture_words_are_on_the_cell_the_grid_and_the_caption(env):
    _show(env, spectrum("a06"))
    d = env.dock
    for widget in (d.cell_at("a06"), d.grid_host, d.caption_label):
        assert "Shift-click" in widget.toolTip() and "Ctrl-click" in widget.toolTip()
