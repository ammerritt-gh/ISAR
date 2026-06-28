"""compact_view: the downsampled, LLM-facing picture of a flagged scan (PHASE_2_DESIGN §6).

A formatter call decoupled from storage — it gives the adjudicator the *shape* of a flagged scan
(a downsampled trace, the flagged indices with their neighbours, summary stats), never the raw store.
"""
import numpy as np

from isar.reduce import compact_view
from isar.schema import axes, scan


def _spec(det, intensity, *, point_flags=(), spectrum_flags=(), q=(1.0, 0.0, 0.0)):
    n = len(intensity)
    return scan.Spectrum(
        detector_id=det, q=list(q), q_source="reported",
        intensity=np.asarray(intensity, float), variance=np.asarray(intensity, float),
        mask=np.ones(n, bool), point_flags=list(point_flags), spectrum_flags=list(spectrum_flags),
    )


def _record(spectra, *, scan_number=42, scan_type="measurement", scan_flags=(), setpoint=2.0):
    n = len(next(iter(spectra.values())).intensity)
    energy = np.linspace(-2.0, 6.0, n)
    return scan.ScanRecord(
        experiment_id="X", scan_number=scan_number, scan_type=scan_type,
        scan_type_source="declared",
        scan_axes=[axes.energy_axis(energy), *axes.constant_q_axes(1.0, 0.0, 0.0, n)],
        spectra=spectra,
        normalization=scan.Normalization("time", np.full(n, 1.0), np.full(n, 1.0), 1.0),
        temperature=scan.Temperature(np.full(n, setpoint), {}, np.ones(n, bool)),
        environment=scan.Environment(), geometry=None,
        energy_calibration=scan.EnergyCalibration("energy", None, None, "needs_zero"),
        scan_flags=list(scan_flags), raw_header={},
        source=scan.Source("f", None, None, "p", "0", "2025", scan.SCHEMA_VERSION),
    )


def test_compact_view_basic_shape_and_flag_context():
    intensity = [40, 52, 60, 8000, 38, 35, 33, 30]
    pf = scan.PointFlag(index=3, type="cosmic_candidate", reason="spike 8000 vs ~55")
    sf = scan.Flag(type="dead_detector", severity="high", reason="flat")
    rec = _record(
        {"ana1": _spec("ana1", intensity, point_flags=[pf], spectrum_flags=[sf], q=(0.5, 0, 0))},
        scan_number=42,
        scan_flags=[scan.Flag(type="truncated", severity="medium", reason="kept 8 of 40")],
        setpoint=2.0,
    )

    view = compact_view.compact_view(rec)

    assert view["scan_number"] == 42
    assert view["scan_type"] == "measurement"
    assert view["temperature"] == 2.0
    assert view["n_points"] == 8
    assert view["energy_range"] == [-2.0, 6.0]
    assert view["scan_flags"] == [{"type": "truncated", "severity": "medium", "reason": "kept 8 of 40"}]

    d = view["detectors"]["ana1"]
    assert d["q"] == [0.5, 0.0, 0.0]
    assert d["spectrum_flags"] == [{"type": "dead_detector", "severity": "high", "reason": "flat"}]
    assert d["summary"]["argmax"] == 3          # the spike is the max, full-resolution
    assert d["summary"]["max"] == 8000.0
    fp = d["flagged_points"][0]
    assert fp["index"] == 3
    assert fp["type"] == "cosmic_candidate"
    assert 8000.0 in fp["neighbors"]            # the model can *see* the spike + its surroundings


def test_compact_view_downsamples_long_intensity_but_keeps_flag_neighborhood():
    n = 200
    intensity = list(np.linspace(10.0, 50.0, n))
    intensity[123] = 9999.0
    pf = scan.PointFlag(index=123, type="cosmic_candidate", reason="spike")
    rec = _record({"ana1": _spec("ana1", intensity, point_flags=[pf])})

    d = compact_view.compact_view(rec)["detectors"]["ana1"]

    assert len(d["intensity"]) <= compact_view.MAX_TRACE_POINTS    # downsampled trace
    assert 9999.0 in d["flagged_points"][0]["neighbors"]           # full-res around the flag
    assert d["summary"]["argmax"] == 123                          # stats over the full array


def test_flagged_records_skips_clean_and_non_measurement():
    flagged = _record(
        {"ana1": _spec("ana1", [1, 2, 3],
                       point_flags=[scan.PointFlag(0, "stale_head", "x")])},
        scan_number=1,
    )
    clean = _record({"ana1": _spec("ana1", [1, 2, 3])}, scan_number=2)
    alignment = _record(
        {"ana1": _spec("ana1", [1, 2, 3],
                       spectrum_flags=[scan.Flag("dead_detector", "low", "x")])},
        scan_number=3, scan_type="alignment",
    )

    out = list(compact_view._flagged_records([flagged, clean, alignment]))

    assert [r.scan_number for r in out] == [1]      # only the flagged measurement
