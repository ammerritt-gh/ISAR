"""The matplotlib backend renders valid PNGs for representative specs (we assert PNG magic bytes and
non-emptiness, not pixels — pin the spec, not the render)."""
import numpy as np

from isar.plotspec import spec as ps
from isar.plotspec.backends import mpl

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


def test_render_png_on_minimal_spec():
    png = mpl.render_png(ps.PlotSpec(title="t", xlabel="x", ylabel="y",
                                     series=[ps.Series(x=np.array([0.0, 1.0]),
                                                       y=np.array([0.0, 1.0]))]))
    assert png.startswith(_PNG)
