"""BL43LXU on the tabulated_elastic calibration: per-analyzer curves, slit ladder, elastic zero.

Everything is generated at test time (a BL43LXU set written in the CsV3Sb5 layout with the shipped
card, and a SARF folder with three settings); nothing reads `real_data/`. The calibration pieces are
driven directly (fast); the end-to-end `isar run` cases fit with the tabulated curve and are `slow`.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest

import isar
from intake_fixtures import (ANALYZERS, SETTINGS, STEM, TRUE_SLIT, ZERO,  # the generators, shared with tests/intake
                             curve_values as _curve_values, workspace as _workspace,
                             write_library as _write_library, write_scan as _write_scan)
from isar.intake.curves import NO_FOLDER
from isar.intake.settings import save_resolution_folder
from isar.parsers import metadata, sarf, sp8_bl43
from isar.reduce import combine, qc
from isar.schema import axes
from isar.schema.scan import Flag
from isar.session import calibrations, driver, guidelines as guide_mod, ingest, scan_delivery

PATTERN = sp8_bl43.card_template().policy.calibration["resolution_pattern"]


def _records(card, folder):
    """What `run_batch` hands the calibration: parsed, reduced and QC'd records."""
    recs = ingest.parse_all(ingest.discover([folder], card), card)
    for r in recs:
        combine.reduce_record(r, card)
        qc.qc_record(r, card)
    return recs


def _calibrated(tmp_path, *, slit=None, library_skip=(), analyzers=ANALYZERS, weak=(), lr=(), monitors=None,
                centroids=None):
    """A workspace with data and library; returns (ws, card, records, state entry of the one scan)."""
    ws = _workspace(tmp_path)
    _write_library(ws.root / "rf", skip=library_skip)
    assert save_resolution_folder(ws.root, ws.root / "rf").ok
    _write_scan(tmp_path / "data", analyzers=analyzers, slit=slit, weak=weak, monitors=monitors,
                centroids=centroids)
    card, _session = ws.load()
    recs = _records(card, tmp_path / "data")
    for det in lr:                       # what the reader does to an _lr spectrum
        recs[0].spectra[det].spectrum_flags.append(Flag("counts_not_integer", "warning", "_lr counts"))
        recs[0].spectra[det].facts["count_recovery"]["value"]["family"] = "_lr"
    calibrations.refresh_tabulated(card, recs, base_dir=ws.root)
    (entry,) = card.state.tabulated_calibration.values()
    return ws, card, recs, entry


def _flag_types(rows):
    return [f["type"] for f in rows]


# ---------------------------------------------------------------------------
# the calibration build (fast)
# ---------------------------------------------------------------------------
def test_import_root_is_the_worktree():
    # A sibling checkout on sys.path would make every assertion below test the wrong code.
    assert Path(isar.__file__).resolve().parent.parent == Path(__file__).resolve().parents[2]


def test_undeclared_slit_is_inferred_and_every_analyzer_zero_is_fitted(tmp_path):
    ws, card, recs, entry = _calibrated(tmp_path)
    assert (entry["setting"], entry["setting_source"]) == (TRUE_SLIT, "inferred")
    assert "slit_inferred" in _flag_types(entry["flags"]) and entry["comparison_set"] == list(ANALYZERS)
    assert min(entry["deviances"], key=entry["deviances"].get) == TRUE_SLIT and set(entry["deviances"]) == set(SETTINGS)
    for det in ANALYZERS:
        d = entry["detectors"][det]
        assert d["zero_source"] == "elastic_fit" and abs(d["zero"] - ZERO) < 0.1
        assert d["fit"]["significant"] and d["fit"]["n_points"] > 10 and d["flags"] == [] and "held_reason" not in d
    # State is keyed exp:scan and holds the numbers, nothing else; the policy file was not written.
    assert list(card.state.tabulated_calibration) == [f"{recs[0].experiment_id}:{recs[0].scan_number}"]
    json.dumps(card.state.tabulated_calibration, allow_nan=False)


