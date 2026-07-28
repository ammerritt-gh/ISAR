"""TAVI parser (§6) — the McStas triple-axis SIMULATOR plugin.

Input is one JSON per completed scan (the saved `GET /scan/{id}/data` body). These tests pin the
classification, the constant-Q-energy axis shape, RAW-COUNTS spectra with honest Poisson variance and
never-dropped masked points, the reported Q, the absolute energy status, and the Cooper-Nathans
header (raw_header["params"]) with its derived KFIX/FX — plus matches(), name-registry resolution,
the emit→parse round-trip, and that the shipped card loads through the Card machinery.

They also pin the BACKGROUND TRUTH FIREWALL: a simulator payload's planted background numerics never
reach the record (only the safe provenance/fingerprint half does), the raw JSON on disk keeps them for
post-fit validation scoring, and a payload without a background block parses exactly as it did before.
"""
import json
from pathlib import Path

import numpy as np
import pytest

from isar.calibrate import tas
from isar.parsers import registry, tavi
from isar.schema import axes, scan
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


def _truth_over():
    """The per-scan instrument-truth fields TAVI's payload now carries (verified live 2026-07-06)."""
    return dict(
        temperature=200.0, eta_m=35, eta_a=35, eta_s=35,
        sense_m=1, sense_s=-1, sense_a=1,
        beta_1=120.0, beta_2=120.0, beta_3=120.0, beta_4=120.0,
        sample_key="Al_phonon_DFT",
    )


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
    assert (p["ETAM"], p["ETAA"]) == (30.0, 30.0)
    assert "ETAS" not in p  # zero card mosaic is omitted so CN reuses ETAM
    assert (p["SM"], p["SS"], p["SA"]) == (1, -1, 1)
    # The whole payload is retained.
    assert rec.raw_header["tavi"]["job_id"] == "j-0001"
    # And the header actually drives the CN resolution engine to a finite positive FWHM.
    fwhm = tas.cn_energy_fwhm(rec, w=0.0)
    assert np.isfinite(fwhm) and fwhm > 0


def test_payload_truth_overrides_card_constants(tmp_path):
    # Payload carries per-scan instrument truth → those values (and payload provenance) are used,
    # not the card constants, and no card_fallback flag fires.
    obj = _deltae_json()
    obj["launch"]["parameters"] = _base_parameters(**_truth_over())
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", obj), _card())[0]
    p = rec.raw_header["params"]
    assert (p["ETAM"], p["ETAA"]) == (35.0, 35.0)
    assert p["ETAS"] == 35.0                          # positive payload eta_s is kept, not omitted
    assert (p["SM"], p["SS"], p["SA"]) == (1, -1, 1)
    assert (p["BET1"], p["BET2"], p["BET3"], p["BET4"]) == (120.0, 120.0, 120.0, 120.0)
    assert rec.temperature.setpoint.tolist() == [200.0] * 5
    prov = rec.raw_header["provenance"]
    assert prov["eta_m"] == prov["eta_a"] == prov["eta_s"] == "payload"
    assert prov["senses"] == prov["beta"] == prov["temperature"] == "payload"
    assert rec.raw_header["sample_key"] == "Al_phonon_DFT" and prov["sample_key"] == "payload"
    assert not any(f.type == "card_fallback" for f in rec.scan_flags)
    # And the payload-truth header still drives CN to a finite positive FWHM.
    fwhm = tas.cn_energy_fwhm(rec, w=0.0)
    assert np.isfinite(fwhm) and fwhm > 0


def test_legacy_payload_falls_back_to_card_with_provenance_flag(tmp_path):
    # A legacy JSON with none of the truth fields parses exactly as before: card constants throughout,
    # card provenance, and one info-level card_fallback flag naming the fallen-back fields.
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", _deltae_json()), _card())[0]
    p = rec.raw_header["params"]
    assert (p["ETAM"], p["ETAA"]) == (30.0, 30.0)
    assert "ETAS" not in p
    assert (p["SM"], p["SS"], p["SA"]) == (1, -1, 1)
    assert rec.temperature.setpoint.tolist() == [300.0] * 5
    prov = rec.raw_header["provenance"]
    assert prov["eta_m"] == prov["senses"] == prov["beta"] == prov["temperature"] == "card"
    assert rec.raw_header["sample_key"] is None and prov["sample_key"] == "absent"
    flags = [f for f in rec.scan_flags if f.type == "card_fallback"]
    assert len(flags) == 1 and flags[0].severity == "info"
    assert "eta_m" in flags[0].reason and "temperature" in flags[0].reason


