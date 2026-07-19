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


def test_per_point_entropy_colours_the_scatter_brushes(qtbot):
    # A DATA scatter with a per-point colour channel produces per-point brushes (calm->alarm), so the
    # live view actually renders the encoding the legend note advertises.
    from isar.plotspec.spec import LN2
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    x = np.array([0.0, 1.0, 2.0])
    spec = PlotSpec(
        title="t", xlabel="x", ylabel="y",
        series=[Series(x=x, y=x, yerr=np.full_like(x, 0.1),
                       color_values=np.array([0.0, LN2 / 2, LN2]), color_norm=LN2,
                       role=Role.DATA, state=State.ENTROPY, label="ω̂")],
    )
    rs = render_into(plot, spec)
    scatters = [it for it in rs.items if isinstance(it, pg.ScatterPlotItem)]
    assert len(scatters) == 1
    colors = {tuple(sp.brush().color().getRgb()) for sp in scatters[0].points()}
    assert len(colors) == 3  # three distinct per-point brush colours
    # whiskers still present, drawn neutral (a separate ErrorBarItem)
    assert _count(rs.items, pg.ErrorBarItem) == 1


def _one_data_series_spec(*, pooled: bool) -> PlotSpec:
    """A single whiskered DATA scatter, no entropy — POOLED or plain member. The degenerate case
    where only the glyph distinguishes a pooled master from a per-scan member point."""
    return PlotSpec(
        title="plane", xlabel="q", ylabel="E",
        series=[Series(x=np.array([2.0]), y=np.array([5.0]), yerr=np.array([0.2]),
                       role=Role.DATA, state=(State.POOLED if pooled else None),
                       label=("pooled (3)" if pooled else "member"))],
    )


def _pg_series_symbol(qtbot, spec: PlotSpec) -> str:
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    rs = render_into(plot, spec)
    lines = [it for it in rs.items if isinstance(it, pg.PlotDataItem) and it.opts.get("symbol")]
    assert len(lines) == 1
    return lines[0].opts["symbol"]


def _mpl_series_marker(spec: PlotSpec) -> str:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from isar.plotspec.backends.mpl import _render_onto

    fig, ax = plt.subplots()
    _render_onto(ax, spec)
    markers = [c.lines[0].get_marker() for c in ax.containers if getattr(c, "lines", None)]
    plt.close(fig)
    assert len(markers) == 1
    return markers[0]


def test_pooled_series_square_glyph_parity(qtbot):
    """POOLED masters get a distinct SQUARE glyph while member DATA points stay circles — in BOTH
    backends (closes the degenerate no-entropy indistinguishability)."""
    pooled = _one_data_series_spec(pooled=True)
    member = _one_data_series_spec(pooled=False)

    assert _pg_series_symbol(qtbot, pooled) == "s"
    assert _pg_series_symbol(qtbot, member) == "o"
    assert _mpl_series_marker(pooled) == "s"
    assert _mpl_series_marker(member) == "o"


def test_muted_whiskered_series_is_filled_and_provisional_stays_hollow(qtbot):
    import matplotlib.pyplot as plt
    from isar.plotspec.backends.mpl import _render_onto

    def _spec(style):
        return PlotSpec(
            title="t", xlabel="x", ylabel="y",
            series=[Series(x=np.array([1.0]), y=np.array([2.0]), yerr=np.array([0.1]),
                           role=Role.DATA, style=style)],
        )

    for style, hollow in (("muted", False), ("provisional", True)):
        glw, plot = _new_plot()
        qtbot.addWidget(glw)
        rendered = render_into(plot, _spec(style))
        line = next(item for item in rendered.items
                    if isinstance(item, pg.PlotDataItem) and item.opts.get("symbol"))
        assert (line.opts.get("symbolBrush") is None) is hollow

        fig, ax = plt.subplots()
        try:
            _render_onto(ax, _spec(style))
            marker = next(container.lines[0] for container in ax.containers
                          if getattr(container, "lines", None))
            assert (marker.get_markerfacecolor() == "none") is hollow
        finally:
            plt.close(fig)


def test_pooled_master_draws_above_legacy_same_position_verdict(qtbot):
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = PlotSpec(
        title="t", xlabel="q", ylabel="E",
        series=[Series(x=np.array([2.0]), y=np.array([6.0]), yerr=np.array([0.2]),
                       role=Role.DATA, state=State.POOLED,
                       style="verdict_resolved", label="pooled")],
        markers=[Marker(x=2.0, y=6.0, kind=MarkerKind.VERDICT_RESOLVED,
                        label="legacy verdict")],
    )
    rs = render_into(plot, spec)
    pooled = next(it for it in rs.items
                  if isinstance(it, pg.PlotDataItem) and it.opts.get("symbol") == "s")
    verdict = next(it for it in rs.items if isinstance(it, pg.ScatterPlotItem))
    assert pooled.zValue() > verdict.zValue()


def test_unanchored_corner_notes_are_stacked_not_overlapping(qtbot):
    """Multiple un-anchored corner notes must NOT all anchor at the same bottom-right point — each is
    offset a row higher so they don't overlap (the offsets differ by the per-note step)."""
    glw, plot = _new_plot()
    qtbot.addWidget(glw)
    spec = PlotSpec(
        title="t", xlabel="x", ylabel="y",
        annotations=[Annotation(text="note one", xy=None),
                     Annotation(text="note two", xy=None)],
    )
    rs = render_into(plot, spec)
    labels = [it for it in rs._overlays if isinstance(it, pg.LabelItem)]
    assert len(labels) == 2
    offsets = [tuple(lbl._GraphicsWidgetAnchor__offset) for lbl in labels]
    assert offsets[0] != offsets[1]
    assert offsets == [(-8, -8), (-8, -22)]


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
