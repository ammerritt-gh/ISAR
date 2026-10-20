"""PROOF (real data, run on purpose): Gate B — real-file smoke for the IN8 numor parser (§4.1).

Parses the real numors and asserts the records are sane (single det0, raw counts, fixed-kf
geometry, identity keyed on EXPNO so scans group across files):

  * a `bs qh` numor → one `det0` *measurement* with a scanned energy axis + constant Q from COMND;
  * a `sc a3` numor → an *alignment* with an angle axis and no constant-Q assertion;
  * raw CNTS are the signal (monitor stored, not divided), temperature setpoint = header RT, sensor = TT;
  * the record survives the canonical JSON store.
"""
import json

import numpy as np

from isar.parsers import ill_in8
from isar.schema import axes, scan

FOLDER = "ILL IN8"
MEAS = "055181"        # bs qh 4 -0.2 0 1 ... mn 100000
ALIGN = "055177"       # sc a3 0 da3 2 np 31 ti 10  (disconnected sensor: header TT=0, RT=221.57)


def test_bs_qh_numor_is_a_single_detector_measurement(real_file, expect):
    card = ill_in8.card_template()
    recs = ill_in8.parse_file(real_file(FOLDER, MEAS), card)
    assert len(recs) == 1
    rec = recs[0]
    assert rec.scan_type == "measurement"
    assert rec.experiment_id == "DIR-397"            # EXPNO, shared across numor files
    assert rec.scan_number == 55181                  # FILE_ numor
    assert set(rec.spectra) == {"det0"}

    axes.assert_constant_q_energy(rec)
    energy = axes.scanned_axes(rec)[0].values
    assert len(energy) == expect(FOLDER)["n_points_measurement"] and energy[0] < energy[-1]

    sp = rec.spectra["det0"]
    assert np.allclose(sp.q, [4.0, -0.2, 0.0])       # constant Q from `bs qh 4 -0.2 0`
    assert np.all(sp.intensity >= 0)
    assert np.allclose(sp.variance, sp.intensity)    # raw counts: variance = N (Poisson √N)
    assert np.all(sp.intensity == sp.intensity.astype(int))   # integer counts, not normalized rates


def test_geometry_is_fixed_kf_and_energy_absolute(real_file):
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(real_file(FOLDER, MEAS), card)[0]
    assert rec.geometry.fixed_mode == "kf"
    assert np.isclose(rec.geometry.fixed_value, 2.66202)
    assert rec.energy_calibration.raw_abscissa_kind == "energy"
    assert rec.energy_calibration.status == "absolute"


def test_normalization_keeps_monitor_and_counting_mode(real_file, expect):
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(real_file(FOLDER, MEAS), card)[0]
    norm = rec.normalization
    assert norm.counting_mode == "monitor"           # bs qh ... mn 100000
    assert np.all(norm.monitor > 0)                   # M1 stored
    assert len(norm.monitor) == expect(FOLDER)["n_points_measurement"]


def test_temperature_setpoint_from_header_rt_sensor_from_tt(real_file, expect):
    # 055181: header RT≈300, sample TT connected. The disconnected case (055177) is below.
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(real_file(FOLDER, MEAS), card)[0]
    assert np.isclose(rec.temperature.setpoint[0], 300.03, atol=0.5)
    assert "TT" in rec.temperature.sensors
    assert np.isclose(rec.temperature.sensors["TT"].values[0],
                      expect(FOLDER)["tt_sensor_K_measurement"], atol=0.1)


def test_sc_a3_numor_is_alignment_with_angle_axis(real_file, expect):
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(real_file(FOLDER, ALIGN), card)[0]
    assert rec.scan_type == "alignment"
    assert set(rec.spectra) == {"det0"}
    scanned = axes.scanned_axes(rec)
    assert len(scanned) == 1 and scanned[0].kind == "angle"
    assert scanned[0].name == "A3"
    # disconnected sensor: header setpoint RT=221.57 vs the sample TT — captured for QC to flag.
    assert np.isclose(rec.temperature.setpoint[0], 221.57, atol=0.5)
    assert np.isclose(rec.temperature.sensors["TT"].values[0],
                      expect(FOLDER)["tt_sensor_K_alignment"], atol=0.1)


def test_in8_record_survives_json_round_trip(real_file):
    card = ill_in8.card_template()
    rec = ill_in8.parse_file(real_file(FOLDER, MEAS), card)[0]
    restored = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert scan.records_close(rec, restored)