def test_payload_zero_eta_s_is_omitted_so_cn_computes(tmp_path):
    # A payload eta_s of 0 must still be omitted (same div-by-zero guard as the zero card mosaic) so
    # Cooper-Nathans reuses ETAM instead of hitting the 0.6 meV fallback.
    obj = _deltae_json()
    obj["launch"]["parameters"] = _base_parameters(eta_m=35, eta_a=35, eta_s=0)
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", obj), _card())[0]
    p = rec.raw_header["params"]
    assert "ETAS" not in p
    assert (p["ETAM"], p["ETAA"]) == (35.0, 35.0)     # payload mosaics still preferred
    assert rec.raw_header["provenance"]["eta_s"] == "payload"   # the (omitted) 0 came from the payload
    fwhm = tas.cn_energy_fwhm(rec)
    assert fwhm != tas.FALLBACK_FWHM and 0.1 < fwhm < 5.0


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
# Background truth firewall — safe provenance on the record, planted numerics redacted
# ---------------------------------------------------------------------------
def _background_block():
    """TAVI's enabled-form `result.metadata.background` block (tavi/background.py metadata_block())."""
    return {
        "background_schema": "tavi.background/1",
        "preset_registry_version": 1,
        "enabled": True,
        "preset": "flat_high",
        "source": "config_default",
        "overrides_applied": {},
        "terms": [{"name": "flat", "shape": "flat", "origin": "instrument", "method": "analytic",
                   "params": {"rate": 5.0e-9}, "optional": False, "units": "counts/monitor"}],
        "parameter_units": {"rate": "counts/monitor"},
        "sample_scale": None,
        "skipped_terms": [],
        "profile_fingerprint": "0123456789abcdef",
        "effective_fingerprint": "fedcba9876543210",
    }


def _with_background(obj, block=None):
    obj["result"]["metadata"] = {"background": block if block is not None else _background_block()}
    # TAVI's launch summary echoes the REQUESTED spec too (preset name; full numeric terms for the
    # frozen form campaigns stamp) — the second leak path the redaction must close. Caught live on
    # 2026-07-27: the metadata block was redacted while launch.background walked the preset name
    # straight onto the record.
    obj["launch"]["background"] = {"enabled": True, "preset": "flat_high"}
    obj["launch"]["background_source"] = "config_default"
    return obj


def test_background_provenance_carries_exactly_the_safe_keys(tmp_path):
    obj = _with_background(_deltae_json())
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", obj), _card())[0]
    prov = rec.raw_header["background_provenance"]
    assert set(prov) == set(tavi.BACKGROUND_SAFE_KEYS)
    assert prov == {"enabled": True, "source": "config_default",
                    "delivery_source": None, "catalog_version": None,
                    "background_schema": "tavi.background/1",
                    "profile_fingerprint": "0123456789abcdef",
                    "effective_fingerprint": "fedcba9876543210"}
    # No magnitude may ride along — not a rate, not a width, not the preset NAME.
    assert "preset" not in prov and "terms" not in prov and "sample_scale" not in prov


def test_background_numerics_are_redacted_from_the_stored_payload(tmp_path):
    obj = _with_background(_deltae_json())
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", obj), _card())[0]
    stored = rec.raw_header["tavi"]["result"]["metadata"]["background"]
    assert stored == {"redacted": "sim_truth", "effective_fingerprint": "fedcba9876543210"}
    # The planted numbers exist nowhere on the record (physical firewall, not a naming convention).
    # `params` is checked against the stored payload only — raw_header["params"] is the unrelated
    # Cooper-Nathans header.
    payload = json.dumps(rec.raw_header["tavi"])
    for forbidden in ("terms", "params", "flat_high", "5e-09", "5.0e-09", "parameter_units"):
        assert forbidden not in payload
    assert "flat_high" not in json.dumps(rec.raw_header["background_provenance"])
    assert rec.raw_header["tavi"]["job_id"] == "j-0001"     # the rest of the payload is intact


def test_launch_background_spec_is_redacted_too(tmp_path):
    # The launch summary's requested spec is truth in its own right: the preset form leaks the
    # name, the frozen form (campaign stamping) leaks every numeric term.
    obj = _with_background(_deltae_json())
    obj["launch"]["background"] = {"enabled": True, "terms": [
        {"name": "flat", "shape": "flat", "origin": "instrument", "params": {"rate": 5.0e-9}}]}
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", obj), _card())[0]
    launch = rec.raw_header["tavi"]["launch"]
    assert launch["background"] == {"redacted": "sim_truth",
                                    "effective_fingerprint": "fedcba9876543210"}
    assert launch["background_source"] == "config_default"   # delivery tag is safe and kept
    assert launch["scan_command1"] == "deltaE -2 2 1"        # the rest of launch is intact


