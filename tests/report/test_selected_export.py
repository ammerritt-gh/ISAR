"""Export round-trips on invented spectra: scaling, model authority and replacement safety."""
import csv
import json
import shutil
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from isar.report.export import export_workspace
from isar.workspace_api.export import ExportOptions
from isar.session.state import ScanEntry, SessionState
from isar.session.unit_data import unit_data_path


@pytest.fixture
def export_ws(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    card = Path(__file__).resolve().parents[2] / "isar/parsers/cards/bl43lxu.policy.toml"
    shutil.copyfile(card, root / "card.policy.toml")
    (root / "card.state.json").write_text("{}", encoding="utf-8", newline="\n")
    state = SessionState()
    parameters = {"phonon.omega0": 3.0, "phonon.Gamma": 0.4, "phonon.h": 12.0,
                  "elastic.h": 20.0, "qe.h": 2.0, "qe.width": 4.0, "bg.BG": 0.5}
    outcome = {"T": 120.0, "q": [1.0, 0.0, 0.0], "best_label": "example",
               "calib": {"center": 0.3, "gauss_fwhm": 1.0, "lorentz_fwhm": 0.2},
               "model_manifest": {"schema": "isar.model_manifest/2", "components": [
                   {"type": "phonon_dho", "prefix": "phonon", "peak": "stokes+astokes"},
                   {"type": "elastic", "prefix": "elastic"},
                   {"type": "central_qe", "prefix": "qe"},
                   {"type": "constant_background", "prefix": "bg"}], "parameters": parameters},
               "modes": [{"omega0": 3.0, "gamma": 0.4, "omega0_err": 0.1, "gamma_err": 0.03}],
               "fast_pass": {"state": "resolved", "models": [], "qe": {"sigma_width": 0.2}}}
    for number in (1, 2):
        state.put(ScanEntry(scan_id=f"E:{number}:a", scan_number=number, scan_type="measurement",
                            status="fitted", content_hash=f"hash{number}", detectors={"a": outcome}))
        path = unit_data_path(root / "data", "E", number, "a")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": "isar.unit_data/1", "scan_id": f"E:{number}:a", "experiment_id": "E",
                   "scan_number": number, "detector_id": "a", "x": [-5, -1, 0, 1, 5],
                   "counts": [0, 4, 9, 16, 25], "variance": [0, 4, 9, 16, 25],
                   "mask": [True, True, False, True, True], "monitor": [100, 100, 200, 100, None],
                   "exposure": {"source": "monitor", "reference": 100, "scale": [1, 1, 2, 1, 0]},
                   "axis": {"kind": "energy", "units": "meV", "calibration_status": "needs_zero"}}
        path.write_text(json.dumps(payload), encoding="utf-8", newline="\n")
    state.save(root / "session.json")
    return root


def read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.reader(stream))


def test_formats_scales_components_and_whole_model(export_ws):
    raw = export_workspace(export_ws, options=ExportOptions(origin=True, split_sides=True,
                                                           parameters_separate=True)).folder
    normalized = export_workspace(export_ws, options=ExportOptions(scale="normalised", split_sides=True)).folder
    curves = read_csv(raw / "csv/fits/1__a.csv")
    arr = np.array(curves[1:], dtype=float)
    assert len(arr) == 2001
    np.testing.assert_allclose(arr[:, 1], arr[:, 2:].sum(axis=1), rtol=1e-11, atol=1e-10)
    assert any("anti-Stokes" in name for name in curves[0])
    norm_arr = np.array(read_csv(normalized / "csv/fits/1__a.csv")[1:], dtype=float)
    np.testing.assert_allclose(norm_arr[:, 1:], arr[:, 1:] / 100, rtol=1e-10)
    data = read_csv(normalized / "csv/spectra/1__a.csv")
    assert len(data) == 6 and data[3][-1] == "0"  # excluded point stays
    assert float(data[2][1]) == 0.04
    assert data[1][1:3] == ["0", "0"]  # no zero-count floor
    assert data[-1][1:3] == ["", ""]  # no raw fallback for missing monitor
    origin = read_csv(raw / "origin/spectra/1__a.csv")
    assert origin[0] == ["Energy", "Intensity", "Uncertainty", "Included"]
    assert origin[1][0] == "meV" and origin[3] == ["X", "Y", "yEr", "Label"]
    assert "T=120" in origin[2][0]
    params = read_csv(raw / "csv/parameters.csv")
    assert {row[6] for row in params[1:]} >= {"Mode 1", "Elastic", "Background", "Quasielastic"}
    assert len(read_csv(raw / "csv/parameters/1__a.csv")) == (len(params) - 1) // 2 + 1
    width = next(row for row in params[1:] if row[6:8] == ["Quasielastic", "Width (FWHM)"])
    assert width[9] == "0.2"


def test_data_only_and_authoritative_parameters(export_ws, monkeypatch):
    data = export_workspace(export_ws, options=ExportOptions(curves=False, parameters_combined=False,
                                                            csv=False, origin=True)).folder
    assert not (data / "csv").exists() and not (data / "origin/fits").exists()
    assert "model_manifest" not in json.loads((data / "export.json").read_text(encoding="utf-8"))["spectra"][0]
    from isar.report import export
    original = export._inter_state

    def current(root, card, session):
        effective, overview, history = original(root, card, session)
        outcome = dict(session.scans["E:1:a"].detectors["a"])
        outcome["model_manifest"] = {**outcome["model_manifest"], "parameters": {
            **outcome["model_manifest"]["parameters"], "phonon.omega0": 6.0}}
        outcome["modes"] = []  # no borrowing solo uncertainties
        effective["E:1:a"] = SimpleNamespace(outcome=outcome, line=SimpleNamespace(fit="refit"))
        return effective, overview, history

    monkeypatch.setattr(export, "_inter_state", current)
    folder = export_workspace(export_ws, options=ExportOptions(spectra=False, curves=False)).folder
    rows = read_csv(folder / "csv/parameters.csv")
    energy = next(row for row in rows[1:] if row[0] == "1" and row[7] == "Undamped energy E0")
    assert energy[8] == "6" and energy[9] == ""


