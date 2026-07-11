"""pyqtgraph backend (isar_gui.plotspec_pg) — semantic-parity + lifecycle tests.

Offscreen (conftest forces QT_QPA_PLATFORM=offscreen), importorskip so the suite skips cleanly where
pyqtgraph/PySide6 are absent, and xdist-safe (no shared global plot). The parity pin compares the
legend *entry set* against the matplotlib backend for the same spec — the mpl import is confined to
this test (tests are exempt from the isar_gui no-matplotlib import contract).
"""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

import pyqtgraph as pg

from isar.plotspec.spec import (
    Annotation,
    Band,
    Marker,
    MarkerKind,
    PlotSpec,
    ProjectionLegend,
    Role,
    Series,
    State,
    Strip,
    Tier,
    mark_unreliable,
)
from isar_gui.plotspec_pg import RenderedSpec, render_into


def _representative_spec() -> PlotSpec:
    x = np.linspace(0.0, 4.0, 12)
    spec = PlotSpec(
        title="fit inspector",
        xlabel="E (meV)",
        ylabel="intensity",
        series=[
            Series(x=x, y=np.sin(x) + 2, yerr=np.full_like(x, 0.1), label="data", role=Role.DATA),
            Series(x=x, y=np.cos(x) + 2, yerr=np.full_like(x, 0.15), label="model", role=Role.FIT),
        ],
        bands=[
            Band(x=x, lo=np.zeros_like(x), hi=np.ones_like(x), tier=Tier.HESSE, certifying=True),
            Band(x=x, lo=np.zeros_like(x), hi=np.full_like(x, 0.5), tier=Tier.BOOTSTRAP),
        ],
        markers=[
            Marker(x=1.0, y=2.5, kind=MarkerKind.VERDICT_RESOLVED, label="resolved"),
            Marker(x=2.0, y=2.5, kind=MarkerKind.VERDICT_FLAGGED, label="flagged"),
        ],
        annotations=[Annotation(text="12 pts excluded", xy=None)],
        projection=ProjectionLegend(plane="(H, 1, 0) x E", integration="|L| < 0.02, |K-1| < 0.02"),
    )
    # The only sanctioned unreliable rendering: a warning marker + "(unreliable)" annotation.
    mark_unreliable(spec, x=3.0, y=2.5)
    return spec


def _mpl_legend_names(spec: PlotSpec) -> set[str]:
    """The mpl backend's legend label set, via its real render path (matplotlib import is test-only,
    outside the isar_gui production import contract)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from isar.plotspec.backends.mpl import _render_onto

    fig, ax = plt.subplots()
    _render_onto(ax, spec)
    _, labels = ax.get_legend_handles_labels()
    plt.close(fig)
    return set(labels)


def _new_plot():
    glw = pg.GraphicsLayoutWidget()
    return glw, glw.addPlot()


def _count(items, cls) -> int:
    return sum(1 for it in items if isinstance(it, cls))


def test_render_item_counts(qtbot):
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = _representative_spec()
    rs = render_into(plot, spec)

    # 2 series w/ yerr -> 2 ErrorBarItem + 2 line PlotDataItem.
    assert _count(rs.items, pg.ErrorBarItem) == 2
    # 2 tier bands -> 2 FillBetweenItem.
    assert _count(rs.items, pg.FillBetweenItem) == 2
    # 2 verdict + 1 warning (unreliable) = 3 marker scatters.
    assert _count(rs.items, pg.ScatterPlotItem) == 3
    # PlotDataItem: 2 series lines (band boundary curves are NOT added to the plot).
    assert _count(rs.items, pg.PlotDataItem) == 2


def test_legend_parity_with_mpl(qtbot):
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = _representative_spec()
    rs = render_into(plot, spec)
    assert rs.legend_names == _mpl_legend_names(spec)


def test_projection_label_anchored_to_viewbox(qtbot):
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = _representative_spec()
    rs = render_into(plot, spec)
    assert rs.projection_label is not None
    # ViewBox-anchored (an overlay), NOT a data-coordinate item that autorange could occlude.
    assert rs.projection_label.parentItem() is plot.getViewBox()


def test_idempotent_rerender_no_leak(qtbot):
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = _representative_spec()

    base = len(plot.items)
    rs1 = render_into(plot, spec)
    first_items = list(rs1.items)
    n_after_first = len(plot.items)
    assert n_after_first > base

    rs2 = render_into(plot, spec)
    # Item count on the plot is stable across re-render (previous set replaced, not accumulated).
    assert len(plot.items) == n_after_first
    assert rs2 is not rs1
    # Every item from the first render is detached from the scene.
    for it in first_items:
        assert it.scene() is None


def test_remove_detaches_everything(qtbot):
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = _representative_spec()

    base = len(plot.items)
    rs = render_into(plot, spec)
    assert len(plot.items) > base
    rs.remove()
    assert len(plot.items) == base
    assert rs.legend is None
    assert rs.projection_label is None


def test_strips_render_as_separate_imageitems_not_spanning_gap(qtbot):
    # Two measured columns at H=2.0 and H=2.4 (gap between); each is its own ImageItem placed over its
    # own E extent, and no item's rect spans the gap (gaps stay gaps; no interpolation across them).
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = PlotSpec(
        title="plane", xlabel="H (rlu)", ylabel="E (meV)",
        strips=[Strip(x_center=2.0, width=0.08, y=np.linspace(-2, 4, 6), values=np.arange(6.0)),
                Strip(x_center=2.4, width=0.08, y=np.linspace(-2, 4, 6), values=np.arange(6.0) + 10)],
        value_label="counts/monitor",
    )
    rs = render_into(plot, spec)
    images = [it for it in rs.items if isinstance(it, pg.ImageItem)]
    assert len(images) == 2
    for im in images:
        r = im.mapRectToView(im.boundingRect())
        assert not (r.left() < 2.2 < r.right()), "an ImageItem spans the gap between strips"


def test_strips_removed_on_rerender(qtbot):
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = PlotSpec(title="p", xlabel="x", ylabel="y",
                    strips=[Strip(x_center=0.0, width=0.1, y=np.array([0.0, 1.0]),
                                  values=np.array([1.0, 2.0]))])
    base = len(plot.items)
    rs = render_into(plot, spec)
    assert _count(rs.items, pg.ImageItem) == 1
    rs.remove()
    assert len(plot.items) == base


def test_unreliable_marker_has_no_errorbar(qtbot):
    """An UNRELIABLE point is a warning marker with NO whiskers; the backend must emit no
    ErrorBarItem for a spec whose only content is the unreliable marker + annotation."""
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = PlotSpec(title="t", xlabel="x", ylabel="y")
    mark_unreliable(spec, x=1.0, y=2.0)
    rs = render_into(plot, spec)
    assert _count(rs.items, pg.ErrorBarItem) == 0
    assert _count(rs.items, pg.ScatterPlotItem) == 1
    assert isinstance(rs, RenderedSpec)
