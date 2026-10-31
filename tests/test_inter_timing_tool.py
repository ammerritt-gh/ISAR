"""tools/inter_timing.py (S1U13, S3U3): one batch runs headless and the report prints every key it reads from the batch's
own timing block (C-timing-report).

The tool runs in process (``main``) on S1U1's synthetic BL43-like workspace (two analyzers at two temperatures,
generated here, with a resolution library so every member prepares) with the fake core of ``_inter_fakes``
(``--core``) and one worker, so the routine suite runs no real fit and no process pool. Nothing reads ``real_data/``;
no measured value is used.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

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
               "assign_s", "likelihood_s", "continuous_s", "uncertainty_s", "release_s", "save_s", "search_s",
               "candidates_priced", "cache_hits", "starts_per_candidate", "optimizer_fits", "failures",
               "evidence_share", "sweeps_to_settle", "searched_fraction", "pool_utilisation", "slowest_spectrum",
               "slowest_stage", "spectrum", "projection_56_spectra_4_workers_s", "projection_56_spectra_16_workers_s")


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
    triggered = {k for k, n in nodes.items() if n.T == 100.0}
    _fake_cfg(ws, triggered=triggered)
    out_dir = tmp_path / "out"
    rc = _tool().main([str(ws), "--kind", "simultaneous", "--core", "_inter_fakes", "--workers", "1",
                       "--out", str(out_dir)])
    out = capsys.readouterr().out
    assert rc == 0, out
    lines = out.splitlines()
    for key in REPORT_KEYS:
        assert any(line.startswith(key) for line in lines), f"no {key} line in:\n{out}"
    assert "searched_fraction round1: 0.50 (2 of 4)" in lines       # two of the four spectra are triggered
    for k in triggered:
        assert any(line.startswith(f"spectrum round1 {k}:") for line in lines), out
    data = json.loads((out_dir / "timing.json").read_text(encoding="utf-8"))
    assert data["stage1"]["round1"]["searched"] == 2
    assert data["end"]["timing"]["spectra"], "timing.json carries the batch's spectra records"
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


def test_the_report_names_the_pool_the_batch_used_not_the_workers_asked_for(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ISAR_WORKERS", "1")                          # the pool is capped below --workers 5
    ws = _workspace(tmp_path)
    _fake_cfg(ws, triggered=set())
    out_dir = tmp_path / "out"
    rc = _tool().main([str(ws), "--kind", "simultaneous", "--core", "_inter_fakes", "--workers", "5",
                       "--out", str(out_dir)])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert any(line.startswith("batch ") and line.endswith("workers 1") for line in out.splitlines()), out
    assert json.loads((out_dir / "timing.json").read_text(encoding="utf-8"))["workers"] == 1


def _record(unit, *, candidates, cached, starts, fits, failures, reasons, evidence_s, refine_s):
    return {"stage": "ring1", "unit": unit, "iterations": 1, "candidates": candidates, "cached": cached,
            "starts": starts, "starts_per_candidate": starts / candidates if candidates else None, "fits": fits,
            "failures": failures, "failure_reasons": reasons, "evidence_s": evidence_s, "refine_s": refine_s,
            "stopped": {"down": "behind_margin:1", "up": "floor"}, "branches_seen": 5, "dormant_seen": 0,
            "error": None}


def test_the_report_reads_the_timing_block_and_guards_zero_time():
    """Hand-made payload: the sums, the ratios, the projection and the empty-run guards are all arithmetic."""
    tool = _tool()
    spec = SimpleNamespace(batch_id="b1", kind="anchored")
    timing = {k: 0.0 for k in ("wall_s", "prepare_s", "ship_s", "assign_s", "likelihood_s", "continuous_s",
                               "uncertainty_s", "release_s", "save_s", "search_s")}
    timing.update(target_s=1800.0, over_target=False, stages=[{"kind": "ring", "index": 1, "wall_s": 2.0,
                                                               "width": 2, "n_refits": 2}],
                  spectra=[_record("A", candidates=3, cached=1, starts=7, fits=19, failures=0, reasons=[],
                                   evidence_s=4.0, refine_s=1.0),
                           _record("B", candidates=1, cached=0, starts=2, fits=0, failures=1,
                                   reasons=[["pricing failed (RuntimeError: boom)", 1]], evidence_s=0.0,
                                   refine_s=0.5)],
                  slowest={"spectrum": {"stage": "ring1", "unit": "A", "seconds": 5.0},
                           "stage": {"kind": "ring", "index": 1, "wall_s": 2.0}},
                  pool={"workers": 3, "iterations": 2, "map_wall_s": 2.0, "busy_s": 3.0})
    end = {"state": "done", "reason": None, "rounds": None, "sweeps": None, "rings_done": 1, "timing": timing}
    lines, data = tool._report(spec, 2, [], end, None)
    assert "candidates_priced: 4" in lines and "cache_hits: 1" in lines and "failures: 1" in lines
    assert "starts_per_candidate: 2.25" in lines                     # 9 starts over 4 fresh candidates
    assert "failure_reasons: pricing failed (RuntimeError: boom) x 1" in lines
    assert "pool_utilisation: 0.50" in lines                         # 3.0 busy over 2.0 wall times 3 workers
    assert "evidence_share: 0.73" in lines                           # 4.0 over 4.0 + 1.5
    assert "searched_fraction ring1: 1.00 (2 of 2)" in lines
    assert "means: per_member_compute_s=2.750" in lines              # 5.5 seconds over 2 members
    assert any(line.startswith("projection_56_spectra_4_workers_s: 38.5 ") for line in lines)
    spectra = [i for i, line in enumerate(lines) if line.startswith("spectrum ")]
    assert [lines[i].split()[2] for i in spectra] == ["A:", "B:"]    # slowest first
    assert data["stage1"]["ring1"] == {"spectra": 2, "searched": 2, "candidates": 4, "cached": 1}

    empty = dict(end, timing=dict(timing, spectra=[], stages=[], slowest={"spectrum": None, "stage": None},
                                  pool=dict(timing["pool"], map_wall_s=0.0, busy_s=0.0)))
    lines0, data0 = tool._report(spec, 0, [], empty, None)
    for line in ("pool_utilisation: -", "starts_per_candidate: -", "evidence_share: -", "slowest_spectrum: -",
                 "slowest_stage: -", "failure_reasons: -", "means: per_member_compute_s=0.000"):
        assert line in lines0, lines0
    assert data0["stage1"] == {}
