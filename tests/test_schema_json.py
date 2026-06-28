"""Schema serialization gate: a ScanRecord must survive the JSON canonical store (D1, §5.7).

The canonical store is one JSON file per physical scan with arrays inline. The load-bearing
risk is numpy-in-JSON: `dataclasses.asdict` leaves `np.ndarray` in place and `json.dumps`
rejects it, so the schema needs a numpy-aware codec and an array-aware equality
(`records_close`) — dataclass `==` cannot compare records because `ndarray == ndarray`
returns an array (ambiguous truth value).
"""
import json

import numpy as np

from isar.schema import scan, axes, _json


def _example_record() -> scan.ScanRecord:
    """A small but representative multi-spectrum constant-Q-energy record (2 of 9 detectors)."""
    n = 5
    energy = np.linspace(-2.0, 2.0, n)
    return scan.ScanRecord(
        experiment_id="FeSe_1xS_1",
        scan_number=53,
        scan_type="measurement",
        scan_type_source="inferred",
        scan_axes=[axes.energy_axis(energy), *axes.constant_q_axes(2.0, 0.05, 0.0, n)],
        spectra={
            "ana1": scan.Spectrum(
                detector_id="ana1",
                q=[1.52794, 0.0366975, -0.0732877],
                q_source="reported",
                intensity=np.array([0.0, 1.5, 9.0, 1.4, 0.0]),
                # variance carries an honest zero for a zero-count channel (D3, no floor).
                variance=np.array([0.0, 0.3, 1.1, 0.28, 0.0]),
                mask=np.array([True, True, True, True, False]),
                point_flags=[scan.PointFlag(index=4, type="monitor_dropout", reason="monitor==0")],
                spectrum_flags=[],
                resolution=None,
            ),
            "ana5": scan.Spectrum(
                detector_id="ana5",
                q=[2.0, 0.050001, -5.65969e-06],
                q_source="reported",
                intensity=np.array([0.1, 2.0, 50.0, 2.1, 0.1]),
                variance=np.array([0.02, 0.4, 5.0, 0.42, 0.02]),
                mask=np.ones(n, dtype=bool),
                point_flags=[],
                spectrum_flags=[scan.Flag(type="dead_detector", severity="low", reason="candidate")],
                resolution=None,
            ),
        },
        normalization=scan.Normalization(
            counting_mode="time",
            monitor=np.full(n, 4.1e6),
            time=np.full(n, 60.0),
            preset=60.0,
            monitor_correction=False,
        ),
        temperature=scan.Temperature(
            setpoint=np.full(n, 290.0),
            sensors={"T_Sample": scan.Sensor(
                name="T_Sample", values=np.full(n, 300.0),
                valid_mask=np.ones(n, dtype=bool), role="monitored",
            )},
            valid_mask=np.ones(n, dtype=bool),
            snap_applied=False,
        ),
        environment=scan.Environment(field=None, logs={}),
        geometry=None,
        energy_calibration=scan.EnergyCalibration(
            raw_abscissa_kind="energy", transform=None,
            elastic_zero_offset=None, status="needs_zero",
        ),
        scan_flags=[],
        raw_header={"#S": "53  fscan", "#T": "2  (Seconds)"},
        source=scan.Source(
            file="FeSe_1-xS_x_1.spec_supplementary.dat", byte_range=None, line_range=[42, 130],
            plugin="spec_s30", plugin_version="0.1.0", format_version="2025",
            schema_version=scan.SCHEMA_VERSION,
        ),
    )


def test_scan_record_json_round_trip():
    rec = _example_record()
    # to_dict must be JSON-serializable (numpy arrays inlined as lists)...
    blob = json.dumps(rec.to_dict())
    restored = scan.ScanRecord.from_dict(json.loads(blob))
    # ...and the restored record must be equal under the array-aware comparison.
    assert scan.records_close(rec, restored)


def test_round_trip_restores_numpy_dtypes():
    rec = _example_record()
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    sp = restored.spectra["ana1"]
    assert isinstance(sp.intensity, np.ndarray)
    assert sp.intensity.dtype == np.float64
    assert sp.mask.dtype == np.bool_
    # The honest zero-count variance survives the round trip exactly (no floor crept in).
    assert sp.variance[0] == 0.0


def test_records_close_detects_a_difference():
    # Guard against a vacuously-true records_close: a perturbed intensity must compare unequal.
    rec = _example_record()
    other = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    other.spectra["ana5"].intensity[2] += 1.0
    assert not scan.records_close(rec, other)


