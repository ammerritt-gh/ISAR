"""The Dispersion pane's Analyzer path choice and the Analyzer array's Plot path button, offscreen with the real widgets.

A generated BL43 workspace (made-up hkl and counts, hard rule 10); the q provider is replaced, as the path read's own
tests do, so the stops have points. Prevents: the path choice enabled with no path to plot, the path drawn from fits
other than the ones in use, a click on the path plot changing anything, the path's fixed ticks left on the fitted plot
after switching back, and a deliberate Plot path request being pulled back to Scan(s) by the next picks.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from PySide6.QtCore import QPointF

import isar.workspace_api.analyzer_path as apath
from gui.array_helpers import make_env
from gui.inter_fakes import FakeInterState, make_link_view, make_unit_links
from inter_fixtures import build_inter_workspace, spectrum_keys
from isar.plotspec import spec as ps
from isar.session.inter_spectra.q_provider import QCoordinates, QPoint
from isar.workspace_api.inter_spectra import MarkerView
from isar.workspace_api.spectra import spectrum_bundles
from isar_gui.main_window import MainWindow
from isar_gui.selection import ORIGIN_ARRAY
from isar_gui.session import Session
from isar_gui.viewmodels.analyzer_path import PLOT_TIP, AnalyzerPathViewModel
from isar_gui.viewmodels.dispersion import DispersionViewModel
from isar_gui.views.analyzer_array_dock import PATH_BUTTON_TIP
from isar_gui.views.dispersion_pane import DispersionPane

WAIT_MS = 15000


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    return build_inter_workspace(tmp_path_factory.mktemp("disp"), temperatures=(100,), setups=("0p5_0p5_3",),
                                 n_analyzers=4)


@pytest.fixture
def root(base, tmp_path):
    return Path(shutil.copytree(base, tmp_path / "ws"))


@pytest.fixture
def q_points(monkeypatch):
    """Replace the graph's q provider: the analyzers 0.1 apart along a, in the order given."""
    def fake(root, keys):
        points = {k: QPoint(hkl=(0.5, 0.5, 3.0), source="gpi", cart=(0.1 * i, 0.0, 0.0), epoch="lattice epoch v1")
                  for i, k in enumerate(keys)}
        return QCoordinates(points=points, reasons={}, lattices={})

    monkeypatch.setattr(apath, "q_coordinates", fake)


def _pick(selection, keys) -> None:
    selection.set(keys, clicked=keys[-1], origin=ORIGIN_ARRAY, picked=keys)


def _bottom_ticks(pane) -> list[str]:
    """The text of the bottom axis' fixed ticks; [] while the axis is on its automatic ticks (pyqtgraph keeps them in
    ``_tickLevels`` and has no public getter)."""
    levels = pane._plot.getAxis("bottom")._tickLevels
    return [] if levels is None else [text for _, text in levels[0]]


@pytest.fixture
def env(qtbot, root, q_points):
    inter = FakeInterState()
    env = make_env(root, inter=inter, active=True)
    env.keys = spectrum_keys(root)[:3]          # a03, a04, a05: one row, click order = this order
    env.pane = DispersionPane(DispersionViewModel(env.ws))
    qtbot.addWidget(env.pane)
    env.path_vm = AnalyzerPathViewModel(env.ws, env.scan_list)
    env.path_vm.set_inter(inter)
    env.pane.set_path_source(env.path_vm)
    env.inter = inter
    return env


def _energies(env, key) -> list[float]:
    row = env.scan_list.snapshot.spectrum(key)
    return [m.energy.value for m in spectrum_bundles(env.ws, [row])[0].summaries["fast"].modes]


def test_the_path_choice_says_why_it_is_off_until_two_analyzers_are_picked(env):
    pane, keys = env.pane, env.keys
    _pick(env.selection, keys[:1])
    assert not pane._path_button.isEnabled()
    assert "1 picked so far" in pane._path_button.toolTip()
    pane.show_path()                                       # asked for while it cannot be plotted: nothing changes
    assert not pane._path_mode and pane._fit_button.isChecked()
    _pick(env.selection, keys[:2])
    assert pane._path_button.isEnabled() and pane._path_button.toolTip() == PLOT_TIP


