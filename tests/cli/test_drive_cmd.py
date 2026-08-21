"""`isar drive` CLI plumbing: --control-token, --resume-latest (LLM-free),
the campaign_start provenance fingerprint, and the operator_stopped exit code.

These exercise the argument parser and the drive handler's config assembly with run_campaign stubbed —
the loop behaviour itself is covered by tests/drive/test_loop.py.
"""
import hashlib
import json

import pytest

from isar.cli.main import _parse_analysis_limit, _parse_budget, build_parser, main
import isar.drive.loop as loop_mod
import isar.drive.supervisor.goal_to_spec as g2s
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import log_event, read_log
from isar.schema.campaign_program import PROGRAM_SCHEMA, targeted_program


_GOAL_SPEC = {"path": {"h_start": 2.1, "h_end": 2.4, "k": 0.0, "l": 0.0},
              "step_h": 0.1, "tolerance_meV": 0.5, "budget_hours": 8}


def _program(payload=None, *, kind="targeted", step_id="targeted-1", **extra):
    return {"schema": PROGRAM_SCHEMA, **extra,
            "steps": [{"id": step_id, "kind": kind,
                       "payload": _GOAL_SPEC if payload is None else payload}]}


def _program_file(tmp_path, payload=None, *, name="program.json", **kw):
    p = tmp_path / name
    p.write_text(json.dumps(_program(payload, **kw)), encoding="utf-8")
    return p


def _stub_goal_program(monkeypatch, objective=None, *, capture=None):
    """Replace the supervisor with a stub that returns a one-step targeted program."""
    if objective is None:
        objective = ObjectiveSpec.along_h(h_start=2.0, h_end=2.4, step=0.1, tolerance_meV=0.5,
                                          budget={"kind": "neutrons", "amount": 1e13})

    def fake(goal, **kwargs):
        if capture is not None:
            capture.update(kwargs)
        return targeted_program(objective.to_dict(), source_text=goal)

    monkeypatch.setattr(g2s, "goal_to_program", fake)
    return objective


def _stub_run_campaign(monkeypatch, *, state="met"):
    """Replace run_campaign with a capture stub; returns the list it appends (objective, config) to."""
    captured = []

    def fake(objective, config):
        captured.append((objective, config))
        return {"stop": {"state": state, "reasons": ["stub"]},
                "session_log": str(config["workspace"]) + "/log"}
    monkeypatch.setattr(loop_mod, "run_campaign", fake)
    return captured


# --------------------------------------------------------------------------- no iteration cap
def test_no_max_iters_option_exists():
    # The budgets are the only stop conditions; an iteration cap silently truncated campaigns that
    # were still making progress with beam time left, so the flag is gone rather than deprecated.
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["drive", "--program", "p.json", "--workspace", "w", "--max-iters", "3"])
    ns = parser.parse_args(["drive", "--program", "p.json", "--workspace", "w"])
    assert not hasattr(ns, "max_iters")


