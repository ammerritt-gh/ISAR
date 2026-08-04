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


def test_analysis_limit_accepts_gui_minute_duration(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    assert main([
        "drive", "--spec", str(_spec_file(tmp_path)),
        "--workspace", str(tmp_path / "ws"), "--analysis-limit", "10m",
    ]) == 0
    assert captured[0][0].analysis_limit_seconds == 600.0


def test_control_token_lands_in_config(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "ws"),
          "--control-token", "abc"])
    assert captured[0][1]["control_token"] == "abc"


def test_control_token_rejects_filename_hostile_values():
    # The token is interpolated into report/control/stop-<token>.json — separators, spaces, or an
    # unbounded length would silently break stop detection or escape the control directory.
    parser = build_parser()
    for bad in ("bad/token", "..", "a b", "x" * 65, ""):
        with pytest.raises(SystemExit):
            parser.parse_args(["drive", "--spec", "s.json", "--workspace", "w",
                               "--control-token", bad])
    # the GUI's uuid4-style tokens fit the alphabet
    ns = parser.parse_args(["drive", "--spec", "s.json", "--workspace", "w",
                            "--control-token", "3f2a9c1e-77aa-4a5b-9c3d-2b1e0f4d5a6b"])
    assert ns.control_token == "3f2a9c1e-77aa-4a5b-9c3d-2b1e0f4d5a6b"


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


def test_resume_latest_keeps_frozen_tier_and_notes_an_explicit_override(tmp_path, monkeypatch, capsys):
    # The frozen objective's tier cannot be overridden on resume (a full ObjectiveSpec dict bypasses
    # the tier default) — an explicitly requested different --tier gets an honest stderr note instead
    # of being silently ignored; the plain resume stays quiet.
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    obj = ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=0.5,
                                budget={"kind": "neutrons", "amount": 1e13})
    log_event(ws / "report" / "session_log.jsonl", "decide",
              {"event": "campaign_start", "action": "campaign_start", "objective": obj.to_dict(),
               "source": {"kind": "goal", "sha256": "abc123"}})
    captured = _stub_run_campaign(monkeypatch)
    rc = main(["drive", "--resume-latest", "--workspace", str(ws), "--tier", "mcstas"])
    assert rc == 0
    assert captured[0][0].tier == obj.tier                       # the frozen tier won
    assert "frozen" in capsys.readouterr().err

    rc = main(["drive", "--resume-latest", "--workspace", str(ws)])
    assert rc == 0
    assert "frozen" not in capsys.readouterr().err               # no note without an explicit --tier


def test_resume_latest_errors_cleanly_when_no_log(tmp_path, monkeypatch, capsys):
    _stub_run_campaign(monkeypatch)                             # never reached, but keep it isolated
    rc = main(["drive", "--resume-latest", "--workspace", str(tmp_path / "empty")])
    assert rc == 2
    assert "no campaign to resume" in capsys.readouterr().err


def test_resume_loads_legacy_objective_without_explicit_scan_window_eig(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    (workspace / "report").mkdir(parents=True)
    objective = ObjectiveSpec.along_h(
        h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e3}).to_dict()
    del objective["scan_window_eig"]
    log_event(
        workspace / "report" / "session_log.jsonl", "decide",
        {"event": "campaign_start", "action": "campaign_start", "objective": objective,
         "source": {"kind": "goal", "sha256": "legacy"}},
    )
    captured = _stub_run_campaign(monkeypatch)
    assert main(["drive", "--resume-latest", "--workspace", str(workspace)]) == 0
    assert captured[0][0].scan_window_eig.policy_set is None
    assert captured[0][0].to_dict()["scan_window_eig"]["scoring_config"][
        "simulation_count"] == 128


