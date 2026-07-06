"""scan_writer — a saved TAVI scan must be sniffable by ISAR's tavi parser plugin."""
import json

import pytest

from isar.drive.isar_io.scan_writer import save_scan_json
from isar.parsers import tavi


def _scan_data(job_id="j-0007"):
    """A minimal but complete GET /scan/{id}/data body (deltaE measurement)."""
    return {
        "job_id": job_id, "source": "api", "state": "done",
        "progress": {"done": 3, "total": 3}, "error": None,
        "launch": {"scan_command1": "deltaE 0.5 3.0 1.25", "scan_command2": "",
                   "number_neutrons": 1000000, "isolated": True,
                   "parameters": {"H": 2.0, "K": 0.0, "L": 0.0, "deltaE": 0.0,
                                  "Kf": 2.662, "Ef": 14.7, "K_fixed": "Kf Fixed",
                                  "monocris": "pg002", "anacris": "pg002",
                                  "lattice_a": 3.94, "lattice_b": 3.94, "lattice_c": 3.94,
                                  "number_neutrons": 1000000}},
        "result": {"mode": "1D", "variable_1": "deltaE", "variable_2": None,
                   "total_counts": 300.0, "max_counts": 150.0, "skipped_points": [],
                   "scan_values_1": [0.5, 1.75, 3.0], "scan_values_2": None,
                   "valid_mask_1": [True, True, True],
                   "counts": [50.0, 150.0, 100.0], "counts_grid": None, "metadata": {}},
    }


def test_writes_job_id_named_file(tmp_path):
    path = save_scan_json(_scan_data("j-0007"), tmp_path / "data")
    assert path.name == "j-0007.json"
    assert path.exists()


def test_written_json_is_verbatim(tmp_path):
    data = _scan_data()
    path = save_scan_json(data, tmp_path / "data")
    assert json.loads(path.read_text(encoding="utf-8")) == data


def test_written_json_is_plugin_sniffable(tmp_path):
    """The whole point: ISAR's tavi.matches() recognizes the saved file."""
    path = save_scan_json(_scan_data(), tmp_path / "data")
    assert tavi.matches(path) is True


def test_written_json_parses_through_the_plugin(tmp_path):
    path = save_scan_json(_scan_data(), tmp_path / "data")
    records = tavi.parse_file(path, tavi.card_template())
    assert len(records) == 1
    rec = records[0]
    assert rec.scan_number == 7
    assert rec.scan_type == "measurement"


def test_rejects_body_without_job_id_and_result(tmp_path):
    with pytest.raises(ValueError):
        save_scan_json({"job_id": "j-1"}, tmp_path / "data")  # no result
    with pytest.raises(ValueError):
        save_scan_json({"result": {}}, tmp_path / "data")     # no job_id


def test_creates_data_dir(tmp_path):
    target = tmp_path / "nested" / "data"
    assert not target.exists()
    save_scan_json(_scan_data(), target)
    assert target.is_dir()
