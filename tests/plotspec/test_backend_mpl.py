"""The matplotlib backend renders valid PNGs for representative specs (we assert PNG magic bytes and
non-emptiness, not pixels — pin the spec, not the render)."""
import matplotlib

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


def test_render_png_on_minimal_spec():
    png = mpl.render_png(ps.PlotSpec(title="t", xlabel="x", ylabel="y",
                                     series=[ps.Series(x=np.array([0.0, 1.0]),
                                                       y=np.array([0.0, 1.0]))]))
    assert png.startswith(_PNG)