def test_overwrite_is_explicit_and_failure_retains_previous(export_ws, tmp_path, monkeypatch):
    target = tmp_path / "export"
    export_workspace(export_ws, options=ExportOptions(), destination=target)
    old = (target / "export.json").read_bytes()
    with pytest.raises(FileExistsError):
        export_workspace(export_ws, options=ExportOptions(), destination=target)
    from isar.report import selected_export
    def fail(*args):
        raise OSError("disk full")
    with monkeypatch.context() as patch:
        patch.setattr(selected_export, "build", fail)
        with pytest.raises(OSError, match="disk full"):
            export_workspace(export_ws, options=ExportOptions(), destination=target, overwrite=True)
    assert (target / "export.json").read_bytes() == old
    from isar.report import export as exporter
    rename = exporter.os.rename
    def fail_publish(src, dst):
        if Path(src).name.startswith(".partial-") and not Path(src).name.endswith("-previous"):
            raise OSError("publication failed")
        return rename(src, dst)
    with monkeypatch.context() as patch:
        patch.setattr(exporter.os, "rename", fail_publish)
        with pytest.raises(OSError, match="publication failed"):
            export_workspace(export_ws, options=ExportOptions(), destination=target, overwrite=True)
    assert (target / "export.json").read_bytes() == old
    export_workspace(export_ws, options=ExportOptions(origin=True, csv=False), destination=target, overwrite=True)
    assert (target / "origin").is_dir() and not (target / "csv").exists()
    with pytest.raises(ValueError):
        export_workspace(export_ws, options=ExportOptions(), destination=export_ws, overwrite=True)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    with pytest.raises(ValueError, match="not an ISAR export"):
        export_workspace(export_ws, options=ExportOptions(), destination=unrelated, overwrite=True)
    assert not list(tmp_path.glob(".partial-*"))


def test_pooled_counts_and_monitor_are_not_confused(export_ws):
    from isar.session.workspace import Workspace
    _, session = Workspace.open(export_ws).load()
    oc = session.scans["E:1:a"].detectors["a"]
    oc.update(pooled=True, spectrum_ref="data/pooled/example.json", effective_exposure=9999)
    blob = export_ws / "data/pooled/example.json"
    blob.parent.mkdir(parents=True)
    blob.write_text(json.dumps({"schema": "isar.pooled_spectrum/1", "energy": [-5, 0, 5],
                               "counts": [10, 40, 30], "variance": [10, 40, 30],
                               "monitor": [100, 200, 100], "mask": [True, True, True]}),
                    encoding="utf-8", newline="\n")
    session.save(export_ws / "session.json")
    raw = export_workspace(export_ws, options=ExportOptions()).folder
    norm = export_workspace(export_ws, options=ExportOptions(scale="normalised")).folder
    assert [float(row[1]) for row in read_csv(raw / "csv/spectra/1__a.csv")[1:]] == [10, 40, 30]
    assert [float(row[1]) for row in read_csv(norm / "csv/spectra/1__a.csv")[1:]] == [0.1, 0.2, 0.3]
    raw_curve = np.array(read_csv(raw / "csv/fits/1__a.csv")[1:], float)
    norm_curve = np.array(read_csv(norm / "csv/fits/1__a.csv")[1:], float)
    np.testing.assert_allclose(norm_curve[:, 1:], raw_curve[:, 1:] / 100, rtol=1e-10)


@pytest.mark.parametrize("peak", ["stokes+astokes", "stokes"])
def test_split_tabulated_resolution_preserves_model(peak):
    from isar.analyze.resolution import TabulatedResolution
    from isar.workspace_api.evaluate import evaluate_manifest
    energy = np.linspace(-3, 3, 101)
    curve = TabulatedResolution(energy, np.exp(-energy ** 2) * (1 + 0.1 * energy), "a")
    resolution = {"center": 0.4, "gauss_fwhm": 1.0, "lorentz_fwhm": 0.0, "T": 200,
                  "resolution_curve": curve}
    manifest = {"schema": "isar.model_manifest/2", "components": [
        {"type": "phonon_dho", "prefix": "phonon", "peak": peak}],
        "parameters": {"phonon.omega0": 2.0, "phonon.Gamma": 3.0, "phonon.h": 12.0}}
    x = np.linspace(-8, 8, 500)
    total, parts = evaluate_manifest(manifest, resolution, x, split_sides=True)
    np.testing.assert_allclose(total, sum(values for _, values in parts), rtol=1e-11, atol=1e-11)
    assert len(parts) == (2 if peak == "stokes+astokes" else 1)


def test_cli_receives_dialog_choices_and_quoted_paths(export_ws, tmp_path, capsys):
    from isar.cli.main import main
    from isar_gui.commands import build_export_command
    request = {"options": ExportOptions(csv=False, origin=True, spectra=False, curves=False,
                                        parameters_separate=True).to_dict(),
               "destination": str(tmp_path / "chosen parent" / "chosen name"), "overwrite": False}
    argv = build_export_command("python", str(export_ws), request)
    assert main(argv[3:]) == 0
    result = json.loads(capsys.readouterr().out.splitlines()[-1])["export"]
    folder = Path(result["folder"])
    assert folder == Path(request["destination"])
    assert (folder / "origin/parameters/1__a.csv").exists()
    assert not (folder / "csv").exists()