def test_resolution_version_records_only_method_folder_and_fingerprint(tmp_path):
    ws, card, recs, entry = _calibrated(tmp_path)
    (version,) = card.state.resolution_versions
    assert set(version) == {"method", "folder", "pattern", "library", "version", "_sig"}
    assert version["method"] == "tabulated_elastic" and version["folder"] == str((ws.root / "rf").resolve())  # the saved folder
    assert version["library"] == sarf.fingerprint(ws.root / "rf", PATTERN) == entry["library"]


def test_declared_slit_is_used_and_never_inferred(tmp_path):
    _ws, _card, _recs, entry = _calibrated(tmp_path, slit=(40, 40))
    assert (entry["setting"], entry["setting_source"], entry["declared"]) == (TRUE_SLIT, "declared", TRUE_SLIT)
    assert "slit_inferred" not in _flag_types(entry["flags"]) and "slit_mismatch" not in _flag_types(entry["flags"])


def test_declared_slit_contradicted_by_the_data_is_kept_and_alerted(tmp_path):
    _ws, _card, _recs, entry = _calibrated(tmp_path, slit=(60, 60))
    assert entry["setting"] == "60x60" and entry["setting_source"] == "declared"    # never swapped
    assert "slit_mismatch" in _flag_types(entry["flags"])
    assert all(d["zero_source"] in ("elastic_fit", "pipeline_zero") for d in entry["detectors"].values())


def test_header_slit_and_curve_file_of_the_same_opening_meet(tmp_path):
    # The reader spells the fact with metadata.slit_label; the library spells the setting the same way.
    _ws, _card, recs, entry = _calibrated(tmp_path, slit=(40.0, 40))
    assert recs[0].facts["slit"]["value"] == metadata.slit_label(40, 40) == entry["setting"] == "40x40"
    lib = sarf.load_library(tmp_path / "ws" / "rf", PATTERN)
    assert lib.get("a03", recs[0].facts["slit"]["value"]) is not None


def test_declared_slit_without_a_curve_holds_every_analyzer_with_missing_resolution(tmp_path):
    # r2 leaves a declared slit with no curve unscored and unflagged; the calibration must say so per analyzer.
    _ws, _card, _recs, entry = _calibrated(tmp_path, slit=(30, 30))
    assert entry["setting"] == "30x30" and entry["setting_source"] == "declared"
    for det in ANALYZERS:
        d = entry["detectors"][det]
        assert det in d["held_reason"] and "30x30" in d["held_reason"] and "zero" not in d
        (flag,) = d["flags"]
        assert flag["type"] == "missing_resolution" and det in flag["reason"]


def test_an_analyzer_without_a_curve_at_the_setting_is_held_the_rest_are_fitted(tmp_path):
    ws, card, recs, entry = _calibrated(tmp_path, library_skip=[("a05", "40x40")])
    # a05 lacks a curve at one candidate, so it is out of the comparison set, and the setting still comes
    # from the others; at that setting a05 has no curve and is held, naming itself.
    assert entry["setting"] == TRUE_SLIT and entry["comparison_set"] == ["a03", "a04"]
    held = entry["detectors"]["a05"]
    assert "a05" in held["held_reason"] and TRUE_SLIT in held["held_reason"]
    assert _flag_types(held["flags"]) == ["missing_resolution"] and "a05" in held["flags"][0]["reason"]
    assert entry["detectors"]["a03"]["zero_source"] == "elastic_fit"
    runcal = calibrations.calibration_view(card)
    calres = calibrations.resolve(recs[0], "a05", card, runcal)
    assert calres.held and calres.center is None and "a05" in calres.held_reason
    assert not calibrations.resolve(recs[0], "a03", card, runcal).held


