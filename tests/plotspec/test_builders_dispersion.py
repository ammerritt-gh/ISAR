"""C5 dispersion builder (Qt-free): dispersion_spec produces the ω̂(q) scatter with certifying
whiskers only, the entropy colour channel aligned to the whiskered subset, unreliable points as a
single warning note, verdict glyphs, optional prior/branch curves, off-curve annotations, and a
MANDATORY projection legend. These specs are what the GUI (and later the report) render."""
from __future__ import annotations

import json

import numpy as np
import pytest

from isar.plotspec import builders
from isar.plotspec import spec as ps

_PROJ = ps.ProjectionLegend(plane="(H, 0, 0) × E", integration="|K|≤0.05, |L|≤0.05")


def _pts():
    return [
        {"q": 2.0, "omega": 4.0, "yerr": 0.4, "entropy": 0.0, "verdict": "resolved"},
        {"q": 2.1, "omega": 5.0, "yerr": 0.4, "entropy": 0.3, "verdict": "resolved"},
        {"q": 2.2, "omega": 6.0, "yerr": None, "entropy": 0.69, "verdict": "flagged"},
    ]


def test_projection_legend_mandatory():
    with pytest.raises(ValueError):
        builders.dispersion_spec(points=_pts(), projection=None)


def test_reliable_points_are_whiskered_and_uncertified_estimate_stays_neutral():
    spec = builders.dispersion_spec(points=_pts(), projection=_PROJ)
    data = [s for s in spec.series if s.role is ps.Role.DATA and s.yerr is not None]
    assert len(data) == 1
    assert len(data[0].x) == 2   # two certifying points; the yerr=None one is NOT whiskered
    assert set(np.round(data[0].x, 3)) == {2.0, 2.1}
    neutral = next(s for s in spec.series if s.label and "individual estimates" in s.label)
    assert neutral.x.tolist() == [2.2] and "muted" in (neutral.style or "")
    assert not any(m.kind is ps.MarkerKind.WARNING for m in spec.markers)
    assert json.loads(json.dumps(spec.to_dict())) == spec.to_dict()


def test_entropy_channel_only_on_whiskered_subset_and_gated():
    spec = builders.dispersion_spec(points=_pts(), projection=_PROJ)
    data = next(s for s in spec.series if s.role is ps.Role.DATA and s.yerr is not None)
    assert data.state is ps.State.ENTROPY
    # aligned to the whiskered subset (2 points), NOT the full 3
    assert data.color_values is not None and len(data.color_values) == 2
    assert data.color_norm == pytest.approx(ps.LN2)
    assert any("assignment entropy" in a.text for a in spec.annotations)


def test_entropy_note_absent_when_no_entropy_present():
    pts = [{"q": 2.0, "omega": 4.0, "yerr": 0.4, "entropy": None},
           {"q": 2.1, "omega": 5.0, "yerr": 0.4, "entropy": None}]
    spec = builders.dispersion_spec(points=pts, projection=_PROJ)
    data = next(s for s in spec.series if s.role is ps.Role.DATA and s.yerr is not None)
    assert data.state is not ps.State.ENTROPY
    assert data.color_values is None
    assert not any("assignment entropy" in a.text for a in spec.annotations)


def test_verdict_glyphs_present():
    spec = builders.dispersion_spec(points=_pts(), projection=_PROJ)
    kinds = {m.kind for m in spec.markers}
    assert ps.MarkerKind.VERDICT_RESOLVED in kinds
    assert ps.MarkerKind.VERDICT_FLAGGED in kinds


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


def test_pooled_master_is_marked_and_whiskered_separately_from_members():
    # A pooled master (delivered per-q answer) with its OWN certifying interval + n_members.
    pts = [{"q": 2.0, "omega": 4.0, "yerr": 0.4},
           {"q": 2.1, "omega": 5.0, "yerr": 0.4},
           {"q": 2.15, "omega": 5.2, "yerr": 0.3, "pooled": True, "n_members": 3}]
    spec = builders.dispersion_spec(points=pts, projection=_PROJ)
    pooled = [s for s in spec.series if s.state is ps.State.POOLED]
    assert len(pooled) == 1
    assert pooled[0].yerr is not None and pooled[0].yerr[0] == pytest.approx(0.3)  # its own whisker
    assert pooled[0].label == "pooled (3)"
    # members stay shown on their own certifying scatter (the master is NOT mixed in)
    data = next(s for s in spec.series if s.role is ps.Role.DATA and s.state is not ps.State.POOLED)
    assert len(data.x) == 2


def test_pooled_master_without_certifying_interval_is_a_neutral_hollow_square():
    pts = [{"q": 2.0, "omega": 4.0, "yerr": 0.4},
           {"q": 2.1, "omega": 5.0, "yerr": 0.4},
           {"q": 2.15, "omega": 5.2, "yerr": None, "pooled": True, "n_members": 3}]
    spec = builders.dispersion_spec(points=pts, projection=_PROJ)
    assert not any(s.state is ps.State.POOLED for s in spec.series)  # no whiskered pooled series
    assert any(m.kind is ps.MarkerKind.POOLED_UNCERTIFIED and "no certifying" in (m.label or "")
               for m in spec.markers)


def test_explicitly_unreliable_pooled_master_is_red_warning():
    spec = builders.dispersion_spec(points=[{
        "q": 2.15, "omega": 5.2, "yerr": None, "pooled": True, "n_members": 3,
        "interval_reliable": False, "verdict": "flagged",
    }], projection=_PROJ)
    assert any(m.kind is ps.MarkerKind.POOLED_WARNING and "unreliable" in (m.label or "")
               for m in spec.markers)
    assert any(a.role is ps.State.UNRELIABLE for a in spec.annotations)


def test_pooled_master_composes_verdict_without_an_occluding_marker():
    pts = [
        {"q": 2.1, "omega": 5.0, "yerr": 0.4, "verdict": "resolved"},
        {"q": 2.1, "omega": 5.1, "yerr": 0.2, "pooled": True,
         "n_members": 3, "verdict": "resolved"},
    ]
    spec = builders.dispersion_spec(points=pts, projection=_PROJ)
    pooled = next(s for s in spec.series if s.state is ps.State.POOLED)
    assert "verdict_resolved" in (pooled.style or "")
    assert not any(m.kind is ps.MarkerKind.VERDICT_RESOLVED and m.x == pytest.approx(2.1)
                   and m.y == pytest.approx(5.1) for m in spec.markers)


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


def test_provisional_bridge_is_one_hollow_series_without_verdict_overlay():
    spec = builders.dispersion_spec(points=[{
        "q": 2.5, "omega": 7.0, "yerr": 0.2, "track_state": "provisional",
        "verdict": "resolved",
    }], projection=_PROJ)
    bridge = next(s for s in spec.series if "provisional" in (s.style or ""))
    assert bridge.label == "provisional bridge"
    assert not any(m.kind is ps.MarkerKind.VERDICT_RESOLVED for m in spec.markers)
