"""Gate B — file smoke for the IN8 numor parser (§4.1).

Parses the committed artificial copies of real numors (`artificial_data/`: the facility's own header
and layout, every data-row number replaced) and asserts the records are sane (single det0, raw
counts, fixed-kf geometry, identity keyed on EXPNO so scans group across files):

  * a `bs qh` numor → one `det0` *measurement* with a scanned energy axis + constant Q from COMND;
  * a `sc a3` numor → an *alignment* with an angle axis and no constant-Q assertion;
  * raw CNTS are the signal (monitor stored, not divided), temperature setpoint = header RT, sensor = TT;
  * the record survives the canonical JSON store.

Values the parser takes from a data column (energy, counts, monitor, the TT sensor, the scanned
motor) are asserted against the copy's truth file, i.e. what the copy tool wrote there; header
values (RT, KFIX, EXPNO, COMND) are the facility's own and stay literal.
"""
import json

import numpy as np

from artificial import IN8_ALIGN as ALIGN, IN8_MEAS as MEAS, scan_truth, values
from isar.parsers import ill_in8
from isar.schema import axes, scan

MEAS_TRUTH = scan_truth(MEAS, 55181)
ALIGN_TRUTH = scan_truth(ALIGN, 55177)


def test_bs_qh_numor_is_a_single_detector_measurement():
    card = ill_in8.card_template()
    recs = ill_in8.parse_file(MEAS, card)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.scan_type == "measurement"
    assert rec.experiment_id == "DIR-397"            # EXPNO, shared across numor files
    assert rec.scan_number == 55181                  # FILE_ numor
    assert set(rec.spectra) == {"det0"}

    axes.assert_constant_q_energy(rec)
    energy = axes.scanned_axes(rec)[0].values
    assert len(energy) == MEAS_TRUTH["points"] == 41 and energy[0] < energy[-1]
    assert np.allclose(energy, values(MEAS_TRUTH, "EN"))

    sp = rec.spectra["det0"]
    assert np.allclose(sp.q, [4.0, -0.2, 0.0])       # constant Q from `bs qh 4 -0.2 0` (the header)
    assert np.all(sp.intensity >= 0)
    assert np.allclose(sp.intensity, values(MEAS_TRUTH, "CNTS"))   # raw counts are the signal
    assert np.allclose(sp.variance, sp.intensity)    # raw counts: variance = N (Poisson √N)
    assert np.all(sp.intensity == sp.intensity.astype(int))   # integer counts, not normalized rates


def test_geometry_is_fixed_kf_and_energy_absolute():
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(MEAS, card)[0]
    assert rec.geometry.fixed_mode == "kf"
    assert np.isclose(rec.geometry.fixed_value, 2.66202)
    assert rec.energy_calibration.raw_abscissa_kind == "energy"
    assert rec.energy_calibration.status == "absolute"


def test_normalization_keeps_monitor_and_counting_mode():
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(MEAS, card)[0]
    norm = rec.normalization
    assert norm.counting_mode == "monitor"           # bs qh ... mn 100000
    assert len(norm.monitor) == 41
    assert np.allclose(norm.monitor, values(MEAS_TRUTH, "M1"))      # M1 stored
    assert np.allclose(norm.time, values(MEAS_TRUTH, "TIME"))


def test_temperature_setpoint_from_header_rt_sensor_from_tt():
    # 055181: setpoint RT from the header (≈300); the TT sensor is a DATA COLUMN, so it is asserted
    # against what the copy tool wrote there. The disconnected case (055177) is below.
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(MEAS, card)[0]
    assert np.isclose(rec.temperature.setpoint[0], 300.03, atol=0.5)
    assert "TT" in rec.temperature.sensors
    assert np.allclose(rec.temperature.sensors["TT"].values, values(MEAS_TRUTH, "TT"))


def test_sc_a3_numor_is_alignment_with_angle_axis():
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(ALIGN, card)[0]
    assert rec.scan_type == "alignment"
    assert set(rec.spectra) == {"det0"}
    scanned = axes.scanned_axes(rec)
    assert len(scanned) == 1 and scanned[0].kind == "angle"
    assert scanned[0].name == "A3"
    assert np.allclose(scanned[0].values, values(ALIGN_TRUTH, "A3"))   # first A3 = the scanned motor
    # disconnected sensor: header setpoint RT=221.57 (the header) vs the TT data column.
    assert np.isclose(rec.temperature.setpoint[0], 221.57, atol=0.5)
    assert np.allclose(rec.temperature.sensors["TT"].values, values(ALIGN_TRUTH, "TT"))


def test_in8_record_survives_json_round_trip():
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(MEAS, card)[0]
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert scan.records_close(rec, restored)
