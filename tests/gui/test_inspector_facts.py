"""Unit facts in the inspector: each value with its source label, the Q rung, the flags; and the
heatmap/projection view models skipping a Q-less unit. Offscreen (conftest forces it), no window."""
from __future__ import annotations

import numpy as np
import pytest

pytest.importorskip("PySide6")
pytest.importorskip("pyqtgraph")

from isar.workspace_api import Workspace
from isar_gui.viewmodels import projection
from isar_gui.viewmodels.heatmap import HeatmapViewModel
from isar_gui.viewmodels.inspector import InspectorViewModel

_X = [-2.0, 0.0, 2.0, 4.0]
_COUNTS = [3.0, 10.0, 40.0, 5.0]


def _fact(value, source, **extra):
    return {"value": value, "source": source, **extra}


def _qless_prov(**over) -> dict:
    prov = {
        "source_file": r"C:\data\CsV3Sb5_302K.dat", "T": 302.0, "q": None,
        "q_source": "none", "q_rung": 4, "n_points": 4,
        "scan_facts": {
            "scan_label": _fact("302K_run1", "header"),
            "T_K": _fact(302.0, "header"),
            "hkl_nominal": _fact([0.0, 0.0, 3.5], "filename"),
            "slit": _fact(None, "missing"),
        },
        "analyzer_facts": {"dq": _fact(0.021, "header"), "efficiency": _fact(0.93, "sidecar")},
        "scan_flags": [{"type": "missing_sidecar", "severity": "warning", "reason": "no .var file"}],
        "spectrum_flags": [{"type": "bin_absent", "severity": "info", "reason": "ana3: 2 bins absent"}],
    }
    prov.update(over)
    return prov


def _unit_vm(make_ws, prov, key="bl43:7:ana3") -> InspectorViewModel:
    exp, scan, det = key.split(":")
    scans = {key: {"scan_id": key, "scan_number": int(scan), "scan_type": "measurement",
                   "status": "held_for_calibration", "detectors": {}, "provenance": prov}}
    root = make_ws(scans=scans, unit_files=[{"exp": exp, "scan_number": int(scan), "det": det,
                                             "x": _X, "counts": _COUNTS, "axis": None}])
    vm = InspectorViewModel(Workspace.open(root))
    vm.set_unit(key)
    return vm


def test_facts_shown_with_source_and_q_rung(make_ws):
    md = _unit_vm(make_ws, _qless_prov()).state.metadata
    assert md["slit"] == "not available (missing)"           # a missing slit is said, not hidden
    assert md["hkl_nominal"] == "[0, 0, 3.5] (filename)"
    assert md["scan_label"] == "302K_run1 (header)"
    assert md["dq"] == "0.021 (header)" and md["efficiency"] == "0.93 (sidecar)"
    assert md["Q rung"] == "4 (none)"
    assert "q" not in md                                       # no Q is shown as no Q, not a number
    assert md["scan flags"] == "missing_sidecar (warning): no .var file"
    assert md["spectrum flags"] == "bin_absent (info): ana3: 2 bins absent"


def test_slit_from_the_card_and_a_source_conflict(make_ws):
    prov = _qless_prov()
    prov["scan_facts"]["slit"] = _fact("2x3", "card")
    prov["scan_facts"]["T_K"] = _fact(302.0, "header", conflicts=[{"source": "filename", "value": 97.0}])
    md = _unit_vm(make_ws, prov).state.metadata
    assert md["slit"] == "2x3 (card)"
    assert md["T_K"] == "302 (header); conflicts: 97.0 (filename)"


def test_unit_without_facts_shows_no_new_rows(make_ws):
    # An S30-style ledger entry (no facts, no Q rung stamped) renders exactly as before.
    prov = {"source_file": r"C:\data\x.dat", "T": 300.0, "q": [1.5, 0.0, 0.0], "n_points": 4}
    md = _unit_vm(make_ws, prov, key="s30:12:ana3").state.metadata
    assert not {"slit", "dq", "efficiency", "hkl_nominal", "Q rung", "scan flags"} & set(md)
    assert md["q"] == "[1.5, 0, 0]"


def _fitted_outcome(h) -> dict:
    return {"detector_id": "det", "verdict": "resolved", "disposition": "resolved",
            "confidence": 0.9, "omega0": 5.0, "gamma": 1.0, "q": [h, 0.0, 0.0], "T": 200.0,
            "sample": "tavi", "calib": {"center": 0.0, "gauss_fwhm": 1.2, "lorentz_fwhm": 0.0},
            "interval": [4.6, 5.4], "interval_reliable": True}


def test_plane_views_skip_qless_units(make_ws):
    x = list(np.linspace(-2.0, 8.0, 21))
    scans = {}
    for n, h in ((1, 2.0), (2, 2.1)):
        sid = f"tavi:{n}:det"
        scans[sid] = {"scan_id": sid, "scan_number": n, "scan_type": "measurement",
                      "status": "fitted", "detectors": {"det": _fitted_outcome(h)},
                      "provenance": {"T": 200.0}}
    # Held, Q-less, and carrying a temperature: the case that would otherwise reach float(None).
    scans["bl43:7:ana3"] = {"scan_id": "bl43:7:ana3", "scan_number": 7, "scan_type": "measurement",
                            "status": "held_for_calibration", "detectors": {},
                            "provenance": _qless_prov()}
    rows = [{"scan": n, "detector": "det", "sample": "tavi", "T": 200.0, "h": h, "k": 0.0, "l": 0.0,
             "status": "fitted"} for n, h in ((1, 2.0), (2, 2.1))]
    data = [{"scan_number": n, "energy": x, "counts": [100.0] * len(x)} for n in (1, 2)]
    ws = Workspace.open(make_ws(scans=scans, rows=rows, data_files=data))
    assert all(fv.q is not None for fv in projection.population(ws))
    assert len(projection.population(ws)) == 2
    vm = HeatmapViewModel(ws)
    assert len(vm.spec.strips) == 2
