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
