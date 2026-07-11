"""Characterization: the facade opens the frozen MVP-16 campaign workspace read-only.

Skips cleanly when the baseline is not present on this machine (same pattern as
tests/report/test_campaign_diagnostic_characterization.py).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from isar.workspace_api import Workspace

_BASELINE = Path(r"C:\Users\AMM\Documents\ISAR_baselines\mvp_zone_test16")


@pytest.fixture(scope="module")
def ws():
    if not _BASELINE.exists():
        pytest.skip(f"frozen baseline not present: {_BASELINE}")
    return Workspace.open(_BASELINE)


def test_opens_with_populated_surfaces(ws):
    assert ws.schema_version == 1
    assert len(ws.scans) > 0
    assert len(ws.results) > 0
    assert len(ws.groups) > 0


def test_every_fitview_has_core_fields(ws):
    # every fitted per-detector result exposes verdict + q + T without raising (a bare non_result
    # master row legitimately carries none of these — the degrade-don't-crash contract still holds).
    fitted = [fv for fv in ws.results if fv.status == "fitted" and fv.detector_id]
    assert fitted, "expected at least one fitted detector row"
    for fv in fitted:
        assert fv.verdict is not None
        assert fv.q is not None
        assert fv.T is not None
    # and accessing fields on the bare master row must not raise
    for fv in ws.results:
        _ = (fv.verdict, fv.q, fv.T, fv.bayes_interval, fv.amplitudes)


def test_session_events_parse(ws):
    events = list(ws.session_events())
    assert len(events) > 0
    assert all("kind" in e for e in events)


def test_evaluate_model_fallback_on_a_fitted_scan(ws):
    # MVP-16 predates stored amplitudes -> the fallback (counts + NNLS) path runs.
    target = None
    for sv in ws.scans:
        if sv.scan_type != "measurement":
            continue
        for det_id, fv in sv.detectors.items():
            if fv.omega0 is not None and fv.gamma is not None and not fv.pooled:
                target = (sv.scan_id, det_id)
                break
        if target:
            break
    assert target is not None, "no reconstructable fitted scan in MVP-16"
    out = ws.evaluate_model(*target)
    assert out.pure is False
    assert np.all(np.isfinite(out.total))
    assert len(out.total) > 0
