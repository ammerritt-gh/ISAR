"""C5 dispersion builder (Qt-free): state-first fit glyphs, asymmetric certifying intervals,
optional entropy colour, neutral prior/branch curves, and the mandatory projection legend."""
from __future__ import annotations

import json

import numpy as np
import pytest

from isar.plotspec import builders
from isar.plotspec import spec as ps

_PROJ = ps.ProjectionLegend(plane="(H, 0, 0) × E", integration="|K|≤0.05, |L|≤0.05")


def _pts():
    return [
        {"q": 2.0, "omega": 4.0, "interval": (3.7, 4.5), "entropy": 0.0,
         "display_state": "individual_valid"},
        {"q": 2.1, "omega": 5.0, "interval": (4.8, 5.6), "entropy": 0.3,
         "display_state": "individual_valid"},
        {"q": 2.2, "omega": 6.0, "interval": None, "entropy": 0.69,
         "display_state": "individual_uncertified"},
    ]


def test_projection_legend_mandatory():
    with pytest.raises(ValueError):
        builders.dispersion_spec(points=_pts(), projection=None)


def test_reliable_points_have_asymmetric_vertical_errors_and_uncertified_has_none():
    spec = builders.dispersion_spec(points=_pts(), projection=_PROJ)
    valid = next(s for s in spec.series if s.glyph is ps.Glyph.INDIVIDUAL_VALID)
    assert valid.x.tolist() == [2.0, 2.1]
    assert valid.yerr is None and valid.errors is not None
    np.testing.assert_allclose(valid.errors.bottom, [0.3, 0.2])
    np.testing.assert_allclose(valid.errors.top, [0.5, 0.6])
    uncertified = next(
        s for s in spec.series if s.glyph is ps.Glyph.INDIVIDUAL_UNCERTIFIED)
    assert uncertified.x.tolist() == [2.2]
    assert uncertified.errors is None
    assert spec.markers == []
    assert not any(m.kind is ps.MarkerKind.WARNING for m in spec.markers)
    assert json.loads(json.dumps(spec.to_dict())) == spec.to_dict()


def test_state_mode_is_default_and_has_no_entropy_note():
    spec = builders.dispersion_spec(points=_pts(), projection=_PROJ)
    assert all(s.color_values is None for s in spec.series)
    assert not any("assignment entropy" in a.text for a in spec.annotations)


def test_entropy_mode_colours_each_semantic_group_without_changing_glyph():
    spec = builders.dispersion_spec(
        points=_pts(), projection=_PROJ, color_by_entropy=True)
    valid = next(s for s in spec.series if s.glyph is ps.Glyph.INDIVIDUAL_VALID)
    uncertified = next(
        s for s in spec.series if s.glyph is ps.Glyph.INDIVIDUAL_UNCERTIFIED)
    assert valid.color_values == [0.0, 0.3]
    assert valid.color_norm == pytest.approx(ps.LN2)
    assert uncertified.color_values == [0.69]
    assert uncertified.glyph is ps.Glyph.INDIVIDUAL_UNCERTIFIED
    assert any("assignment entropy" in a.text for a in spec.annotations)


def test_entropy_mode_maps_an_all_null_group_to_calm_instead_of_state_colour():
    spec = builders.dispersion_spec(
        points=[{
            "q": 2.0, "omega": 5.0, "entropy": None,
            "display_state": "master_mismatch",
        }],
        projection=_PROJ,
        color_by_entropy=True,
    )
    mismatch = next(s for s in spec.series if s.glyph is ps.Glyph.MASTER_MISMATCH)
    assert mismatch.color_values == [None]
    np.testing.assert_allclose(ps.entropy_rgb(mismatch.color_values), [[0.13, 0.47, 0.71]])


def test_fit_display_states_emit_one_composed_glyph_in_fixed_legend_order():
    states = [
        "master_certified",
        "individual_unreliable",
        "master_mismatch",
        "individual_valid",
        "unselected_candidate",
        "master_target",
        "individual_uncertified",
        "master_unassessed",
        "master_ambiguous",
    ]
    points = [
        {"q": float(i), "omega": float(i + 1), "display_state": state,
         "interval": ((i + 0.8, i + 1.2)
                      if state not in {"individual_unreliable", "unselected_candidate"}
                      else None)}
        for i, state in enumerate(states)
    ]
    spec = builders.dispersion_spec(points=points, projection=_PROJ)
    assert [s.glyph for s in spec.series] == [
        ps.Glyph.INDIVIDUAL_VALID,
        ps.Glyph.INDIVIDUAL_UNCERTIFIED,
        ps.Glyph.INDIVIDUAL_UNRELIABLE,
        ps.Glyph.MASTER_TARGET,
        ps.Glyph.MASTER_AMBIGUOUS,
        ps.Glyph.MASTER_MISMATCH,
        ps.Glyph.MASTER_CERTIFIED,
        ps.Glyph.MASTER_UNASSESSED,
        ps.Glyph.UNSELECTED_CANDIDATE,
    ]
    assert spec.markers == []
    assert all("master scan" not in (s.label or "") for s in spec.series)
    assert all(
        s.errors is None
        for s in spec.series
        if s.glyph in {ps.Glyph.INDIVIDUAL_UNRELIABLE, ps.Glyph.UNSELECTED_CANDIDATE}
    )


