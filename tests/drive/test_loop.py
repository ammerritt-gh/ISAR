"""run_campaign orchestration against a MOCK TAVI client + a MOCK isar runner (no network, no fits).

The mock isar runner is the analysis boundary stand-in: it reads the saved scan JSON and writes a
``report/results.csv`` exactly as ISAR would, so the loop exercises the real readers, real σ-calibration,
real acquisition, and real stopping — only the fitting and the instrument are faked.
"""
import csv
import json
import math
from pathlib import Path

import pytest

from isar.drive.acquisition.calibration import Calibration
from isar.drive.loop import _pick_interval, run_campaign
from isar.drive.objective import ObjectiveSpec
from isar.drive.session_log import read_log
from isar.drive.supervisor.stopping import PRODUCTION_NCOUNT, QUICKLOOK_NCOUNT

_UNITY_CAL = Calibration.from_dict({"inflation": 1.0, "buckets": [], "center_pull_mean": 0.0})


def _parse_grid(scan_command):
    _, lo, hi, step = scan_command.split()
    lo, hi, step = float(lo), float(hi), float(step)
    n = int((hi - lo) / step) + 1
    return [lo + step * i for i in range(n)]


def _counts_at(grid, peak):
    return [100.0 * math.exp(-((v - peak) / 0.5) ** 2) + 1.0 for v in grid]


class MockTavi:
    """A scripted TAVI: validate/submit/wait/data + resolution. ``omega_true`` places the peak."""
    def __init__(self, omega_true=lambda h: (h - 2.0) * 2.0):
        self.jobs = {}
        self.n = 0
        self.omega_true = omega_true

    def validate(self, body):
        return {"would_queue": True, "blockers": [], "eta": {"estimated_seconds": 5.0}}

    def submit_scan(self, body, idempotency_key=None):
        self.n += 1
        jid = f"j-{self.n}"
        self.jobs[jid] = body
        return {"job_id": jid, "state": "queued", "eta": {"estimated_seconds": 5.0}}

    def wait_scan(self, job_id, timeout=0.0, **kw):
        return {"job_id": job_id, "state": "done"}

    def get_scan_data(self, job_id):
        body = self.jobs[job_id]
        params = body["parameters"]
        grid = _parse_grid(params["scan_command1"])
        peak = self.omega_true(params["H"])
        return {"job_id": job_id, "state": "done",
                "result": {"counts": _counts_at(grid, peak), "scan_values_1": grid},
                "launch": {"parameters": params}}

    def get_resolution(self, h, k, l, deltaE):
        return {"ok": True, "fwhm": {"dE": 0.8}}


import math as _math