def test_no_resolution_folder_holds_every_analyzer_and_says_why(tmp_path):
    ws = _workspace(tmp_path)                                  # no folder saved, none beside the data
    _write_scan(tmp_path / "data")
    card, _s = ws.load()
    lines = calibrations.refresh_tabulated(card, _records(card, tmp_path / "data"), base_dir=ws.root)
    assert any(NO_FOLDER in line for line in lines)
    (entry,) = card.state.tabulated_calibration.values()
    assert all(NO_FOLDER in d["held_reason"] and d["flags"][0]["type"] == "missing_resolution"
               for d in entry["detectors"].values())


def test_weak_elastic_line_falls_back_to_the_pipeline_zero_labelled(tmp_path):
    _ws, card, recs, entry = _calibrated(tmp_path, weak=("a05",))
    d = entry["detectors"]["a05"]
    assert d["zero_source"] == "pipeline_zero" and d["zero"] == 0.0 and not d["fit"]["significant"]
    (flag,) = d["flags"]
    assert flag["type"] == "pipeline_zero" and "a05" in flag["reason"]
    assert entry["detectors"]["a03"]["zero_source"] == "elastic_fit"
    # The fallback is a labelled axis, not a held unit: it resolves, with a curve.
    calres = calibrations.resolve(recs[0], "a05", card, calibrations.calibration_view(card))
    assert not calres.held and calres.zero_source == "pipeline_zero" and calres.curve is not None


def test_print_rounding_notice_is_fitted_not_held(tmp_path):
    ws = _workspace(tmp_path)
    _write_library(ws.root / "rf")
    assert save_resolution_folder(ws.root, ws.root / "rf").ok
    _write_scan(tmp_path / "data", analyzers=ANALYZERS)
    card, _session = ws.load()
    recs = _records(card, tmp_path / "data")
    recs[0].spectra["a04"].spectrum_flags.append(Flag("counts_not_integer", "info", "print rounding"))
    calibrations.refresh_tabulated(card, recs, base_dir=ws.root)
    (entry,) = card.state.tabulated_calibration.values()
    assert not entry["detectors"]["a04"].get("held_reason")
    assert "a04" in entry["comparison_set"]


def test_lr_spectrum_is_held_never_fitted_and_not_scored(tmp_path):
    _ws, card, recs, entry = _calibrated(tmp_path, lr=("a04",))
    assert entry["detectors"]["a04"]["held_reason"] == "non-raw counts (_lr) are not fitted"
    assert "a04" not in entry["comparison_set"] and entry["setting"] == TRUE_SLIT
    calres = calibrations.resolve(recs[0], "a04", card, calibrations.calibration_view(card))
    assert calres.held and calres.held_reason == calibrations.NON_RAW_REASON


def test_beamline_centroid_flags_only_the_analyzer_whose_zero_disagrees(tmp_path):
    # a03 agrees with the centroid, a04 is 1 meV off, a05 has no centroid in the .var (no fact, no flag)
    _ws, _card, recs, entry = _calibrated(tmp_path, centroids={"a03": ZERO, "a04": ZERO + 1.0})
    assert recs[0].spectra["a03"].facts["elastic_centroid_meV"]["value"] == pytest.approx(ZERO)
    assert recs[0].spectra["a04"].facts["elastic_centroid_meV"]["value"] == pytest.approx(ZERO + 1.0)
    assert recs[0].spectra["a05"].facts["elastic_centroid_meV"]["value"] is None
    flags = {det: [f for f in entry["detectors"][det]["flags"] if f["type"] == "zero_disagrees_with_beamline"]
             for det in ANALYZERS}
    assert flags["a03"] == [] and flags["a05"] == []
    (flag,) = flags["a04"]
    assert "a04" in flag["reason"] and f"{entry['detectors']['a04']['zero']:.3f}" in flag["reason"]
    assert f"{ZERO + 1.0:.3f}" in flag["reason"] and flag["severity"] == "warning"


