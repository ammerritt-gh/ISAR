"""readers — parse ISAR's file contract (results.csv, report.json, card.state.json)."""
import csv
import json

import pytest

from isar.drive.isar_io import readers
from isar.session.outcome import _CSV_COLUMNS  # the authoritative results.csv header (test-side import ok)


def _write_results(workspace, rows):
    rd = workspace / "report"
    rd.mkdir(parents=True, exist_ok=True)
    with open(rd / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=_CSV_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) for k in _CSV_COLUMNS})


def test_reads_results_csv_with_numeric_coercion(tmp_path):
    _write_results(tmp_path, [
        {"scan": 7, "detector": "det0", "sample": "FeSe", "T": 300.0, "h": 2.0, "k": 0.0, "l": 0.0,
         "status": "fitted", "verdict": "resolved", "disposition": "resolved", "confidence": 0.82,
         "omega0": 5.1, "omega0_err": 0.12, "interval_lo": 4.9, "interval_hi": 5.3,
         "gamma_provisional": "True", "prior_sigma": 0.3, "reasons": "ok"},
    ])
    rows = readers.read_results_csv(tmp_path)
    assert len(rows) == 1
    r = rows[0]
    assert r["scan"] == 7 and isinstance(r["scan"], int)
    assert r["omega0"] == pytest.approx(5.1)
    assert r["interval_lo"] == pytest.approx(4.9)
    assert r["interval_hi"] == pytest.approx(5.3)
    assert r["confidence"] == pytest.approx(0.82)
    # Non-numeric columns stay strings.
    assert r["verdict"] == "resolved"
    assert r["gamma_provisional"] == "True"


def test_profile_interval_and_bool_coercion(tmp_path):
    _write_results(tmp_path, [
        {"scan": 7, "detector": "det0", "status": "fitted", "verdict": "resolved",
         "disposition": "resolved", "omega0": 0.95, "interval_lo": 0.949, "interval_hi": 0.951,
         "interval_reliable": "False", "omega0_profile_lo": 0.944, "omega0_profile_hi": 0.956,
         "reasons": "ok"},
    ])
    r = readers.read_results_csv(tmp_path)[0]
    # The Gamma-at-bound bootstrap collapsed (tiny) but the profile interval is the reliable one.
    assert r["omega0_profile_lo"] == pytest.approx(0.944)
    assert r["omega0_profile_hi"] == pytest.approx(0.956)
    assert r["interval_reliable"] is False          # coerced from the string "False"


def test_interval_reliable_true_and_empty(tmp_path):
    _write_results(tmp_path, [
        {"scan": 1, "detector": "d", "status": "fitted", "interval_reliable": "True"},
        {"scan": 2, "detector": "d", "status": "fitted"},   # absent → empty → None
    ])
    rows = readers.read_results_csv(tmp_path)
    assert rows[0]["interval_reliable"] is True
    assert rows[1]["interval_reliable"] is None


def test_empty_cells_become_none(tmp_path):
    # A held scan row: no detector/omega0.
    _write_results(tmp_path, [{"scan": 3, "status": "held_for_calibration", "reasons": "no zero"}])
    rows = readers.read_results_csv(tmp_path)
    assert rows[0]["omega0"] is None
    assert rows[0]["interval_lo"] is None
    assert rows[0]["status"] == "held_for_calibration"


def test_members_list_column_coercion(tmp_path):
    # A pooled master's `members` cell (";"-joined) coerces to a list; empty → None; absent → key absent.
    _write_results(tmp_path, [
        {"scan": 9_000_001, "detector": "det", "status": "fitted", "pooled": "True",
         "n_members": 3, "members": "473;493;494"},
        {"scan": 5, "detector": "det", "status": "fitted", "members": ""},   # empty → None
    ])
    rows = readers.read_results_csv(tmp_path)
    assert rows[0]["members"] == ["473", "493", "494"]
    assert rows[1]["members"] is None


def test_members_column_absent_key_absent(tmp_path):
    # A results.csv predating the members column: DictReader never yields the key.
    rd = tmp_path / "report"
    rd.mkdir(parents=True, exist_ok=True)
    cols = [c for c in _CSV_COLUMNS if c != "members"]
    with open(rd / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerow({c: {"scan": 5, "status": "fitted"}.get(c) for c in cols})
    rows = readers.read_results_csv(tmp_path)
    assert "members" not in rows[0]


def test_missing_results_csv_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        readers.read_results_csv(tmp_path)


def test_reads_report_json(tmp_path):
    rd = tmp_path / "report"
    rd.mkdir()
    (rd / "report.json").write_text(json.dumps({"instrument": "tavi", "counts_status": {"fitted": 2}}),
                                    encoding="utf-8")
    rep = readers.read_report_json(tmp_path)
    assert rep["instrument"] == "tavi"
    assert rep["counts_status"]["fitted"] == 2


def test_reads_card_state_dispersion_prior(tmp_path):
    (tmp_path / "card.state.json").write_text(json.dumps({
        "dispersion_prior": [{"scan_id": "tavi:7:det0", "q_value": 0.5, "omega0": 5.1, "sigma": 0.3}],
        "resolution_versions": [{"version": 1, "method": "cooper_nathans"}],
    }), encoding="utf-8")
    state = readers.read_card_state(tmp_path)
    assert len(state["dispersion_prior"]) == 1
    prior = readers.dispersion_prior(tmp_path)
    assert prior[0]["omega0"] == pytest.approx(5.1)


def test_missing_or_empty_card_state_is_empty(tmp_path):
    assert readers.read_card_state(tmp_path) == {}
    (tmp_path / "card.state.json").write_text("{}", encoding="utf-8")
    assert readers.read_card_state(tmp_path) == {}
    assert readers.dispersion_prior(tmp_path) == []