# --------------------------------------------------------------------------- provenance fingerprint
def test_spec_start_fingerprint_is_sha256_of_file_bytes(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    p = _spec_file(tmp_path)
    main(["drive", "--spec", str(p), "--workspace", str(tmp_path / "ws")])
    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    source = captured[0][1]["source"]
    assert source["kind"] == "spec" and source["sha256"] == expected
    assert len(source["objective_fingerprint"]) == 64
    assert len(source["campaign_authoring_fingerprint"]) == 64


def test_goal_start_fingerprint_is_sha256_of_goal_text(tmp_path, monkeypatch):
    goal = "map the acoustic phonon (2,0,0)->(2.4,0,0) to 0.5 meV"
    monkeypatch.setattr(g2s, "goal_to_spec",
                        lambda g, **k: ObjectiveSpec.along_h(
                            h_start=2.0, h_end=2.4, step=0.1, tolerance_meV=0.5,
                            budget={"kind": "neutrons", "amount": 1e13}))
    captured = _stub_run_campaign(monkeypatch)
    main(["drive", "--goal", goal, "--workspace", str(tmp_path / "ws")])
    expected = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    source = captured[0][1]["source"]
    assert source["kind"] == "goal" and source["sha256"] == expected
    assert len(source["objective_fingerprint"]) == 64
    assert len(source["campaign_authoring_fingerprint"]) == 64


def test_shadow_mode_override_is_frozen_into_new_compact_spec(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    assert main([
        "drive", "--spec", str(_spec_file(tmp_path)),
        "--workspace", str(tmp_path / "ws"), "--scan-window-eig-mode", "shadow",
    ]) == 0
    assert captured[0][0].steering["scan_window_eig_mode"] == "shadow"


def test_resume_rejects_mode_that_conflicts_with_frozen_objective(
        tmp_path, monkeypatch, capsys):
    workspace = tmp_path / "ws"
    (workspace / "report").mkdir(parents=True)
    objective = ObjectiveSpec.along_h(
        h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e3})
    log_event(
        workspace / "report" / "session_log.jsonl", "decide",
        {"action": "campaign_start", "objective": objective.to_dict(),
         "source": {"kind": "goal", "sha256": "legacy"}},
    )
    _stub_run_campaign(monkeypatch)
    assert main([
        "drive", "--resume-latest", "--workspace", str(workspace),
        "--scan-window-eig-mode", "shadow",
    ]) == 2
    assert "conflicts with the frozen objective (off)" in capsys.readouterr().err


def test_natural_goal_passes_inline_card_eig_authoring_to_objective_compiler(tmp_path, monkeypatch):
    import isar.schema.card as card_module

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "card.policy.toml").write_text("# mocked card\n", encoding="utf-8")
    authoring = {"policies": []}

    class _Policy:
        hypotheses = {"acoustic": {}}
        scan_window_eig_policy_set = authoring

    monkeypatch.setattr(card_module, "load_policy", lambda _path: _Policy())
    seen = {}

    def fake_goal_to_spec(goal, **kwargs):
        seen.update(kwargs)
        return ObjectiveSpec.along_h(
            h_start=2.0, h_end=2.1, step=0.1, tolerance_meV=0.5,
            budget={"kind": "neutrons", "amount": 1e3})

    monkeypatch.setattr(g2s, "goal_to_spec", fake_goal_to_spec)
    _stub_run_campaign(monkeypatch)
    assert main(["drive", "--goal", "map acoustic", "--workspace", str(workspace)]) == 0
    assert seen["card_hypotheses"] == _Policy.hypotheses
    assert seen["card_eig_policy_set"] is authoring


# --------------------------------------------------------------------------- exit codes
def test_operator_stopped_maps_to_exit_code_4(tmp_path, monkeypatch):
    _stub_run_campaign(monkeypatch, state="operator_stopped")
    rc = main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "ws")])
    assert rc == 4


def test_pending_recovery_maps_to_exit_code_5(tmp_path, monkeypatch):
    _stub_run_campaign(monkeypatch, state="pending_recovery")
    rc = main(["drive", "--spec", str(_spec_file(tmp_path)),
               "--workspace", str(tmp_path / "ws")])
    assert rc == 5