def test_drive_config_carries_no_iteration_cap(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    main(["drive", "--program", str(_program_file(tmp_path)), "--workspace", str(tmp_path / "ws")])
    assert "max_iters" not in captured[0][1]


def test_analysis_limit_accepts_gui_minute_duration(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    assert main([
        "drive", "--program", str(_program_file(tmp_path)),
        "--workspace", str(tmp_path / "ws"), "--analysis-limit", "10m",
    ]) == 0
    assert captured[0][0].analysis_limit_seconds == 600.0


@pytest.mark.parametrize("text,expected", [
    ("10m", 600.0), ("10min", 600.0), ("10mins", 600.0), ("10minute", 600.0),
    ("10minutes", 600.0), ("10 min", 600.0), ("10s", 10.0), ("10sec", 10.0),
    ("2h", 7200.0), ("2hrs", 7200.0), ("2hours", 7200.0), ("90", 90.0),
])
def test_analysis_limit_parses_every_duration_abbreviation(text, expected):
    """``10min`` used to crash: the unit table matched ``n`` for neutrons before ``min``."""
    assert _parse_analysis_limit(text) == expected


def test_analysis_limit_accepts_none_and_rejects_neutrons():
    assert _parse_analysis_limit("none") is None
    with pytest.raises(ValueError):
        _parse_analysis_limit("1e10n")


@pytest.mark.parametrize("text", ["1e10n", "1e10neutron", "1e10neutrons"])
def test_budget_parses_neutrons_in_every_spelling(text):
    assert _parse_budget(text) == {"kind": "neutrons", "amount": 1e10}


@pytest.mark.parametrize("text", ["inf", "-inf", "1e999", "nan", "10mi", "10xyz", ""])
def test_budget_rejects_non_finite_and_unparseable_amounts(text):
    # A non-finite amount serializes as bare ``Infinity``, which is not JSON, and is the input that
    # made the GUI's budget line raise OverflowError mid-campaign.
    with pytest.raises(ValueError):
        _parse_budget(text)


def test_control_token_lands_in_config(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    main(["drive", "--program", str(_program_file(tmp_path)), "--workspace", str(tmp_path / "ws"),
          "--control-token", "abc"])
    assert captured[0][1]["control_token"] == "abc"


def test_control_token_rejects_filename_hostile_values():
    # The token is interpolated into report/control/stop-<token>.json — separators, spaces, or an
    # unbounded length would silently break stop detection or escape the control directory.
    parser = build_parser()
    for bad in ("bad/token", "..", "a b", "x" * 65, ""):
        with pytest.raises(SystemExit):
            parser.parse_args(["drive", "--program", "p.json", "--workspace", "w",
                               "--control-token", bad])
    # the GUI's uuid4-style tokens fit the alphabet
    ns = parser.parse_args(["drive", "--program", "p.json", "--workspace", "w",
                            "--control-token", "3f2a9c1e-77aa-4a5b-9c3d-2b1e0f4d5a6b"])
    assert ns.control_token == "3f2a9c1e-77aa-4a5b-9c3d-2b1e0f4d5a6b"


# --------------------------------------------------------------------------- --resume-latest
def test_resume_latest_is_mutually_exclusive_with_goal_and_program():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["drive", "--resume-latest", "--goal", "g", "--workspace", "w"])
    with pytest.raises(SystemExit):
        parser.parse_args(["drive", "--resume-latest", "--program", "p.json", "--workspace", "w"])


def test_the_retired_source_flags_no_longer_exist():
    # H1: --spec and --envelope are not deprecated aliases, they are gone. argparse must reject
    # them outright rather than prefix-matching --spec onto some future flag.
    parser = build_parser()
    for retired in ("--spec", "--envelope"):
        with pytest.raises(SystemExit):
            parser.parse_args(["drive", retired, "x.json", "--workspace", "w"])


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
    monkeypatch.setattr(g2s, "goal_to_program",
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


def test_resume_latest_refuses_a_discovery_campaign_by_name(tmp_path, monkeypatch, capsys):
    """A discovery campaign_start carries the program-step seam's CARRIER objective, not a
    resumable target. Fed to the objective reader it fails as a schema complaint about something
    the operator never authored; the refusal has to name the real reason."""
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True)
    carrier = ObjectiveSpec(path=[(2.0, 0.0, 0.0), (3.0, 0.0, 0.0)], tolerance_meV=0.0,
                            budget=None, name="discover:discover-1",
                            meta={"discover_and_map": True, "step_id": "discover-1"})
    log_event(ws / "report" / "session_log.jsonl", "decide",
              {"event": "campaign_start", "action": "campaign_start",
               "objective": carrier.to_dict(), "source": {"kind": "program", "sha256": "abc"}})
    captured = _stub_run_campaign(monkeypatch)

    assert main(["drive", "--resume-latest", "--workspace", str(ws)]) == 2
    assert captured == []
    error = capsys.readouterr().err
    assert "--resume-latest cannot resume a discover_and_map campaign" in error
    assert "--program" in error


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
def test_program_start_fingerprint_is_sha256_of_file_bytes(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    p = _program_file(tmp_path)
    main(["drive", "--program", str(p), "--workspace", str(tmp_path / "ws")])
    expected = hashlib.sha256(p.read_bytes()).hexdigest()
    source = captured[0][1]["source"]
    assert source["kind"] == "program" and source["sha256"] == expected
    assert source["program_schema"] == PROGRAM_SCHEMA
    assert source["program_step_id"] == "targeted-1"
    assert len(source["objective_fingerprint"]) == 64
    assert len(source["campaign_authoring_fingerprint"]) == 64


def test_goal_start_fingerprint_is_sha256_of_goal_text(tmp_path, monkeypatch):
    goal = "map the acoustic phonon (2,0,0)->(2.4,0,0) to 0.5 meV"
    _stub_goal_program(monkeypatch)
    captured = _stub_run_campaign(monkeypatch)
    main(["drive", "--goal", goal, "--workspace", str(tmp_path / "ws")])
    expected = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    source = captured[0][1]["source"]
    assert source["kind"] == "goal" and source["sha256"] == expected
    # The operator's command travels with the campaign verbatim, replacing original_goal.
    assert source["source_text"] == goal
    assert source["program_schema"] == PROGRAM_SCHEMA
    assert len(source["objective_fingerprint"]) == 64
    assert len(source["campaign_authoring_fingerprint"]) == 64


def test_shadow_mode_cli_choice_is_retired(tmp_path, monkeypatch, capsys):
    """SCENE-4 PR 4.4, D3: "shadow" is retired — the CLI narrows its own ``choices`` rather than
    accepting it and refusing downstream, so argparse itself rejects it (exit code 2) before any
    objective is built. Was test_shadow_mode_override_is_frozen_into_new_compact_spec, which pinned
    the opposite (that "shadow" compiled cleanly)."""
    _stub_run_campaign(monkeypatch)
    with pytest.raises(SystemExit) as raised:
        main([
            "drive", "--program", str(_program_file(tmp_path)),
            "--workspace", str(tmp_path / "ws"), "--scan-window-eig-mode", "shadow",
        ])
    assert raised.value.code == 2
    assert "invalid choice: 'shadow'" in capsys.readouterr().err


def test_mode_override_rebinds_the_card_eig_policy_to_the_new_objective_scope(
        tmp_path, monkeypatch):
    """--scan-window-eig-mode recomputes the hypothesis fingerprint, which is part of the objective
    comparison scope a card EIG policy binds to. Without a rebind the policy stops matching and the
    whole campaign scores qoi_policy_missing (the O41 Test9 failure)."""
    import isar.schema.card as card_module
    from isar.drive.objective import current_objective_comparison_scope
    from isar.schema.eig_policy import matching_qoi_policies

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "card.policy.toml").write_text("# mocked card\n", encoding="utf-8")
    authoring = {"policies": [{
        "policy_id": "eig4_slice8_excitation_energy",
        "policy_kind": "isar.scalar_absolute_partitions/o36.5",
        "source_qoi": {"id": "excitation.energy", "unit": "meV"},
        "scope": {"bind_to_current_campaign_configuration": True,
                  "annotation": {"campaign_name": "regression",
                                 "experiment_description": "policy scope rebinding",
                                 "date": "2026-08-05"}},
        "provenance": {"kind": "explicit_user_analysis/1", "source_id": "O41",
                       "rationale": "regression fixture"},
        "variants": [{"variant_id": "primary_0p5_mev",
                      "parameters": {"origin": 0.0, "bin_width": 0.5, "offsets": [0.0, 0.25]}}],
    }]}

    class _Policy:
        hypotheses = None
        scan_window_eig_policy_set = authoring

    monkeypatch.setattr(card_module, "load_policy", lambda _path: _Policy())
    captured = _stub_run_campaign(monkeypatch)
    assert main([
        "drive", "--program", str(_program_file(tmp_path)), "--workspace", str(workspace),
        "--scan-window-eig-mode", "authoritative",
    ]) == 0
    objective = captured[0][0]
    assert objective.steering["scan_window_eig_mode"] == "authoritative"
    scope = current_objective_comparison_scope(objective)
    assert [policy.policy_id for policy in matching_qoi_policies(
        objective.scan_window_eig.policy_set, qoi_id="excitation.energy", qoi_unit="meV",
        scope_kind="campaign_configuration",
        scope_fingerprint=scope["fingerprint"])] == ["eig4_slice8_excitation_energy"]


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
        "--scan-window-eig-mode", "authoritative",
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
    _stub_goal_program(
        monkeypatch,
        ObjectiveSpec.along_h(h_start=2.0, h_end=2.1, step=0.1, tolerance_meV=0.5,
                              budget={"kind": "neutrons", "amount": 1e3}),
        capture=seen)
    _stub_run_campaign(monkeypatch)
    assert main(["drive", "--goal", "map acoustic", "--workspace", str(workspace)]) == 0
    assert seen["card_hypotheses"] == _Policy.hypotheses
    assert seen["card_eig_policy_set"] is authoring


# --------------------------------------------------------------------------- exit codes
def test_operator_stopped_maps_to_exit_code_4(tmp_path, monkeypatch):
    _stub_run_campaign(monkeypatch, state="operator_stopped")
    rc = main(["drive", "--program", str(_program_file(tmp_path)),
               "--workspace", str(tmp_path / "ws")])
    assert rc == 4


def test_pending_recovery_maps_to_exit_code_5(tmp_path, monkeypatch):
    _stub_run_campaign(monkeypatch, state="pending_recovery")
    rc = main(["drive", "--program", str(_program_file(tmp_path)),
               "--workspace", str(tmp_path / "ws")])
    assert rc == 5


def test_incomplete_infeasible_and_met_exit_codes(tmp_path, monkeypatch):
    _stub_run_campaign(monkeypatch, state="met")
    program = _program_file(tmp_path)
    assert main(["drive", "--program", str(program), "--workspace", str(tmp_path / "w1")]) == 0
    _stub_run_campaign(monkeypatch, state="incomplete")
    assert main(["drive", "--program", str(program), "--workspace", str(tmp_path / "w2")]) == 3
    _stub_run_campaign(monkeypatch, state="infeasible")
    assert main(["drive", "--program", str(program), "--workspace", str(tmp_path / "w3")]) == 6


def test_compile_only_writes_a_frozen_program_without_running_campaign(tmp_path, monkeypatch):
    captured = _stub_run_campaign(monkeypatch)
    out = tmp_path / "campaign-program.json"
    rc = main(["drive", "--program", str(_program_file(tmp_path)), "--compile-only", str(out)])
    assert rc == 0 and captured == []
    program = json.loads(out.read_text(encoding="utf-8"))
    assert program["schema"] == PROGRAM_SCHEMA
    step = program["steps"][0]
    assert step["kind"] == "targeted" and step["payload"]["path"][0] == [2.1, 0.0, 0.0]
    assert program["source"]["kind"] == "program"


def test_a_compiled_program_starts_a_campaign_with_its_original_fingerprint(tmp_path, monkeypatch):
    """--compile-only then --program is the GUI's confirm-then-start route.

    The compiled program carries the fingerprint of the authoring input it came from, so the dock's
    Resume-vs-Start comparison still recognises the campaign it just confirmed.
    """
    _stub_run_campaign(monkeypatch)
    authoring = _program_file(tmp_path)
    out = tmp_path / "compiled.json"
    assert main(["drive", "--program", str(authoring), "--compile-only", str(out)]) == 0
    captured = _stub_run_campaign(monkeypatch)
    assert main(["drive", "--program", str(out), "--workspace", str(tmp_path / "ws")]) == 0
    assert captured[0][1]["source"]["sha256"] == hashlib.sha256(
        authoring.read_bytes()).hexdigest()
    assert captured[0][0].path[0] == (2.1, 0.0, 0.0)


def test_compiled_goal_program_carries_the_command_verbatim(tmp_path, monkeypatch):
    goal = "map the acoustic phonon (2,0,0)->(2.4,0,0) to 0.5 meV"
    _stub_goal_program(monkeypatch)
    _stub_run_campaign(monkeypatch)
    out = tmp_path / "compiled.json"
    assert main(["drive", "--goal", goal, "--compile-only", str(out)]) == 0
    program = json.loads(out.read_text(encoding="utf-8"))
    assert program["source_text"] == goal
    captured = _stub_run_campaign(monkeypatch)
    assert main(["drive", "--program", str(out), "--workspace", str(tmp_path / "ws")]) == 0
    assert captured[0][1]["source"]["source_text"] == goal


# ------------------------------------------------------------- steps this build cannot execute
from isar.schema.question_policy import resolve_policy as _resolve_policy

_DISCOVER_PAYLOAD = {
    "schema": "isar.campaign-step.discover_and_map/2",
    "region": {"kind": "path", "q_start": [2.0, 0.0, 0.0], "q_end": [3.0, 0.0, 0.0],
               "maximum_initial_spacing_rlu": 0.1},
    "energy": {"intrinsic_omega_domain_meV": [0.0, 10.0]},
    "catalogue_profile": "isar.catalogue.inelastic-default/1",
    "deliverables": [{"kind": "phenomenon_map"}],
    "include_unknown": True,
    "resolved_policy": _resolve_policy(),
}


def _stub_run_program(monkeypatch, *, verdict="complete", directive="release"):
    """Replace the SCENE-4.3 program-step seam with a capture stub (SCENE-4.3 C5)."""
    from isar.drive import program_runner

    captured = []

    def fake(program, step_id=None, config=None):
        captured.append((program, step_id, config))
        result = program_runner.StepResult(step_id=step_id, kind="discover_and_map", deliveries=2)
        return program_runner.ProgramStepOutcome(result=result, verdict=verdict,
                                                 directive=directive)

    monkeypatch.setattr(program_runner, "run_program", fake)
    return captured


def test_a_discovery_step_now_routes_into_the_program_step_seam(tmp_path, monkeypatch, capsys):
    """The exit-7 gate LIFTS for ``discover_and_map`` (SCENE-4.3 C5): the step froze cleanly and
    this build has a runner for it, so it runs. The targeted path is untouched — ``run_campaign``
    is never called, and the discovery config carries no ObjectiveSpec."""
    targeted = _stub_run_campaign(monkeypatch)
    captured = _stub_run_program(monkeypatch)
    path = _program_file(tmp_path, _DISCOVER_PAYLOAD, kind="discover_and_map",
                         step_id="discover-1")

    rc = main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws")])

    assert rc == 0 and targeted == []
    assert len(captured) == 1
    program, step_id, config = captured[0]
    assert step_id == "discover-1"
    assert config["workspace"] == str(tmp_path / "ws")
    assert config["budget"] is None
    assert "discover-1 complete" in capsys.readouterr().out


def test_the_discovery_exit_code_vocabulary_matches_the_targeted_one(tmp_path, monkeypatch):
    """One vocabulary, so an operator script branches once: 0 met, 3 incomplete, 4 operator stop."""
    path = _program_file(tmp_path, _DISCOVER_PAYLOAD, kind="discover_and_map",
                         step_id="discover-1")
    for verdict, code in (("complete", 0), ("over_budget", 3), ("in_progress", 3),
                          ("operator_stopped", 4), ("machine_failure", 2)):
        _stub_run_program(monkeypatch, verdict=verdict)
        assert main(["drive", "--program", str(path),
                     "--workspace", str(tmp_path / "ws")]) == code, verdict


def test_a_discovery_budget_override_travels_as_run_configuration(tmp_path, monkeypatch):
    """``--budget`` still applies: a frozen discovery step has no ObjectiveSpec to mutate, so the
    override travels in the run config rather than being folded into a compiled objective."""
    captured = _stub_run_program(monkeypatch)
    path = _program_file(tmp_path, _DISCOVER_PAYLOAD, kind="discover_and_map",
                         step_id="discover-1")
    assert main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws"),
                 "--budget", "1e10n"]) == 0
    assert captured[0][2]["budget"] == {"kind": "neutrons", "amount": 1e10}


