"""C4 waterfall builder (Qt-free): waterfall_spec stacks per-T traces with a vertical offset — a
pure display device (G10), always declared on screen via an annotation. A pooled-master trace is
curve-only (Task-10 honesty rule: a master's raw counts are never persisted, so never drawn)."""
from __future__ import annotations

import json

import numpy as np
import pytest

from isar.plotspec import builders
from isar.plotspec import spec as ps

_X = np.linspace(-2.0, 8.0, 11)


def _trace(label, center, base=100.0, amp=500.0, pooled=False, n_members=None):
    counts = base + amp * np.exp(-((_X - center) ** 2) / 2.0)
    return {"label": label, "x": _X, "counts": None if pooled else counts,
            "yerr": None if pooled else np.sqrt(counts),
            "curve_x": _X, "curve_total": counts,
            "pooled": pooled, "n_members": n_members}


def test_offsets_applied_per_trace_yerr_unshifted():
    traces = [_trace("T=10 K", 4.0), _trace("T=20 K", 5.0)]
    spec = builders.waterfall_spec(traces=traces, offset_step=1000.0)
    data = [s for s in spec.series if s.role is ps.Role.DATA]
    assert len(data) == 2
    # trace 0 unshifted, trace 1 shifted by +1*step
    base0 = traces[0]["counts"]
    base1 = traces[1]["counts"]
    assert np.allclose(data[0].y, base0)
    assert np.allclose(data[1].y, base1 + 1000.0)
    # yerr magnitudes preserved (not shifted)
    assert np.allclose(data[0].yerr, traces[0]["yerr"])
    assert np.allclose(data[1].yerr, traces[1]["yerr"])
    fit = [s for s in spec.series if s.role is ps.Role.FIT]
    assert len(fit) == 2
    assert np.allclose(fit[0].y, traces[0]["curve_total"])
    assert np.allclose(fit[1].y, traces[1]["curve_total"] + 1000.0)
    assert json.loads(json.dumps(spec.to_dict())) == spec.to_dict()


def test_pooled_trace_is_curve_only_with_member_note():
    traces = [_trace("T=10 K", 4.0),
              _trace("T=20 K", 5.0, pooled=True, n_members=3)]
    spec = builders.waterfall_spec(traces=traces, offset_step=1000.0)
    data = [s for s in spec.series if s.role is ps.Role.DATA]
    assert len(data) == 1   # the pooled trace draws NO data series
    fit = [s for s in spec.series if s.role is ps.Role.FIT]
    assert len(fit) == 2
    pooled_fit = fit[1]
    assert "pooled" in pooled_fit.label and "3" in pooled_fit.label
    assert "member data not shown" in pooled_fit.label
    assert np.allclose(pooled_fit.y, traces[1]["curve_total"] + 1000.0)


def test_offset_annotation_present():
    traces = [_trace("T=10 K", 4.0)]
    spec = builders.waterfall_spec(traces=traces, offset_step=250.0)
    notes = [a.text for a in spec.annotations]
    assert any("offset" in t and "250" in t for t in notes)


def test_per_trace_legend_labels():
    traces = [_trace("T=10 K", 4.0), _trace("T=20 K", 5.0)]
    spec = builders.waterfall_spec(traces=traces, offset_step=1000.0)
    labels = {s.label for s in spec.series}
    assert "T=10 K" in labels and "T=20 K" in labels


def test_projection_legend_optional_when_q_given():
    traces = [_trace("T=10 K", 4.0)]
    spec = builders.waterfall_spec(traces=traces, offset_step=1000.0)
    assert spec.projection is None
    spec2 = builders.waterfall_spec(traces=traces, offset_step=1000.0, q=2.1, axis_label="H")
    assert spec2.projection is not None
    assert "2.1" in spec2.projection.integration