def test_background_redaction_does_not_mutate_the_parsed_payload_on_disk(tmp_path):
    # The record is redacted; the raw JSON file is untouched, because validation scoring reads the
    # planted truth there AFTER fitting.
    path = _write(tmp_path, "j-0001.json", _with_background(_deltae_json()))
    tavi.parse_file(path, _card())
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["result"]["metadata"]["background"]["terms"][0]["params"]["rate"] == 5.0e-9


def test_disabled_background_block_still_records_provenance(tmp_path):
    disabled = {"background_schema": "tavi.background/1", "enabled": False, "preset": "none",
                "source": "config_default", "profile_fingerprint": "aaaa000011112222",
                "effective_fingerprint": "bbbb333344445555"}
    rec = tavi.parse_file(
        _write(tmp_path, "j-0001.json", _with_background(_deltae_json(), disabled)), _card())[0]
    prov = rec.raw_header["background_provenance"]
    assert prov["enabled"] is False and prov["effective_fingerprint"] == "bbbb333344445555"
    assert rec.raw_header["tavi"]["result"]["metadata"]["background"]["redacted"] == "sim_truth"


def test_launch_only_background_is_redacted_and_pools_as_unknown(tmp_path):
    # A malformed/interrupted job: the launch summary records the REQUESTED spec (here the frozen
    # form a campaign stamps — every numeric term) but the result block never landed. Redacting the
    # launch spec only when a result block existed left those numerics on the record.
    from isar.reduce import collate

    obj = _deltae_json()
    obj["launch"]["background"] = {"enabled": True, "terms": [
        {"name": "flat", "shape": "flat", "origin": "instrument", "params": {"rate": 5.0e-9}}]}
    obj["launch"]["background_source"] = "per_scan_override"
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", obj), _card())[0]

    assert rec.raw_header["tavi"]["launch"]["background"] == {
        "redacted": "sim_truth", "effective_fingerprint": None}
    assert "5e-09" not in json.dumps(rec.raw_header["tavi"])
    assert rec.raw_header["tavi"]["launch"]["scan_command1"] == "deltaE -2 2 1"
    # No result block to redact — it stays exactly as the payload had it.
    assert "background" not in (rec.raw_header["tavi"]["result"].get("metadata") or {})

    prov = rec.raw_header["background_provenance"]
    assert set(prov) == set(tavi.BACKGROUND_SAFE_KEYS)
    assert prov == {"enabled": None, "source": "per_scan_override", "background_schema": None,
                    "delivery_source": None, "catalog_version": None,
                    "profile_fingerprint": None, "effective_fingerprint": None}
    # Fingerprint-less provenance fails CLOSED at collation rather than pooling as background-free.
    assert collate.background_pool_key(rec.raw_header) == collate.UNKNOWN_BACKGROUND


def test_current_v2_background_redacts_cosmic_events_seed_and_source_numerics(tmp_path):
    block = {
        "background_schema": "tavi.background/2",
        "catalog_version": 2,
        "enabled": True,
        "delivery_source": "per_scan_override",
        "sources": {
            "environment_cosmic_spikes": {
                "enabled": True, "scale": 2.0, "shape": "cosmic_spikes",
                "base_numerics": {
                    "events_per_1e10_neutrons": 0.005,
                    "amplitude_median_counts": 1000,
                    "amplitude_log_sigma": 0.8,
                    "amplitude_cap_counts": 100000,
                },
            },
            "instrument_aluminum_powder": {
                "enabled": True, "scale": 0.5, "shape": "powder_elastic",
                "base_numerics": {
                    "lattice_constant_ang": 4.0495,
                    "q_centers_inv_ang": [2.687, 3.103, 4.389, 5.146, 5.375, 6.206],
                    "rate_integrated_per_line": 1.5e-8,
                },
            },
        },
        "seed": 12345,
        "realized_events": [{
            "point_index": 47,
            "source_id": "environment_cosmic_spikes",
            "event_count": 1,
            "added_counts": 1437,
        }],
        "profile_fingerprint": "v2-profile",
        "effective_fingerprint": "v2-effective",
    }
    obj = _with_background(_deltae_json(), block)
    obj["launch"]["background"] = {
        "catalog_version": 2, "enabled": True,
        "sources": {
            "environment_cosmic_spikes": {"enabled": True, "scale": 2.0},
            "instrument_aluminum_powder": {"enabled": True, "scale": 0.5},
        },
    }
    rec = tavi.parse_file(_write(tmp_path, "j-0002.json", obj), _card())[0]

    assert rec.raw_header["background_provenance"] == {
        "enabled": True,
        "source": None,
        "delivery_source": "per_scan_override",
        "background_schema": "tavi.background/2",
        "catalog_version": 2,
        "profile_fingerprint": "v2-profile",
        "effective_fingerprint": "v2-effective",
    }
    stored = json.dumps(rec.raw_header["tavi"])
    for forbidden in (
        "environment_cosmic_spikes", "instrument_aluminum_powder", "realized_events",
        "point_index", "added_counts", "1437", '"seed": 12345', "base_numerics",
        "q_centers_inv_ang", "2.687", '"scale": 2.0',
    ):
        assert forbidden not in stored
    assert rec.raw_header["tavi"]["result"]["metadata"]["background"] == {
        "redacted": "sim_truth", "effective_fingerprint": "v2-effective"}


