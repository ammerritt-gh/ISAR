"""The matplotlib backend renders valid PNGs for representative specs (we assert PNG magic bytes and
non-emptiness, not pixels — pin the spec, not the render)."""
import matplotlib
import pytest

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

from isar.plotspec import spec as ps  # noqa: E402
from isar.plotspec.backends import mpl  # noqa: E402

_PNG = b"\x89PNG\r\n\x1a\n"


def _rich_spec():
    x = np.linspace(0.0, 10.0, 20)
    return ps.PlotSpec(
        title="demo", xlabel="energy (meV)", ylabel="counts",
        series=[
            ps.Series(x=x, y=np.sin(x) + 2, yerr=np.full_like(x, 0.3),
                      label="data", role=ps.Role.DATA),
            ps.Series(x=x, y=np.sin(x) + 2, label="fit", role=ps.Role.FIT),
        ],
        bands=[ps.Band(x=x, lo=np.sin(x) + 1.5, hi=np.sin(x) + 2.5,
                       role=ps.Role.FIT, tier=ps.Tier.BOOTSTRAP, certifying=True)],
        markers=[ps.Marker(x=2.0, y=2.5, kind=ps.MarkerKind.MASKED),
                 ps.Marker(x=4.0, y=1.5, kind=ps.MarkerKind.WARNING)],
        annotations=[ps.Annotation(text="(unreliable)", xy=(4.0, 1.5), role=ps.State.UNRELIABLE)],
        projection=ps.ProjectionLegend(plane="E", integration="Q = [1, 0, 0]"),
    )


def test_render_png_returns_a_valid_png():
    png = mpl.render_png(_rich_spec())
    assert png.startswith(_PNG) and len(png) > 1000


def test_marker_labels_reach_the_legend_once_per_group():
    # Two masked + two cosmic markers sharing a label each; the legend must show each label exactly
    # once (not once per point) alongside the data/fit series.
    x = np.linspace(0.0, 5.0, 6)
    spec = ps.PlotSpec(
        title="t", xlabel="x", ylabel="y",
        series=[ps.Series(x=x, y=x, label="kept", role=ps.Role.DATA)],
        markers=[
            ps.Marker(x=1.0, y=1.0, kind=ps.MarkerKind.MASKED, label="masked"),
            ps.Marker(x=2.0, y=2.0, kind=ps.MarkerKind.MASKED, label="masked"),
            ps.Marker(x=3.0, y=3.0, kind=ps.MarkerKind.COSMIC_RAY, label="cosmic"),
            ps.Marker(x=4.0, y=4.0, kind=ps.MarkerKind.COSMIC_RAY, label="cosmic"),
        ],
    )
    fig, ax = plt.subplots()
    try:
        mpl._render_onto(ax, spec)
        labels = [t.get_text() for t in ax.get_legend().get_texts()]
    finally:
        plt.close(fig)
    assert labels.count("masked") == 1
    assert labels.count("cosmic") == 1
    assert "kept" in labels


def test_masked_point_is_a_grey_x():
    from matplotlib.markers import MarkerStyle
    from isar.plotspec.backends.mpl import _draw_marker
    fig, ax = plt.subplots()
    try:
        _draw_marker(ax, ps.Marker(x=1.0, y=2.0, kind=ps.MarkerKind.MASKED))
        coll = ax.collections[-1]
        assert np.array_equal(coll.get_paths()[0].codes, MarkerStyle("x").get_path().codes)
        assert np.allclose(coll.get_edgecolor()[0][:3], 0.5)
    finally:
        plt.close(fig)


def test_strips_render_as_separate_rectangles_with_a_gap():
    # Two measured columns at H=2.0 and H=2.4 (a gap between 2.04 and 2.36); each is its own
    # AxesImage over its own E extent, and NO artist spans the gap (gaps stay gaps, no interpolation).
    from matplotlib.image import AxesImage
    spec = ps.PlotSpec(
        title="plane", xlabel="H (rlu)", ylabel="E (meV)",
        strips=[ps.Strip(x_center=2.0, width=0.08, y=np.linspace(-2, 4, 6),
                         values=np.arange(6.0)),
                ps.Strip(x_center=2.4, width=0.08, y=np.linspace(-2, 4, 6),
                         values=np.arange(6.0) + 10.0)],
        value_label="counts/monitor",
    )
    fig, ax = plt.subplots()
    try:
        mpl._render_onto(ax, spec)
        images = [a for a in ax.get_images() if isinstance(a, AxesImage)]
        assert len(images) == 2  # one rectangle per strip, not a single interpolated field
        # No image covers the gap x-region (2.04 .. 2.36).
        for im in images:
            x0, x1, _y0, _y1 = im.get_extent()
            assert not (x0 < 2.2 < x1), "an artist spans the gap between strips"
    finally:
        plt.close(fig)