def test_an_unknown_step_kind_still_exits_7(tmp_path, monkeypatch, capsys):
    """The gate did not open, it MOVED: a kind with no runner still froze cleanly and still
    refuses with exit 7 rather than crashing — through ``main``, which is where an operator meets
    the code."""
    from isar.schema import campaign_program as cp

    targeted = _stub_run_campaign(monkeypatch)
    discovery = _stub_run_program(monkeypatch)
    monkeypatch.setattr(cp, "STEP_KINDS", ("targeted", "discover_and_map", "align"))
    document = _program(_GOAL_SPEC)
    document["steps"] = [{"id": "a1", "kind": "align", "payload": {}}]
    path = tmp_path / "align.json"
    path.write_text(json.dumps(document), encoding="utf-8")

    rc = main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws")])

    assert rc == 7 and targeted == [] and discovery == []
    assert "align" in capsys.readouterr().err


def test_a_malformed_discovery_payload_is_a_parse_error_not_a_refusal(tmp_path, monkeypatch,
                                                                     capsys):
    _stub_run_campaign(monkeypatch)
    payload = json.loads(json.dumps(_DISCOVER_PAYLOAD))
    payload["energy"]["intrinsic_omega_domain_meV"] = [-10.0, 10.0]
    path = _program_file(tmp_path, payload, kind="discover_and_map", step_id="discover-1")
    assert main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws")]) == 2
    assert "not a signed measurement window" in capsys.readouterr().err