def test_beamline_centroid_flag_does_not_change_the_calibration_fingerprint(tmp_path):
    # Same workspace before and after the .var is added: a library fingerprint hashes the folder path and
    # mtimes, so only an in-place comparison isolates the flag.
    ws, card, _recs, _entry = _calibrated(tmp_path)
    before = calibrations.calibration_fingerprint(calibrations.calibration_view(card), _recs[0], "a04")
    _write_scan(tmp_path / "data", centroids={"a04": ZERO + 1.0})
    recs = _records(card, tmp_path / "data")
    calibrations.refresh_tabulated(card, recs, base_dir=ws.root)
    (entry,) = card.state.tabulated_calibration.values()
    assert any(f["type"] == "zero_disagrees_with_beamline" for f in entry["detectors"]["a04"]["flags"])
    assert calibrations.calibration_fingerprint(calibrations.calibration_view(card), recs[0], "a04") == before


def test_state_is_updated_in_place_for_new_or_changed_scans_only(tmp_path):
    ws, card, recs, entry = _calibrated(tmp_path)
    state = card.state.tabulated_calibration
    key = next(iter(state))
    assert calibrations.refresh_tabulated(card, recs, base_dir=ws.root) == []      # nothing new: nothing built
    assert state[key] is entry and len(card.state.resolution_versions) == 1
    _write_scan(tmp_path / "data", stem=STEM + "_runB", seed=2)
    recs2 = _records(card, tmp_path / "data")
    calibrations.refresh_tabulated(card, recs2, base_dir=ws.root)
    assert len(state) == 2 and state[key] is entry
    # A replaced curve file changes the fingerprint: a new resolution version, every scan recomputed.
    path = ws.root / "rf" / "sarf_t_s2_40_40_a03"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="\n")
    calibrations.refresh_tabulated(card, recs2, base_dir=ws.root)
    assert len(card.state.resolution_versions) == 2 and state[key] is not entry
    assert all(e["library"] == card.state.resolution_versions[-1]["library"] for e in state.values())


def test_other_methods_are_untouched_by_the_refresh(tmp_path):
    ws, card, recs, _entry = _calibrated(tmp_path)
    card.policy.calibration = {"method": "cooper_nathans"}
    card.state.tabulated_calibration.clear()
    assert calibrations.refresh_tabulated(card, recs, base_dir=ws.root) == []
    assert card.state.tabulated_calibration == {}


def _sha(path):
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_state_records_how_the_slit_was_chosen_and_the_curve_each_zero_used(tmp_path):
    ws, _card, _recs, entry = _calibrated(tmp_path)
    assert entry["setting_basis"] == "inferred"
    for det in ANALYZERS:
        # the curve file the zero was fitted with, by its bytes: TRUE_SLIT is the second curve written (s2)
        assert entry["detectors"][det]["curve"] == {"file": f"sarf_t_s2_40_40_{det}",
                                                    "sha256": _sha(ws.root / "rf" / f"sarf_t_s2_40_40_{det}")}
    _ws, _card, _recs, declared = _calibrated(tmp_path / "declared", slit=(40, 40))
    assert declared["setting_basis"] == "declared"


def test_no_slit_comparison_possible_is_the_broadest_curve_fallback(tmp_path):
    _ws, _card, _recs, entry = _calibrated(tmp_path, weak=ANALYZERS)     # no elastic line anywhere
    assert entry["setting_basis"] == "broadest_curve_fallback" and entry["setting_source"] == "inferred"
    for det in ANALYZERS:
        assert entry["detectors"][det]["zero_source"] == "pipeline_zero" and "curve" in entry["detectors"][det]


