"""C2 inspector builders (Qt-free): fit_overlay_spec / residuals_spec produce the expected series
roles + honesty annotations for reliable, unreliable, and pooled inputs. These specs are what the
GUI and (later) the report golden-snapshot; the unreliable case asserts the builder respects the
spec-layer no-whiskers rule rather than tripping its ValueError."""
from __future__ import annotations

import json

import numpy as np
import pytest

from isar.plotspec import builders
from isar.plotspec import spec as ps


def _data():
    x = np.linspace(-2.0, 8.0, 21)
    counts = 100.0 + 500.0 * np.exp(-((x - 5.0) ** 2) / 2.0)
    yerr = np.sqrt(counts)
    x_fit = np.linspace(-2.0, 8.0, 200)
    total = 100.0 + 500.0 * np.exp(-((x_fit - 5.0) ** 2) / 2.0)
    components = [("phonon", total - 100.0), ("bg", np.full_like(x_fit, 100.0))]
    return x, counts, yerr, x_fit, total, components


def test_fit_overlay_reliable_has_data_fit_components_and_omega0_whisker():
    x, counts, yerr, x_fit, total, components = _data()
    spec = builders.fit_overlay_spec(
        x=x, counts=counts, yerr=yerr, x_fit=x_fit, total=total, components=components,
        omega0=5.0, interval=(4.6, 5.4), interval_reliable=True,
        scan_number=7, det_id="det", q=[2.0, 0.0, 0.0])

    roles = [s.role for s in spec.series]
    assert ps.Role.DATA in roles
    assert ps.Role.FIT in roles
    assert roles.count(ps.Role.COMPONENT) == 2
    # data series carries whiskers; no UNRELIABLE state anywhere
    data = next(s for s in spec.series if s.role is ps.Role.DATA)
    assert data.yerr is not None
    assert all(s.state is not ps.State.UNRELIABLE for s in spec.series)
    # ω₀ interval whisker: a single-point FIT series with yerr, no unreliable annotation/marker
    omega_series = [s for s in spec.series if s.role is ps.Role.FIT and s.yerr is not None]
    assert len(omega_series) == 1 and omega_series[0].x[0] == 5.0
    assert omega_series[0].yerr[0] == pytest.approx(0.4)  # half-width of (4.6, 5.4)
    assert not any(a.role is ps.State.UNRELIABLE for a in spec.annotations)
    assert not any(m.kind is ps.MarkerKind.WARNING for m in spec.markers)
    assert spec.projection is not None
    # JSON-stable
    assert json.loads(json.dumps(spec.to_dict())) == spec.to_dict()


def test_fit_overlay_unreliable_no_omega0_whisker_and_annotation():
    x, counts, yerr, x_fit, total, components = _data()
    # Must NOT raise (spec layer forbids UNRELIABLE + yerr): the builder emits the sanctioned form.
    spec = builders.fit_overlay_spec(
        x=x, counts=counts, yerr=yerr, x_fit=x_fit, total=total, components=components,
        omega0=5.0, interval=(4.6, 5.4), interval_reliable=False,
        scan_number=7, det_id="det", q=[2.0, 0.0, 0.0])

    # no ω₀ whisker series (a single-point FIT series with yerr)
    single_point_yerr = [s for s in spec.series
                         if s.role is ps.Role.FIT and s.yerr is not None and np.size(s.x) == 1]
    assert single_point_yerr == []
    # the sanctioned rendering: warning marker + "(unreliable)" annotation
    assert any(m.kind is ps.MarkerKind.WARNING for m in spec.markers)
    unreliable = [a for a in spec.annotations if a.role is ps.State.UNRELIABLE]
    assert len(unreliable) == 1 and "(unreliable)" in unreliable[0].text


def test_fit_overlay_pooled_labels_and_enumerates_members():
    x, counts, yerr, x_fit, total, components = _data()
    members = ["101", "102", "103"]
    spec = builders.fit_overlay_spec(
        x=x, counts=counts, yerr=yerr, x_fit=x_fit, total=total, components=components,
        omega0=5.0, interval=(4.6, 5.4), interval_reliable=True,
        scan_number=None, det_id="det", q=[2.0, 0.0, 0.0],
        pooled=True, members=members, n_members=3)

    data = next(s for s in spec.series if s.role is ps.Role.DATA)
    assert data.state is ps.State.POOLED
    assert "pooled" in spec.title.lower()
    assert spec.meta["pooled"] is True
    assert spec.meta["members"] == members
    assert spec.meta["n_members"] == 3


def test_residuals_spec_single_residual_series():
    x = np.linspace(-2.0, 8.0, 21)
    resid = np.random.default_rng(0).normal(size=x.size)
    spec = builders.residuals_spec(x=x, residuals=resid, yerr=np.abs(resid) + 1.0)
    assert len(spec.series) == 1
    assert spec.series[0].role is ps.Role.RESIDUAL
    np.testing.assert_allclose(spec.series[0].y, resid)
    assert json.loads(json.dumps(spec.to_dict())) == spec.to_dict()