def test_a_multi_step_program_freezes_but_refuses_to_sequence(tmp_path, monkeypatch, capsys):
    captured = _stub_run_campaign(monkeypatch)
    document = _program(_GOAL_SPEC)
    document["steps"].append({"id": "discover-2", "kind": "discover_and_map",
                              "payload": _DISCOVER_PAYLOAD})
    path = tmp_path / "two-step.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    rc = main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws")])
    assert rc == 7 and captured == []
    assert "2 steps" in capsys.readouterr().err


def test_two_valid_targeted_steps_still_refuse_to_sequence(tmp_path, monkeypatch, capsys):
    """Exit 7 stays exit 7 when every payload really does validate — that is what it claims."""
    captured = _stub_run_campaign(monkeypatch)
    document = _program(_GOAL_SPEC)
    document["steps"].append({"id": "targeted-2", "kind": "targeted", "payload": _GOAL_SPEC})
    path = tmp_path / "two-targeted.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    rc = main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws")])
    assert rc == 7 and captured == []
    assert "2 steps" in capsys.readouterr().err


def test_a_malformed_later_step_is_a_parse_error_not_the_multi_step_refusal(tmp_path, monkeypatch,
                                                                           capsys):
    """The refusal says the program "froze cleanly" — so every payload must validate before it.

    Refusing on the step count first reported "valid but unsupported" (7) for a program that was
    simply written wrong (2), sending the operator after a missing feature instead of their typo.
    """
    captured = _stub_run_campaign(monkeypatch)
    document = _program(_GOAL_SPEC)
    document["steps"].append({"id": "targeted-2", "kind": "targeted",
                              "payload": {"path": {"h_start": 2.0}, "step_h": 0.1}})
    path = tmp_path / "two-step-malformed.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    rc = main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws")])
    assert rc == 2 and captured == []
    assert "targeted-2" in capsys.readouterr().err