def test_a_curve_replaced_at_the_same_path_recalibrates_the_scan(tmp_path):
    import os
    ws, card, recs, entry = _calibrated(tmp_path)
    path = ws.root / "rf" / "sarf_t_s2_40_40_a03"
    st = path.stat()
    data = path.read_bytes()
    i = next(k for k in range(200, len(data)) if chr(data[k]).isdigit())    # one digit, changed in place
    path.write_bytes(data[:i] + bytes([48 + (data[i] - 47) % 10]) + data[i + 1:])
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))                     # same size and mtime
    assert path.stat().st_size == st.st_size and path.stat().st_mtime_ns == st.st_mtime_ns
    old_library = entry["library"]
    assert calibrations.refresh_tabulated(card, recs, base_dir=ws.root)               # not reused: rebuilt
    (new,) = card.state.tabulated_calibration.values()
    assert new["library"] != old_library and new["detectors"]["a03"]["curve"]["sha256"] == _sha(path)
    assert new["detectors"]["a04"]["curve"] == entry["detectors"]["a04"]["curve"]    # other curves unchanged


def test_an_analyzer_whose_curve_cannot_be_identified_is_held_alone(tmp_path, monkeypatch):
    # The curve loaded, then its file became unreadable before its identity was taken: only that analyzer is held.
    ws = _workspace(tmp_path)
    _write_library(ws.root / "rf")
    assert save_resolution_folder(ws.root, ws.root / "rf").ok
    _write_scan(tmp_path / "data")
    card, _s = ws.load()
    recs = _records(card, tmp_path / "data")
    identify = sarf.curve_identity
    monkeypatch.setattr(sarf, "curve_identity",
                        lambda folder, lib: {k: v for k, v in identify(folder, lib).items() if k != ("a03", TRUE_SLIT)})
    calibrations.refresh_tabulated(card, recs, base_dir=ws.root)
    (entry,) = card.state.tabulated_calibration.values()
    assert "error" not in entry and entry["setting"] == TRUE_SLIT
    held = entry["detectors"]["a03"]
    assert "a03" in held["held_reason"] and TRUE_SLIT in held["held_reason"] and "unreadable" in held["held_reason"]
    assert _flag_types(held["flags"]) == ["missing_resolution"]
    assert all("zero" in entry["detectors"][det] and "curve" in entry["detectors"][det] for det in ("a04", "a05"))


# ---------------------------------------------------------------------------
# resolve and the fit context (fast)
# ---------------------------------------------------------------------------
def test_resolve_hands_the_fit_its_curve_and_zero(tmp_path):
    ws, card, recs, entry = _calibrated(tmp_path)
    runcal = calibrations.calibration_view(card)
    calres = calibrations.resolve(recs[0], "a03", card, runcal)
    assert not calres.held and calres.zero_source == "elastic_fit"
    assert calres.center == entry["detectors"]["a03"]["zero"]
    assert calres.curve.detector_id == "a03" and calres.curve.setting == TRUE_SLIT
    assert calres.gauss_fwhm == calres.curve.fwhm and calres.lorentz_fwhm == 0.0
    assert calres.resolution_source == "tabulated slit 40x40 (inferred)"
    summary = calres.summary()
    assert summary["tabulated"]["setting"] == TRUE_SLIT and summary["calibration"]["zero"] == calres.center
    json.dumps(summary, allow_nan=False)
    # build_context passes the curve and zero on; the context widths are the curve's.
    ctx, n_used, reasons = scan_delivery.build_context(recs[0], "a03", card, calres, None, guide_mod.resolve(card))
    assert ctx.resolution_curve is calres.curve and ctx.center == calres.center
    assert ctx.gauss_fwhm == calres.curve.fwhm and n_used == int(ctx.mask.sum()) and reasons == []


def test_a_fit_range_beyond_the_table_is_flagged_and_still_built(tmp_path):
    ws, card, recs, _entry = _calibrated(tmp_path)
    runcal = calibrations.calibration_view(card)
    calres = calibrations.resolve(recs[0], "a03", card, runcal)
    from isar.analyze.resolution import TabulatedResolution
    short = np.linspace(-3.0, 3.0, 61)                         # the fit range is +-8 meV
    calres.curve = TabulatedResolution(short, _curve_values(short, 1.3), "a03", TRUE_SLIT)
    ctx, _n, reasons = scan_delivery.build_context(recs[0], "a03", card, calres, None, guide_mod.resolve(card))
    assert ctx is not None and any(r.startswith("resolution_range_exceeded") for r in reasons)
    assert "resolution_range_exceeded" in _flag_types(calres.provenance()["calibration"]["flags"])