def test_prior_and_branch_series_present_only_when_given():
    bare = builders.dispersion_spec(points=_pts(), projection=_PROJ)
    assert not any(s.role in (ps.Role.PRIOR, ps.Role.BRANCH) for s in bare.series)

    spec = builders.dispersion_spec(
        points=_pts(), projection=_PROJ,
        prior=(np.array([2.0, 2.2]), np.array([4.0, 6.0])),
        branches=[("branch belief", np.array([2.0, 2.2]), np.array([4.1, 6.1]))])
    roles = [s.role for s in spec.series]
    assert ps.Role.PRIOR in roles and ps.Role.BRANCH in roles
    prior = next(s for s in spec.series if s.role is ps.Role.PRIOR)
    assert prior.label == "dispersion prior"


def test_off_curve_annotations_rendered():
    spec = builders.dispersion_spec(
        points=_pts(), projection=_PROJ,
        off_curve=[{"q": 2.2, "omega": 6.0, "n_sigma": 4.5}])
    off = [a for a in spec.annotations if a.xy is not None and "off prior" in a.text]
    assert len(off) == 1
    assert off[0].xy == (2.2, 6.0)
    assert "4.5" in off[0].text


def test_pooled_master_is_a_distinct_composed_glyph_with_its_own_interval():
    pts = [
        {"q": 2.0, "omega": 4.0, "interval": (3.6, 4.4),
         "display_state": "individual_valid"},
        {"q": 2.1, "omega": 5.0, "interval": (4.6, 5.4),
         "display_state": "individual_valid"},
        {"q": 2.15, "omega": 5.2, "interval": (4.9, 5.7),
         "display_state": "master_certified"},
    ]
    spec = builders.dispersion_spec(points=pts, projection=_PROJ)
    pooled = next(s for s in spec.series if s.glyph is ps.Glyph.MASTER_CERTIFIED)
    np.testing.assert_allclose(pooled.errors.bottom, [0.3])
    np.testing.assert_allclose(pooled.errors.top, [0.5])
    assert pooled.label == "pooled master (certified)"
    members = next(s for s in spec.series if s.glyph is ps.Glyph.INDIVIDUAL_VALID)
    assert len(members.x) == 2


def test_legacy_unassessed_master_has_neutral_square_and_no_interval():
    pts = [{"q": 2.15, "omega": 5.2, "interval": None,
            "display_state": "master_unassessed"}]
    spec = builders.dispersion_spec(points=pts, projection=_PROJ)
    pooled = next(s for s in spec.series if s.glyph is ps.Glyph.MASTER_UNASSESSED)
    assert pooled.errors is None
    assert pooled.label == "pooled master (legacy unassessed)"
    assert spec.markers == []


def test_segmented_target_and_acoustic_rival_do_not_bridge_a_gap():
    branches = [
        {"branch_id": "optic", "label": "optic target", "role": "target", "segments": [
            {"state": "confirmed", "x": [2.0, 2.1], "y": [6.0, 6.2]},
            {"state": "ambiguous", "x": [2.1, 2.4], "y": [6.2, 7.0]},
            {"state": "confirmed", "x": [2.8, 2.9], "y": [7.8, 8.0]},
        ]},
        {"branch_id": "acoustic", "label": "acoustic (non-target context)",
         "role": "rival", "segments": [
             {"state": "confirmed", "x": [2.0, 2.1], "y": [0.0, 1.0]},
         ]},
    ]
    spec = builders.dispersion_spec(points=[], projection=_PROJ, branches=branches)
    optic = [s for s in spec.series if s.role is ps.Role.BRANCH
             and "rival" not in (s.style or "")]
    # Two independent confirmed optic islands, never one [2.0 ... 2.9] polyline.
    assert len(optic) == 2
    assert all(np.max(s.x) - np.min(s.x) <= 0.11 for s in optic)
    acoustic = next(s for s in spec.series if s.label and s.label.startswith("acoustic"))
    assert "rival" in (acoustic.style or "")


def test_unselected_candidate_is_one_glyph_without_interval():
    spec = builders.dispersion_spec(points=[{
        "q": 2.5, "omega": 7.0, "interval": (6.8, 7.2),
        "display_state": "unselected_candidate",
    }], projection=_PROJ)
    candidate = next(s for s in spec.series if s.glyph is ps.Glyph.UNSELECTED_CANDIDATE)
    assert candidate.label == "unselected candidates"
    assert candidate.errors is None
    assert spec.markers == []