def test_a_retired_envelope_file_is_refused_with_the_new_contract_named(tmp_path, monkeypatch,
                                                                       capsys):
    _stub_run_campaign(monkeypatch)
    spec = ObjectiveSpec.along_h(h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.5, budget=None)
    path = tmp_path / "old-envelope.json"
    path.write_text(json.dumps({"schema": "isar.objective-envelope/2",
                                "objective": spec.to_dict(),
                                "source": {"kind": "goal", "sha256": "frozen123"}}),
                    encoding="utf-8")
    assert main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws")]) == 2
    err = capsys.readouterr().err
    assert "isar.objective-envelope/2" in err and "isar.campaign-program/1" in err


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
    # This card mock declares an EMPTY scan-window EIG policy set on purpose (asserted below), so
    # the objective must ask for "off" explicitly: the compiler's authoritative default requires
    # exactly one O41 policy, which an empty set cannot satisfy.
    payload = {**_GOAL_SPEC, "steering": {"scan_window_eig_mode": "off"}}
    assert main([
        "drive", "--program", str(_program_file(tmp_path, payload)), "--workspace", str(workspace),
        "--compile-only", str(out),
    ]) == 0
    objective = json.loads(out.read_text(encoding="utf-8"))["steps"][0]["payload"]
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
    path = _program_file(tmp_path, legacy, name="frozen.json")
    captured = _stub_run_campaign(monkeypatch)
    assert main(["drive", "--program", str(path), "--workspace", str(workspace)]) == 0
    assert captured[0][0].scan_window_eig.policy_set is None

    out = tmp_path / "legacy-compiled.json"
    assert main(["drive", "--program", str(path), "--workspace", str(workspace),
                 "--compile-only", str(out)]) == 0
    compiled = json.loads(out.read_text(encoding="utf-8"))["steps"][0]["payload"]
    assert compiled["scan_window_eig"]["policy_set"] is None
    assert compiled["scan_window_eig"]["scoring_config"]["simulation_count"] == 128


