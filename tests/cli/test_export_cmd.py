"""`isar export`: exit codes, the progress lines, the summary and the JSON line (the protocol the
GUI's Export action reads). Data generated at test time (hard rule 10)."""
import json
from pathlib import Path

from isar.cli.main import main
from isar.report import export as export_mod


def test_export_prints_progress_summary_path_and_json(bl43_ws, capsys):
    assert main(["export", "--workspace", str(bl43_ws), "--json"]) == 0
    lines = capsys.readouterr().out.splitlines()
    progress = [ln for ln in lines if ln.startswith("export: ") and ln.endswith(" spectra")]
    n = len(progress)
    assert n > 0 and progress == [f"export: {i}/{n} spectra" for i in range(1, n + 1)]
    result = json.loads(lines[-1])["export"]
    folder = Path(result["folder"])
    assert folder.is_dir() and folder.parent == (bl43_ws / "report" / "exports").resolve()
    assert lines[-2] == str(folder)
    assert lines[-3] == (f"export: {n} spectra, {result['modes']} mode rows ({result['modes_fast']} "
                         f"fast, {result['modes_deep']} deep), {result['resolution_curves']} "
                         f"resolution curves, {result['problems']} problems (listed in fits.json)")
    assert result["spectra"] == n and result["modes"] == result["modes_fast"] + result["modes_deep"]


def test_export_without_json_ends_with_the_folder(bl43_ws, capsys):
    assert main(["export", "--workspace", str(bl43_ws)]) == 0
    last = capsys.readouterr().out.splitlines()[-1]
    assert Path(last).is_dir() and Path(last).name.startswith("export-")


def test_not_a_workspace_exits_2(tmp_path, capsys):
    assert main(["export", "--workspace", str(tmp_path), "--json"]) == 2
    err = json.loads(capsys.readouterr().out.splitlines()[-1])["error"]
    assert err["kind"] == "no_workspace"
    assert main(["export", "--workspace", str(tmp_path)]) == 2
    assert capsys.readouterr().err.startswith("error: no workspace")


def test_a_failure_exits_1_with_its_reason_and_leaves_no_folder(bl43_ws, capsys, monkeypatch):
    def boom(folder):
        raise OSError("disk full")
    monkeypatch.setattr(export_mod, "_write_readme", boom)
    assert main(["export", "--workspace", str(bl43_ws), "--json"]) == 1
    err = json.loads(capsys.readouterr().out.splitlines()[-1])["error"]
    assert err == {"kind": "export_failed", "message": "OSError: disk full"}
    assert list((bl43_ws / "report" / "exports").iterdir()) == []
