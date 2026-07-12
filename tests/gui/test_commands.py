"""Qt-free command-builder + instrument-preview tests.

The New Experiment wizard and the Run action both shell the same engine (``isar init`` / ``isar
run``) via subprocess; the argument assembly and the instrument preview live in ``isar_gui.commands``
so they are unit-tested here with no Qt and no QApplication.
"""
from __future__ import annotations

from pathlib import Path

from isar_gui import commands
from isar_gui.commands import DetectResult, RunOptions


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
