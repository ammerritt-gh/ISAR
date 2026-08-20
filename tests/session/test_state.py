"""Step-1 gates (the retired unification build order §10.1, git history): the schema additions and the ledger are backward-
compatible, the new TOML sections survive the dataclass round-trip, and the U7 warning fires.
"""
import json

from isar.schema import card
from isar.session.state import SCHEMA_VERSION, ScanEntry, SessionState, canonical_hash


# --------------------------------------------------------------------------- schema: backward compat
def test_old_state_json_loads_unchanged(tmp_path):
    """A pre-unification state.json (only exclusions + corrections) must still load."""
    p = tmp_path / "old.state.json"
    p.write_text(json.dumps({
        "exclusions": [{"scope": "spectrum", "id": "53/ana3", "reason": "dead"}],
        "corrections": [{"id": "c1", "decision": "mask"}],
    }), encoding="utf-8")
    st = card.load_state(p)
    assert st.exclusions[0]["id"] == "53/ana3"
    assert st.corrections[0]["decision"] == "mask"
    # The new accumulated-state fields default to empty lists.
    assert st.resolution_versions == []
    assert st.energy_zero_series == []
    assert st.dispersion_prior == []
    assert st.sample_map == []


def test_new_state_fields_roundtrip(tmp_path):
    st = card.CardState()
    st.energy_zero_series.append({"key": 168, "e0": -0.17, "source_scan": 160})
    st.resolution_versions.append({"version": "v1", "method": "bragg_companion"})
    st.dispersion_prior.append({"scan_id": "e:168", "omega0": 5.7, "modality": "fold"})
    st.sample_map.append({"from_scan": 10, "sample": "FeSe", "source": "alignment"})
    p = tmp_path / "new.state.json"
    p.write_text(json.dumps(st.to_dict(), indent=2), encoding="utf-8")
    back = card.load_state(p)
    assert back.energy_zero_series[0]["e0"] == -0.17
    assert back.dispersion_prior[0]["modality"] == "fold"


# --------------------------------------------------------------------------- schema: TOML sections survive
_POLICY_WITH_NEW_SECTIONS = """\
facility = "APS"
instrument = "S30"
format_version = "2025"
plugin = "spec_s30"
energy_column = "HerixE"
monitor_column = "ICO-C"
counting_mode = "time"
normalize_by = "monitor"
raw_abscissa_kind = "energy"
energy_status = "needs_zero"
measurement_pattern = "fscan"
alignment_pattern = "sc a3"

[temperature_rule]
setpoint_field = "T_Control"
sensor_fields = ["T_Sample"]
divergence_tol = 5.0

[collation]
key = ["sample", "q", "T"]
merge = "separate"

[calibration]
method = "bragg_companion"
companion_glob = "FeSe_1-xS_x_1.spec"
analyzer_column = "Ana7"
k_max = 0.05

[fitting]
detectors = ["ana7"]
monitor_frac = 0.9
min_fit_points = 6

[[fitting.expectations]]
scans = [223, 224]
omega0 = 5.2
omega0_sigma = 0.5

[sample]
name = "FeSe_1-xS_x"

[[detectors]]
detector_id = "ana7"
value_index = 10
signal_column = "Ana7_3std"
enabled = true
"""


def test_new_policy_sections_survive_roundtrip(tmp_path):
    """Guards the _json.from_dataclass silent-drop trap: without the CardPolicy field additions,
    [calibration]/[fitting]/[sample] would vanish silently."""
    p = tmp_path / "s.policy.toml"
    p.write_text(_POLICY_WITH_NEW_SECTIONS, encoding="utf-8")
    pol = card.load_policy(p)
    assert pol.calibration["method"] == "bragg_companion"
    assert pol.calibration["analyzer_column"] == "Ana7"
    assert pol.fitting["detectors"] == ["ana7"]
    assert pol.fitting["min_fit_points"] == 6
    assert pol.fitting["expectations"][0]["omega0"] == 5.2
    assert pol.sample["name"] == "FeSe_1-xS_x"


def test_unknown_toml_tables_flagged(tmp_path):
    body = _POLICY_WITH_NEW_SECTIONS + "\n[fittng]\nomega0 = 1.0\n"  # typo'd section
    p = tmp_path / "typo.policy.toml"
    p.write_text(body, encoding="utf-8")
    unknown = card.unknown_toml_tables(p)
    assert "fittng" in unknown
    # Real sections are not flagged.
    assert "fitting" not in unknown
    assert "calibration" not in unknown


def test_known_policy_has_no_unknown_tables(tmp_path):
    p = tmp_path / "clean.policy.toml"
    p.write_text(_POLICY_WITH_NEW_SECTIONS, encoding="utf-8")
    assert card.unknown_toml_tables(p) == []


# --------------------------------------------------------------------------- session ledger
def test_session_state_roundtrip(tmp_path):
    st = SessionState(data_paths=["/data/run1"])
    st.files["/data/run1/055182"] = {"sha256": "abc", "size": 1234, "n_scans": 1, "format": "2025"}
    st.put(ScanEntry(
        scan_id="exp:168", scan_number=168, scan_type="measurement", status="fitted",
        content_hash="h1", guidelines_hash="g1",
        detectors={"ana7": {"verdict": "resolved", "omega0": 5.70}},
        reasons=["ok"], provenance={"M": 200, "seed": 6168},
    ))
    p = tmp_path / "session.json"
    st.save(p)

    back = SessionState.load(p)
    assert back.data_paths == ["/data/run1"]
    assert back.files["/data/run1/055182"]["size"] == 1234
    e = back.get("exp:168")
    assert isinstance(e, ScanEntry)
    assert e.status == "fitted"
    assert e.detectors["ana7"]["omega0"] == 5.70
    assert e.provenance["M"] == 200


def test_session_last_ingest_is_additive_and_old_ledgers_default_empty(tmp_path):
    old = {"schema_version": "isar.session/2", "files": {}, "scans": {}, "data_paths": []}
    path = tmp_path / "old-session.json"
    path.write_text(json.dumps(old), encoding="utf-8")
    assert SessionState.load(path).last_ingest == {}

    state = SessionState(last_ingest={
        "completed_at": "2026-07-13T12:00:00+00:00", "logical_scan_count": 2,
        "analyzer_unit_count": 18, "failed_file_count": 1, "gaps": [["SYN", 4, 7]],
    })
    state.save(path)
    assert SessionState.load(path).last_ingest["analyzer_unit_count"] == 18


def test_session_load_missing_is_empty(tmp_path):
    st = SessionState.load(tmp_path / "nope.json")
    assert st.scans == {}
    assert st.files == {}
    # Track the constant, not a literal: an additive-field bump is legitimate and must not fail here.
    # The old-ledger literal in test_session_last_ingest_is_additive... is deliberately pinned.
    assert st.schema_version == SCHEMA_VERSION


def test_session_save_is_atomic_no_tmp_left(tmp_path):
    st = SessionState()
    p = tmp_path / "session.json"
    st.save(p)
    assert p.exists()
    assert not (tmp_path / "session.json.tmp").exists()   # temp cleaned by os.replace


def test_canonical_hash_stable_and_order_independent():
    a = canonical_hash({"x": 1, "y": [1, 2, 3]})
    b = canonical_hash({"y": [1, 2, 3], "x": 1})   # key order must not matter
    assert a == b
    c = canonical_hash({"x": 1, "y": [1, 2, 4]})
    assert a != c
