"""`isar drive` CLI plumbing: --max-iters validation, --control-token, --resume-latest (LLM-free),
the campaign_start provenance fingerprint, and the operator_stopped exit code.

These exercise the argument parser and the drive handler's config assembly with run_campaign stubbed —
the loop behaviour itself is covered by tests/drive/test_loop.py.
"""
import hashlib
import json

import pytest

from isar.cli.main import build_parser, main
import isar.drive.loop as loop_mod
import isar.drive.supervisor.goal_to_spec as g2s
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import log_event, read_log


_GOAL_SPEC = {"path": {"h_start": 2.1, "h_end": 2.4, "k": 0.0, "l": 0.0},
              "step_h": 0.1, "tolerance_meV": 0.5, "budget_hours": 8}


def _spec_file(tmp_path):
    p = tmp_path / "spec.json"
    p.write_text(json.dumps(_GOAL_SPEC), encoding="utf-8")
    return p


def _stub_run_campaign(monkeypatch, *, state="met"):
    """Replace run_campaign with a capture stub; returns the list it appends (objective, config) to."""
    captured = []

    def fake(objective, config):
        captured.append((objective, config))
        return {"stop": {"state": state, "reasons": ["stub"]},
                "session_log": str(config["workspace"]) + "/log"}
    monkeypatch.setattr(loop_mod, "run_campaign", fake)
    return captured


# --------------------------------------------------------------------------- --max-iters validation
def test_max_iters_rejects_zero_and_negative_at_argparse():
    parser = build_parser()
    for bad in ("0", "-1"):
        with pytest.raises(SystemExit):
            parser.parse_args(["drive", "--spec", "s.json", "--workspace", "w", "--max-iters", bad])
    # a valid value parses to the int
    ns = parser.parse_args(["drive", "--spec", "s.json", "--workspace", "w", "--max-iters", "3"])
    assert ns.max_iters == 3


def test_max_iters_lands_in_config(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    rc = main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "ws"),
               "--max-iters", "5"])
    assert rc == 0
    assert captured[0][1]["max_iters"] == 5


def test_max_iters_absent_is_not_in_config(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "ws")])
    assert "max_iters" not in captured[0][1]         # the loop's built-in default is used


def test_control_token_lands_in_config(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "ws"),
          "--control-token", "abc"])
    assert captured[0][1]["control_token"] == "abc"


# --------------------------------------------------------------------------- --resume-latest
def test_resume_latest_is_mutually_exclusive_with_goal_and_spec():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["drive", "--resume-latest", "--goal", "g", "--workspace", "w"])
    with pytest.raises(SystemExit):
        parser.parse_args(["drive", "--resume-latest", "--spec", "s.json", "--workspace", "w"])


def test_resume_latest_rebuilds_frozen_objective_without_llm(tmp_path, monkeypatch):
    # Seed a workspace session log with a campaign_start carrying a known objective.to_dict() + source.
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    log = ws / "report" / "session_log.jsonl"
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e13}, name="mvp")
    log_event(log, "decide", {"event": "campaign_start", "action": "campaign_start",
                              "objective": obj.to_dict(),
                              "source": {"kind": "goal", "sha256": "abc123"}})
    # Any goal_to_spec / LLM call on the resume path is a bug — make it explode if reached.
    monkeypatch.setattr(g2s, "goal_to_spec",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("LLM called on resume")))
    captured = _stub_run_campaign(monkeypatch)
    rc = main(["drive", "--resume-latest", "--workspace", str(ws)])
    assert rc == 0
    resumed_obj, config = captured[0]
    assert resumed_obj.to_dict() == obj.to_dict()               # equivalent, round-trip
    # The fingerprint is kind=resume with the sha256 carried over so it stays comparable across resumes.
    assert config["source"] == {"kind": "resume", "sha256": "abc123"}


def test_resume_latest_errors_cleanly_when_no_log(tmp_path, monkeypatch, capsys):
    _stub_run_campaign(monkeypatch)                             # never reached, but keep it isolated
    rc = main(["drive", "--resume-latest", "--workspace", str(tmp_path / "empty")])
    assert rc == 2
    assert "no campaign to resume" in capsys.readouterr().err


# --------------------------------------------------------------------------- provenance fingerprint
def test_spec_start_fingerprint_is_sha256_of_file_bytes(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    p = _spec_file(tmp_path)
    main(["drive", "--spec", str(p), "--workspace", str(tmp_path / "ws")])
    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    assert captured[0][1]["source"] == {"kind": "spec", "sha256": expected}


def test_goal_start_fingerprint_is_sha256_of_goal_text(tmp_path, monkeypatch):
    goal = "map the acoustic phonon (2,0,0)->(2.4,0,0) to 0.5 meV"
    monkeypatch.setattr(g2s, "goal_to_spec",
                        lambda g, **k: ObjectiveSpec.along_h(
                            h_start=2.0, h_end=2.4, step=0.1, tolerance_meV=0.5,
                            budget={"kind": "neutrons", "amount": 1e13}))
    captured = _stub_run_campaign(monkeypatch)
    main(["drive", "--goal", goal, "--workspace", str(tmp_path / "ws")])
    expected = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    assert captured[0][1]["source"] == {"kind": "goal", "sha256": expected}


# --------------------------------------------------------------------------- exit codes
def test_operator_stopped_maps_to_exit_code_4(tmp_path, monkeypatch):
    _stub_run_campaign(monkeypatch, state="operator_stopped")
    rc = main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "ws")])
    assert rc == 4


def test_unmeetable_and_met_exit_codes(tmp_path, monkeypatch):
    _stub_run_campaign(monkeypatch, state="met")
    assert main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "w1")]) == 0
    _stub_run_campaign(monkeypatch, state="unmeetable")
    assert main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "w2")]) == 3
