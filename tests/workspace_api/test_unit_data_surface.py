"""`Workspace.unit_data` — the read-only per-unit accessor (Pass 2b Phase 2).

Reads the ``data/ingest/`` files ingest wrote, keyed by a full ``exp:scan:det`` id; yerr is the
honest Poisson whisker (σ=0 stays 0, D3); a missing unit degrades to ``None``; and the legacy
single-detector ``data/j-*.json`` fallback plus the untouched ``scan_data`` are both exercised.
"""
from __future__ import annotations

import json

import numpy as np

from isar.session import unit_data as ud
from isar.workspace_api import Workspace

from .conftest import scan_entry


def _write_unit(root, exp, scan, det, *, x, counts, variance, mask, axis):
    p = ud.unit_data_path(root / "data", exp, scan, det)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema": "isar.unit_data/1", "scan_id": f"{exp}:{scan}:{det}",
        "experiment_id": exp, "scan_number": scan, "detector_id": det,
        "x": x, "counts": counts, "variance": variance, "mask": mask, "axis": axis,
    }), encoding="utf-8")
    return p


def test_unit_data_full_key(make_ws):
    root = make_ws(scans={"FeSeS:12:ana3": scan_entry(12, status="ingested", detectors={})})
    axis = {"name": "energy", "kind": "energy", "units": "meV",
            "raw_abscissa_kind": "energy", "calibration_status": "needs_zero"}
    _write_unit(root, "FeSeS", 12, "ana3", x=[-2.0, 0.0, 2.0], counts=[4.0, 0.0, 9.0],
                variance=[4.0, 0.0, 9.0], mask=[True, True, True], axis=axis)
    ws = Workspace.open(root)

    u = ws.unit_data("FeSeS:12:ana3")
    assert u is not None
    np.testing.assert_array_equal(u.x, [-2.0, 0.0, 2.0])
    np.testing.assert_array_equal(u.counts, [4.0, 0.0, 9.0])
    # yerr = sqrt(clip(variance)); the zero-count point keeps a 0 whisker (D3)
    np.testing.assert_allclose(u.yerr, [2.0, 0.0, 3.0])
    np.testing.assert_array_equal(u.mask, [True, True, True])
    assert u.axis["calibration_status"] == "needs_zero"


def test_unit_data_exp_scan_plus_detector_arg(make_ws):
    root = make_ws(scans={"FeSeS:5:ana1": scan_entry(5, status="ingested", detectors={})})
    axis = {"name": "energy", "kind": "energy", "units": "meV",
            "raw_abscissa_kind": "energy", "calibration_status": "absolute"}
    _write_unit(root, "FeSeS", 5, "ana1", x=[0.0, 1.0], counts=[1.0, 4.0],
                variance=[1.0, 4.0], mask=[True, True], axis=axis)
    ws = Workspace.open(root)
    u = ws.unit_data("FeSeS:5", detector_id="ana1")
    assert u is not None
    np.testing.assert_allclose(u.yerr, [1.0, 2.0])


def test_unit_data_missing_returns_none(make_ws):
    root = make_ws(scans={"FeSeS:9:ana2": scan_entry(9, status="ingested", detectors={})})
    ws = Workspace.open(root)
    assert ws.unit_data("FeSeS:9:ana2") is None       # no file written, no legacy file → None


def test_unit_data_rejects_corrupt_or_identity_mismatched_canonical_file(make_ws):
    root = make_ws(
        scans={"FeSeS:9:ana2": scan_entry(9, status="ingested", detectors={})},
        data_files=[{"scan_number": 9, "energy": [0.0], "counts": [99.0]}],
    )
    p = ud.unit_data_path(root / "data", "FeSeS", 9, "ana2")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "schema": ud.SCHEMA, "scan_id": "other:9:ana2",
        "experiment_id": "other", "scan_number": 9, "detector_id": "ana2",
        "x": [0.0], "counts": [5.0], "variance": [5.0], "mask": [True], "axis": {},
    }), encoding="utf-8")
    ws = Workspace.open(root)
    assert ws.unit_data("FeSeS:9:ana2") is None  # must not borrow the legacy scan-level 99-count file


def test_unit_data_legacy_fallback_absolute_axis(make_ws):
    """No per-unit file, but a drive ``data/j-0007.json`` exists → fallback with an absolute axis."""
    root = make_ws(
        scans={"tavi:7:det": scan_entry(7, status="fitted")},
        data_files=[{"scan_number": 7, "energy": [-1.0, 0.0, 1.0], "counts": [1.0, 0.0, 4.0]}])
    ws = Workspace.open(root)
    u = ws.unit_data("tavi:7:det")
    assert u is not None
    np.testing.assert_array_equal(u.counts, [1.0, 0.0, 4.0])
    np.testing.assert_allclose(u.yerr, [1.0, 0.0, 2.0])   # sqrt(clip(counts)); σ=0 at the zero point
    assert u.axis["calibration_status"] == "absolute"     # TAVI is calibrated


def test_scan_data_untouched(make_ws):
    """The pre-existing ``scan_data`` accessor is unchanged by the new unit surface."""
    root = make_ws(
        scans={"tavi:7:det": scan_entry(7, status="fitted")},
        data_files=[{"scan_number": 7, "energy": [-1.0, 0.0, 1.0], "counts": [1.0, 0.0, 4.0]}])
    ws = Workspace.open(root)
    energy, counts, yerr = ws.scan_data("tavi:7:det")
    np.testing.assert_array_equal(energy, [-1.0, 0.0, 1.0])
    np.testing.assert_array_equal(counts, [1.0, 0.0, 4.0])
    np.testing.assert_allclose(yerr, [1.0, 0.0, 2.0])
