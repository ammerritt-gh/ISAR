"""Real-widget Scan(s) behavior, using only the shared synthetic workspace."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import Qt

from isar.workspace_api import Workspace
from isar.workspace_api.spectra import spectrum_bundles
from isar_gui.gui_store import GuiStore
from isar_gui.session import Session
from isar_gui.viewmodels.scan_list import ScanListViewModel
from isar_gui.views.scans_page import ScansPage, EMPTY, PLOT, LIST
from scans_view_fixture import (ONE_MODE, TWO_MODE, NO_Q, NO_ZERO, NO_MONITOR, OUTDATED,
    DEEP_TRUSTED, DEEP_OUTDATED, SPECTRA_A, SPECTRA_B, STOKES_ONLY, FITTED)


@pytest.fixture
def page(qtbot, scans_view_ws_readonly):
    ws = Workspace.open(scans_view_ws_readonly)
    vm = ScanListViewModel(ws)
    widget = ScansPage(vm, ws)
    qtbot.addWidget(widget)
    widget.set_store(GuiStore(scans_view_ws_readonly))
    widget.resize(600, 600)
    widget.show()
    widget.flush_redraw()
    return widget


def select(page, *keys):
    page.selection.set(keys)
    page.flush_redraw()
    return page.drawn()


def test_single_fit_uses_scaled_bundle_and_freezes_reference(page, qtbot):
    emissions = []
    page.store_dirty.connect(lambda: emissions.append(True))
    page.vm.set_fit('fast')
    result = select(page, ONE_MODE)
    assert result.layout == 'single'
    assert page._resid_plot.isVisible()
    assert {line.entry for line in result.legend} == {ONE_MODE, 'Total', 'Mode 1', 'Elastic'}
    assert result.legend[0].text == 'a03 · 97 K'
    assert result.ylabel == 'Counts per 10⁵ monitor'
    reference = page.store.normalisation_reference()
    row = page.scan_list_vm.snapshot.spectrum(ONE_MODE)
    bundle = spectrum_bundles(page.ws, [row], cache=page.scan_list_vm.spectra_cache)[0]
    scaled = bundle.scaled('normalised', reference)
    series = page._drawn_state.main_spec.series[0]
    np.testing.assert_array_equal(series.y, scaled.y[scaled.kept])
    np.testing.assert_array_equal(page._drawn_state.residual_spec.series[0].y,
                                  scaled.passes['fast'].residuals[scaled.kept])
    select(page, TWO_MODE)
    qtbot.mouseClick(page.scale_buttons['raw'], Qt.MouseButton.LeftButton)
    page.flush_redraw()
    qtbot.mouseClick(page.scale_buttons['normalised'], Qt.MouseButton.LeftButton)
    page.flush_redraw()
    assert page.store.normalisation_reference() == reference
    assert len(emissions) == 1
    from PySide6.QtCore import QCoreApplication
    QCoreApplication.processEvents()
    single_height = page._main_plot.getViewBox().height()
    select(page, ONE_MODE, TWO_MODE)
    QCoreApplication.processEvents()
    assert page._main_plot.getViewBox().height() > single_height + 50
    select(page, ONE_MODE)
    QCoreApplication.processEvents()
    assert page._resid_plot.isVisible()


def test_deep_missing_outdated_and_trust_are_visible(page):
    page.vm.set_fit('deep')
    drawn = select(page, ONE_MODE, NO_Q)
    assert next(l.text for l in drawn.legend if l.entry == NO_Q).endswith('no deep pass')
    assert not any(c.key == NO_Q for c in drawn.curves)
    assert drawn.curves_header == 'Curves · Deep (untrusted)'
    assert 'Untrusted:' in page.fit_buttons['deep'].toolTip()
    assert all(c.style == ('dotted' if c.role == 'background' else 'dashed') for c in drawn.curves)
    drawn = select(page, DEEP_TRUSTED, DEEP_OUTDATED)
    assert drawn.curves_header == 'Curves · Deep'
    assert 'currentness not recorded' in next(l.text for l in drawn.legend if l.entry == DEEP_TRUSTED)
    assert next(l.text for l in drawn.legend if l.entry == DEEP_OUTDATED) == 'a07 · 97 K · stale'
    page.vm.set_fit('fast')
    drawn = select(page, OUTDATED)
    assert drawn.legend[0].text == 'a03 · 97 K · stale'
    assert drawn.curves


def test_overlay_remembers_fit_and_single_overrides_layout(page, qtbot):
    page.vm.set_fit('fast')
    select(page, ONE_MODE, TWO_MODE)
    qtbot.mouseClick(page.layout_buttons['overlay'], Qt.MouseButton.LeftButton)
    page.flush_redraw()
    assert page.drawn().layout == 'overlay'
    assert not page.drawn().curves
    assert not page.fit_buttons['fast'].isEnabled()
    assert 'Overlay compares raw data' in page.hint.full_text()
    assert len({s.color for s in page._drawn_state.main_spec.series}) == 2
    assert len({s.symbol for s in page._drawn_state.main_spec.series}) == 2
    one = select(page, ONE_MODE)
    assert one.layout == 'single' and one.curves
    assert not page.layout_buttons['offset'].isEnabled()
    assert 'one spectrum' in page.hint.full_text()
    assert select(page, ONE_MODE, TWO_MODE).layout == 'overlay'
    page._layout_actions['offset'].trigger()
    page.flush_redraw()
    assert page.drawn().curves and page.vm.fit == 'fast'


def test_over_limit_reads_nothing_and_returns_to_plot(page, monkeypatch):
    from isar_gui.viewmodels import scans_page as module
    def forbidden(*args, **kwargs):
        pytest.fail('over-limit selection tried to read a spectrum bundle')
    with monkeypatch.context() as patch:
        patch.setattr(module, 'spectrum_bundles', forbidden)
        drawn = select(page, *SPECTRA_A, *SPECTRA_B[:4])
    assert drawn.body == LIST
    assert not drawn.keys and not drawn.curves and not drawn.legend
    assert not drawn.not_drawn_line
    assert page.limit_tree.topLevelItemCount() == 2
    items = [page.limit_tree.topLevelItem(i) for i in range(page.limit_tree.topLevelItemCount())]
    set_a = next(item for item in items if '7 analyzers (a03–a09)' in item.text(0))
    assert set_a.childCount() == 7
    assert page.limit_tree.selectionMode() == page.limit_tree.SelectionMode.NoSelection
    assert select(page, *SPECTRA_A, *SPECTRA_B[:3]).body == PLOT
    assert select(page).body == EMPTY


def test_beamline_zero_is_tagged_and_shares_the_axis(page):
    drawn = select(page, NO_ZERO, ONE_MODE)
    assert set(drawn.keys) == {NO_ZERO, ONE_MODE} and not drawn.not_drawn
    tagged = [t.identity for t in page._drawn_state.traces if t.identity.endswith('(beamline zero)')]
    assert len(tagged) == 1
    assert page._drawn_state.main_spec.xlabel == 'Energy transfer (meV)'


def test_unzeroed_scan_waits_while_zeroed_scans_are_drawn(page, monkeypatch):
    from dataclasses import replace
    from isar.workspace_api.spectra import ZERO_NONE
    from isar_gui.viewmodels import scans_page as module
    original = module.spectrum_bundles

    def raw_axis(*args, **kwargs):          # NO_ZERO as a needs_zero instrument with no fitted zero yet
        return tuple(replace(b, zero=ZERO_NONE) if b.key == NO_ZERO else b for b in original(*args, **kwargs))

    monkeypatch.setattr(module, 'spectrum_bundles', raw_axis)
    drawn = select(page, NO_ZERO, ONE_MODE)
    assert drawn.keys == (ONE_MODE,)
    assert dict(drawn.not_drawn) == {NO_ZERO: 'waiting for the fast pass to set the energy zero'}
    assert drawn.not_drawn_line == '1 of 2 selected not drawn: waiting for the fast pass to set the energy zero (1)'
    drawn = select(page, NO_ZERO)
    assert drawn.keys == (NO_ZERO,)
    assert page._drawn_state.main_spec.xlabel == 'Energy, not zeroed (meV)'


def test_energy_zero_guide_only_on_a_zeroed_axis(page, monkeypatch):
    from dataclasses import replace
    from isar.workspace_api.spectra import ZERO_NONE
    from isar_gui.viewmodels import scans_page as module
    original = module.spectrum_bundles

    def raw_axis(*args, **kwargs):
        return tuple(replace(b, zero=ZERO_NONE) if b.key == NO_ZERO else b for b in original(*args, **kwargs))

    monkeypatch.setattr(module, 'spectrum_bundles', raw_axis)
    page.vm.set_display_option('energy_zero_line', True)
    select(page, NO_ZERO)
    assert page._drawn_state.main_spec.xlabel == 'Energy, not zeroed (meV)'
    assert not [g for g in page._drawn_state.main_spec.guides if g.axis == 'x']   # file-axis 0 is no zero
    select(page, ONE_MODE)
    assert [(g.axis, g.value) for g in page._drawn_state.main_spec.guides if g.axis == 'x'] == [('x', 0.0)]


def test_missing_data_counts_and_offset_controls(page, qtbot):
    drawn = select(page, NO_ZERO, NO_MONITOR, ONE_MODE)
    assert set(drawn.keys) == {NO_ZERO, ONE_MODE}
    assert '1 of 3 selected not drawn:' in drawn.not_drawn_line
    assert {reason for key, reason in drawn.not_drawn} == {'no monitor recorded'}
    page.vm.set_scale('raw')
    page.flush_redraw()
    assert page.drawn().not_drawn_line == ''
    page.vm.set_scale('normalised')
    drawn = select(page, NO_MONITOR)
    assert not drawn.keys
    assert 'Nothing drawn' in page._drawn_state.main_spec.annotations[0].text
    select(page, ONE_MODE, TWO_MODE, NO_Q)
    page.offset_box.setValue(50)
    page.flush_redraw()
    assert not page._drawn_state.step_auto
    assert page.drawn().ylabel.endswith(', traces offset')
    np.testing.assert_allclose(page._drawn_state.main_spec.series[0].y,
                               page._drawn_state.traces[0].y[page._drawn_state.traces[0].kept] + 100)
    qtbot.mouseClick(page.auto_button, Qt.MouseButton.LeftButton)
    page.flush_redraw()
    assert page._drawn_state.step_auto
    page.vm.set_step(50)
    page.vm.set_scale('raw')
    page.vm.set_scale('normalised')
    page.flush_redraw()
    assert page._drawn_state.step_auto


def test_active_and_live_updates_keep_zoom_and_reuse_bundles(page, monkeypatch):
    from isar_gui.viewmodels import scans_page as module
    page.vm.set_fit('fast')
    select(page, ONE_MODE, TWO_MODE)
    page._main_plot.setRange(xRange=(1, 5), yRange=(0, 800), padding=0)
    zoom = page.drawn().view_range
    def forbidden(*args, **kwargs):
        pytest.fail('active-only change reread spectrum bundles')
    with monkeypatch.context() as patch:
        patch.setattr(module, 'spectrum_bundles', forbidden)
        page.selection.step(1)
        page.flush_redraw()
    assert page.last_redraw.read_s == 0
    assert page.drawn().view_range == zoom
    assert next(l.entry for l in page.drawn().legend if l.bold) == TWO_MODE
    before = page.last_redraw
    page.scan_list_vm.on_workspace_changed()
    page.flush_redraw()
    assert page.last_redraw is before
    original = module.spectrum_bundles
    def changed(*args, **kwargs):
        bundles = original(*args, **kwargs)
        return tuple(replace(b, revision=b.revision + 'new') for b in bundles)
    monkeypatch.setattr(module, 'spectrum_bundles', changed)
    page.scan_list_vm.on_workspace_changed()
    page.flush_redraw()
    assert page.last_redraw is not before
    assert page.drawn().view_range == zoom


def test_view_state_and_control_widths(page):
    session = Session()
    page.vm.set_fit('deep')
    page.vm.set_layout('overlay')
    page.vm.set_scale('raw')
    page.save_view_state(session)
    assert session.view_state['scans_page'] == {'fit': 'deep', 'layout': 'overlay', 'scale': 'raw',
        'zero_lines': True, 'energy_zero_line': False, 'peak_lines': True, 'filled_symbols': False,
        'out_of_range': True, 'offset_ratio': 1.1, 'offset_absolute': None}
    session.view_state['scans_page'] = {'fit': 'x', 'layout': 3}
    page.load_view_state(session)
    page.flush_redraw()
    assert (page.vm.fit, page.vm.layout, page.vm.scale) == ('fast', 'offset', 'normalised')
    assert page.minimumSizeHint().width() <= 300
    assert page.fit_mount.minimumSizeHint().width() <= 150
    assert page.layout_mount.minimumSizeHint().width() + page.scale_mount.minimumSizeHint().width() + 6 <= 300


def test_redraw_timing_report(page):
    page.vm.set_fit('fast')
    select(page, *FITTED[:10])
    cold = page.last_redraw
    page.vm.set_fit('deep')
    page.flush_redraw()
    warm = page.last_redraw
    print(f'cold: {cold}\nwarm: {warm}')
    assert cold.n == warm.n == 10
    assert all(value >= 0 for t in (cold, warm) for value in (t.read_s, t.build_s, t.render_s, t.total_s))


def test_the_central_qe_is_drawn_and_named_qe(page):
    page.vm.set_fit('fast')
    drawn = select(page, STOKES_ONLY)
    assert 'QE' in {line.entry for line in drawn.legend}
    assert [c.role for c in drawn.curves if c.key == STOKES_ONLY and c.name == 'QE'] == ['quasielastic']
