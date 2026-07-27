"""Spec-layer contract tests (G8): construction, JSON round-trip, and the binding rendering
semantics (unreliable-has-no-whiskers, exactly-one-certifying-tier). These pin the spec JSON — the
contract later golden snapshots and both backends depend on."""
import json

import numpy as np
import pytest

from isar.plotspec import spec as ps


def test_series_to_dict_is_json_serializable():
    s = ps.Series(x=np.array([0.0, 1.0]), y=np.array([2.0, 3.0]),
                  yerr=np.array([0.5, 0.5]), label="kept", role=ps.Role.DATA)
    d = s.to_dict()
    json.dumps(d)  # must not raise
    assert d["x"] == [0.0, 1.0]
    assert d["y"] == [2.0, 3.0]
    assert d["yerr"] == [0.5, 0.5]
    assert d["role"] == "data"
    assert d["certifying"] is False


def test_directional_error_bars_serialize_with_missing_directions():
    errors = ps.ErrorBars(
        left=np.array([0.2, 0.3]),
        right=np.array([0.4, 0.5]),
        bottom=None,
        top=np.array([0.1, 0.6]),
    )
    series = ps.Series(
        x=np.array([1.0, 2.0]), y=np.array([3.0, 4.0]), errors=errors,
        glyph=ps.Glyph.MASTER_CERTIFIED,
    )
    d = series.to_dict()
    json.dumps(d)
    assert d["yerr"] is None
    assert d["errors"] == {
        "left": [0.2, 0.3],
        "right": [0.4, 0.5],
        "bottom": None,
        "top": [0.1, 0.6],
    }
    assert d["glyph"] == "master_certified"


def test_directional_error_bars_reject_shape_mismatch():
    with pytest.raises(ValueError, match="same shape"):
        ps.ErrorBars(left=np.array([0.1]), right=np.array([0.1, 0.2]))
    with pytest.raises(ValueError, match="same shape"):
        ps.Series(
            x=np.array([1.0, 2.0]), y=np.array([3.0, 4.0]),
            errors=ps.ErrorBars(left=np.array([0.1])),
        )


def test_directional_error_bars_reject_negative_spans():
    with pytest.raises(ValueError, match="non-negative"):
        ps.ErrorBars(bottom=np.array([-0.1]))


def test_series_rejects_legacy_and_directional_errors_together():
    with pytest.raises(ValueError, match="mutually exclusive"):
        ps.Series(
            x=np.array([1.0]), y=np.array([2.0]), yerr=np.array([0.1]),
            errors=ps.ErrorBars(top=np.array([0.2])),
        )


def test_band_marker_annotation_projection_round_trip():
    band = ps.Band(x=np.array([0.0, 1.0]), lo=np.array([1.0, 1.5]), hi=np.array([2.0, 2.5]),
                   role=ps.Role.FIT, tier=ps.Tier.BOOTSTRAP, certifying=True)
    mk = ps.Marker(x=0.3, y=1.2, kind=ps.MarkerKind.WARNING, label="!")
    an = ps.Annotation(text="(unreliable)", xy=(0.3, 1.2), role=ps.State.UNRELIABLE)
    pj = ps.ProjectionLegend(plane="(H, 1, 0) x E", integration="|L| < 0.02, |K-1| < 0.02")
    for obj in (band, mk, an, pj):
        json.dumps(obj.to_dict())
    assert band.to_dict()["tier"] == "bootstrap"
    assert band.to_dict()["certifying"] is True
    assert mk.to_dict()["kind"] == "warning"
    assert an.to_dict()["xy"] == [0.3, 1.2]
    assert pj.to_dict() == {"plane": "(H, 1, 0) x E", "integration": "|L| < 0.02, |K-1| < 0.02"}


def test_plotspec_to_dict_full_round_trip():
    sp = ps.PlotSpec(
        title="t", xlabel="x", ylabel="y",
        series=[ps.Series(x=np.array([0.0]), y=np.array([1.0]))],
        bands=[ps.Band(x=np.array([0.0]), lo=np.array([0.0]), hi=np.array([2.0]))],
        markers=[ps.Marker(x=0.0, y=1.0, kind=ps.MarkerKind.MASKED)],
        annotations=[ps.Annotation(text="7 excluded")],
        projection=ps.ProjectionLegend(plane="E", integration="Q fixed"),
        meta={"pooled": True, "members": [101, 102]},
    )
    d = sp.to_dict()
    s = json.dumps(d)
    assert json.loads(s)["meta"]["members"] == [101, 102]
    assert d["projection"]["plane"] == "E"
    assert len(d["series"]) == 1 and len(d["bands"]) == 1


def test_unreliable_series_may_not_carry_whiskers():
    with pytest.raises(ValueError):
        ps.Series(x=np.array([0.0]), y=np.array([1.0]), yerr=np.array([0.5]),
                  state=ps.State.UNRELIABLE)


def test_unreliable_series_may_not_carry_directional_whiskers():
    with pytest.raises(ValueError, match="UNRELIABLE"):
        ps.Series(
            x=np.array([0.0]), y=np.array([1.0]),
            errors=ps.ErrorBars(left=np.array([0.5])),
            state=ps.State.UNRELIABLE,
        )


def test_unreliable_band_is_rejected():
    with pytest.raises(ValueError):
        ps.Band(x=np.array([0.0]), lo=np.array([0.0]), hi=np.array([1.0]),
                state=ps.State.UNRELIABLE)


def test_mark_unreliable_helper_adds_marker_and_annotation_no_whiskers():
    sp = ps.PlotSpec(title="t", xlabel="x", ylabel="y")
    ps.mark_unreliable(sp, x=0.5, y=1.0)
    assert any(m.kind == ps.MarkerKind.WARNING for m in sp.markers)
    assert any("unreliable" in a.text for a in sp.annotations)
    # a spec built this way carries no yerr and no bands for that point
    assert all(s.yerr is None for s in sp.series)


