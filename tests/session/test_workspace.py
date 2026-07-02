"""Step-1 gate: the Workspace load/save pair — policy stays untouched (D8), the two machine files
round-trip."""
from isar.session.workspace import Workspace
from isar.session.state import ScanEntry

import pytest


_POLICY = """\
facility = "ILL"
instrument = "IN8"
format_version = "2025"
plugin = "ill_in8"
energy_column = "EN"
monitor_column = "M1"
counting_mode = "monitor"
normalize_by = "monitor"
raw_abscissa_kind = "energy"
energy_status = "absolute"
measurement_pattern = "bs"
alignment_pattern = "sc"

[temperature_rule]
setpoint_param = "RT"
sensor_columns = ["TT"]
divergence_tol = 20.0

[collation]
key = ["sample", "q", "T"]
merge = "merge"

[[detectors]]
detector_id = "det0"
value_index = 0
signal_column = "CNTS"
enabled = true
"""


def _make_ws(tmp_path):
    ws = Workspace.open(tmp_path)
    ws.root.mkdir(parents=True, exist_ok=True)
    ws.policy_path.write_text(_POLICY, encoding="utf-8")
    return ws


def test_load_missing_policy_raises(tmp_path):
    ws = Workspace.open(tmp_path / "empty")
    with pytest.raises(FileNotFoundError):
        ws.load()


def test_load_save_roundtrip_and_policy_untouched(tmp_path):
    ws = _make_ws(tmp_path)
    policy_before = ws.policy_path.read_bytes()

    card, session = ws.load()
    assert card.policy.instrument == "IN8"
    assert session.scans == {}

    card.state.energy_zero_series.append({"key": 100, "e0": 0.0, "source_scan": None})
    session.put(ScanEntry(scan_id="e:1", scan_number=1, scan_type="measurement", status="fitted"))
    ws.save(card, session)

    # Policy byte-for-byte untouched (D8).
    assert ws.policy_path.read_bytes() == policy_before
    # State + session persisted and reload.
    card2, session2 = ws.load()
    assert card2.state.energy_zero_series[0]["key"] == 100
    assert session2.get("e:1").status == "fitted"