def test_each_analyzers_own_monitor_normalises_its_intensity(tmp_path):
    ws, card, recs, _entry = _calibrated(tmp_path, monitors={"a03": 1.0e5, "a04": 2.0e5})
    runcal = calibrations.calibration_view(card)
    g = guide_mod.resolve(card)
    per_det = {}
    for det in ("a03", "a04"):
        calres = calibrations.resolve(recs[0], det, card, runcal)
        ctx, _n, _r = scan_delivery.build_context(recs[0], det, card, calres, None, g)
        per_det[det] = (ctx, scan_delivery._exposure_per_point(recs[0], ctx, det)[0],
                        scan_delivery._effective_exposure_for(recs[0], ctx, {}, det=det)["value"])
    assert per_det["a03"][1] == 1.0e5 and per_det["a04"][1] == 2.0e5     # the record's NaN is never read
    assert per_det["a03"][2] == 1.0e5 and per_det["a04"][2] == 2.0e5


def test_driver_freshness_follows_the_calibration_fingerprint(tmp_path):
    ws, card, recs, entry = _calibrated(tmp_path)
    runcal = calibrations.calibration_view(card)
    fp = calibrations.calibration_fingerprint(runcal, recs[0], "a03")
    assert fp and fp == calibrations.resolve(recs[0], "a03", card, runcal).fingerprint
    assert fp != calibrations.calibration_fingerprint(runcal, recs[0], "a04")
    path = ws.root / "rf" / "sarf_t_s2_40_40_a03"                  # replace a curve file
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="\n")
    calibrations.refresh_tabulated(card, recs, base_dir=ws.root)
    runcal2 = calibrations.calibration_view(card)
    assert calibrations.calibration_fingerprint(runcal2, recs[0], "a03") != fp
    cooper = calibrations.RunCalibration(method="cooper_nathans", zeros=None, resolution_by_slit={},
                                         slit_by_scan={}, default_resolution=(1, 1), tabulated=runcal2.tabulated)
    assert calibrations.calibration_fingerprint(cooper, recs[0], "a03") is None      # other methods: no fingerprint


def test_repointing_at_a_moved_library_refits_from_the_new_folder(tmp_path):
    # A moved folder keeps every file's name, size and mtime, so only its path tells the curves apart.
    ws, card, recs, _entry = _calibrated(tmp_path)
    old_fp = calibrations.calibration_fingerprint(calibrations.calibration_view(card), recs[0], "a03")
    shutil.copytree(ws.root / "rf", ws.root / "rf_moved", copy_function=shutil.copy2)
    shutil.rmtree(ws.root / "rf")
    assert save_resolution_folder(ws.root, ws.root / "rf_moved").ok
    calibrations.refresh_tabulated(card, recs, base_dir=ws.root)
    sarf._LIBRARIES.clear()                                      # what a fresh `isar run` starts with
    runcal = calibrations.calibration_view(card)
    calres = calibrations.resolve(recs[0], "a03", card, runcal)
    assert not calres.held and calres.reference["folder"] == str((ws.root / "rf_moved").resolve())
    assert calibrations.calibration_fingerprint(runcal, recs[0], "a03") != old_fp    # the driver refits


# ---------------------------------------------------------------------------
# end to end (slow): isar run fits with the curve; reopening shows the fitted model
# ---------------------------------------------------------------------------
def _run(ws, data, **kw):
    return driver.run_batch(ws, [data], M=10, scan_M=10, errors_tier="fast", workers=1,
                            formats=("markdown",), **kw)