def test_the_path_is_drawn_and_switching_back_restores_the_fitted_plot(env):
    pane, keys = env.pane, env.keys
    units = []
    for i, key in enumerate(keys):
        energies = _energies(env, key)
        names = ("B1",) + (None,) * (len(energies) - 1)
        markers = ((MarkerView("change", 2, 0, "energy", "energy differs from neighbours",
                               "Mode 1 energy differs from its neighbours by more than its tolerance", "warn"),)
                   if i == 1 else ())
        units.append(make_unit_links(key, names=names, peak_E=tuple(energies), markers=markers))
    env.inter.links = make_link_view(*units)
    _pick(env.selection, keys)
    fitted = pane._vm.spec
    assert fitted is not None, "the BL43 fixture should have a fitted dispersion to switch back to"

    pane._path_button.click()
    assert pane._path_mode and pane._path_button.isChecked()
    assert pane._t_row.isHidden() and pane._status_scroll.isHidden() and not pane._path_caption.isHidden()
    spec = env.path_vm.state.spec
    assert [t for _, t in spec.x_ticks] == [key.rsplit(":", 1)[1] for key in keys]
    assert "B1" in [s.label for s in spec.series]
    assert [m.kind for m in spec.markers] == [ps.MarkerKind.LINK_CHANGE]
    assert spec.markers[0].tooltip.startswith("Mode 1 energy differs")
    assert spec.markers[0].y == pytest.approx(units[1].peak_E[0])          # at E*, not at omega0
    assert _bottom_ticks(pane) == [key.rsplit(":", 1)[1] for key in keys]

    clicks = []
    pane._vm.activate_at = lambda x, y: clicks.append((x, y))
    pane._on_click(SimpleNamespace(scenePos=lambda: QPointF(0.5, 1.0)))   # on the path plot: activates nothing
    assert pane._path_mode and clicks == []

    pane._fit_button.click()
    assert not pane._path_mode and pane._fit_button.isChecked()
    assert pane._path_caption.isHidden()
    assert pane._t_row.isHidden() == (len(pane._vm.temperatures) <= 1)   # the T selector returns when it is needed
    assert _bottom_ticks(pane) == []                      # the path's ticks are gone with the path
    assert pane._plot.titleLabel.text == fitted.title     # and the fitted plot is drawn again, as it was


def test_a_path_that_can_no_longer_be_plotted_takes_its_ticks_off_the_plot(env):
    pane, keys = env.pane, env.keys
    _pick(env.selection, keys)
    pane._path_button.click()
    assert _bottom_ticks(pane) == [key.rsplit(":", 1)[1] for key in keys]
    _pick(env.selection, keys[:1])                        # one analyzer: the path is invalidated, not plotted
    assert env.path_vm.state.spec is None and pane._path_mode
    assert _bottom_ticks(pane) == []                      # the previous path's ticks do not stay on the empty plot


def test_the_fitted_plot_with_no_spec_takes_the_path_ticks_off_too(env, monkeypatch):
    pane, keys = env.pane, env.keys
    _pick(env.selection, keys)
    pane._path_button.click()
    assert _bottom_ticks(pane) != []
    monkeypatch.setattr(type(pane._vm), "spec", property(lambda self: None))   # no fitted dispersion to draw
    pane._fit_button.click()
    assert not pane._path_mode and _bottom_ticks(pane) == []


def test_the_array_dock_button_plots_the_path_and_the_next_picks_do_not_pull_the_view_back(qtbot, root, q_points):
    w = MainWindow(None, session=Session(), watch=False)
    qtbot.addWidget(w)
    w.show()
    with qtbot.waitSignal(w.intake.worker_changed, timeout=WAIT_MS):
        assert w.load_workspace(str(root))
    dock, selection = w.array_dock, w.scan_list_vm.selection
    keys = spectrum_keys(root)[:3]
    _pick(selection, keys[:1])
    assert not dock.path_button.isEnabled() and "1 picked so far" in dock.path_button.toolTip()
    _pick(selection, keys)
    assert dock.path_button.isEnabled() and PATH_BUTTON_TIP in dock.path_button.toolTip()

    dock.path_button.click()
    host = w._center_host
    assert host.current_key == "dispersion" and not host.auto_follow
    pane = host.pane("dispersion")
    assert pane._path_mode and pane._path_button.isChecked()

    _pick(selection, keys[:2])                            # an array gesture: with auto-follow off the view stays put
    assert host.current_key == "dispersion" and pane._path_mode
