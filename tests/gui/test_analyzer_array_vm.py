"""The Analyzer array view model, no widgets: which scan, which words, which y range, and what it never does.

Real workspaces generated at test time (hard rule 10); the read layer's grid is replaced by a hand-made 4 x 7
``ArrayView`` where a test needs analyzers the small fixtures do not have.
"""
from __future__ import annotations

import ast
import math
import os
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

import isar
from bl43_fixture import KEY_A, KEY_B, KEY_SINGLE, pooled_workspace
from gui.array_helpers import ROWS, inter_row, make_array_view, make_env, rlu
from gui.inter_fakes import FakeInterState, make_overview
from isar.workspace_api.spectra import ZERO_NONE
from isar_gui.selection import ORIGIN_ARRAY
from isar_gui.viewmodels import analyzer_array as mod
from isar_gui.viewmodels.analyzer_array import (ABSENCE_WORDS, EMPTY_MESSAGE, POOLED_MESSAGE, BatchMark,
                                                BatchOverlay)
from scans_view_fixture import NO_MONITOR, SPECTRA_A, SPECTRA_B

WORKTREE = Path(__file__).resolve().parents[2]
A3, A4, A5, A6, A7, A8, A9 = SPECTRA_A


def test_import_root_is_this_checkout():
    root = Path(isar.__file__).resolve().parents[1]
    print("import root:", root)
    assert os.path.normcase(str(root)) == os.path.normcase(str(WORKTREE))


@pytest.fixture
def env(scans_view_ws_readonly):
    return make_env(scans_view_ws_readonly)


def _by_name(state):
    return {c.beamline: c for c in state.cells}


def _grid(monkeypatch, env, set_key=KEY_A, **kw):
    """The VM over a hand-made 4 x 7 view of ``set_key``: every analyzer has a file in the view, but only the ones
    the workspace really has (a03-a09) carry a spectrum; the others read as no file."""
    view = make_array_view(set_key, **kw)
    monkeypatch.setattr(mod, "array_view", lambda snapshot, key, ws: view)
    return view


def test_body_states(scans_view_ws_readonly, tmp_path):
    env = make_env(scans_view_ws_readonly)
    assert env.vm.recompute().message == EMPTY_MESSAGE and env.vm.state.body == "message"
    tavi = make_env(pooled_workspace(tmp_path / "tavi"))
    tavi.selection.set(("tavi:900:det",))
    state = tavi.vm.recompute()
    assert state.body == "message" and state.message == POOLED_MESSAGE
    tavi.selection.set(("tavi:11:det",))
    state = tavi.vm.recompute()
    assert state.body == "message" and "no analyzer layout" in state.message.lower()
    assert "BL43LXU for now" in state.message and state.cells == ()
    assert "scan 11" in state.title and state.body == "message"       # the header still says which scan
    env.selection.set((A6,))
    assert env.vm.recompute().body == "grid"


def test_positions_header_and_labels(env, monkeypatch):
    _grid(monkeypatch, env)
    env.selection.set((A6,), clicked=A6)
    state = env.vm.recompute()
    cells = _by_name(state)
    assert len(cells) == 28
    assert (cells["a03"].screen_row, cells["a03"].screen_col) == (3, 6)       # bottom right
    assert (cells["a09"].screen_row, cells["a09"].screen_col) == (3, 0)       # bottom left
    assert (cells["a41"].screen_row, cells["a41"].screen_col) == (0, 0)       # top left
    assert (cells["a06"].internal, cells["a06"].screen_row, cells["a06"].screen_col) == ("A4", 3, 3)
    assert cells["a35"].internal == "D1" and cells["a14"].internal == "B1"
    # a made-up hkl (0.5 + 0.1 p, ...) at position p = 3: two decimals, the full value on hover
    assert cells["a06"].hkl == "0.80, 0.50, 3.00" and "0.8, 0.5, 3 r.l.u." in cells["a06"].hkl_tip
    assert cells["a06"].q.startswith("|Q| ") and cells["a06"].q.endswith("nm⁻¹")
    assert cells["a41"].key is None and cells["a41"].absence == "no file" and cells["a41"].q == ""
    # experiment, scan, sample, temperature and the scan's nominal hkl (all made up in the hand-made view)
    assert state.title == "exp1 · scan 13 · Sr2RuO4 · 97 K · hkl 0.50, 0.50, 3.00"
    assert state.layout_line == "Layout BL43LXU-7x4 (the only BL43LXU layout)"
    assert state.selection_note == ""
    assert state.caption.startswith("x: energy transfer (meV), the same in every cell · y: ")
    assert state.caption.endswith(", linear")


