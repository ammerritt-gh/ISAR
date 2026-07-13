"""`isar.session.unit_data` — per-unit data files + provenance extras (Pass 2b Phase 2).

Contracts pinned here: the lossless ``data/ingest/`` identity path and legacy readability; a verbatim round-trip
(counts/variance/mask/x unshifted, σ²=0 kept — D3); the axis block carries the honest 3-state
calibration (never a defaulted E0); and ``_provenance_extras`` is additive + strictly tolerant
(q_target present for an S30 target #Q, absent when no #Q line exists).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from isar.parsers import spec_s30
from isar.schema import scan as scan_mod
from isar.session import unit_data
from isar.synth_format import run_builder


def _s30_record(seed0=1000, status="needs_zero"):
    card = spec_s30.card_template()
    run = run_builder.build_s30_run("SYN", n_scans=1, seed0=seed0)
    import tempfile
    p = Path(tempfile.mkdtemp()) / "run.spec_supplementary.dat"
    p.write_bytes(spec_s30.emit_file(run, card))
    rec = spec_s30.parse_file(p, card)[0]
    rec.energy_calibration.status = status
    return rec


def test_unit_data_path_is_lossless_and_legacy_path_remains_addressable(tmp_path):
    p = unit_data.unit_data_path(tmp_path / "data", "FeSeS_1.spec", 12, "ana3")
    assert p.parent == tmp_path / "data" / "ingest"
    assert p.name.startswith("ingest-v2-e") and p.suffix == ".json"
    # The old sanitiser collided for these distinct identities; v2 must not.
    first = unit_data.unit_data_path(tmp_path / "data", "a/b:c", 7, "d e")
    second = unit_data.unit_data_path(tmp_path / "data", "a:b/c", 7, "d_e")
    assert first != second
    assert all(c not in first.name for c in "\\/:*?\"<>")
    legacy = unit_data.legacy_unit_data_path(tmp_path / "data", "a/b:c", 7, "d e")
    assert legacy.name == "ingest-a_b_c-0007-d_e.json"
    assert unit_data.unit_data_paths_for_read(tmp_path / "data", "a/b:c", 7, "d e") == (first, legacy)


def test_round_trip_verbatim(tmp_path):
    rec = _s30_record()
    det = "ana1"
    path = unit_data.write_unit_data(rec, det, tmp_path / "data")
    assert path.parent.name == "ingest"
    d = unit_data.read_unit_data(path)
    sp = rec.spectra[det]
    assert d["schema"] == "isar.unit_data/1"
    assert d["scan_id"] == f"{rec.experiment_id}:{rec.scan_number}:{det}"
    assert d["detector_id"] == det
    np.testing.assert_array_equal(d["counts"], np.asarray(sp.intensity))
    np.testing.assert_array_equal(d["variance"], np.asarray(sp.variance))
    np.testing.assert_array_equal(d["mask"], np.asarray(sp.mask, dtype=bool))
    # x is the RAW scanned abscissa, never energy-zero-shifted (hard rule 4)
    from isar.schema.axes import scanned_axes
    np.testing.assert_array_equal(d["x"], np.asarray(scanned_axes(rec)[0].values))


def test_variance_zero_preserved(tmp_path):
    """D3: a zero-count point keeps σ²=0 through the file — no floor is silently added."""
    rec = _s30_record()
    sp = rec.spectra["ana1"]
    sp.intensity = np.array(sp.intensity, dtype="float64")
    sp.variance = np.array(sp.variance, dtype="float64")
    sp.intensity[0] = 0.0
    sp.variance[0] = 0.0
    path = unit_data.write_unit_data(rec, "ana1", tmp_path / "data")
    d = unit_data.read_unit_data(path)
    assert d["counts"][0] == 0.0
    assert d["variance"][0] == 0.0


def test_axis_records_three_calibration_states(tmp_path):
    for status in scan_mod.ENERGY_STATUSES:
        rec = _s30_record(status=status)
        path = unit_data.write_unit_data(rec, "ana1", tmp_path / "data")
        d = unit_data.read_unit_data(path)
        assert d["axis"]["calibration_status"] == status
        assert d["axis"]["raw_abscissa_kind"] == "energy"
        assert d["axis"]["units"] == "meV"


def test_read_unit_data_tolerant(tmp_path):
    assert unit_data.read_unit_data(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert unit_data.read_unit_data(bad) is None
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2, 3]", encoding="utf-8")   # valid JSON, wrong root type
    assert unit_data.read_unit_data(arr) is None


def test_read_unit_data_validates_schema_identity_and_aligned_vectors(tmp_path):
    payload = {
        "schema": unit_data.SCHEMA, "scan_id": "exp:4:det",
        "experiment_id": "exp", "scan_number": 4, "detector_id": "det",
        "x": [0.0, 1.0], "counts": [1.0, 0.0], "variance": [1.0, 0.0],
        "mask": [True, False], "axis": {},
    }
    path = tmp_path / "unit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert unit_data.read_unit_data(path, expected_identity=("exp", 4, "det")) == payload
    assert unit_data.read_unit_data(path, expected_identity=("other", 4, "det")) is None
    payload["variance"] = [1.0]
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert unit_data.read_unit_data(path) is None
    payload["variance"] = [1.0, 0.0]
    payload["mask"] = [[True], [False]]
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert unit_data.read_unit_data(path) is None  # malformed nested/ragged masks are tolerant-missing


def test_read_unit_data_for_unit_reads_valid_legacy_after_bad_canonical(tmp_path):
    data = tmp_path / "data"
    canonical = unit_data.unit_data_path(data, "exp", 4, "det")
    legacy = unit_data.legacy_unit_data_path(data, "exp", 4, "det")
    canonical.parent.mkdir(parents=True)
    canonical.write_text("{ torn", encoding="utf-8")
    legacy.write_text(json.dumps({
        "schema": unit_data.SCHEMA, "scan_id": "exp:4:det",
        "experiment_id": "exp", "scan_number": 4, "detector_id": "det",
        "x": [0.0], "counts": [0.0], "variance": [0.0], "mask": [True], "axis": {},
    }), encoding="utf-8")
    assert unit_data.read_unit_data_for_unit(data, "exp", 4, "det")["scan_id"] == "exp:4:det"
    assert not unit_data.canonical_unit_data_is_valid(data, "exp", 4, "det")


def test_provenance_extras_s30_has_q_target():
    rec = _s30_record()
    extras = unit_data._provenance_extras(rec)
    assert extras["q_target"] == [float(v) for v in rec.raw_header["Q"]]
    assert extras["command"]
    assert extras["counting_mode"] in scan_mod.COUNTING_MODES
    assert extras["scan_axis"]["kind"] == "energy"
    assert extras["n_points"] == len(rec.spectra["ana1"].intensity)
    assert extras["energy_status"] == rec.energy_calibration.status
    assert extras["raw_abscissa_kind"] == "energy"
    assert "B" not in extras                      # synthetic S30 carries no applied field


def test_provenance_extras_absent_q_target_when_no_q_line():
    """A non-S30 record (no ``#Q`` in raw_header) yields no q_target key — never a placeholder."""
    rec = _s30_record()
    del rec.raw_header["Q"]                        # emulate an instrument with no target-Q line (IN8)
    extras = unit_data._provenance_extras(rec)
    assert "q_target" not in extras
    assert "counting_mode" in extras              # the other extras are unaffected


def test_provenance_extras_b_when_field_present():
    rec = _s30_record()
    n = len(rec.spectra["ana1"].intensity)
    rec.environment.field = scan_mod.EnvField(
        name="B", setpoint=np.full(n, 6.0), sensors={}, valid_mask=np.ones(n, bool))
    extras = unit_data._provenance_extras(rec)
    assert extras["B"] == 6.0