@pytest.mark.slow
def test_run_fits_with_the_tabulated_curve_holds_the_rest_and_refits_on_a_changed_library(tmp_path):
    ws = _workspace(tmp_path)
    _write_library(ws.root / "rf", skip=[("a05", "40x40")])
    assert save_resolution_folder(ws.root, ws.root / "rf").ok
    data = tmp_path / "data"
    _write_scan(data, monitors={"a03": 1.0e5, "a04": 2.0e5})
    policy_before = ws.policy_path.read_bytes()
    summary = _run(ws, data, detectors="a03,a04,a05")
    assert ws.policy_path.read_bytes() == policy_before             # code never writes the policy
    card, session = ws.load()
    status = {e.scan_id.rsplit(":", 1)[1]: e for e in session.scans.values()}
    assert status["a03"].status == "fitted" and status["a04"].status == "fitted"
    assert status["a05"].status == "held_for_calibration" and "a05" in status["a05"].reasons[0]
    out = status["a03"].detectors["a03"]
    assert out["calib"]["zero_source"] == "elastic_fit" and abs(out["calib"]["center"] - ZERO) < 0.1
    assert out["calib"]["tabulated"]["setting"] == TRUE_SLIT
    assert out["calib"]["gauss_fwhm"] == pytest.approx(
        sarf.curve_from_reference(out["calib"]["tabulated"]).fwhm)
    # Provenance carries the slit with its source, candidates, zero and flags for the inspector.
    cal = status["a03"].provenance["calibration"]
    assert cal["setting"] == TRUE_SLIT and cal["setting_source"] == "inferred" and cal["zero_source"] == "elastic_fit"
    assert "slit_inferred" in _flag_types(cal["flags"])
    assert status["a05"].provenance["calibration"]["held_reason"]
    # A second run leaves fitted units alone; a replaced curve refits exactly the affected unit.
    first_fp = status["a03"].provenance["calibration_fingerprint"]
    assert _run(ws, data, detectors="a03,a04").n_fitted_this_run == 0
    path = ws.root / "rf" / "sarf_t_s2_40_40_a03"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8", newline="\n")
    sarf._LIBRARIES.clear()
    assert _run(ws, data, detectors="a03,a04").n_fitted_this_run >= 1
    _card, session = ws.load()
    assert any(e.provenance.get("calibration_fingerprint") not in (None, first_fp)
               for e in session.scans.values() if e.scan_id.endswith("a03"))


@pytest.mark.slow
def test_reopened_workspace_rebuilds_the_fitted_model_with_its_curve_and_shows_the_calibration(tmp_path):
    from isar.analyze.context import ScanContext
    from isar.analyze.model_manifest import compose_manifest
    from isar.workspace_api import Workspace as ReadOnlyWorkspace
    ws = _workspace(tmp_path)
    _write_library(ws.root / "rf")
    assert save_resolution_folder(ws.root, ws.root / "rf").ok
    data = tmp_path / "data"
    _write_scan(data)
    _run(ws, data, detectors="a03")
    view = ReadOnlyWorkspace.open(ws.root)
    (sv,) = [s for s in view.scans if s.scan_id.endswith("a03")]
    fit = sv.detectors["a03"]
    assert fit.verdict is not None
    fine = np.linspace(-8, 8, 161)
    model = view.evaluate_fit(fit, x_eval=fine)
    # The fit's own model: the persisted manifest evaluated on the context the fit used.
    card, _s = ws.load()
    rec = _records(card, data)[0]
    calres = calibrations.resolve(rec, "a03", card, calibrations.calibration_view(card))
    ctx = ScanContext.from_scan(rec, "a03", card, center=calres.center, gauss_fwhm=calres.gauss_fwhm,
                                lorentz_fwhm=calres.lorentz_fwhm, resolution_curve=calres.curve)
    comp, theta = compose_manifest(fit.model_manifest, card.policy.quasielastic_fwhm)
    assert np.allclose(model.total, comp.evaluate(fine, theta, ctx), rtol=1e-9, atol=1e-9)
    # ...and it is not the Voigt of the same widths: the curve really is in the displayed model.
    voigt = ScanContext.from_scan(rec, "a03", card, center=calres.center, gauss_fwhm=calres.gauss_fwhm,
                                  lorentz_fwhm=0.0)
    assert not np.allclose(model.total, comp.evaluate(fine, theta, voigt), rtol=1e-3, atol=1e-3)
    # The inspector (offscreen) shows the slit and its source, the zero and its source, the elastic fit.
    pytest.importorskip("PySide6")
    from isar_gui.viewmodels.inspector import InspectorViewModel
    vm = InspectorViewModel(view)
    vm.set_unit(sv.scan_id)
    md = vm.state.metadata
    assert md["resolution slit"] == "40x40 (inferred)"
    assert "elastic_fit" in md["energy zero"] and "centre" in md["elastic fit"] and "slit_inferred" in md["calibration flags"]
    assert "a03" in md["slit candidates"] and "40x40:" in md["slit candidates"]
    vm.set_fit(fit)
    # (The inspector still withholds the overlay of a needs_zero unit; that rule is not this slice's.)
    assert vm.state.provenance["resolution slit"] == "40x40 (inferred)"
    assert "elastic_fit" in vm.state.provenance["energy zero"]