def test_delta_hkl_words_full_partial_and_none(env, monkeypatch):
    env.selection.set((A6,))
    _grid(monkeypatch, env)
    state = env.vm.recompute()
    assert state.x_label == "In plane — 2θ increases ←   Δhkl ≈ (0.100, 0.000, 0.000) per analyzer"
    assert state.y_label == "Out of plane ↑   Δhkl ≈ (0.000, 0.100, 0.000) per row"
    lines = state.x_tip.splitlines()
    assert lines[1] == "A1 → A2  (0.1, 0, 0)" and len(lines) == 1 + 24         # the exact step of every pair
    _grid(monkeypatch, env, no_hkl=("a03", "a14", "a25", "a35"))                  # one end of every row has no hkl
    state = env.vm.recompute()
    assert state.x_label.endswith("per analyzer (from 20 of 24 pairs)")
    assert state.y_label.endswith("per row (from 18 of 21 pairs)")
    _grid(monkeypatch, env, no_hkl=tuple(n for names in ROWS for n in names))
    state = env.vm.recompute()
    assert state.x_label == "In plane — 2θ increases ←   no hkl to compute the step"
    assert state.y_label == "Out of plane ↑   no hkl to compute the step"


def test_first_picked_wins_and_is_said(env):
    env.selection.set((A6, SPECTRA_B[0], A4), clicked=SPECTRA_B[0], first=SPECTRA_B[0])
    assert env.selection.first_picked == SPECTRA_B[0]
    state = env.vm.recompute()
    assert state.title.startswith("scan ") and "_rep_sum" in state.title
    assert state.selection_note.startswith("Showing scan ") and state.selection_note.endswith(
        "— 2 scans selected, only one is shown")
    env.selection.set((A6, SPECTRA_B[0], A4), clicked=A6, first=A6)           # the same set, the other scan first
    state = env.vm.recompute()
    assert state.title.startswith("scan CsV3Sb5_97K_0p5_0p5_3_sum ") and "_rep_sum" not in state.title
    env.selection.set((A6, A4))
    assert env.vm.recompute().selection_note == ""


def test_scale_and_the_typed_limit_per_scale(env, monkeypatch):
    env.selection.set((A6,))
    state = env.vm.recompute()
    auto = {c.beamline: c.y_range for c in state.cells if c.plot is not None}
    assert state.y_mode == "auto" and state.scale == "normalised" and not state.limit_enabled
    assert state.limit_reason == mod.LIMIT_REASON
    assert all(lo == min(0.0, lo) and hi > 0 for lo, hi in auto.values()) and len(set(auto.values())) > 1
    env.vm.set_y_mode("shared")
    state = env.vm.recompute()
    top = max(hi for _, hi in auto.values())
    assert {c.y_range for c in state.cells if c.plot is not None} == {(0.0, top)}
    assert state.limit_placeholder == f"largest shown ({top:g})" and state.limit_text == ""
    env.vm.set_scale("raw")
    assert env.vm.set_limit("raw", "250")
    state = env.vm.recompute()
    assert {c.y_range for c in state.cells if c.plot is not None} == {(0.0, 250.0)}
    assert state.limit_text == "250" and state.limit_unit == "Counts" and state.caption.endswith("Counts, linear")
    env.vm.set_scale("normalised")                      # the Raw limit is kept apart
    state = env.vm.recompute()
    assert env.vm.limits == {"raw": 250.0, "normalised": None} and state.limit_text == ""
    assert {c.y_range for c in state.cells if c.plot is not None} == {(0.0, top)}
    assert state.limit_enabled and state.limit_reason is None
    for bad in ("abc", "-3", "0", "nan", "inf"):
        assert not env.vm.set_limit("normalised", bad)
        assert env.vm.limits["normalised"] is None and "was not used" in env.vm.recompute().limit_reason
    assert env.vm.set_limit("normalised", "") and env.vm.limits["normalised"] is None
    assert env.vm.recompute().limit_reason is None


