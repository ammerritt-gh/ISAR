"""Qt-free command-builder + instrument-preview tests.

The Files-dock bootstrap flow and the Run action both shell the same engine (``isar init`` /
``isar ingest`` / ``isar run``) via subprocess; the argument assembly and the instrument preview live
in ``isar_gui.commands`` so they are unit-tested here with no Qt and no QApplication.
"""
from __future__ import annotations

from pathlib import Path

from isar_gui import commands
from isar_gui.commands import DetectResult, DriveOptions, RunOptions


def test_module_is_qt_and_isar_free():
    """commands.py must import without pulling in PySide6 or any isar engine module (G16)."""
    import ast

    src = Path(commands.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not any(m == "isar" or m.startswith("isar.") for m in imported), imported
    assert not any(m.startswith("PySide6") for m in imported), imported


def test_build_init_command_basic():
    cmd = commands.build_init_command("py.exe", r"D:\beam\numors", r"D:\ws\exp1")
    assert cmd == ["py.exe", "-m", "isar", "init", r"D:\beam\numors", "--out", r"D:\ws\exp1"]


def test_build_run_command_defaults_are_minimal():
    """Engine-default options add no flags — the command stays clean."""
    cmd = commands.build_run_command("py.exe", [r"D:\beam\numors"], r"D:\ws\exp1", RunOptions())
    assert cmd == ["py.exe", "-m", "isar", "run", r"D:\beam\numors", "--workspace", r"D:\ws\exp1"]


def test_build_run_command_multiple_data_paths_are_positional():
    cmd = commands.build_run_command("py", ["a", "b", "c"], "ws", RunOptions())
    assert cmd[:5] == ["py", "-m", "isar", "run", "a"]
    assert cmd[3:7] == ["run", "a", "b", "c"]
    assert cmd[-2:] == ["--workspace", "ws"]


def test_build_run_command_maps_nondefault_options():
    opts = RunOptions(llm=True, force=True, M=400, errors="bayes", detectors="ana5,ana7")
    cmd = commands.build_run_command("py", ["d"], "ws", opts)
    assert "--llm" in cmd
    assert "--force" in cmd
    assert cmd[cmd.index("--M") + 1] == "400"
    assert cmd[cmd.index("--errors") + 1] == "bayes"
    assert cmd[cmd.index("--detectors") + 1] == "ana5,ana7"


def test_build_run_command_omits_default_M_and_errors():
    opts = RunOptions(M=200, errors="full")
    cmd = commands.build_run_command("py", ["d"], "ws", opts)
    assert "--M" not in cmd
    assert "--errors" not in cmd
    assert "--detectors" not in cmd


def test_default_workspace_dir_sibling_of_data():
    """The suggested workspace sits next to the data, named isar_<instr>_<date> (init parity)."""
    out = commands.default_workspace_dir(r"D:\beam\numors", "S30", "20260712")
    assert Path(out).name == "isar_S30_20260712"
    assert Path(out).parent == Path(r"D:\beam").resolve() or Path(out).parent == Path(r"D:\beam")


def test_default_workspace_dir_for_a_file_uses_its_parent():
    out = commands.default_workspace_dir(r"D:\beam\scan_055182", "IN8", "20260712")
    assert Path(out).name == "isar_IN8_20260712"


def test_build_detect_command():
    assert commands.build_detect_command("py", r"D:\d") == ["py", "-m", "isar", "detect", r"D:\d",
                                                            "--json"]


def test_parse_detect_json_supported():
    text = ('some warning line\n'
            '{"facility": "APS", "instrument": "S30", "fmt": "raw", '
            '"plugin_name": "s30", "probe": "spec_001.dat"}\n')
    res = commands.parse_detect_json(text)
    assert res == DetectResult("APS", "S30", "raw", "s30", "spec_001.dat")
    assert res.supported


def test_parse_detect_json_unsupported_when_no_plugin():
    res = commands.parse_detect_json('{"facility": "SP8", "instrument": "BL43", "fmt": "spec", '
                                     '"plugin_name": null, "probe": "x"}')
    assert res is not None and not res.supported


def test_parse_detect_json_none_on_empty_or_garbage():
    assert commands.parse_detect_json("{}") is None
    assert commands.parse_detect_json("not json at all") is None
    assert commands.parse_detect_json("") is None


def test_last_error_line_typical_stderr_tail():
    lines = ["starting init...", "error: workspace already exists", "cleaning up"]
    assert commands.last_error_line(lines) == "workspace already exists"


def test_last_error_line_multiple_error_lines_last_wins():
    lines = ["error: first problem", "retrying...", "error: second problem"]
    assert commands.last_error_line(lines) == "second problem"


def test_last_error_line_none_when_no_error_line():
    lines = ["starting init...", "scaffolding workspace", "done"]
    assert commands.last_error_line(lines) is None


def test_last_error_line_leading_whitespace_tolerated():
    lines = ["  error: indented cause  "]
    assert commands.last_error_line(lines) == "indented cause"


def test_last_error_line_bare_prefix_with_no_cause_is_none():
    lines = ["starting init...", "error:", "aborting"]
    assert commands.last_error_line(lines) is None


# ---- ingest command + JSON parse (D1) --------------------------------------
def test_build_ingest_command_json():
    cmd = commands.build_ingest_command("py.exe", [r"D:\a", r"D:\b"], r"D:\ws", json=True)
    assert cmd == ["py.exe", "-m", "isar", "ingest", r"D:\a", r"D:\b", "--workspace", r"D:\ws",
                   "--json"]


def test_build_ingest_command_without_json_omits_flag():
    cmd = commands.build_ingest_command("py", ["d"], "ws")
    assert "--json" not in cmd and cmd[3] == "ingest"


def _ingest_success_line():
    import json
    return json.dumps({
        "workspace": r"D:\ws",
        "files": [{"path": r"D:\ws\s1.dat", "class": "measurement", "n_scans": 2,
                   "scan_ids": ["exp:1", "exp:2"], "n_context": 0}],
        "scans": {"added": ["exp:1", "exp:2"], "updated": [], "unchanged": []},
        "deduped": [{"scan": "exp:1", "kept": "b.dat", "superseded": ["a.dat"]}],
        "gaps": [["exp", 3, 5]]})


def test_parse_ingest_json_valid():
    rep = commands.parse_ingest_json("progress banner\n" + _ingest_success_line() + "\n")
    assert rep is not None
    assert len(rep.files) == 1 and rep.files[0]["class"] == "measurement"
    assert rep.added == ("exp:1", "exp:2")
    assert rep.updated == () and rep.unchanged == ()
    assert rep.deduped[0]["kept"] == "b.dat"
    assert rep.gaps == (("exp", 3, 5),)


def test_parse_ingest_json_accepts_line_iterable():
    rep = commands.parse_ingest_json(["noise", _ingest_success_line()])
    assert rep is not None and rep.added == ("exp:1", "exp:2")


def test_parse_ingest_json_ignores_log_noise_around_the_object():
    text = "reducing...\nqc pass\n" + _ingest_success_line() + "\n(done)\n"
    assert commands.parse_ingest_json(text) is not None


def test_parse_ingest_json_none_on_garbage_or_error_object():
    assert commands.parse_ingest_json("not json") is None
    assert commands.parse_ingest_json("") is None
    # An error object is not a success object (no "files" key) — parse yields None.
    assert commands.parse_ingest_json('{"error": {"kind": "no_workspace", "message": "x"}}') is None


def test_ingest_failure_cause_from_json_error():
    lines = ['{"error": {"kind": "no_workspace", "message": "run init first"}}']
    assert commands.ingest_failure_cause(lines) == "no_workspace: run init first"


def test_ingest_failure_cause_falls_back_to_error_line():
    lines = ["parsing...", "error: could not read numor"]
    assert commands.ingest_failure_cause(lines) == "could not read numor"


def test_ingest_failure_cause_generic_fallback():
    assert commands.ingest_failure_cause(["nothing useful"]).startswith("ingest failed")


def test_last_exception_line_lifts_traceback_tail():
    lines = [
        "Traceback (most recent call last):",
        '  File "ill_in8.py", line 58, in _bs_q_and_deltas',
        "    q = (float(toks[2]), float(toks[3]), float(toks[4]))",
        "ValueError: could not convert string to float: 'da3p'",
    ]
    assert commands.last_exception_line(lines) == \
        "ValueError: could not convert string to float: 'da3p'"


def test_last_exception_line_matches_dotted_custom_exception():
    lines = ["some noise", "isar.schema.card.CardError: bad section"]
    assert commands.last_exception_line(lines) == "isar.schema.card.CardError: bad section"


def test_last_exception_line_none_when_no_exception_shape():
    assert commands.last_exception_line(["just progress", "done"]) is None


def test_last_exception_line_skips_error_prefixed_lines():
    # `error:`-prefixed lines are last_error_line's job; this must not claim them.
    assert commands.last_exception_line(["error: ValueError: x"]) is None


def test_ingest_failure_cause_falls_through_to_traceback_tail():
    """No JSON, no `error:` line — a bare traceback: the tail exception line is the cause."""
    lines = [
        "$ isar ingest ...",
        "Traceback (most recent call last):",
        "ValueError: could not convert string to float: 'da3p'",
    ]
    assert commands.ingest_failure_cause(lines) == \
        "ValueError: could not convert string to float: 'da3p'"


# ---- drive command (M2) -----------------------------------------------------
def test_build_drive_command_goal_only_defaults_are_minimal():
    """A goal source with engine-default options adds only --goal and --workspace."""
    cmd = commands.build_drive_command("py.exe", r"D:\ws\exp1",
                                       DriveOptions(goal="map the LA branch near (2 0 0)"))
    assert cmd == ["py.exe", "-m", "isar", "drive",
                   "--goal", "map the LA branch near (2 0 0)",
                   "--workspace", r"D:\ws\exp1"]


def test_build_drive_command_program_with_budget_seed_token():
    opts = DriveOptions(program_path=r"D:\obj\program.json", budget="8h", seed=7,
                        control_token="abc123")
    cmd = commands.build_drive_command("py", "ws", opts)
    assert cmd[:6] == ["py", "-m", "isar", "drive", "--program", r"D:\obj\program.json"]
    assert cmd[cmd.index("--workspace") + 1] == "ws"
    assert cmd[cmd.index("--budget") + 1] == "8h"
    assert cmd[cmd.index("--seed") + 1] == "7"
    assert cmd[cmd.index("--control-token") + 1] == "abc123"
    assert "--max-iters" not in cmd                  # budgets are the only stop conditions
    assert "--goal" not in cmd and "--resume-latest" not in cmd


def test_build_drive_command_resume_latest():
    cmd = commands.build_drive_command("py", "ws", DriveOptions(resume_latest=True))
    assert cmd == ["py", "-m", "isar", "drive", "--resume-latest", "--workspace", "ws"]


def test_build_drive_command_compiled_program_freezes_tier():
    # A compiled program the operator already confirmed carries its own tier; the GUI must not
    # re-assert one on the command line and silently disagree with what was confirmed.
    cmd = commands.build_drive_command(
        "py", "ws", DriveOptions(compiled_program_path="program.json", tier="mcstas",
                                 budget="8h"))
    assert cmd == ["py", "-m", "isar", "drive", "--program", "program.json",
                   "--workspace", "ws", "--budget", "8h"]


def test_build_compile_command_has_no_workspace_or_measurement_flags():
    cmd = commands.build_compile_command(
        "py", DriveOptions(goal="map transverse optic along k", budget="8h"), "frozen.json")
    assert cmd == ["py", "-m", "isar", "drive", "--goal",
                   "map transverse optic along k", "--compile-only", "frozen.json",
                   "--budget", "8h"]
    assert "--workspace" not in cmd and "--tavi" not in cmd and "--worker" not in cmd


def test_build_drive_command_resume_never_emits_tier():
    # Tier is frozen with a resumed objective — the CLI ignores --tier on --resume-latest, so
    # emitting it would only imply an effect the invocation doesn't have.
    cmd = commands.build_drive_command("py", "ws", DriveOptions(resume_latest=True, tier="mcstas"))
    assert "--tier" not in cmd and "--resume-latest" in cmd


def test_build_drive_command_no_worker_flag():
    cmd = commands.build_drive_command("py", "ws", DriveOptions(goal="g", worker=False))
    assert "--no-worker" in cmd


def test_build_drive_command_tavi_and_calibration_flags():
    opts = DriveOptions(goal="g", tavi_url="http://host:9000/api/v1",
                        calibration=r"D:\cal\phonon.json", tier="mcstas")
    cmd = commands.build_drive_command("py", "ws", opts)
    assert cmd[cmd.index("--tavi") + 1] == "http://host:9000/api/v1"
    assert cmd[cmd.index("--calibration") + 1] == r"D:\cal\phonon.json"
    assert cmd[cmd.index("--tier") + 1] == "mcstas"


def test_build_drive_command_defaults_omit_optional_flags():
    cmd = commands.build_drive_command("py", "ws", DriveOptions(goal="g"))
    for flag in ("--tier", "--budget", "--calibration", "--tavi", "--seed", "--max-iters",
                 "--control-token", "--no-worker"):
        assert flag not in cmd


def test_authoritative_mode_is_emitted_only_for_authoring_sources():
    authored = commands.build_drive_command(
        "py", "ws", DriveOptions(goal="g", scan_window_eig_mode="authoritative"))
    assert authored[authored.index("--scan-window-eig-mode") + 1] == "authoritative"
    resumed = commands.build_drive_command(
        "py", "ws", DriveOptions(resume_latest=True, scan_window_eig_mode="authoritative"))
    assert "--scan-window-eig-mode" not in resumed


def test_authoritative_compile_binds_the_ready_workspace():
    cmd = commands.build_compile_command(
        "py", DriveOptions(goal="g", scan_window_eig_mode="authoritative"), "frozen.json",
        workspace="ready-ws")
    assert cmd[cmd.index("--workspace") + 1] == "ready-ws"
    assert cmd[cmd.index("--scan-window-eig-mode") + 1] == "authoritative"


def test_build_drive_command_requires_exactly_one_source():
    import pytest
    # Zero sources → ValueError.
    with pytest.raises(ValueError):
        commands.build_drive_command("py", "ws", DriveOptions())
    # Two sources → ValueError.
    with pytest.raises(ValueError):
        commands.build_drive_command("py", "ws", DriveOptions(goal="g", resume_latest=True))
    with pytest.raises(ValueError):
        commands.build_drive_command("py", "ws", DriveOptions(goal="g",
                                                             program_path="p.json"))


# ---- source fingerprint (must match the CLI's campaign_start sha256) ---------
def test_source_fingerprint_goal_text_vector():
    import hashlib
    goal = "map the LA branch near (2 0 0)"
    expected = hashlib.sha256(goal.encode("utf-8")).hexdigest()
    assert commands.source_fingerprint(goal=goal) == expected


def test_source_fingerprint_program_bytes_vector_non_ascii(tmp_path):
    """A program's fingerprint is over its raw BYTES, not decoded text — non-ASCII proves it."""
    import hashlib
    program = tmp_path / "program.json"
    raw = '{"goal": "phonon α→β at 2Å⁻¹"}'.encode("utf-8")
    program.write_bytes(raw)
    expected = hashlib.sha256(raw).hexdigest()
    assert commands.source_fingerprint(program_path=str(program)) == expected


def test_source_fingerprint_requires_exactly_one_arg():
    import pytest
    with pytest.raises(ValueError):
        commands.source_fingerprint()
    with pytest.raises(ValueError):
        commands.source_fingerprint(goal="g", program_path="p.json")
