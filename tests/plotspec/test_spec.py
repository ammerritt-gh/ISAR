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


def test_pooled_master_enumerates_members_in_meta():
    sp = ps.PlotSpec(title="t", xlabel="x", ylabel="y",
                     series=[ps.Series(x=np.array([0.0]), y=np.array([1.0]),
                                       state=ps.State.POOLED)],
                     meta={"pooled": True, "members": [55182, 55183]})
    assert sp.to_dict()["meta"]["members"] == [55182, 55183]