def test_historical_v2_catalog1_background_remains_readable_and_redacted(tmp_path):
    """The catalog moved; historical tavi.background/2 scan files remain valid evidence."""
    block = {
        "background_schema": "tavi.background/2",
        "catalog_version": 1,
        "enabled": True,
        "delivery_source": "per_scan_override",
        "sources": {
            "sample_diffuse": {
                "enabled": True, "scale": 3.0,
                "shape": "flat", "base_numerics": {"rate": 1.5e-9},
            },
        },
        "profile_fingerprint": "catalog1-profile",
        "effective_fingerprint": "catalog1-effective",
    }
    obj = _with_background(_deltae_json(), block)
    obj["launch"]["background"] = {
        "catalog_version": 1, "enabled": True,
        "sources": {"sample_diffuse": {"enabled": True, "scale": 3.0}},
    }

    rec = tavi.parse_file(_write(tmp_path, "j-catalog1.json", obj), _card())[0]

    assert rec.raw_header["background_provenance"]["catalog_version"] == 1
    assert rec.raw_header["background_provenance"]["effective_fingerprint"] == "catalog1-effective"
    stored = json.dumps(rec.raw_header["tavi"])
    assert "sample_diffuse" not in stored and "1.5e-09" not in stored
    assert rec.raw_header["tavi"]["result"]["metadata"]["background"] == {
        "redacted": "sim_truth", "effective_fingerprint": "catalog1-effective"}


def test_payload_without_background_block_is_unchanged(tmp_path):
    # Older TAVI and every real instrument: no provenance key, and the stored payload is the payload.
    path = _write(tmp_path, "j-0001.json", _deltae_json())
    rec = tavi.parse_file(path, _card())[0]
    assert "background_provenance" not in rec.raw_header
    assert rec.raw_header["tavi"] == json.loads(path.read_text(encoding="utf-8"))


def test_real_instrument_parsers_never_stamp_background_provenance(tmp_path):
    # Cross-parser parity: background provenance is a simulator concept. A real-numor record must be
    # byte-identical to what it was before the firewall existed — no key, no redaction.
    from isar.parsers import ill_in8

    in8_card = ill_in8.card_template()
    run = run_builder.build_in8_run("FIREWALL", n_scans=1, seed0=3)
    path = tmp_path / "FIREWALL.numor"
    path.write_bytes(ill_in8.emit_file(run, in8_card))
    for rec in ill_in8.parse_file(path, in8_card):
        assert "background_provenance" not in rec.raw_header


def test_background_provenance_survives_the_record_json_round_trip(tmp_path):
    rec = tavi.parse_file(
        _write(tmp_path, "j-0001.json", _with_background(_deltae_json())), _card())[0]
    back = scan.ScanRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert back.raw_header["background_provenance"] == rec.raw_header["background_provenance"]
    assert back.raw_header["tavi"]["result"]["metadata"]["background"]["redacted"] == "sim_truth"


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


# ---------------------------------------------------------------------------
# registry detection + Cooper-Nathans regressions (post-integration fixes)
# ---------------------------------------------------------------------------
def test_registry_detect_recognizes_tavi_json(tmp_path):
    path = _write(tmp_path, "j-0001.json", _deltae_json())
    assert registry.detect(path) == ("TAVI", "PUMA", "v1")
    assert registry.default_plugin_name("TAVI", "PUMA", "v1") == "tavi"


def test_registry_detect_rejects_foreign_json(tmp_path):
    path = _write(tmp_path, "other.json", {"foo": 1})
    with pytest.raises(ValueError):
        registry.detect(path)


def test_zero_sample_mosaic_omits_etas_so_cn_computes(tmp_path):
    # A zero ETAS divides by zero inside cn_resolution_matrix; the plugin must
    # omit it (cn then reuses ETAM) so the resolution is computed, not the
    # 0.6 meV fallback.
    rec = tavi.parse_file(_write(tmp_path, "j-0001.json", _deltae_json()), _card())[0]
    assert "ETAS" not in rec.raw_header["params"]
    fwhm = tas.cn_energy_fwhm(rec)
    assert fwhm != tas.FALLBACK_FWHM
    assert 0.1 < fwhm < 5.0
