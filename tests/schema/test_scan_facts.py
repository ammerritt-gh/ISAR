"""Facts, the optional Q, and per-spectrum exposure survive the JSON store; records written before
they existed load with defaults; exposure_for applies a spectrum's own monitor and time only."""
import json

import numpy as np

from isar.schema import scan


def _spectrum(det: str) -> scan.Spectrum:
    n = 3
    return scan.Spectrum(
        detector_id=det, q=[2.0, 0.0, 0.0], q_source="reported",
        intensity=np.arange(n, dtype=float), variance=np.arange(n, dtype=float),
        mask=np.ones(n, dtype=bool), point_flags=[], spectrum_flags=[],
    )


def _record() -> scan.ScanRecord:
    n = 3
    return scan.ScanRecord(
        experiment_id="E", scan_number=1, scan_type="measurement", scan_type_source="declared",
        scan_axes=[], spectra={"ana1": _spectrum("ana1"), "ana2": _spectrum("ana2")},
        normalization=scan.Normalization(counting_mode="monitor", monitor=np.full(n, 4.0e6),
                                         time=np.full(n, 60.0), preset=4.0e6),
        temperature=scan.Temperature(setpoint=np.full(n, 290.0), sensors={},
                                     valid_mask=np.ones(n, dtype=bool)),
        environment=scan.Environment(), geometry=None,
        energy_calibration=scan.EnergyCalibration(raw_abscissa_kind="energy", transform=None,
                                                  elastic_zero_offset=None, status="needs_zero"),
        scan_flags=[], raw_header={},
        source=scan.Source(file="f", byte_range=None, line_range=None, plugin="p",
                           plugin_version="0", format_version="0", schema_version=scan.SCHEMA_VERSION),
    )


def test_record_written_before_new_fields_loads_with_defaults():
    d = _record().to_dict()
    d.pop("facts")
    for sp in d["spectra"].values():
        for key in ("facts", "monitor", "time"):
            sp.pop(key)
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(d)))
    assert restored.facts == {}
    assert restored.spectra["ana1"].facts == {}
    assert restored.spectra["ana1"].monitor is None and restored.spectra["ana1"].time is None


def test_facts_rung4_q_and_spectrum_exposure_round_trip():
    rec = _record()
    rec.facts = {"scan_numbers": {"value": [58], "source": "header"},
                 "slit": {"value": "60x60", "source": "card",
                          "conflicts": [{"source": "header", "value": "30x30"}]}}
    sp = rec.spectra["ana1"]
    sp.q, sp.q_source = None, "none"                      # rung 4: analyzer label only
    sp.facts = {"hkl": {"value": None, "source": "missing"}}
    sp.monitor = np.full(3, np.nan)                       # wholly missing exposure (Ba122)
    sp.time = np.array([10.0, 10.0, 10.0])
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert scan.records_close(rec, restored)
    assert restored.facts == rec.facts and restored.spectra["ana1"].q is None
    assert restored.spectra["ana1"].facts == {"hkl": {"value": None, "source": "missing"}}
    assert np.isnan(restored.spectra["ana1"].monitor).all()


def test_exposure_for_applies_spectrum_override_and_falls_back_to_record():
    rec = _record()
    rec.spectra["ana1"].monitor = np.full(3, 2.0e6)
    own = scan.exposure_for(rec, "ana1")
    assert np.all(own.monitor == 2.0e6)
    assert own.time is rec.normalization.time             # no time override: the record's time
    assert scan.exposure_for(rec, "ana2") is rec.normalization