# ---------------------------------------------------------------------------
# Raw-S30 miniscan ensemble: 2-D arrays through the JSON codec (the #1 risk, §step 1).
# ---------------------------------------------------------------------------
def test_spectrum_miniscan_ensemble_2d_round_trip():
    """A Spectrum carrying the raw miniscan ensemble (2-D counts + 2-D reject mask) must survive
    the JSON store: nested lists serialize, dtype/shape restore, array-aware equality holds."""
    nP, n_mini = 5, 15
    counts = np.arange(nP * n_mini, dtype="float64").reshape(nP, n_mini)
    reject = np.zeros((nP, n_mini), dtype=bool)
    reject[3, 14] = True   # one rejected miniscan (a cosmic ray at point 3, pass 14)
    sp = scan.Spectrum(
        detector_id="ana3", q=[2.2, 0.05, 0.0], q_source="reported",
        intensity=np.zeros(nP), variance=np.zeros(nP), mask=np.zeros(nP, dtype=bool),
        point_flags=[], spectrum_flags=[],
        miniscan_counts=counts, miniscan_reject=reject, miniscan_reject_source="sigma_clip",
    )
    restored = _json.from_dataclass(scan.Spectrum, json.loads(json.dumps(_json.to_jsonable(sp))))
    assert _json.objects_close(sp, restored)
    assert restored.miniscan_counts.shape == (nP, n_mini)
    assert restored.miniscan_counts.dtype == np.float64
    assert restored.miniscan_reject.dtype == np.bool_
    assert bool(restored.miniscan_reject[3, 14]) is True
    assert restored.miniscan_reject_source == "sigma_clip"


def test_spectrum_without_miniscans_defaults_to_none():
    """The miniscan fields are optional — the supplementary path never sets them, and a None
    array field must serialize/restore as None (not crash the array-aware comparison)."""
    sp = scan.Spectrum(
        detector_id="ana1", q=[2.0, 0.05, 0.0], q_source="reported",
        intensity=np.zeros(3), variance=np.zeros(3), mask=np.ones(3, dtype=bool),
        point_flags=[], spectrum_flags=[],
    )
    assert sp.miniscan_counts is None and sp.miniscan_reject is None
    restored = _json.from_dataclass(scan.Spectrum, json.loads(json.dumps(_json.to_jsonable(sp))))
    assert restored.miniscan_counts is None
    assert restored.miniscan_reject is None
    assert restored.miniscan_reject_source is None
    assert _json.objects_close(sp, restored)


def test_normalization_miniscan_monitor_time_2d_round_trip():
    """Per-miniscan monitor/time (shared across the 9 analyzers) survive the JSON store as 2-D."""
    nP, n_mini = 4, 15
    mon = np.full((nP, n_mini), 2.7e5)
    tim = np.full((nP, n_mini), 4.0)
    norm = scan.Normalization(
        counting_mode="monitor", monitor=np.full(nP, 4.0e6), time=np.full(nP, 60.0),
        preset=4.0e6, miniscan_monitor=mon, miniscan_time=tim,
    )
    restored = _json.from_dataclass(scan.Normalization, json.loads(json.dumps(_json.to_jsonable(norm))))
    assert _json.objects_close(norm, restored)
    assert restored.miniscan_monitor.shape == (nP, n_mini)
    assert restored.miniscan_time.dtype == np.float64


# ---------------------------------------------------------------------------
# LLM adjudication: the optional decision block survives the JSON store (§7.3).
# ---------------------------------------------------------------------------
def test_scan_record_adjudication_defaults_none():
    """A record reduced without the LLM stage has no adjudication, and that round-trips as None."""
    rec = _example_record()
    assert rec.adjudication is None
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert restored.adjudication is None
    assert scan.records_close(rec, restored)


def test_adjudication_round_trip():
    """An attached Adjudication (decisions + narrative) survives to_dict → JSON → from_dict."""
    rec = _example_record()
    rec.adjudication = scan.Adjudication(
        decisions=[
            scan.Decision(target="point:ana1:4", decision="mask",
                          shape="not_applicable", reason="cosmic ray", confidence=0.99),
            scan.Decision(target="spectrum:ana5", decision="drop_detector",
                          shape="not_applicable", reason="dead detector", confidence=0.9),
        ],
        narrative="One cosmic ray masked on ana1; ana5 dropped as dead.",
        model_id="local_desktop/qwen3p5-9b",
    )
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert scan.records_close(rec, restored)
    adj = restored.adjudication
    assert adj.model_id == "local_desktop/qwen3p5-9b"
    assert adj.decisions[0].decision == "mask"
    assert adj.decisions[0].confidence == 0.99
    assert adj.decisions[1].target == "spectrum:ana5"
