"""tools/inter_timing.py (S1U13): one batch runs headless and the report prints every key the measurement reads.

The tool runs in process (``main``) on S1U1's synthetic BL43-like workspace (two analyzers at two temperatures,
generated here, with a resolution library so every member prepares) with the fake core of ``_inter_fakes``
(``--core``) and one worker, so the routine suite runs no real fit. Nothing reads ``real_data/``; no measured
value is used.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import isar
import intake_fixtures as fx
from inter_fixtures import build_inter_workspace
from isar.session.inter_spectra import q_provider, records
from isar.session.inter_spectra.nodes import read_nodes
from isar.session.workspace import Workspace

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(HERE / "session") not in sys.path:
    sys.path.insert(0, str(HERE / "session"))       # the fake core is imported by its public name

SYNTHETIC = "synthetic q coordinates: timing only"
REPORT_KEYS = ("batch_wall_s", "target_s", "over_target", "rounds", "sweeps", "rings", "prepare_s", "ship_s",
               "assign_s", "likelihood_s", "continuous_s", "uncertainty_s", "release_s", "save_s",
               "triggered_fraction", "sweeps_to_settle", "price_jobs_per_triggered_spectrum", "cache_hits",
               "no_evidence_or_twin_fraction", "projection_56_spectra_4_workers_s",
               "projection_56_spectra_16_workers_s")


def test_import_root_is_the_worktree():
    root = Path(isar.__file__).resolve().parents[1]
    print("import root:", root)
    assert root == REPO


def _tool():
    spec = importlib.util.spec_from_file_location("inter_timing", REPO / "tools" / "inter_timing.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _workspace(tmp_path) -> Path:
    """The S1U1 fixture with a resolution library saved first, so every member prepares a fit context."""
    ws = tmp_path / "ws"
    ws.mkdir(parents=True)
    fx.write_library(ws / "rf", analyzers=("a03", "a04"))
    assert fx.save_resolution_folder(ws, ws / "rf").ok
    return build_inter_workspace(tmp_path, temperatures=(100, 150), setups=("0p5_0p5_3",), n_analyzers=2)


def _fake_cfg(ws: Path, triggered) -> None:
    """Each spectrum's fake behaviour, read from its stored fast solo: the "keep" toy settles at once, and the
    spectra in ``triggered`` are priced every round."""
    w = Workspace.open(ws)
    card, session = w.load()
    for key in session.scans:
        session.scans[key].detectors[key.rsplit(":", 1)[1]]["fast_pass"]["fake"] = {
            "toy": "keep", "triggered": key in triggered}
    w.save(card, session)


def test_the_tool_runs_a_batch_and_prints_every_key_of_its_report(tmp_path, capsys):
    ws = _workspace(tmp_path)
    nodes = read_nodes(ws).nodes
    _fake_cfg(ws, triggered={k for k, n in nodes.items() if n.T == 100.0})
    rc = _tool().main([str(ws), "--kind", "simultaneous", "--core", "_inter_fakes", "--workers", "1"])
    out = capsys.readouterr().out
    assert rc == 0, out
    lines = out.splitlines()
    for key in REPORT_KEYS:
        assert any(line.startswith(key) for line in lines), f"no {key} line in:\n{out}"
    assert "triggered_fraction round1: 0.50 (2 of 4)" in lines       # two of the four spectra are triggered
    (batch,) = records.read_log(ws).batches.values()
    assert batch.end is not None and batch.metric.get("note") is None  # a plain run carries no synthetic label


def test_the_synthetic_grid_labels_the_report_and_the_batch_and_is_unpatched_after(tmp_path, capsys):
    ws = _workspace(tmp_path)
    _fake_cfg(ws, triggered=set())
    before = q_provider.q_coordinates
    rc = _tool().main([str(ws), "--kind", "simultaneous", "--core", "_inter_fakes", "--workers", "1",
                       "--synthetic-grid-q"])
    out = capsys.readouterr().out
    assert rc == 0 and SYNTHETIC in out.splitlines()
    (batch,) = records.read_log(ws).batches.values()
    assert batch.metric.get("note") == SYNTHETIC
    assert q_provider.q_coordinates is before                        # the tool restores the runner's binding