def test_x_range_is_one_padded_union_for_every_cell(env):
    env.selection.set((A6,))
    ranges = {c.x_range for c in env.vm.recompute().cells}
    assert len(ranges) == 1
    lo, hi = ranges.pop()
    assert lo < -3.9 and hi > 7.7 and hi - lo > 11.7              # the fixture's energies run about -4 to 7.8 meV


def test_the_reference_is_the_scans_pages_one(env):
    sentinel = 12345.0
    seen = []
    env.vm.reference_source = lambda snapshot: (seen.append(snapshot), sentinel)[1]
    env.selection.set((A6,))
    plots = [c.plot for c in env.vm.recompute().cells if c.plot is not None]
    assert seen and all(s is env.scan_list.snapshot for s in seen)
    assert plots and all(p.token[-1] == sentinel for p in plots)
    # With the Scan(s) page on Raw the dock still gets that page's (normalised) reference, never its own
    env.page.set_scale("raw")
    env.vm.reference_source = env.page.reference_for
    reference = env.page.reference_for(env.scan_list.snapshot)
    assert reference is not None and reference > 0
    plots = [c.plot for c in env.vm.recompute().cells if c.plot is not None]
    assert plots and all(p.token[-1] == reference for p in plots)


def test_the_fit_drawn_is_the_authoritative_one_not_the_menu_pick(scans_view_ws_readonly, monkeypatch):
    calls = []
    real = mod.spectrum_bundles

    def wrapped(ws, rows, *, cache=None, fit_choice=None):
        calls.append(dict(fit_choice or {}))
        out = real(ws, rows, cache=cache)       # the real refit curves are another unit's: name the fit only
        return tuple(replace(b, fit_id=fit_choice[b.key], fit_label="Batch 1 · ring 2",
                             revision=b.revision + "|b1") if b.key in (fit_choice or {}) else b for b in out)

    monkeypatch.setattr(mod, "spectrum_bundles", wrapped)
    inter = FakeInterState()
    inter.overview = make_overview(by_key={A6: inter_row(A6, "b1:fit"), A4: inter_row(A4, "fast")})
    env = make_env(scans_view_ws_readonly, inter=inter)
    env.selection.set((A6,))
    state = env.vm.recompute()
    assert calls[-1] == {A6: "b1:fit"}                       # the authoritative refit; a fast solo stays out of the choice
    assert next(c for c in state.cells if c.key == A6).fit_label == "Batch 1 · ring 2"
    assert next(c for c in state.cells if c.key == A4).fit_label == "Fast solo"
    env.page.recompute()
    env.page.choose_fit("other")                             # a fit picked in the Scan(s) fit menu
    assert env.page.chosen == {A6: "other"}
    assert env.vm.recompute() == state and calls[-1] == {A6: "b1:fit"}