@pytest.mark.slow
def test_pooled_master_is_held_under_tabulated_calibration_and_the_member_fits_stand(tmp_path):
    ws = _workspace(tmp_path, merge="merge", sample="Smp")
    _write_library(ws.root / "rf")
    assert save_resolution_folder(ws.root, ws.root / "rf").ok
    data = tmp_path / "data"
    for i, stem in enumerate((STEM + "_runA", STEM + "_runB")):
        _write_scan(data, stem=stem, analyzers=("a03",), seed=10 + i, hkl=(0.5, 0.5, 3))
    _run(ws, data, detectors="a03")
    _card, session = ws.load()
    masters = [e for e in session.scans.values() if e.scan_id.startswith("master:")]
    members = [e for e in session.scans.values() if not e.scan_id.startswith("master:")]
    assert len(members) == 2 and all(e.status == "fitted" for e in members)
    assert len(masters) == 1 and masters[0].status == "held_for_calibration"
    assert masters[0].reasons == [f"{masters[0].scan_id}: pooling across tabulated calibrations not supported"]
    assert masters[0].detectors == {}


def test_the_facade_rebuilds_a_fitted_curve_from_its_file_and_refuses_a_changed_one(tmp_path):
    """A fit records its curve's file and stamp; the read facade rebuilds the same curve from them
    through the shared resolution module (no parser), and refuses a file changed since the fit."""
    import os

    from isar.analyze.resolution import curve_from_record, file_stamp, read_table
    e = np.round(np.arange(-5.0, 5.0001, 0.1), 4)
    f = tmp_path / "curve_a03"
    f.write_text("# energy value fit\n# second comment\n"
                 + "".join(f"{x:.4f} {y:.6g} 0\n" for x, y in zip(e, _curve_values(e, 1.0))),
                 encoding="utf-8", newline="\n")
    ref = {"folder": str(tmp_path), "file": f.name, "stamp": file_stamp(f),
           "detector": "a03", "setting": "40x40"}
    rebuilt, direct = curve_from_record(ref), read_table(f, "a03", "40x40")
    assert np.array_equal(rebuilt.energy, direct.energy) and np.array_equal(rebuilt.values, direct.values)
    assert rebuilt.fwhm == pytest.approx(direct.fwhm)
    with pytest.raises(ValueError, match="names no curve file"):
        curve_from_record({k: v for k, v in ref.items() if k != "file"})
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
    with pytest.raises(ValueError, match="changed since the fit"):
        curve_from_record(ref)
    f.unlink()
    with pytest.raises(ValueError, match="unreadable"):
        curve_from_record(ref)