def test_exactly_one_certifying_tier_is_allowed():
    # zero certifying is fine
    ps.PlotSpec(title="t", xlabel="x", ylabel="y",
                bands=[ps.Band(x=np.array([0.0]), lo=np.array([0.0]), hi=np.array([1.0]),
                               tier=ps.Tier.HESSE)])
    # exactly one is fine
    ps.PlotSpec(title="t", xlabel="x", ylabel="y",
                bands=[ps.Band(x=np.array([0.0]), lo=np.array([0.0]), hi=np.array([1.0]),
                               tier=ps.Tier.HESSE),
                       ps.Band(x=np.array([0.0]), lo=np.array([0.0]), hi=np.array([1.0]),
                               tier=ps.Tier.BOOTSTRAP, certifying=True)])


def test_more_than_one_certifying_tier_is_rejected():
    with pytest.raises(ValueError):
        ps.PlotSpec(title="t", xlabel="x", ylabel="y",
                    series=[ps.Series(x=np.array([0.0]), y=np.array([1.0]),
                                      tier=ps.Tier.PROFILE, certifying=True)],
                    bands=[ps.Band(x=np.array([0.0]), lo=np.array([0.0]), hi=np.array([1.0]),
                                   tier=ps.Tier.BOOTSTRAP, certifying=True)])


def test_strip_construction_and_to_dict_round_trip():
    st = ps.Strip(x_center=2.1, width=0.08, y=np.array([-2.0, 0.0, 2.0]),
                  values=np.array([10.0, 50.0, 12.0]), meta={"scan_id": "tavi:7"})
    d = st.to_dict()
    json.dumps(d)  # must not raise
    assert d["x_center"] == 2.1 and d["width"] == 0.08
    assert d["y"] == [-2.0, 0.0, 2.0]
    assert d["values"] == [10.0, 50.0, 12.0]
    assert d["meta"] == {"scan_id": "tavi:7"}


def test_plotspec_carries_strips_and_value_label_in_to_dict():
    sp = ps.PlotSpec(
        title="plane", xlabel="H (rlu)", ylabel="E (meV)",
        strips=[ps.Strip(x_center=2.0, width=0.08, y=np.array([0.0, 1.0]),
                         values=np.array([1.0, 2.0]), meta={"scan_id": "a"}),
                ps.Strip(x_center=2.2, width=0.08, y=np.array([0.0, 1.0]),
                         values=np.array([3.0, 4.0]), meta={"scan_id": "b"})],
        value_label="counts/monitor",
    )
    d = sp.to_dict()
    json.dumps(d)
    assert len(d["strips"]) == 2
    assert d["value_label"] == "counts/monitor"
    assert d["strip_colormap"] == "viridis"
    assert d["strips"][0]["meta"]["scan_id"] == "a"


def test_plotspec_strip_colormap_is_configurable():
    sp = ps.PlotSpec(title="plane", xlabel="H", ylabel="E", strip_colormap="magma")
    assert sp.strip_colormap == "magma"
    assert sp.to_dict()["strip_colormap"] == "magma"


def test_region_box_serializes_and_rejects_inverted_bounds():
    region = ps.RegionBox(
        x_min=1.96, x_max=2.04, y_min=-2.0, y_max=8.0,
        kind=ps.RegionKind.LATEST_ACQUISITION,
        label="latest completed acquisition",
    )
    d = region.to_dict()
    json.dumps(d)
    assert d == {
        "x_min": 1.96,
        "x_max": 2.04,
        "y_min": -2.0,
        "y_max": 8.0,
        "kind": "latest_acquisition",
        "label": "latest completed acquisition",
    }
    sp = ps.PlotSpec(title="plane", xlabel="H", ylabel="E", regions=[region])
    assert sp.to_dict()["regions"] == [d]
    with pytest.raises(ValueError, match="maxima"):
        ps.RegionBox(
            x_min=2.0, x_max=1.0, y_min=0.0, y_max=1.0,
            kind=ps.RegionKind.LATEST_ACQUISITION,
        )


def test_entropy_rgb_maps_calm_to_alarm():
    rgb = ps.entropy_rgb([0.0, ps.LN2], norm=ps.LN2)
    assert rgb.shape == (2, 3)
    # 0 -> calm, ln2 -> alarm: the two endpoints are distinct colours.
    assert not np.allclose(rgb[0], rgb[1])
    # calm is bluer (more blue than red); alarm is redder.
    assert rgb[0][2] > rgb[0][0]
    assert rgb[1][0] > rgb[1][2]
    # None / non-finite reads as calm.
    assert np.allclose(ps.entropy_rgb([None])[0], rgb[0])


def test_series_carries_per_point_colour_channel_in_to_dict():
    s = ps.Series(x=np.array([0.0, 1.0]), y=np.array([2.0, 3.0]), yerr=np.array([0.1, 0.1]),
                  color_values=np.array([0.0, 0.6]), color_norm=ps.LN2, state=ps.State.ENTROPY)
    d = s.to_dict()
    json.dumps(d)
    assert d["color_values"] == [0.0, 0.6]
    assert d["color_norm"] == pytest.approx(ps.LN2)


def test_pooled_master_enumerates_members_in_meta():
    sp = ps.PlotSpec(title="t", xlabel="x", ylabel="y",
                     series=[ps.Series(x=np.array([0.0]), y=np.array([1.0]),
                                       state=ps.State.POOLED)],
                     meta={"pooled": True, "members": [55182, 55183]})
    assert sp.to_dict()["meta"]["members"] == [55182, 55183]
