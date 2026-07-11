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


def test_reliable_points_are_whiskered_scatter_unreliable_is_warning():
    spec = builders.dispersion_spec(points=_pts(), projection=_PROJ)
    data = [s for s in spec.series if s.role is ps.Role.DATA and s.yerr is not None]
    assert len(data) == 1
    assert len(data[0].x) == 2   # two certifying points; the yerr=None one is NOT whiskered
    assert set(np.round(data[0].x, 3)) == {2.0, 2.1}
    # the uncertified point is a single warning marker + one "(unreliable)" note (not per point)
    warn = [m for m in spec.markers if m.kind is ps.MarkerKind.WARNING]
    assert len(warn) == 1 and abs(warn[0].x - 2.2) < 1e-9
    unrel = [a for a in spec.annotations if a.role is ps.State.UNRELIABLE]
    assert len(unrel) == 1 and unrel[0].xy is None
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


def test_pooled_point_is_labeled_without_whisker():
    pts = [{"q": 2.0, "omega": 4.0, "yerr": 0.4},
           {"q": 2.1, "omega": 5.0, "yerr": 0.4},
           {"q": 2.15, "omega": 5.2, "yerr": 0.3, "pooled": True}]
    spec = builders.dispersion_spec(points=pts, projection=_PROJ)
    pooled = [s for s in spec.series if s.state is ps.State.POOLED]
    assert len(pooled) == 1
    assert pooled[0].yerr is None            # no per-member whisker
    assert "pooled" in (pooled[0].label or "").lower()
    # the pooled point is NOT in the certifying scatter
    data = next(s for s in spec.series if s.role is ps.Role.DATA and s.yerr is not None)
    assert len(data.x) == 2