def test_absences_are_words_never_zero(scans_view_ws_readonly, monkeypatch):
    real = mod.spectrum_bundles

    def wrapped(ws, rows, *, cache=None, fit_choice=None):
        return tuple(replace(b, zero=ZERO_NONE) if b.key == A5 else b
                     for b in real(ws, rows, cache=cache, fit_choice=fit_choice))

    monkeypatch.setattr(mod, "spectrum_bundles", wrapped)
    env = make_env(scans_view_ws_readonly)
    _grid(monkeypatch, env)
    env.selection.set((A6,))
    cells = _by_name(env.vm.recompute())
    assert cells["a41"].absence == "no file" and cells["a41"].plot is None and cells["a41"].y_range is None
    assert cells["a05"].absence == "not zeroed" and cells["a05"].plot is None       # held: no curve, no data
    assert "no energy zero" in cells["a05"].absence_tip
    assert cells["a09"].absence == "no fit" and cells["a09"].plot is not None       # data still drawn
    assert cells["a09"].fit_label is None and cells["a06"].fit_label == "Fast solo"  # a fit is named only when drawn
    assert cells["a06"].absence is None and cells["a06"].plot.total is not None
    assert all(c.plot is None for c in cells.values() if c.absence in ("no file", "not zeroed"))
    assert {"no file", "no fit", "not zeroed"} <= {word for word, _ in ABSENCE_WORDS.values()}
    monkeypatch.undo()
    # a spectrum with no monitor is "no monitor" in Normalised and drawn in Raw
    single = make_env(scans_view_ws_readonly)
    single.selection.set((NO_MONITOR,))
    cell = next(c for c in single.vm.recompute().cells if c.key == NO_MONITOR)
    assert cell.absence == "no monitor" and cell.plot is None and cell.absence_tip
    single.vm.set_scale("raw")
    cell = next(c for c in single.vm.recompute().cells if c.key == NO_MONITOR)
    assert cell.plot is not None and cell.absence in (None, "no fit")


def test_anchor_lock_and_both(scans_view_ws_readonly):
    inter = FakeInterState()
    inter.overview = make_overview(by_key={A3: inter_row(A3, locked=True, locked_fit_id="fast"),
                                           A4: inter_row(A4, locked=True, locked_fit_id="fast", lock_stale=True),
                                           A6: inter_row(A6)}, anchors=(A4, A6))
    env = make_env(scans_view_ws_readonly, inter=inter)
    env.selection.set((A6,))
    cells = _by_name(env.vm.recompute())
    assert (cells["a03"].anchor, cells["a03"].lock) == (False, True)
    assert (cells["a04"].anchor, cells["a04"].lock) == (True, True)         # a stale lock keeps its plain mark
    assert (cells["a06"].anchor, cells["a06"].lock) == (True, False)
    assert (cells["a07"].anchor, cells["a07"].lock) == (False, False)


def test_selection_both_ways_and_no_file_is_inert(env, monkeypatch):
    view = _grid(monkeypatch, env)
    env.selection.set((A6,))
    env.vm.recompute()
    env.vm.click_cell(A4)
    assert env.selection.keys == (A4,) and env.selection.active == A4
    change = []
    env.selection.changed.connect(change.append)
    env.vm.click_cell(A6, ctrl=True)
    assert set(env.selection.keys) == {A4, A6} and change[-1].origin == ORIGIN_ARRAY
    assert env.selection.picks[0] == A4                                      # the first pick is kept
    env.vm.click_cell(A4, ctrl=True)
    assert env.selection.keys == (A6,) and env.selection.first_picked == A6
    env.vm.click_cell(None)
    env.vm.click_cell("exp:1:a99")
    assert env.selection.keys == (A6,)
    cells = _by_name(env.vm.recompute())
    assert [n for n, c in cells.items() if c.selected] == ["a06"]
    env.selection.set((A3, A4))                                              # the list selects: those cells light
    assert {n for n, c in _by_name(env.vm.recompute()).items() if c.selected} == {"a03", "a04"}
    assert view is not None


