"""TAVI parser (§6) — the McStas triple-axis SIMULATOR plugin.

Input is one JSON per completed scan (the saved `GET /scan/{id}/data` body). These tests pin the
classification, the constant-Q-energy axis shape, RAW-COUNTS spectra with honest Poisson variance and
never-dropped masked points, the reported Q, the absolute energy status, and the Cooper-Nathans
header (raw_header["params"]) with its derived KFIX/FX — plus matches(), name-registry resolution,
the emit→parse round-trip, and that the shipped card loads through the Card machinery.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from isar.calibrate import tas
from isar.parsers import registry, tavi
from isar.schema import axes
from isar.schema.card import Card
from isar.synth_format import run_builder


# ---------------------------------------------------------------------------
# Fixtures — realistic TAVI scan JSONs
# ---------------------------------------------------------------------------
def _base_parameters(**over):
    p = {
        "H": 2.0, "K": 0.0, "L": 0.0, "deltaE": 0.0,
        "Ki": 2.662, "Ei": 14.7, "Kf": 2.662, "Ef": 14.7,
        "K_fixed": "Kf Fixed", "fixed_E": 14.7,
        "monocris": "pg002", "anacris": "pg002",
        "lattice_a": 3.94, "lattice_b": 3.94, "lattice_c": 3.94,
        "lattice_alpha": 90.0, "lattice_beta": 90.0, "lattice_gamma": 90.0,
        "collimation": {"alpha_1": "40", "alpha_2": ["40", "60"], "alpha_3": "30", "alpha_4": "30"},
        "number_neutrons": 20000,
    }
    p.update(over)
    return p


def _job(job_id, result, parameters):
    return {
        "job_id": job_id, "source": "api", "state": "done",
        "submitted_at": 1783078264.6, "started_at": 1783078265.0, "finished_at": 1783078290.0,
        "progress": {"done": len(result.get("scan_values_1") or []),
                     "total": len(result.get("scan_values_1") or [])},
        "error": None,
        "launch": {"scan_command1": "deltaE -2 2 1", "scan_command2": "",
                   "number_neutrons": 20000, "isolated": True, "parameters": parameters},
        "result": result,
    }


def _deltae_json():
    values = [-2.0, -1.0, 0.0, 1.0, 2.0]
    counts = [5.0, 12.0, 40.0, 11.0, 6.0]           # a peak at deltaE = 0
    result = {
        "mode": "1D", "variable_1": "deltaE", "variable_2": None,
        "total_counts": sum(counts), "max_counts": max(counts),
        "output_folder": "out/j-0001", "skipped_points": [],
        "scan_values_1": values, "scan_values_2": None,
        "valid_mask_1": [True] * 5,
        "counts": counts, "counts_grid": None, "metadata": {},
    }
    return _job("j-0001", result, _base_parameters())


def _hscan_elastic_json():
    values = [1.98, 1.99, 2.0, 2.01, 2.02]
    counts = [8.0, 30.0, 95.0, 28.0, 7.0]
    result = {
        "mode": "1D", "variable_1": "H", "variable_2": None,
        "total_counts": sum(counts), "max_counts": max(counts),
        "output_folder": "out/j-0002", "skipped_points": [],
        "scan_values_1": values, "scan_values_2": None,
        "valid_mask_1": [True] * 5,
        "counts": counts, "counts_grid": None, "metadata": {},
    }
    return _job("j-0002", result, _base_parameters(deltaE=0.0))


def _write(tmp_path, name, obj) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


def _card() -> Card:
    return tavi.card_template()


# ---------------------------------------------------------------------------
# parse_file — deltaE measurement
# ---------------------------------------------------------------------------
def test_deltae_scan_is_a_measurement_with_constant_q_energy_axes(tmp_path):
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", _deltae_json()), _card())[0]
    assert rec.scan_type == "measurement"
    assert rec.scan_type_source == "inferred"
    assert rec.scan_number == 1
    scanned = axes.scanned_axes(rec)
    assert len(scanned) == 1 and scanned[0].kind == "energy"
    assert np.allclose(scanned[0].values, [-2, -1, 0, 1, 2])
    axes.assert_constant_q_energy(rec)              # one scanned energy + three constant Q axes


def test_deltae_spectrum_raw_counts_variance_mask_and_q(tmp_path):
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", _deltae_json()), _card())[0]
    sp = rec.spectra["det"]
    assert np.allclose(sp.intensity, [5, 12, 40, 11, 6])
    assert np.allclose(sp.variance, sp.intensity)   # honest Poisson: variance = counts
    assert sp.mask.all()
    assert sp.q == [2.0, 0.0, 0.0] and sp.q_source == "reported"


def test_energy_calibration_is_absolute(tmp_path):
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", _deltae_json()), _card())[0]
    assert rec.energy_calibration.status == "absolute"
    assert rec.energy_calibration.raw_abscissa_kind == "energy"
    assert rec.normalization.monitor.tolist() == [20000.0] * 5     # source budget per point
    assert rec.normalization.counting_mode == "monitor"


def test_cooper_nathans_params_present_and_derived(tmp_path):
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", _deltae_json()), _card())[0]
    p = rec.raw_header["params"]
    # d-spacings from the crystal-id map, cell from lattice constants, Q from H/K/L.
    assert p["DM"] == pytest.approx(3.355) and p["DA"] == pytest.approx(3.355)
    assert (p["AS"], p["BS"], p["CS"]) == (3.94, 3.94, 3.94)
    assert (p["QH"], p["QK"], p["QL"]) == (2.0, 0.0, 0.0)
    # Kf Fixed → FX = 2, KFIX = Kf.
    assert p["FX"] == 2 and p["KFIX"] == pytest.approx(2.662)
    # Horizontal collimation from the payload (alpha_2 multi-select → tightest = 40); vertical from card.
    assert (p["ALF1"], p["ALF2"], p["ALF3"], p["ALF4"]) == (40.0, 40.0, 30.0, 30.0)
    assert p["BET1"] == 30.0
    # Mosaics + senses from the card constants.
    assert (p["ETAM"], p["ETAA"], p["ETAS"]) == (30.0, 30.0, 0.0)
    assert (p["SM"], p["SS"], p["SA"]) == (1, -1, 1)
    # The whole payload is retained.
    assert rec.raw_header["tavi"]["job_id"] == "j-0001"
    # And the header actually drives the CN resolution engine to a finite positive FWHM.
    fwhm = tas.cn_energy_fwhm(rec, w=0.0)
    assert np.isfinite(fwhm) and fwhm > 0


def test_ki_fixed_derives_fx1_and_kfix_from_ki(tmp_path):
    obj = _deltae_json()
    obj["launch"]["parameters"] = _base_parameters(K_fixed="Ki Fixed", Ki=1.55, Ei=4.98)
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", obj), _card())[0]
    p = rec.raw_header["params"]
    assert p["FX"] == 1 and p["KFIX"] == pytest.approx(1.55)
    assert rec.geometry.fixed_mode == "ki"


# ---------------------------------------------------------------------------
# parse_file — H-scan elastic (calibration) + masked points
# ---------------------------------------------------------------------------
def test_elastic_h_scan_classifies_as_calibration(tmp_path):
    rec = tavi.parse_file(_write(tmp_path, "j-0002.json", _hscan_elastic_json()), _card())[0]
    assert rec.scan_type == "calibration"
    scanned = axes.scanned_axes(rec)
    assert len(scanned) == 1 and scanned[0].kind == "q_h"
    assert np.allclose(scanned[0].values, [1.98, 1.99, 2.0, 2.01, 2.02])
    assert rec.scan_number == 2


def test_masked_points_are_kept_not_dropped(tmp_path):
    obj = _deltae_json()
    obj["result"]["counts"] = [5.0, None, 40.0, 11.0, 6.0]     # one infeasible/skipped point
    obj["result"]["valid_mask_1"] = [True, False, True, True, True]
    obj["result"]["skipped_points"] = [1]
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", obj), _card())[0]
    sp = rec.spectra["det"]
    assert len(sp.intensity) == 5                    # never silently dropped
    assert sp.mask.tolist() == [True, False, True, True, True]
    assert np.isnan(sp.intensity[1])
    assert any(pf.index == 1 for pf in sp.point_flags)


# ---------------------------------------------------------------------------
# 2D scans are out of scope for v1
# ---------------------------------------------------------------------------
def test_2d_scan_raises_clear_error(tmp_path):
    obj = _deltae_json()
    obj["result"]["mode"] = "2D"
    obj["result"]["variable_2"] = "K"
    obj["result"]["counts_grid"] = [[1, 2], [3, 4]]
    with pytest.raises(ValueError, match="2D"):
        tavi.parse_file(_write(tmp_path, "j-0003.json", obj), _card())


# ---------------------------------------------------------------------------
# matches() + registry
# ---------------------------------------------------------------------------
def test_matches_accepts_tavi_json_and_rejects_others(tmp_path):
    good = _write(tmp_path, "scan.json", _deltae_json())
    assert tavi.matches(good) is True
    bad = _write(tmp_path, "random.json", {"foo": 1, "bar": [2, 3]})   # no job_id/result
    assert tavi.matches(bad) is False
    txt = tmp_path / "notjson.txt"
    txt.write_text("hello", encoding="utf-8")
    assert tavi.matches(txt) is False


def test_registry_resolves_tavi_by_name():
    assert registry.plugin_by_name("tavi") is tavi


# ---------------------------------------------------------------------------
# emit → parse round-trip (Gate A)
# ---------------------------------------------------------------------------
def test_emit_parse_round_trip(tmp_path):
    card = _card()
    run = run_builder.build_in8_run("SIM-RT", n_scans=1, seed0=2024)
    sc = run.scans[0]
    d = sc.detectors[0]
    path = tmp_path / "rt.json"
    path.write_bytes(tavi.emit_file(run, card))

    assert tavi.matches(path)
    rec = tavi.parse_file(path, card)[0]
    assert rec.scan_type == "measurement"
    assert set(rec.spectra) == {"det"}
    energy = axes.scanned_axes(rec)[0].values
    assert np.allclose(energy, sc.energy)
    sp = rec.spectra["det"]
    assert np.allclose(sp.intensity, d.counts)
    assert np.allclose(sp.variance, d.counts)
    assert np.allclose(sp.q, d.q)
    assert np.allclose(rec.normalization.monitor, sc.monitor)


# ---------------------------------------------------------------------------
# card template loads through the Card machinery
# ---------------------------------------------------------------------------
def test_card_template_loads_and_validates():
    card = tavi.card_template()
    assert isinstance(card, Card)
    assert card.policy.plugin == "tavi"
    assert card.policy.facility == "TAVI" and card.policy.instrument == "PUMA"
    assert card.policy.calibration["method"] == "cooper_nathans"
    assert card.validate() == []
    assert card.policy.detectors[0].detector_id == "det"