def test_strips_share_one_colour_scale():
    # Shared (vmin, vmax) across both strips' finite values.
    spec = ps.PlotSpec(
        title="t", xlabel="x", ylabel="y",
        strips=[ps.Strip(x_center=0.0, width=0.1, y=np.array([0.0, 1.0]), values=np.array([1.0, 2.0])),
                ps.Strip(x_center=1.0, width=0.1, y=np.array([0.0, 1.0]), values=np.array([3.0, 9.0]))],
    )
    fig, ax = plt.subplots()
    try:
        mpl._render_onto(ax, spec)
        clims = {im.get_clim() for im in ax.get_images()}
        assert clims == {(1.0, 9.0)}
    finally:
        plt.close(fig)


def test_per_point_entropy_colours_the_scatter():
    # A DATA scatter with a per-point colour channel renders each point a different colour
    # (calm->alarm), so the legend's "color = assignment entropy" note is not a lie.
    x = np.array([0.0, 1.0, 2.0])
    spec = ps.PlotSpec(
        title="t", xlabel="x", ylabel="y",
        series=[ps.Series(x=x, y=x, yerr=np.full_like(x, 0.1),
                          color_values=np.array([0.0, ps.LN2 / 2, ps.LN2]), color_norm=ps.LN2,
                          role=ps.Role.DATA, state=ps.State.ENTROPY, label="ω̂")],
    )
    from matplotlib.collections import PathCollection
    fig, ax = plt.subplots()
    try:
        mpl._render_onto(ax, spec)
        fig.canvas.draw()  # force the deferred scalar-to-RGBA colour mapping to resolve
        scatters = [c for c in ax.collections if isinstance(c, PathCollection)]
        assert len(scatters) == 1  # the coloured scatter (whiskers are a separate errorbar)
        faces = scatters[0].get_facecolors()
        assert len(faces) == 3
        # three distinct per-point colours
        assert len({tuple(np.round(c, 4)) for c in faces}) == 3
    finally:
        plt.close(fig)


def test_pooled_master_draws_above_legacy_same_position_verdict():
    """Even an old spec carrying both glyphs cannot let the verdict hide the pooled square."""
    spec = ps.PlotSpec(
        title="t", xlabel="q", ylabel="E",
        series=[ps.Series(x=np.array([2.0]), y=np.array([6.0]), yerr=np.array([0.2]),
                          role=ps.Role.DATA, state=ps.State.POOLED,
                          style="verdict_resolved", label="pooled")],
        markers=[ps.Marker(x=2.0, y=6.0, kind=ps.MarkerKind.VERDICT_RESOLVED,
                           label="legacy verdict")],
    )
    from matplotlib.collections import PathCollection
    fig, ax = plt.subplots()
    try:
        mpl._render_onto(ax, spec)
        pooled_z = max(line.get_zorder() for line in ax.lines)
        verdict_z = max(c.get_zorder() for c in ax.collections if isinstance(c, PathCollection))
        assert pooled_z > verdict_z
    finally:
        plt.close(fig)


@pytest.mark.parametrize('include_baseline', [False, True])
def test_infinite_guides_follow_axes_limits_and_preserve_baseline_bounds(include_baseline):
    spec = ps.PlotSpec('', 'x', 'y',
                       series=[ps.Series(np.array([1000., 1001.]), np.array([1e6, 1e6 + 1]))],
                       guides=[ps.GuideLine('x', 0), ps.GuideLine('y', 0, include_in_bounds=include_baseline)])
    fig, ax = plt.subplots()
    try:
        mpl._render_onto(ax, spec)
        assert ax.get_xlim()[0] > 999
        assert ax.get_ylim()[0] <= 0 if include_baseline else ax.get_ylim()[0] > 1e6 - 1
        guides = [line for line in ax.lines if line.get_zorder() == 0]
        assert len(guides) == 2
        for xlim, ylim in (((-10, 20), (-100, 200)), ((-1e6, 2e6), (-1e9, 2e9))):
            ax.set_xlim(*xlim)
            ax.set_ylim(*ylim)
            fig.canvas.draw()
            vertical, horizontal = [line.get_transform().transform(line.get_xydata()) for line in guides]
            np.testing.assert_allclose(vertical[:, 1], [ax.bbox.ymin, ax.bbox.ymax])
            np.testing.assert_allclose(horizontal[:, 0], [ax.bbox.xmin, ax.bbox.xmax])
        assert ax.get_legend() is None
    finally:
        plt.close(fig)


def test_render_png_on_minimal_spec():
    png = mpl.render_png(ps.PlotSpec(title="t", xlabel="x", ylabel="y",
                                     series=[ps.Series(x=np.array([0.0, 1.0]),
                                                       y=np.array([0.0, 1.0]))]))
    assert png.startswith(_PNG)


def test_render_png_closes_figure_when_render_raises(monkeypatch):
    # A failure anywhere in the render workflow must not leak a live figure: render_png owns the
    # figure lifecycle and closes it in a finally.
    def _boom(ax, spec):
        raise RuntimeError("render blew up")

    monkeypatch.setattr(mpl, "_render_onto", _boom)
    before = len(plt.get_fignums())
    with pytest.raises(RuntimeError):
        mpl.render_png(_rich_spec())
    assert len(plt.get_fignums()) == before  # no figure left open