def test_unchanged_state_is_equal_and_emits_nothing(env):
    env.selection.set((A6,))
    emitted = []
    env.vm.state_changed.connect(emitted.append)
    first = env.vm.recompute()
    second = env.vm.recompute()
    assert second is first and len(emitted) == 1
    env.selection.set((A6,))
    assert env.vm.recompute() is first and len(emitted) == 1


def test_a_hidden_dock_does_no_work(env, monkeypatch):
    env.vm.set_active(False)
    read = []
    monkeypatch.setattr(mod, "spectrum_bundles", lambda *a, **k: read.append(1))
    env.selection.set((A6,))
    state = env.vm.recompute()
    assert read == [] and state.body == "message"
    env.vm.set_active(True)
    monkeypatch.undo()
    assert env.vm.recompute().body == "grid"


def test_view_state_round_trip_and_garbage(env):
    from isar_gui.session import Session
    env.vm.set_scale("raw")
    env.vm.set_y_mode("shared")
    env.vm.set_limit("raw", "40")
    env.vm.set_limit("normalised", "2.5")
    session = Session()
    env.vm.save_view_state(session)
    other = make_env(env.ws.root).vm
    other.load_view_state(session)
    assert (other.scale, other.y_mode, other.limits) == ("raw", "shared", {"raw": 40.0, "normalised": 2.5})
    for junk in ({"scale": 3, "y_mode": "x", "limit_raw": -1, "limit_normalised": "a"}, "text", None,
                 {"scale": "raw", "limit_raw": True, "limit_normalised": float("nan")}):
        session.view_state["analyzer_array"] = junk
        other.load_view_state(session)
        assert other.y_mode == "auto" and other.limits == {"raw": None, "normalised": None}
        assert other.scale in ("normalised", "raw")


def test_batch_overlay_seam_with_a_hand_made_overlay(env, monkeypatch):
    _grid(monkeypatch, env)
    env.selection.set((A6,))
    base = env.vm.recompute()
    assert base.batch_line == "" and all(c.batch is None for c in base.cells)
    marks = {A6: BatchMark("fitting"), A7: BatchMark("done"), A5: BatchMark("waiting"),
             "e:14:a06": BatchMark("failed", "did not converge")}
    overlay = BatchOverlay("b1", "Anchored batch · ring 2 · 1 of 3 done", "waiting 1 · fitting 1 · done 1", marks)
    emitted = []
    env.vm.state_changed.connect(emitted.append)
    env.vm.set_batch_overlay(overlay)
    state = env.vm.recompute()
    cells = _by_name(state)
    assert cells["a06"].batch.state == "fitting" and cells["a07"].batch.state == "done"
    assert cells["a05"].batch.state == "waiting" and cells["a04"].batch is None
    assert state.batch_line == overlay.line and state.batch_line_tip == overlay.line_tip
    env.vm.set_batch_overlay(BatchOverlay("b1", overlay.line, overlay.line_tip, dict(marks)))   # an equal overlay
    env.vm.recompute()
    assert len(emitted) == 1
    env.vm.set_batch_overlay(None)
    state = env.vm.recompute()
    assert all(c.batch is None for c in state.cells) and state.batch_line == "" and len(emitted) == 2
    assert set(mod.BATCH_STATES) == {"waiting", "fitting", "done", "refused", "failed", "not_reached"}


def test_the_view_model_imports_only_the_read_layer():
    tree = ast.parse((WORKTREE / "isar_gui" / "viewmodels" / "analyzer_array.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        names = ([a.name for a in node.names] if isinstance(node, ast.Import)
                 else [node.module or ""] if isinstance(node, ast.ImportFrom) else [])
        for name in names:
            assert not name.startswith("isar.") or name.startswith("isar.workspace_api"), name
    # no Qt widgets either: a view model holds no widgets
    assert "QtWidgets" not in (WORKTREE / "isar_gui" / "viewmodels" / "analyzer_array.py").read_text(encoding="utf-8")