def test_a_frozen_program_start_preserves_its_original_source_fingerprint(tmp_path, monkeypatch):
    spec = ObjectiveSpec.segment(q_start=[2, 0, 0], q_end=[2, 0.5, 0], step_rlu=0.05,
                                 tolerance_meV=0.5,
                                 budget={"kind": "neutrons", "amount": 1e9},
                                 target_branch="optic")
    path = tmp_path / "program.json"
    path.write_text(json.dumps(_program(spec.to_dict(),
                                        source={"kind": "goal", "sha256": "frozen123"},
                                        source_text="map transverse optic")), encoding="utf-8")
    captured = _stub_run_campaign(monkeypatch)
    assert main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws")]) == 0
    assert captured[0][0].target_branch == "optic"
    assert captured[0][1]["source"]["sha256"] == "frozen123"
    assert captured[0][1]["source"]["source_text"] == "map transverse optic"


def test_a_program_loads_a_frozen_objective_without_explicit_scan_window_eig(tmp_path,
                                                                            monkeypatch):
    objective = ObjectiveSpec.along_h(
        h_start=2.1, h_end=2.2, step=0.1, tolerance_meV=0.5,
        budget={"kind": "neutrons", "amount": 1e3}).to_dict()
    del objective["scan_window_eig"]
    path = _program_file(tmp_path, objective, source={"kind": "goal", "sha256": "legacy"})
    captured = _stub_run_campaign(monkeypatch)
    assert main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws")]) == 0
    assert captured[0][0].scan_window_eig.policy_set is None
    assert captured[0][0].to_dict()["scan_window_eig"]["policy_set"] is None