def test_incomplete_infeasible_and_met_exit_codes(tmp_path, monkeypatch):
    _stub_run_campaign(monkeypatch, state="met")
    assert main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "w1")]) == 0
    _stub_run_campaign(monkeypatch, state="incomplete")
    assert main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "w2")]) == 3
    _stub_run_campaign(monkeypatch, state="infeasible")
    assert main(["drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(tmp_path / "w3")]) == 6


def test_compile_only_writes_frozen_envelope_without_running_campaign(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    out = tmp_path / "objective-envelope.json"
    rc = main(["drive", "--spec", str(_spec_file(tmp_path)), "--compile-only", str(out)])
    assert rc == 0 and captured == []
    envelope = json.loads(out.read_text(encoding="utf-8"))
    assert envelope["schema"] == "isar.objective-envelope/2"
    assert envelope["objective"]["path"][0] == [2.1, 0.0, 0.0]


def test_compact_spec_compile_only_freezes_inline_card_eig_policy(tmp_path, monkeypatch):
    import isar.schema.card as card_module

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "card.policy.toml").write_text("# mocked card\n", encoding="utf-8")

    class _Policy:
        hypotheses = {}
        scan_window_eig_policy_set = {"policies": []}

    monkeypatch.setattr(card_module, "load_policy", lambda _path: _Policy())
    out = tmp_path / "compact-envelope.json"
    assert main([
        "drive", "--spec", str(_spec_file(tmp_path)), "--workspace", str(workspace),
        "--compile-only", str(out),
    ]) == 0
    objective = json.loads(out.read_text(encoding="utf-8"))["objective"]
    assert objective["scan_window_eig"]["policy_set"]["policies"] == []
    assert objective["scan_window_eig"]["scoring_config"]["simulation_count"] == 128


def test_full_spec_never_reads_or_supplements_card_policy(tmp_path, monkeypatch):
    import isar.schema.card as card_module

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "card.policy.toml").write_text("malformed on purpose", encoding="utf-8")
    monkeypatch.setattr(
        card_module, "load_policy",
        lambda _path: (_ for _ in ()).throw(AssertionError("full spec read card policy")),
    )
    frozen = ObjectiveSpec.along_h(
        h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e3})
    legacy = frozen.to_dict()
    del legacy["scan_window_eig"]
    path = tmp_path / "frozen.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    captured = _stub_run_campaign(monkeypatch)
    assert main(["drive", "--spec", str(path), "--workspace", str(workspace)]) == 0
    assert captured[0][0].scan_window_eig.policy_set is None

    out = tmp_path / "legacy-compiled.json"
    assert main(["drive", "--spec", str(path), "--workspace", str(workspace),
                 "--compile-only", str(out)]) == 0
    compiled = json.loads(out.read_text(encoding="utf-8"))["objective"]
    assert compiled["scan_window_eig"]["policy_set"] is None
    assert compiled["scan_window_eig"]["scoring_config"]["simulation_count"] == 128


def test_envelope_start_preserves_original_source_fingerprint(tmp_path, monkeypatch):
    spec = ObjectiveSpec.segment(q_start=[2, 0, 0], q_end=[2, 0.5, 0], step_rlu=0.05,
                                 tolerance_meV=0.5,
                                 budget={"kind": "neutrons", "amount": 1e9},
                                 target_branch="optic")
    envelope = {"schema": "isar.objective-envelope/2", "objective": spec.to_dict(),
                "source": {"kind": "goal", "sha256": "frozen123"},
                "original_goal": "map transverse optic"}
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    captured = _stub_run_campaign(monkeypatch)
    assert main(["drive", "--envelope", str(path), "--workspace", str(tmp_path / "ws")]) == 0
    assert captured[0][0].target_branch == "optic"
    assert captured[0][1]["source"]["sha256"] == envelope["source"]["sha256"]
    assert captured[0][1]["source"]["original_goal"] == "map transverse optic"


def test_envelope_loads_legacy_objective_without_explicit_scan_window_eig(tmp_path, monkeypatch):
    objective = ObjectiveSpec.along_h(
        h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e3}).to_dict()
    del objective["scan_window_eig"]
    path = tmp_path / "legacy-envelope.json"
    path.write_text(json.dumps({
        "schema": "isar.objective-envelope/2", "objective": objective,
        "source": {"kind": "goal", "sha256": "legacy"},
    }), encoding="utf-8")
    captured = _stub_run_campaign(monkeypatch)
    assert main(["drive", "--envelope", str(path), "--workspace", str(tmp_path / "ws")]) == 0
    assert captured[0][0].scan_window_eig.policy_set is None
    assert captured[0][0].to_dict()["scan_window_eig"]["policy_set"] is None


def test_envelope_start_applies_absolute_budget_override(tmp_path, monkeypatch):
    spec = ObjectiveSpec.segment(q_start=[2, 0, 0], q_end=[2, 0.5, 0], step_rlu=0.05,
                                 tolerance_meV=0.5,
                                 budget={"kind": "neutrons", "amount": 1234},
                                 target_branch="optic")
    envelope = {"schema": "isar.objective-envelope/2", "objective": spec.to_dict(),
                "source": {"kind": "goal", "sha256": "frozen123"},
                "original_goal": "map transverse optic"}
    path = tmp_path / "envelope.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    captured = _stub_run_campaign(monkeypatch)
    rc = main(["drive", "--envelope", str(path), "--workspace", str(tmp_path / "ws"),
               "--budget", "8h"])
    assert rc == 0
    assert captured[0][0].budget == {"kind": "counting_seconds", "amount": 8 * 3600.0}
