"""The Scan(s) page with inter-spectra fits, on real widgets (offscreen): which fit opens, the viewed-fit
choice, the notes and header, the waterfall of one spectrum's fits and the writers' call counts.

``spectrum_bundles`` is wrapped, not faked: it records the ``fit_choice`` it was given and returns the real
fast-solo bundles with the fit id folded into the revision (the real fit curves are S1U5's)."""
from __future__ import annotations

import os
from dataclasses import replace

import pytest

import isar
from gui.inter_fakes import FakeInterState, make_entry, make_history, make_overview
from isar.workspace_api import Workspace
from isar.workspace_api.inter_spectra import SpectrumInter
from isar_gui.inter_words import CONDITIONAL_ERRORS
from isar_gui.gui_store import GuiStore
from isar_gui.viewmodels import scans_page as vm_module
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.scans_page import ScansPage
from scans_view_fixture import ONE_MODE, TWO_MODE

WORKTREE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WRITERS = {"set_anchors", "pick_fit", "set_lock", "save_settings"}


def test_import_root_is_this_checkout():
    root = os.path.dirname(os.path.dirname(isar.__file__))
    print("import root:", root)
    assert os.path.normcase(root) == os.path.normcase(os.path.dirname(WORKTREE))


def _row(key, fit_id="fast", **overrides):
    fields = dict(key=key, fit_id=fit_id, in_use=fit_id != "fast", source="batch" if fit_id != "fast" else "fast",
                  label="Batch 1 · ring 2" if fit_id != "fast" else "Fast solo", flag=None, locked=False,
                  locked_fit_id=None, lock_stale=False, stale=False, stale_text=None)
    return SpectrumInter(**{**fields, **overrides})


def _refit(fit_id, stage, **overrides):
    return make_entry(fit_id, f"Batch 1 · {stage}", batch_id="b1", batch_label="Batch 1", stage_label=stage,
                      **overrides)


@pytest.fixture
def reads(monkeypatch):
    """Every ``spectrum_bundles`` call as ``fit_choice`` (None when the kwarg is absent)."""
    calls = []
    real = vm_module.spectrum_bundles

    def wrapped(ws, rows, *, cache=None, fit_choice=None):
        calls.append(fit_choice)
        fit_for = lambda b: (fit_choice or {}).get(b.key, "fast")
        return tuple(replace(b, revision=f"{b.revision}|{fit_for(b)}") for b in real(ws, rows, cache=cache))

    monkeypatch.setattr(vm_module, "spectrum_bundles", wrapped)
    return calls


@pytest.fixture
def setup(qtbot, scans_view_ws_readonly, reads):
    ws = Workspace.open(scans_view_ws_readonly)
    page = ScansPage(ScanListViewModel(ws), ws)
    qtbot.addWidget(page)
    page.set_store(GuiStore(scans_view_ws_readonly))
    inter = FakeInterState()
    page.set_inter(inter)
    page.resize(700, 600)
    page.show()
    page.vm.set_fit("fast")
    return page, inter


def _select(page, *keys, active=None):
    page.selection.set(keys)
    if active:
        page.selection.set_active(active)
    page.flush_redraw()
    return page.drawn()


def test_opens_on_the_authoritative_fit_and_passes_fit_choice_for_in_use_spectra_only(setup, reads):
    page, inter = setup
    inter.overview = make_overview(by_key={ONE_MODE: _row(ONE_MODE, "r2"), TWO_MODE: _row(TWO_MODE)})
    inter.histories[ONE_MODE] = make_history(_refit("r2", "ring 2", authoritative=True))
    drawn = _select(page, ONE_MODE, TWO_MODE, active=ONE_MODE)
    assert reads[-1] == {ONE_MODE: "r2"}
    notes = {line.entry: line.text for line in drawn.legend}
    assert "Batch 1 · ring 2" in notes[ONE_MODE] and CONDITIONAL_ERRORS in notes[ONE_MODE]
    assert CONDITIONAL_ERRORS not in notes[TWO_MODE]
    assert drawn.curves_header == "Curves · Fast and inter-spectra"
    assert page._drawn_state.inter.viewed == "r2"