def test_a_frozen_program_start_applies_an_absolute_budget_override(tmp_path, monkeypatch):
    spec = ObjectiveSpec.segment(q_start=[2, 0, 0], q_end=[2, 0.5, 0], step_rlu=0.05,
                                 tolerance_meV=0.5,
                                 budget={"kind": "neutrons", "amount": 1234},
                                 target_branch="optic")
    path = _program_file(tmp_path, spec.to_dict(),
                         source={"kind": "goal", "sha256": "frozen123"})
    captured = _stub_run_campaign(monkeypatch)
    rc = main(["drive", "--program", str(path), "--workspace", str(tmp_path / "ws"),
               "--budget", "8h"])
    assert rc == 0
    assert captured[0][0].budget == {"kind": "counting_seconds", "amount": 8 * 3600.0}


# ------------------------------------------------------- --goal is prose, --program is a file
def test_a_program_path_passed_to_goal_is_refused_rather_than_compiled(tmp_path, capsys,
                                                                      monkeypatch):
    """Handing a path to the LLM supervisor does not fail — it compiles a *different* campaign.

    A spec for the acoustic branch along h 2.0→3.0 compiled instead as an optic campaign along
    [hh0] from the zone origin over [-2, 12] meV, and the only symptom was an immediately exhausted
    action inventory. The file must be refused before it reaches the supervisor.
    """
    program = _program_file(tmp_path)
    called = []
    monkeypatch.setattr(g2s, "goal_to_program", lambda *a, **k: called.append(a) or None)

    code = main(["drive", "--goal", str(program), "--workspace", str(tmp_path / "ws")])

    assert code == 2
    assert called == []
    err = capsys.readouterr().err
    assert "natural-language measurement goal, not a file path" in err
    assert f"--program {program}" in err


def test_a_missing_spec_suffix_is_still_refused_as_a_path(tmp_path, capsys):
    """A mistyped path is the same mistake as a correct one, so absence is not the test."""
    code = main(["drive", "--goal", str(tmp_path / "does-not-exist.json"),
                 "--workspace", str(tmp_path / "ws")])

    assert code == 2
    assert "not a file path" in capsys.readouterr().err


def test_a_real_natural_language_goal_still_reaches_the_supervisor(tmp_path, monkeypatch):
    seen = []

    def _fake(goal, **kwargs):
        seen.append(goal)
        raise RuntimeError("stop here — reaching the supervisor is the whole assertion")

    monkeypatch.setattr("isar.drive.supervisor.goal_to_spec.goal_to_program", _fake)
    with pytest.raises(RuntimeError, match="stop here"):
        main(["drive", "--goal", "measure the acoustic branch along h from 2 to 3",
              "--workspace", str(tmp_path / "ws")])

    assert seen == ["measure the acoustic branch along h from 2 to 3"]