def make_mock_runner(omega_true, *, window_gate=True):
    """A fake `isar run`: writes results.csv from the saved scans, including the master (pooled) row
    per (Q,T) that ISAR now emits. The master's interval shrinks ~1/√N with the pooled production
    count, so a q converges by accumulation. Declines when the window misses the peak."""
    assigned: dict[str, int] = {}

    def runner(workspace, data_paths, **kw):
        data_dir = Path(data_paths[0])
        rows = []
        prod_per_h: dict[float, int] = {}       # production scans that hit the peak, per H (the pool)
        for f in sorted(data_dir.glob("*.json")):
            d = json.loads(f.read_text(encoding="utf-8"))
            params = d.get("launch", {}).get("parameters", {})
            h, k, l = params.get("H"), params.get("K"), params.get("L")
            n = params.get("number_neutrons")
            if f.stem not in assigned:
                assigned[f.stem] = len(assigned) + 1
            scan_id = assigned[f.stem]
            grid = _parse_grid(params["scan_command1"])
            true = omega_true(h)
            contained = (grid[0] <= true <= grid[-1]) if window_gate else True
            base = {"scan": scan_id, "detector": "det", "h": h, "k": k, "l": l, "status": "fitted",
                    "interval_reliable": True}
            if not contained:
                base.update({"verdict": "no_signal", "disposition": "declined", "omega0": None,
                             "interval_lo": None, "interval_hi": None, "reasons": "peak outside window"})
            else:
                raw_hw = 0.6 if n <= QUICKLOOK_NCOUNT else 0.3
                base.update({"verdict": "resolved", "disposition": "resolved", "omega0": true,
                             "interval_lo": true - raw_hw, "interval_hi": true + raw_hw,
                             "confidence": 0.9, "reasons": "resolved"})
                if n > QUICKLOOK_NCOUNT:
                    prod_per_h[h] = prod_per_h.get(h, 0) + 1
            rows.append(base)
        # Master (pooled) rows: one per H with pooled production scans; interval ~ 0.3/√N.
        master_scan = 9_000_000
        for h, npool in sorted(prod_per_h.items()):
            hw = 0.3 / _math.sqrt(npool)
            true = omega_true(h)
            rows.append({"scan": master_scan, "detector": "det", "h": h, "k": 0.0, "l": 0.0,
                         "status": "fitted", "verdict": "resolved", "disposition": "resolved",
                         "omega0": true, "interval_lo": true - hw, "interval_hi": true + hw,
                         "interval_reliable": True, "pooled": True, "n_members": npool,
                         "confidence": 0.9, "reasons": "pooled master"})
            master_scan += 1
        report = Path(workspace) / "report"
        report.mkdir(parents=True, exist_ok=True)
        cols = ["scan", "detector", "h", "k", "l", "status", "verdict", "disposition",
                "confidence", "omega0", "omega0_err", "interval_lo", "interval_hi",
                "interval_reliable", "pooled", "n_members", "reasons"]
        with open(report / "results.csv", "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c) for c in cols})
    return runner


def test_pick_interval_prefers_bootstrap_when_reliable():
    row = {"interval_lo": 4.9, "interval_hi": 5.1, "interval_reliable": True,
           "omega0_profile_lo": 4.95, "omega0_profile_hi": 5.05}
    assert _pick_interval(row) == (4.9, 5.1, True)


def test_pick_interval_falls_back_to_profile_when_gamma_at_bound():
    # Bootstrap collapsed (spurious spike) + flagged unreliable → use the robust profile interval.
    row = {"interval_lo": 0.9499, "interval_hi": 0.9501, "interval_reliable": False,
           "omega0_profile_lo": 0.944, "omega0_profile_hi": 0.956}
    assert _pick_interval(row) == (0.944, 0.956, True)


def test_pick_interval_none_reliable_is_trusted():
    # Older ledgers have no interval_reliable column (None) → trust the bootstrap interval.
    row = {"interval_lo": 4.9, "interval_hi": 5.1, "interval_reliable": None}
    assert _pick_interval(row) == (4.9, 5.1, True)


def test_pick_interval_unusable_when_flagged_and_no_profile():
    row = {"interval_lo": 0.9499, "interval_hi": 0.9501, "interval_reliable": False,
           "omega0_profile_lo": None, "omega0_profile_hi": None}
    assert _pick_interval(row) == (None, None, False)


def _objective(tol=0.5, amount=1e13):
    return ObjectiveSpec.along_h(h_start=2.1, h_end=2.4, step=0.1, tolerance_meV=tol,
                                 budget={"kind": "neutrons", "amount": amount}, name="mvp")


def _run(tmp_path, objective, client, runner, **extra):
    ws = tmp_path / "ws"
    (ws / "report").mkdir(parents=True, exist_ok=True)
    cfg = {"workspace": ws, "client": client, "run_isar": runner, "calibration": _UNITY_CAL,
           "resolution": {"fwhm_dE": 0.8}, "seed": 7, **extra}
    return run_campaign(objective, cfg), ws


def test_campaign_reaches_met(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0     # small ω, always inside the first window
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    assert summary["stop"]["state"] == "met", summary["stop"]["reasons"]
    # Every grid q ended converged with a final ω(q).
    assert all(g["converged"] for g in summary["grid"])
    assert len(summary["dispersion"]) == 4
    # The campaign summary file was written.
    assert (ws / "report" / "campaign_summary.json").exists()


def test_session_log_shows_propose_analyze_decide_stop_chain(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    kinds = [e["kind"] for e in read_log(summary["session_log"])]
    for expected in ("propose", "validate", "run", "analyze", "decide", "stop"):
        assert expected in kinds, f"missing {expected} in session log ({set(kinds)})"
    assert kinds[-1] == "stop"


def test_starts_with_quicklooks_then_escalates(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client, make_mock_runner(omega))
    proposals = [e["payload"] for e in read_log(summary["session_log"]) if e["kind"] == "propose"]
    # The first four proposals are quick-looks (one per unvisited grid q).
    assert [p["kind"] for p in proposals[:4]] == ["quicklook"] * 4
    assert {tuple(p["hkl"]) for p in proposals[:4]} == {
        (2.1, 0.0, 0.0), (2.2, 0.0, 0.0), (2.3, 0.0, 0.0), (2.4, 0.0, 0.0)}
    # Production scans (accumulated into the master) appear later in the ladder.
    later = {p["kind"] for p in proposals[4:]}
    assert "production" in later


def test_campaign_unmeetable_on_budget(tmp_path):
    omega = lambda h: (h - 2.0) * 2.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(amount=1.0), client, make_mock_runner(omega))
    assert summary["stop"]["state"] == "unmeetable"
    assert any("budget" in r for r in summary["stop"]["reasons"])


def test_missed_window_triggers_recenter(tmp_path):
    # ω sits at 3.0 — outside the cold quick-look window — so the first scan misses and re-centers.
    omega = lambda h: 3.0
    client = MockTavi(omega)
    summary, ws = _run(tmp_path, _objective(), client,
                       make_mock_runner(omega, window_gate=True), max_iters=60)
    events = read_log(summary["session_log"])
    recenters = [e for e in events if e["kind"] == "exception"
                 and e["payload"].get("action") == "recenter"]
    assert recenters, "a missed window should have logged a recenter action"