def test_header_words_for_all_solo_and_all_refit(setup, reads):
    page, inter = setup
    assert _select(page, ONE_MODE).curves_header == "Curves · Fast"
    assert reads[-1] is None                        # nothing in use: today's plain call
    inter.overview = make_overview(by_key={ONE_MODE: _row(ONE_MODE, "r2")}, revision="2")
    inter.changed.emit()
    page.flush_redraw()
    assert page.drawn().curves_header == "Curves · Inter-spectra fits"


def test_choice_applies_to_the_active_spectrum_only_and_switches_fit_to_fast(setup, reads):
    page, inter = setup
    inter.histories[ONE_MODE] = make_history(_refit("r1", "ring 1"), _refit("r2", "ring 2", stale=True))
    page.vm.set_fit("none")
    _select(page, ONE_MODE, TWO_MODE, active=ONE_MODE)
    page.vm.choose_fit("r2")
    page.flush_redraw()
    assert page.vm.fit == "fast"
    assert reads[-1] == {ONE_MODE: "r2"}
    text = next(line.text for line in page.drawn().legend if line.entry == ONE_MODE)
    assert "stale" in text and CONDITIONAL_ERRORS in text
    page.selection.set_active(TWO_MODE)
    page.flush_redraw()
    assert page.vm.chosen == {} and reads[-1] is None


def test_waterfall_draws_the_fits_oldest_at_the_bottom_with_labels(setup, reads):
    page, inter = setup
    inter.histories[ONE_MODE] = make_history(
        _refit("r1", "ring 1"), _refit("r2", "ring 2", changed=False), _refit("r3", "ring 3", authoritative=True))
    inter.overview = make_overview(by_key={ONE_MODE: _row(ONE_MODE, "r3")})
    _select(page, ONE_MODE)
    page.inter_bar.waterfall_button.click()
    page.flush_redraw()
    state = page._drawn_state
    assert [t.identity for t in state.traces] == ["Batch 1 · ring 3", "Batch 1 · ring 1", "Fast solo"]    # the first is drawn on top
    assert [t.active for t in state.traces] == [True, False, False]
    assert state.view == "offset" and state.residual_spec is None
    assert state.main_spec.meta["n"] == 3
    assert None in reads                                                              # Fast solo read plainly
    assert {ONE_MODE: "r1"} in reads and {ONE_MODE: "r3"} in reads and {ONE_MODE: "r2"} not in reads
    assert not state.layout.enabled and state.layout.reason == "waterfall of one spectrum's fits"
    assert not page.layout_buttons["overlay"].isEnabled()
    assert state.curves_header == "Curves · Fast and inter-spectra"
    page.inter_bar.waterfall_button.click()
    page.flush_redraw()
    assert page.drawn().layout == "single"


def test_waterfall_caps_at_ten_keeping_first_authoritative_and_latest(setup):
    page, inter = setup
    refits = [_refit(f"r{i}", f"ring {i}", authoritative=i == 3) for i in range(1, 17)]
    inter.histories[ONE_MODE] = make_history(*refits)
    _select(page, ONE_MODE)
    page.vm.set_waterfall(True)
    page.flush_redraw()
    state = page._drawn_state
    labels = [t.identity for t in state.traces]
    assert len(labels) == 10 and "Fast solo" in labels and "Batch 1 · ring 3" in labels
    assert "Batch 1 · ring 16" in labels and "Batch 1 · ring 2" not in labels
    assert state.not_drawn_text == "showing 10 of 17 fits"


def test_waterfall_needs_one_spectrum_with_a_refit(setup):
    page, inter = setup
    inter.histories[ONE_MODE] = make_history()
    _select(page, ONE_MODE)
    assert page._drawn_state.inter.waterfall_off.startswith("This spectrum has no inter-spectra fits")
    assert not page.inter_bar.waterfall_button.isEnabled()
    inter.histories[ONE_MODE] = make_history(_refit("r1", "ring 1"))
    inter.history_ready.emit(ONE_MODE)
    page.flush_redraw()
    assert page.inter_bar.waterfall_button.isEnabled()
    _select(page, ONE_MODE, TWO_MODE, active=ONE_MODE)
    assert "exactly one spectrum" in page._drawn_state.inter.waterfall_off


