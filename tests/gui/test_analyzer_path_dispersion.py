"""The Dispersion view of an analyzer-array experiment is its Analyzer path, offscreen with the real widgets, and the
Analyzer array's Plot path button brings it up.

A generated BL43 workspace (made-up hkl and counts, hard rule 10); the q provider is replaced, as the path read's own
tests do, so the stops have points. Prevents: an empty path plot that does not say how to make a path, the path drawn
from fits other than the ones in use, a phonon drawn anywhere but its peak energy, a click on the path plot changing
anything, and a deliberate Plot path request being pulled back to Scan(s) by the next picks.
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
from gui.array_helpers import inter_row, make_env
from gui.inter_fakes import FakeInterState, make_link_view, make_overview, make_unit_links
from inter_fixtures import build_inter_workspace, spectrum_keys
from isar.plotspec import spec as ps
from isar.session.inter_spectra.q_provider import QCoordinates, QPoint
from isar.workspace_api.spectra import spectrum_bundles
from isar_gui.main_window import MainWindow
from isar_gui.selection import ORIGIN_ARRAY
from isar_gui.session import Session
from isar_gui.viewmodels import analyzer_array
from isar_gui.viewmodels.analyzer_path import AnalyzerPathViewModel
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
    env.pane.show()                             # the path's heavy spec is read while the view is shown
    env.inter = inter
    return env


def _energies(env, key) -> list[float]:
    row = env.scan_list.snapshot.spectrum(key)
    return [m.energy.value for m in spectrum_bundles(env.ws, [row])[0].summaries["fast"].modes]


def test_one_pick_says_how_to_make_a_path_and_two_picks_draw_it(env):
    pane, keys = env.pane, env.keys
    _pick(env.selection, keys[:1])
    spec = env.path_vm.state.spec
    assert spec is not None and not spec.series and not spec.annotations
    assert spec.title == "No path yet: 1 picked so far"
    assert "Ctrl-click analyzers" in spec.xlabel and "shift-click two" in spec.xlabel
    assert not any(pane._plot.getAxis(side).style["showValues"] for side in ("bottom", "left"))   # no fake 0-1 range
    _pick(env.selection, keys[:2])
    assert env.path_vm.state.can_plot and env.path_vm.state.spec.series
    assert all(pane._plot.getAxis(side).style["showValues"] for side in ("bottom", "left"))
    assert _bottom_ticks(pane) == [key.rsplit(":", 1)[1] for key in keys[:2]]


def test_the_analyzer_array_experiment_shows_only_its_path(env):
    pane = env.pane
    _pick(env.selection, env.keys)
    assert pane._t_row.isHidden() and pane._status_scroll.isHidden()
    assert pane._plot.titleLabel.text.startswith("Analyzer path")


def test_a_named_phonon_is_one_series_at_its_peak_energy_and_a_click_on_the_path_activates_nothing(env):
    pane, keys = env.pane, env.keys
    units = [make_unit_links(k, names=("B1",), peak_E=tuple(e + 0.5 for e in _energies(env, k))) for k in keys]
    env.inter.links = make_link_view(*units)
    _pick(env.selection, keys)
    (p1,) = [s for s in env.path_vm.state.spec.series if s.label == "P1"]
    assert list(p1.y) == pytest.approx([e + 0.5 for k in keys for e in _energies(env, k)])   # at E*, not at omega0
    assert not any(p1.errors.bottom) and not any(p1.errors.top)
    assert [m for m in env.path_vm.state.spec.markers] == []

    clicks = []
    pane._vm.activate_at = lambda x, y: clicks.append((x, y))
    pane._on_click(SimpleNamespace(scenePos=lambda: QPointF(0.5, 1.0)))
    assert clicks == []
    assert ps.Role.BRANCH in {s.role for s in env.path_vm.state.spec.series}


def test_the_path_and_the_array_are_handed_the_same_fit_choice(env, monkeypatch):
    """The one fit-in-use rule: a refit picked on one analyzer reaches both view models as the same choice."""
    k0, k1 = env.keys[:2]
    refit = "f00000000a001"
    env.inter.overview = make_overview(by_key={k0: inter_row(k0, refit), k1: inter_row(k1, "fast")})
    calls = {"path": [], "array": []}

    def recording(name, real):
        def wrapped(ws, rows, *, cache=None, fit_choice=None):
            calls[name].append(dict(fit_choice or {}))
            return real(ws, rows, cache=cache, fit_choice=fit_choice)
        return wrapped

    monkeypatch.setattr(apath, "spectrum_bundles", recording("path", apath.spectrum_bundles))
    monkeypatch.setattr(analyzer_array, "spectrum_bundles", recording("array", analyzer_array.spectrum_bundles))
    _pick(env.selection, env.keys[:2])
    env.vm.recompute()
    assert env.path_vm.state.can_plot
    assert calls["path"][-1] == calls["array"][-1] == {k0: refit}


def test_a_path_that_drops_below_two_picks_takes_its_ticks_off_the_plot(env):
    pane, keys = env.pane, env.keys
    _pick(env.selection, keys)
    assert _bottom_ticks(pane) == [key.rsplit(":", 1)[1] for key in keys]
    _pick(env.selection, keys[:1])                        # one analyzer: the path is invalidated, not plotted
    assert _bottom_ticks(pane) == []                      # the previous path's ticks do not stay on the empty plot


def test_the_array_dock_button_brings_up_the_path_and_the_next_picks_do_not_pull_the_view_back(qtbot, root, q_points):
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
    assert pane._path_vm is not None and pane._path_vm.state.spec is not None

    _pick(selection, keys[:2])                            # an array gesture: with auto-follow off the view stays put
    assert host.current_key == "dispersion" and pane._path_vm.state.can_plot
