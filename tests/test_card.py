"""Experiment-card gate (§6, D8): policy is human-authored TOML, state is machine-written JSON.

The two halves have opposite lifecycles and live in two files behind one in-memory `Card`:
`<exp>.policy.toml` is read-only to the machine (the scientist hand-edits it, comments and all);
`<exp>.state.json` is the only thing reduction rewrites. The repo rule: machine-written → JSON,
human-authored → TOML. These tests pin that ownership and the loaders.
"""
import json
from pathlib import Path

from isar.schema import card


POLICY_TOML = """\
# FeSe(1-x)S(x) sample 1 — HERIX, March 2025 beamtime
facility = "APS"
instrument = "S30"
format_version = "2025"
plugin = "spec_s30"

energy_column = "HerixE"
monitor_column = "ICO-C"
time_column = "Seconds"
counting_mode = "time"
normalize_by = "monitor"          # divide by ICO-C (the flux monitor), per the reference parser
raw_abscissa_kind = "energy"
energy_status = "needs_zero"
variance_convention = "none"
min_points = 5
hkl_round_step = 0.05
measurement_pattern = "fscan"
alignment_pattern = "sc a3"
exclusions_seed = [55217]          # bad mono feedback, T drifted

[temperature_rule]
setpoint_field = "T_Control"
sensor_fields = ["T_Sample"]
divergence_tol = 5.0

[collation]
key = ["sample", "q", "T"]
merge = "separate"

[[detectors]]
detector_id = "ana1"
value_index = 4
signal_column = "Ana1_3std"
enabled = true

[[detectors]]
detector_id = "ana5"
value_index = 8
signal_column = "Ana5_3std"
enabled = true
"""


def _write_policy(tmp_path: Path, body: str = POLICY_TOML) -> Path:
    p = tmp_path / "FeSe_1.policy.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_policy_from_toml(tmp_path):
    c = card.Card.load(_write_policy(tmp_path))
    pol = c.policy
    assert pol.instrument == "S30"
    assert pol.normalize_by == "monitor"
    assert pol.exclusions_seed == [55217]
    assert len(pol.detectors) == 2
    ana1 = pol.detectors[0]
    assert ana1.detector_id == "ana1"
    assert ana1.value_index == 4
    assert ana1.signal_column == "Ana1_3std"
    assert ana1.q_offset is None          # omitted in TOML → dataclass default
    # ana5 ↔ #V8 is the central analyzer (the D2 mapping).
    assert pol.detectors[1].value_index == 8


def test_state_empty_when_file_missing(tmp_path):
    c = card.Card.load(_write_policy(tmp_path))   # no state path given → fresh card
    assert c.state.exclusions == []
    assert c.state.corrections == []


def test_save_state_writes_json_and_never_touches_policy(tmp_path):
    policy_path = _write_policy(tmp_path)
    policy_bytes_before = policy_path.read_bytes()
    state_path = tmp_path / "FeSe_1.state.json"

    c = card.Card.load(policy_path, state_path)
    c.state.exclusions.append({"scope": "spectrum", "id": "53/ana3", "reason": "dead", "source": "human"})
    c.state.corrections.append({"id": "c1", "decision": "mask", "source": "llm"})
    c.save_state(state_path)

    # The machine writes JSON state...
    on_disk = json.loads(state_path.read_text())
    assert on_disk["exclusions"][0]["id"] == "53/ana3"
    # ...and reloading reconstructs it...
    reloaded = card.Card.load(policy_path, state_path)
    assert reloaded.state.exclusions == c.state.exclusions
    assert reloaded.state.corrections == c.state.corrections
    # ...and the human-authored policy.toml is byte-for-byte untouched (D8 ownership).
    assert policy_path.read_bytes() == policy_bytes_before


def test_save_state_is_atomic_no_temp_leftover(tmp_path):
    # The drive reads card.state.json across a process boundary to recover the branch after a crash,
    # so the write is atomic (temp + os.replace): valid JSON on disk, and no .tmp file left behind.
    policy_path = _write_policy(tmp_path)
    state_path = tmp_path / "FeSe_1.state.json"
    c = card.Card.load(policy_path, state_path)
    c.state.branch_models.append({"branch_id": "acoustic", "sample": "A", "t_bin": 300.0})
    c.save_state(state_path)

    json.loads(state_path.read_text())          # complete, parseable
    leftovers = list(tmp_path.glob("*.tmp*"))
    assert not leftovers, f"atomic write left temp files: {leftovers}"
    assert card.Card.load(policy_path, state_path).state.branch_models == c.state.branch_models


def test_validate_passes_a_well_formed_card(tmp_path):
    c = card.Card.load(_write_policy(tmp_path))
    assert c.validate() == []


def test_validate_flags_bad_energy_status(tmp_path):
    bad = POLICY_TOML.replace('energy_status = "needs_zero"', 'energy_status = "nonsense"')
    c = card.Card.load(_write_policy(tmp_path, bad))
    errs = c.validate()
    assert any("energy_status" in e for e in errs)


def test_validate_flags_duplicate_detector_ids(tmp_path):
    dup = POLICY_TOML.replace('detector_id = "ana5"', 'detector_id = "ana1"')
    c = card.Card.load(_write_policy(tmp_path, dup))
    errs = c.validate()
    assert any("ana1" in e for e in errs)