def test_buttons_call_the_writers_once_with_the_viewed_fit(setup):
    page, inter = setup
    inter.histories[ONE_MODE] = make_history(_refit("r1", "ring 1"), _refit("r2", "ring 2", stale=True))
    _select(page, ONE_MODE)
    page.vm.choose_fit("r1")
    page.flush_redraw()
    page.inter_bar.authoritative_button.click()
    assert inter.calls == [("pick_fit", (ONE_MODE, "r1"))]
    inter.overview = make_overview(by_key={ONE_MODE: _row(ONE_MODE, "r1")}, revision="2")    # the reload
    inter.changed.emit()
    page.flush_redraw()
    page.inter_bar.lock_button.click()
    assert inter.calls[1:] == [("set_lock", (ONE_MODE, True, "r1"))]
    inter.overview = make_overview(by_key={ONE_MODE: _row(ONE_MODE, "r1", locked=True, locked_fit_id="r1")}, revision="2")
    inter.changed.emit()
    page.flush_redraw()
    assert page.inter_bar.lock_button.text() == "Unlock"
    page.inter_bar.lock_button.click()
    assert inter.calls[2:] == [("set_lock", (ONE_MODE, False, None))]


def test_make_authoritative_is_greyed_for_stale_and_does_not_write(setup):
    page, inter = setup
    inter.histories[ONE_MODE] = make_history(_refit("r2", "ring 2", stale=True))
    _select(page, ONE_MODE)
    page.vm.choose_fit("r2")
    page.flush_redraw()
    assert not page.inter_bar.authoritative_button.isEnabled()
    assert "stale" in page.inter_bar.authoritative_button.toolTip()
    assert page.vm.make_authoritative() is None and inter.calls == []


def test_a_revision_change_redraws_and_a_refresh_never_writes(setup, reads):
    page, inter = setup
    inter.histories[ONE_MODE] = make_history(_refit("r2", "ring 2", authoritative=True))
    _select(page, ONE_MODE)
    assert reads[-1] is None
    before = len(reads)
    inter.overview = make_overview(by_key={ONE_MODE: _row(ONE_MODE, "r2")}, revision="2")
    inter.changed.emit()
    inter.history_ready.emit(ONE_MODE)
    page.flush_redraw()
    assert len(reads) > before and reads[-1] == {ONE_MODE: "r2"}
    assert not [c for c in inter.calls if c[0] in WRITERS]


def test_bar_is_hidden_without_inter_state_and_loading_history_is_said(qtbot, scans_view_ws_readonly, reads):
    ws = Workspace.open(scans_view_ws_readonly)
    page = ScansPage(ScanListViewModel(ws), ws)
    qtbot.addWidget(page)
    page.show()
    _select(page, ONE_MODE)
    assert page.inter_mount.isHidden()
    inter = FakeInterState()
    page.set_inter(inter)
    page.flush_redraw()
    assert not page.inter_mount.isHidden() and "reading" in page.inter_bar.menu_button.text()


def test_a_fresh_page_draws_the_authoritative_refit_at_the_default_fit(qtbot, scans_view_ws_readonly, reads):
    ws = Workspace.open(scans_view_ws_readonly)
    page = ScansPage(ScanListViewModel(ws), ws)
    qtbot.addWidget(page)
    page.set_store(GuiStore(scans_view_ws_readonly))
    inter = FakeInterState()
    page.set_inter(inter)
    page.resize(700, 600)
    page.show()
    assert page.vm.fit == "none"
    inter.overview = make_overview(by_key={ONE_MODE: _row(ONE_MODE, "r2")})
    inter.histories[ONE_MODE] = make_history(_refit("r2", "ring 2", authoritative=True))
    drawn = _select(page, ONE_MODE, active=ONE_MODE)
    assert page._drawn_state.traces[0].curves
    assert drawn.curves_header == "Curves · Inter-spectra fits"
    text = next(line.text for line in drawn.legend if line.entry == ONE_MODE)
    assert "Batch 1 · ring 2" in text and CONDITIONAL_ERRORS in text
